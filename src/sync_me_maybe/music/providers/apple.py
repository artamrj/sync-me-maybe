"""Apple Music provider adapter.

Apple Music links are converted into YouTube Music searches or expanded through
public page metadata; no Apple Music audio is downloaded directly.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

import requests

from sync_me_maybe.music.filenames import clean_title
from sync_me_maybe.music.providers.base import (
    ExpandedCollection,
    ProviderError,
    ResolvedTrack,
    TrackSearchItem,
)
from sync_me_maybe.music.providers.common import (
    clean_slug,
    duration_from_ms,
    int_or_none,
    slug_query,
)
from sync_me_maybe.music.providers.public_scrape import PublicCollectionScraper
from sync_me_maybe.music.urls import ClassifiedLink, LinkKind, LinkScope

APPLE_API_BASE = "https://amp-api.music.apple.com"
APPLE_WEB_BASE = "https://music.apple.com"
ITUNES_LOOKUP_URL = "https://itunes.apple.com/lookup"


@dataclass(frozen=True)
class AppleCollectionRef:
    """Apple Music catalog identifiers parsed from a public collection URL."""

    storefront: str
    collection_id: str
    language: str | None
    scope: LinkScope


class AppleMusicProvider:
    """Resolve Apple Music links and expand public Apple Music collections."""

    kind = LinkKind.APPLE_MUSIC

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds
        self.public_scraper = PublicCollectionScraper(timeout_seconds)

    def classify(self, url: str) -> ClassifiedLink | None:
        """Recognize Apple Music/iTunes track, album, and playlist URLs."""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in {"music.apple.com", "itunes.apple.com"}:
            return None
        path = parsed.path.lower()
        query = parse_qs(parsed.query)
        if "/album/" in path and "i" not in query:
            return ClassifiedLink(LinkKind.APPLE_MUSIC, url, LinkScope.ALBUM)
        if "/playlist/" in path:
            return ClassifiedLink(LinkKind.APPLE_MUSIC, url, LinkScope.PLAYLIST)
        return ClassifiedLink(LinkKind.APPLE_MUSIC, url)

    async def resolve_track(self, link: ClassifiedLink) -> ResolvedTrack:
        """Resolve track metadata with the iTunes lookup API, then URL slugs."""
        track_id = apple_track_id(link.url)
        if track_id:
            looked_up = await asyncio.to_thread(self._itunes_track, link.url, track_id)
            if looked_up:
                return looked_up
        query = self._apple_music_query(link.url)
        if not query:
            raise ProviderError(
                "Could not build a YouTube Music search query from this link.", retryable=False
            )
        return ResolvedTrack(
            source_url=link.url,
            download_url=f"ytsearch1:{query}",
            search_query=query,
            title=query,
        )

    def _itunes_track(self, url: str, track_id: str) -> ResolvedTrack | None:
        """Look up the real track title/artist; album URLs only carry the album slug."""
        try:
            response = requests.get(
                ITUNES_LOOKUP_URL,
                params={"id": track_id, "entity": "song", "country": apple_storefront(url)},
                timeout=self.timeout_seconds,
            )
            response.raise_for_status()
            data = response.json()
        except (requests.RequestException, ValueError):
            return None
        results = data.get("results") if isinstance(data, dict) else None
        song = next(
            (r for r in results or [] if isinstance(r, dict) and r.get("kind") == "song"),
            None,
        )
        if not song:
            return None
        title = clean_title(song.get("trackName"))
        artist = clean_title(song.get("artistName"))
        if not title:
            return None
        query = " ".join(part for part in [artist, title] if part)
        return ResolvedTrack(
            source_url=url,
            download_url=f"ytsearch1:{query}",
            search_query=query,
            title=title,
            artist=artist,
            album=clean_title(song.get("collectionName")),
            track_number=int_or_none(song.get("trackNumber")),
            duration_seconds=duration_from_ms(song.get("trackTimeMillis")),
        )

    async def expand_collection(self, link: ClassifiedLink) -> ExpandedCollection:
        """Expand albums/playlists using public metadata extraction."""
        return await asyncio.to_thread(self._collection_sync, link)

    def _collection_sync(self, link: ClassifiedLink) -> ExpandedCollection:
        """Return track items from a public Apple Music collection page."""
        try:
            collection = self._catalog_collection(link.url)
        except ProviderError:
            collection = ExpandedCollection([])
        if collection.tracks:
            return collection

        collection = self.public_scraper.collection(link.url)
        if not collection.tracks:
            raise ProviderError(
                "Could not expand this Apple Music collection. "
                "Public extraction did not expose track data.",
                retryable=False,
            )
        return collection

    def _catalog_collection(self, url: str) -> ExpandedCollection:
        """Expand Apple Music playlists through the paginated catalog API."""
        ref = self._collection_ref(url)
        if not ref:
            return ExpandedCollection([])
        token = self._developer_token()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": APPLE_WEB_BASE,
            "Referer": APPLE_WEB_BASE,
            "User-Agent": "Mozilla/5.0",
        }
        language = ref.language or "en"
        collection_type = "albums" if ref.scope == LinkScope.ALBUM else "playlists"
        include = "tracks" if ref.scope == LinkScope.PLAYLIST else "tracks"
        next_url: str | None = (
            f"{APPLE_API_BASE}/v1/catalog/{ref.storefront}/{collection_type}/"
            f"{ref.collection_id}?l={language}&include={include}"
        )
        tracks: list[TrackSearchItem] = []
        owner: str | None = None
        title: str | None = None
        while next_url:
            data = self._apple_json(next_url, headers)
            page_tracks, next_path, page_owner, page_title = self._catalog_tracks_page(data)
            tracks.extend(page_tracks)
            owner = owner or page_owner
            title = title or page_title
            next_url = f"{APPLE_API_BASE}{next_path}" if next_path else None
        return ExpandedCollection(tracks=tracks, owner=owner, title=title)

    def _collection_ref(self, url: str) -> AppleCollectionRef | None:
        """Parse storefront, playlist/album id, and language from an Apple URL."""
        parsed = urlparse(url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 4:
            return None
        if "playlist" in parts:
            scope = LinkScope.PLAYLIST
        elif "album" in parts:
            scope = LinkScope.ALBUM
        else:
            return None
        storefront = parts[0].lower()
        collection_id = parts[-1]
        if not collection_id:
            return None
        language_values = parse_qs(parsed.query).get("l") or []
        raw_language = language_values[0] if language_values else None
        language = raw_language.replace("_", "-") if raw_language else None
        if language and "-" not in language:
            language = f"{language}-{storefront.upper()}"
        return AppleCollectionRef(storefront, collection_id, language, scope)

    def _developer_token(self) -> str:
        """Read the public MusicKit developer token from Apple Music web assets."""
        try:
            response = requests.get(
                APPLE_WEB_BASE,
                timeout=self.timeout_seconds,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(
                f"Could not fetch Apple Music web metadata: {exc}", retryable=True
            ) from exc
        asset_match = re.search(r"/assets/index~[^\" ]+\.js", response.text)
        if not asset_match:
            raise ProviderError("Could not find Apple Music web asset.", retryable=True)

        try:
            asset_response = requests.get(
                f"{APPLE_WEB_BASE}{asset_match.group(0)}",
                timeout=self.timeout_seconds,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            asset_response.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(
                f"Could not fetch Apple Music web asset: {exc}", retryable=True
            ) from exc
        token_match = re.search(r'qc\s*=\s*"([^"]+)"', asset_response.text)
        if not token_match:
            raise ProviderError("Could not find Apple Music developer token.", retryable=True)
        return token_match.group(1)

    def _apple_json(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        """Fetch one Apple catalog JSON page."""
        try:
            response = requests.get(url, timeout=self.timeout_seconds, headers=headers)
            response.raise_for_status()
            data = response.json()
        except (ValueError, requests.RequestException) as exc:
            raise ProviderError(
                f"Could not fetch Apple Music catalog metadata: {exc}", retryable=True
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError("Apple Music catalog returned invalid metadata.", retryable=True)
        return data

    def _catalog_tracks_page(
        self, data: dict[str, Any]
    ) -> tuple[list[TrackSearchItem], str | None, str | None, str | None]:
        """Convert either an initial playlist page or a paginated tracks page."""
        items = data.get("data")
        if not isinstance(items, list):
            return [], None, None, None

        page_items: list[Any]
        next_path: str | None
        owner: str | None = None
        title: str | None = None
        if items and isinstance(items[0], dict) and items[0].get("type") in {"albums", "playlists"}:
            attributes = items[0].get("attributes")
            attributes = attributes if isinstance(attributes, dict) else {}
            title = clean_title(attributes.get("name"))
            owner = clean_title(
                attributes.get("artistName")
                or attributes.get("curatorName")
                or attributes.get("name")
            )
            relationships = items[0].get("relationships")
            relationships = relationships if isinstance(relationships, dict) else {}
            tracks = relationships.get("tracks")
            tracks = tracks if isinstance(tracks, dict) else {}
            track_data = tracks.get("data")
            page_items = track_data if isinstance(track_data, list) else []
            track_next = tracks.get("next")
            next_path = track_next if isinstance(track_next, str) else None
        else:
            page_items = items
            data_next = data.get("next")
            next_path = data_next if isinstance(data_next, str) else None

        return (
            [track for item in page_items if (track := self._catalog_track(item))],
            next_path,
            owner,
            title,
        )

    def _catalog_track(self, item: Any) -> TrackSearchItem | None:
        """Convert one Apple catalog song item into a queueable search item."""
        if not isinstance(item, dict):
            return None
        attributes = item.get("attributes")
        attributes = attributes if isinstance(attributes, dict) else {}
        title = clean_title(attributes.get("name"))
        if not title:
            return None
        return TrackSearchItem(
            title=title,
            artist=clean_title(attributes.get("artistName")),
            album=clean_title(attributes.get("albumName")),
            track_number=int_or_none(attributes.get("trackNumber")),
            source_url=attributes.get("url") if isinstance(attributes.get("url"), str) else None,
            duration_seconds=duration_from_ms(attributes.get("durationInMillis")),
            isrc=clean_title(attributes.get("isrc")),
        )

    def _apple_music_query(self, url: str) -> str | None:
        """Prefer the album/track slug, then fall back to the best URL segment."""
        parsed = urlparse(url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if "album" in parts:
            index = parts.index("album")
            if index + 1 < len(parts):
                return clean_slug(parts[index + 1])
        return slug_query(url)


def apple_track_id(url: str) -> str | None:
    """Return the numeric song ID from ``?i=`` or ``/song/<slug>/<id>`` URLs."""
    parsed = urlparse(url)
    track_id = next(iter(parse_qs(parsed.query).get("i", [])), "")
    if track_id.isdigit():
        return track_id
    parts = [part for part in parsed.path.split("/") if part]
    if "song" in parts and parts[-1].isdigit():
        return parts[-1]
    return None


def apple_storefront(url: str) -> str:
    """Return the two-letter storefront from an Apple Music URL path."""
    parts = [part for part in urlparse(url).path.split("/") if part]
    if parts and len(parts[0]) == 2 and parts[0].isalpha():
        return parts[0].lower()
    return "us"
