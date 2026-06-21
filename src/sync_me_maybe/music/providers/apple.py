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
from sync_me_maybe.music.providers.base import ProviderError, ResolvedTrack, TrackSearchItem
from sync_me_maybe.music.providers.common import clean_slug, int_or_none, slug_query
from sync_me_maybe.music.providers.public_scrape import PublicCollectionScraper
from sync_me_maybe.music.urls import ClassifiedLink, LinkKind, LinkScope

APPLE_API_BASE = "https://amp-api.music.apple.com"
APPLE_WEB_BASE = "https://music.apple.com"


@dataclass(frozen=True)
class AppleCollectionRef:
    """Apple Music catalog identifiers parsed from a public collection URL."""

    storefront: str
    collection_id: str
    language: str | None


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
        """Build a YouTube Music search query from the Apple Music URL."""
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

    async def expand_collection(self, link: ClassifiedLink) -> list[TrackSearchItem]:
        """Expand albums/playlists using public metadata extraction."""
        return await asyncio.to_thread(self._collection_sync, link)

    def _collection_sync(self, link: ClassifiedLink) -> list[TrackSearchItem]:
        """Return track items from a public Apple Music collection page."""
        try:
            tracks = self._catalog_collection(link.url)
        except ProviderError:
            tracks = []
        if tracks:
            return tracks

        tracks = self.public_scraper.collection(link.url)
        if not tracks:
            raise ProviderError(
                "Could not expand this Apple Music collection. "
                "Public extraction did not expose track data.",
                retryable=False,
            )
        return tracks

    def _catalog_collection(self, url: str) -> list[TrackSearchItem]:
        """Expand Apple Music playlists through the paginated catalog API."""
        ref = self._collection_ref(url)
        if not ref:
            return []
        token = self._developer_token()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Origin": APPLE_WEB_BASE,
            "Referer": APPLE_WEB_BASE,
            "User-Agent": "Mozilla/5.0",
        }
        language = ref.language or "en"
        next_url: str | None = (
            f"{APPLE_API_BASE}/v1/catalog/{ref.storefront}/playlists/"
            f"{ref.collection_id}?l={language}&include=tracks"
        )
        tracks: list[TrackSearchItem] = []
        while next_url:
            data = self._apple_json(next_url, headers)
            page_tracks, next_path = self._catalog_tracks_page(data)
            tracks.extend(page_tracks)
            next_url = f"{APPLE_API_BASE}{next_path}" if next_path else None
        return tracks

    def _collection_ref(self, url: str) -> AppleCollectionRef | None:
        """Parse storefront, playlist/album id, and language from an Apple URL."""
        parsed = urlparse(url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if len(parts) < 4:
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
        return AppleCollectionRef(storefront, collection_id, language)

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
    ) -> tuple[list[TrackSearchItem], str | None]:
        """Convert either an initial playlist page or a paginated tracks page."""
        items = data.get("data")
        if not isinstance(items, list):
            return [], None

        page_items: list[Any]
        next_path: str | None
        if items and isinstance(items[0], dict) and items[0].get("type") == "playlists":
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

        return [track for item in page_items if (track := self._catalog_track(item))], next_path

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
