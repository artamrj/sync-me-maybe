from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import requests
import yt_dlp
from bs4 import BeautifulSoup

from sync_me_maybe.config import Settings

from .filenames import clean_title
from .urls import ClassifiedLink, LinkKind, LinkScope


class CollectionResolveError(RuntimeError):
    pass


@dataclass(frozen=True)
class TrackSearchItem:
    title: str
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    source_url: str | None = None

    @property
    def search_query(self) -> str:
        return " ".join(part for part in [self.artist, self.title] if part)


class CollectionResolver:
    def __init__(self, settings: Settings, timeout_seconds: int = 20) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds

    async def expand(self, classified: ClassifiedLink) -> list[TrackSearchItem]:
        return await asyncio.to_thread(self._expand_sync, classified)

    def _expand_sync(self, classified: ClassifiedLink) -> list[TrackSearchItem]:
        if classified.scope == LinkScope.TRACK:
            raise CollectionResolveError("This link is not a playlist or album.")

        if classified.kind == LinkKind.YOUTUBE:
            tracks = self._youtube_playlist(classified.url)
        elif classified.kind == LinkKind.SPOTIFY:
            tracks = self._spotify_collection(classified)
        elif classified.kind == LinkKind.APPLE_MUSIC:
            tracks = self._apple_music_collection(classified)
        else:
            raise CollectionResolveError(
                "This provider does not support playlist or album expansion."
            )

        if not tracks:
            raise CollectionResolveError("No tracks found in this collection.")
        if len(tracks) > self.settings.max_collection_tracks:
            raise CollectionResolveError(
                f"Collection has {len(tracks)} tracks, "
                f"above MAX_COLLECTION_TRACKS={self.settings.max_collection_tracks}."
            )
        return tracks

    def _youtube_playlist(self, url: str) -> list[TrackSearchItem]:
        options: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
        }
        if self.settings.ytdlp_cookies_file:
            options["cookiefile"] = str(self.settings.ytdlp_cookies_file)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp has many concrete error types.
            raise CollectionResolveError(f"Could not read YouTube playlist: {exc}") from exc

        tracks: list[TrackSearchItem] = []
        for index, entry in enumerate((info or {}).get("entries") or [], start=1):
            if not entry:
                continue
            title = clean_title(entry.get("title"))
            if not title:
                continue
            tracks.append(
                TrackSearchItem(
                    title=title,
                    artist=clean_title(entry.get("uploader")),
                    track_number=index,
                    source_url=entry.get("url") or entry.get("webpage_url"),
                )
            )
        return tracks

    def _spotify_collection(self, classified: ClassifiedLink) -> list[TrackSearchItem]:
        tracks = self._public_collection(classified.url)
        if not tracks:
            raise CollectionResolveError(
                "Could not expand this Spotify collection. "
                "Public extraction did not expose track data."
            )
        return tracks

    def _apple_music_collection(self, classified: ClassifiedLink) -> list[TrackSearchItem]:
        tracks = self._public_collection(classified.url)
        if not tracks:
            raise CollectionResolveError(
                "Could not expand this Apple Music collection. "
                "Public extraction did not expose track data."
            )
        return tracks

    def _public_collection(self, url: str) -> list[TrackSearchItem]:
        tracks = self._yt_dlp_public_entries(url)
        if tracks:
            return tracks
        return self._html_public_entries(url)

    def _yt_dlp_public_entries(self, url: str) -> list[TrackSearchItem]:
        options: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
        }
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:  # noqa: BLE001 - public metadata extraction is best-effort.
            return []
        return _tracks_from_entries((info or {}).get("entries") or [])

    def _html_public_entries(self, url: str) -> list[TrackSearchItem]:
        try:
            response = requests.get(
                url, timeout=self.timeout_seconds, headers={"User-Agent": "Mozilla/5.0"}
            )
            response.raise_for_status()
        except requests.RequestException:
            return []

        soup = BeautifulSoup(response.text, "html.parser")
        json_roots: list[Any] = []
        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            if not text:
                continue
            script_type = str(script.get("type") or "").lower()
            script_id = str(script.get("id") or "")
            if "json" in script_type or script_id == "__NEXT_DATA__":
                parsed = _loads_json(text)
                if parsed is not None:
                    json_roots.append(parsed)

        if not json_roots:
            json_roots.extend(_extract_balanced_json_objects(response.text))

        tracks: list[TrackSearchItem] = []
        for root in json_roots:
            tracks.extend(_walk_track_items(root))
        return _dedupe_tracks(tracks)


def _artists(artists: Any) -> str | None:
    if isinstance(artists, dict):
        return clean_title(artists.get("name") or artists.get("artistName"))
    if not isinstance(artists, list):
        return clean_title(str(artists)) if artists else None
    names = [
        clean_title(artist.get("name") or artist.get("artistName"))
        for artist in artists
        if isinstance(artist, dict)
    ]
    return ", ".join(name for name in names if name) or None


def _tracks_from_entries(entries: list[Any]) -> list[TrackSearchItem]:
    tracks: list[TrackSearchItem] = []
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            continue
        title = clean_title(entry.get("track") or entry.get("title") or entry.get("name"))
        if not title:
            continue
        tracks.append(
            TrackSearchItem(
                title=title,
                artist=clean_title(
                    entry.get("artist") or entry.get("uploader") or entry.get("creator")
                ),
                album=clean_title(entry.get("album") or entry.get("albumName")),
                track_number=_int(entry.get("track_number") or entry.get("trackNumber")) or index,
                source_url=entry.get("url") or entry.get("webpage_url"),
            )
        )
    return _dedupe_tracks(tracks)


def _walk_track_items(value: Any) -> list[TrackSearchItem]:
    tracks: list[TrackSearchItem] = []
    if isinstance(value, dict):
        track = _track_from_dict(value)
        if track:
            tracks.append(track)
        for child in value.values():
            tracks.extend(_walk_track_items(child))
    elif isinstance(value, list):
        for child in value:
            tracks.extend(_walk_track_items(child))
    return tracks


def _track_from_dict(value: dict[str, Any]) -> TrackSearchItem | None:
    attrs = value.get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    source = {**value, **attrs}
    type_value = str(source.get("@type") or source.get("type") or "").casefold()
    title = clean_title(source.get("title") or source.get("name") or source.get("trackName"))
    artist = clean_title(source.get("artistName"))
    if not artist:
        artist = _artists(source.get("byArtist") or source.get("artists") or source.get("artist"))
    album = clean_title(source.get("albumName") or source.get("album"))
    track_number = _int(
        source.get("trackNumber") or source.get("track_number") or source.get("position")
    )
    external_urls = source.get("external_urls")
    external_urls = external_urls if isinstance(external_urls, dict) else {}
    url = source.get("url") or external_urls.get("spotify")

    if not title:
        return None
    if (
        not artist
        and "musicrecording" not in type_value
        and "track" not in type_value
        and not track_number
    ):
        return None
    return TrackSearchItem(
        title=title, artist=artist, album=album, track_number=track_number, source_url=url
    )


def _dedupe_tracks(tracks: list[TrackSearchItem]) -> list[TrackSearchItem]:
    deduped: list[TrackSearchItem] = []
    seen: set[tuple[str, str]] = set()
    for track in tracks:
        key = ((track.artist or "").casefold(), track.title.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(track)
    return deduped


def _loads_json(text: str) -> Any | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _extract_balanced_json_objects(text: str) -> list[Any]:
    roots: list[Any] = []
    for marker in ('"tracks"', '"trackList"', '"trackName"', '"artistName"'):
        start = 0
        while True:
            index = text.find(marker, start)
            if index == -1:
                break
            brace = text.rfind("{", 0, index)
            if brace == -1:
                start = index + len(marker)
                continue
            parsed = _loads_json(_balanced_object(text, brace))
            if parsed is not None:
                roots.append(parsed)
            start = index + len(marker)
    return roots


def _balanced_object(text: str, start: int) -> str:
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return text[start:]


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
