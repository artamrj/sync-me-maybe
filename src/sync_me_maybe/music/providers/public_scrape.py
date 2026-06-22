"""Best-effort public metadata scraper for provider playlists and albums."""

from __future__ import annotations

import json
import re
from typing import Any

import requests
import yt_dlp
from bs4 import BeautifulSoup

from sync_me_maybe.music.filenames import clean_title
from sync_me_maybe.music.providers.base import ExpandedCollection, ProviderError, TrackSearchItem
from sync_me_maybe.music.providers.common import int_or_none


class PublicCollectionScraper:
    """Extract track lists from public provider pages without API credentials."""

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def collection(self, url: str) -> ExpandedCollection:
        """Try yt-dlp metadata first, then fall back to HTML/JSON scraping."""
        collection = self.yt_dlp_collection(url)
        if collection.tracks:
            return collection
        return self.html_entries(url)

    def yt_dlp_collection(self, url: str) -> ExpandedCollection:
        """Ask yt-dlp for flat collection metadata without downloading media."""
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
            return ExpandedCollection([])
        return ExpandedCollection(
            tracks_from_entries((info or {}).get("entries") or []),
            owner=clean_title(
                (info or {}).get("uploader")
                or (info or {}).get("channel")
                or (info or {}).get("creator")
            ),
            title=clean_title(
                (info or {}).get("playlist_title")
                or (info or {}).get("title")
                or (info or {}).get("album")
            ),
        )

    def yt_dlp_entries(self, url: str) -> list[TrackSearchItem]:
        """Return only yt-dlp track entries for tests and legacy call sites."""
        return self.yt_dlp_collection(url).tracks

    def html_entries(self, url: str) -> ExpandedCollection:
        """Scrape embedded JSON from public HTML pages."""
        try:
            response = requests.get(
                url, timeout=self.timeout_seconds, headers={"User-Agent": "Mozilla/5.0"}
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(
                f"Could not fetch public collection metadata: {exc}", retryable=True
            ) from exc

        soup = BeautifulSoup(response.text, "html.parser")
        json_roots: list[Any] = []
        for script in soup.find_all("script"):
            text = script.string or script.get_text()
            if not text:
                continue
            script_type = str(script.get("type") or "").lower()
            script_id = str(script.get("id") or "")
            # Modern music pages often place the useful track list in JSON-LD,
            # Next.js data, or another script tag instead of visible markup.
            if "json" in script_type or script_id == "__NEXT_DATA__":
                parsed = loads_json(text)
                if parsed is not None:
                    json_roots.append(parsed)

        if not json_roots:
            # Some pages embed JSON-like blobs inside regular scripts. The
            # balanced-object scan gives us a last chance before failing.
            json_roots.extend(extract_balanced_json_objects(response.text))

        tracks: list[TrackSearchItem] = []
        for root in json_roots:
            tracks.extend(walk_track_items(root))
        json_owner, json_title = collection_metadata(json_roots)
        page_owner, page_title = collection_metadata_from_page_title(
            meta(soup, "og:title") or (soup.title.string if soup.title else None)
        )
        return ExpandedCollection(
            dedupe_tracks(tracks),
            owner=clean_title(
                json_owner
                or page_owner
                or meta(soup, "music:musician")
                or meta(soup, "music:creator")
                or meta(soup, "og:site_name")
            ),
            title=clean_title(json_title or page_title),
        )


def meta(soup: BeautifulSoup, property_name: str) -> str | None:
    """Read one meta tag value from a BeautifulSoup document."""
    tag = soup.find("meta", attrs={"property": property_name}) or soup.find(
        "meta", attrs={"name": property_name}
    )
    if not tag:
        return None
    content = tag.get("content")
    return str(content) if content else None


def artists(artists_value: Any) -> str | None:
    """Normalize provider-specific artist shapes into display text."""
    if isinstance(artists_value, dict):
        return clean_title(artists_value.get("name") or artists_value.get("artistName"))
    if not isinstance(artists_value, list):
        return clean_title(str(artists_value)) if artists_value else None
    names = [
        clean_title(artist.get("name") or artist.get("artistName"))
        for artist in artists_value
        if isinstance(artist, dict)
    ]
    return ", ".join(name for name in names if name) or None


def tracks_from_entries(entries: list[Any]) -> list[TrackSearchItem]:
    """Convert yt-dlp playlist entries into queueable search items."""
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
                track_number=int_or_none(entry.get("track_number") or entry.get("trackNumber"))
                or index,
                source_url=entry.get("url") or entry.get("webpage_url"),
            )
        )
    return dedupe_tracks(tracks)


def walk_track_items(value: Any) -> list[TrackSearchItem]:
    """Recursively search nested JSON for objects that look like tracks."""
    tracks: list[TrackSearchItem] = []
    if isinstance(value, dict):
        track = track_from_dict(value)
        if track:
            tracks.append(track)
        for child in value.values():
            tracks.extend(walk_track_items(child))
    elif isinstance(value, list):
        for child in value:
            tracks.extend(walk_track_items(child))
    return tracks


def collection_metadata(values: list[Any]) -> tuple[str | None, str | None]:
    """Return best-effort collection owner and title from embedded JSON roots."""
    for value in values:
        metadata = collection_metadata_from_dict(value)
        if metadata != (None, None):
            return metadata
    return None, None


def collection_metadata_from_page_title(value: str | None) -> tuple[str | None, str | None]:
    """Parse public page titles like 'feels - playlist by Romy B | Spotify'."""
    title = clean_title(value)
    if not title:
        return None, None
    match = re.match(r"(?P<title>.+?)\s+-\s+(?:playlist|album)\s+by\s+(?P<owner>.+)$", title, re.I)
    if not match:
        return None, title
    return clean_title(match.group("owner")), clean_title(match.group("title"))


def collection_metadata_from_dict(value: Any) -> tuple[str | None, str | None]:
    if isinstance(value, dict):
        entity = value.get("entity")
        if isinstance(entity, dict):
            type_value = str(entity.get("type") or entity.get("entityType") or "").casefold()
            track_list = entity.get("trackList")
            if type_value in {"playlist", "album"} and isinstance(track_list, list):
                return clean_title(entity.get("subtitle")), clean_title(
                    entity.get("title") or entity.get("name")
                )
        for child in value.values():
            metadata = collection_metadata_from_dict(child)
            if metadata != (None, None):
                return metadata
    elif isinstance(value, list):
        for child in value:
            metadata = collection_metadata_from_dict(child)
            if metadata != (None, None):
                return metadata
    return None, None


def track_from_dict(value: dict[str, Any]) -> TrackSearchItem | None:
    """Convert one provider JSON object into a track when it has enough data."""
    attrs = value.get("attributes")
    attrs = attrs if isinstance(attrs, dict) else {}
    source = {**value, **attrs}
    type_value = str(
        source.get("@type") or source.get("type") or source.get("entityType") or ""
    ).casefold()
    title = clean_title(source.get("title") or source.get("name") or source.get("trackName"))
    artist = clean_title(source.get("artistName"))
    if not artist:
        artist = artists(source.get("byArtist") or source.get("artists") or source.get("artist"))
    if not artist and type_value == "track":
        artist = clean_title(source.get("subtitle"))
    album = clean_title(source.get("albumName") or source.get("album"))
    track_number = int_or_none(
        source.get("trackNumber") or source.get("track_number") or source.get("position")
    )
    external_urls = source.get("external_urls")
    external_urls = external_urls if isinstance(external_urls, dict) else {}
    url = source.get("url") or external_urls.get("spotify") or source.get("uri")

    if not title:
        return None
    # Avoid treating every named object on a page as a track. A missing artist is
    # allowed only when the JSON type or position strongly suggests music data.
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


def dedupe_tracks(tracks: list[TrackSearchItem]) -> list[TrackSearchItem]:
    """Remove duplicate artist/title pairs while preserving page order."""
    deduped: list[TrackSearchItem] = []
    seen: set[tuple[str, str]] = set()
    for track in tracks:
        key = ((track.artist or "").casefold(), track.title.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(track)
    return deduped


def loads_json(text: str) -> Any | None:
    """Parse JSON and return None when a script is not valid JSON."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def extract_balanced_json_objects(text: str) -> list[Any]:
    """Find JSON objects near music-related markers in raw HTML text."""
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
            parsed = loads_json(balanced_object(text, brace))
            if parsed is not None:
                roots.append(parsed)
            start = index + len(marker)
    return roots


def balanced_object(text: str, start: int) -> str:
    """Return a balanced JSON object substring starting at an opening brace."""
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
