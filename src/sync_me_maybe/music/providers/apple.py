from __future__ import annotations

import asyncio
from urllib.parse import parse_qs, unquote, urlparse

from sync_me_maybe.music.providers.base import ProviderError, ResolvedTrack, TrackSearchItem
from sync_me_maybe.music.providers.common import clean_slug, slug_query
from sync_me_maybe.music.providers.public_scrape import PublicCollectionScraper
from sync_me_maybe.music.urls import ClassifiedLink, LinkKind, LinkScope


class AppleMusicProvider:
    kind = LinkKind.APPLE_MUSIC

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.public_scraper = PublicCollectionScraper(timeout_seconds)

    def classify(self, url: str) -> ClassifiedLink | None:
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
        return await asyncio.to_thread(self._collection_sync, link)

    def _collection_sync(self, link: ClassifiedLink) -> list[TrackSearchItem]:
        tracks = self.public_scraper.collection(link.url)
        if not tracks:
            raise ProviderError(
                "Could not expand this Apple Music collection. "
                "Public extraction did not expose track data.",
                retryable=False,
            )
        return tracks

    def _apple_music_query(self, url: str) -> str | None:
        parsed = urlparse(url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if "album" in parts:
            index = parts.index("album")
            if index + 1 < len(parts):
                return clean_slug(parts[index + 1])
        return slug_query(url)
