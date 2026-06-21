"""Spotify provider adapter.

Spotify is used only for public metadata. Actual audio is found by turning that
metadata into a YouTube Music search query.
"""

from __future__ import annotations

import asyncio

import requests
from bs4 import BeautifulSoup

from sync_me_maybe.music.filenames import clean_title
from sync_me_maybe.music.providers.base import ExpandedCollection, ProviderError, ResolvedTrack
from sync_me_maybe.music.providers.common import slug_query
from sync_me_maybe.music.providers.public_scrape import PublicCollectionScraper
from sync_me_maybe.music.urls import ClassifiedLink, LinkKind, LinkScope


class SpotifyProvider:
    """Resolve Spotify links and expand public Spotify collections."""

    kind = LinkKind.SPOTIFY

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds
        self.public_scraper = PublicCollectionScraper(timeout_seconds)

    def classify(self, url: str) -> ClassifiedLink | None:
        """Recognize Spotify track, playlist, and album URLs."""
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in {"open.spotify.com", "spotify.link"}:
            return None
        path = parsed.path.lower()
        if "/playlist/" in path:
            return ClassifiedLink(LinkKind.SPOTIFY, url, LinkScope.PLAYLIST)
        if "/album/" in path:
            return ClassifiedLink(LinkKind.SPOTIFY, url, LinkScope.ALBUM)
        return ClassifiedLink(LinkKind.SPOTIFY, url)

    async def resolve_track(self, link: ClassifiedLink) -> ResolvedTrack:
        """Resolve a Spotify track link without blocking the event loop."""
        return await asyncio.to_thread(self._resolve_track_sync, link)

    async def expand_collection(self, link: ClassifiedLink) -> ExpandedCollection:
        """Expand playlists/albums using public page data."""
        return await asyncio.to_thread(self._collection_sync, link)

    def _resolve_track_sync(self, link: ClassifiedLink) -> ResolvedTrack:
        """Build the YouTube Music search query for one Spotify item."""
        spotify = self._spotify_oembed(link.url)
        if spotify:
            query, title, artist, album = spotify
        else:
            query, title, artist, album = self._best_effort_metadata_or_slug(
                link.url, slug_query(link.url)
            )
        if not query:
            raise ProviderError(
                "Could not build a YouTube Music search query from this link.", retryable=False
            )
        return ResolvedTrack(
            source_url=link.url,
            download_url=f"ytsearch1:{query}",
            search_query=query,
            title=title,
            artist=artist,
            album=album,
        )

    def _collection_sync(self, link: ClassifiedLink) -> ExpandedCollection:
        """Return track items from a public playlist or album page."""
        collection = self.public_scraper.collection(link.url)
        if not collection.tracks:
            raise ProviderError(
                "Could not expand this Spotify collection. "
                "Public extraction did not expose track data.",
                retryable=False,
            )
        return collection

    def _best_effort_metadata_or_slug(
        self, url: str, fallback_query: str | None
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Use page metadata when available, otherwise fall back to URL text."""
        try:
            title, artist, album = self._page_metadata(url)
        except ProviderError:
            return fallback_query, fallback_query, None, None

        parts = [artist, title] if artist else [title]
        metadata_query = " ".join(part for part in parts if part)
        query = metadata_query or fallback_query
        return query or None, title or fallback_query, artist, album

    def _spotify_oembed(
        self, url: str
    ) -> tuple[str | None, str | None, str | None, str | None] | None:
        """Ask Spotify's public oEmbed endpoint for a simple title."""
        try:
            response = requests.get(
                "https://open.spotify.com/oembed",
                params={"url": url},
                timeout=self.timeout_seconds,
                headers={"User-Agent": "sync-me-maybe/0.1"},
            )
            response.raise_for_status()
            title = clean_title(response.json().get("title"))
        except Exception:  # noqa: BLE001 - metadata fallback should be best-effort.
            return None

        if not title:
            return None
        return title, title, None, None

    def _page_metadata(self, url: str) -> tuple[str | None, str | None, str | None]:
        """Scrape Open Graph metadata from the public Spotify page."""
        try:
            response = requests.get(
                url, timeout=self.timeout_seconds, headers={"User-Agent": "sync-me-maybe/0.1"}
            )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProviderError(f"Could not fetch link metadata: {exc}", retryable=True) from exc

        soup = BeautifulSoup(response.text, "html.parser")
        title = _meta(soup, "og:title") or (soup.title.string if soup.title else None)
        artist = _meta(soup, "music:musician") or _meta(soup, "og:site_name")
        album = _meta(soup, "music:album")
        return clean_title(title), clean_title(artist), clean_title(album)


def _meta(soup: BeautifulSoup, property_name: str) -> str | None:
    """Read one meta tag value from a BeautifulSoup document."""
    tag = soup.find("meta", attrs={"property": property_name}) or soup.find(
        "meta", attrs={"name": property_name}
    )
    if not tag:
        return None
    content = tag.get("content")
    return str(content) if content else None
