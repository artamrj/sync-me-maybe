"""Shazam provider adapter.

Shazam links identify tracks but are not download sources, so they become
YouTube Music search queries.
"""

from __future__ import annotations

import asyncio
import re
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from sync_me_maybe.music.filenames import clean_title
from sync_me_maybe.music.providers.base import (
    ResolvedTrack,
    TrackSearchItem,
    unsupported_collection,
)
from sync_me_maybe.music.providers.common import clean_slug, slug_query, usable_slug_query
from sync_me_maybe.music.urls import ClassifiedLink, LinkKind


class ShazamProvider:
    """Resolve Shazam track URLs into searchable track metadata."""

    kind = LinkKind.SHAZAM

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def classify(self, url: str) -> ClassifiedLink | None:
        """Recognize any shazam.com URL as a Shazam track link."""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if not host.endswith("shazam.com"):
            return None
        return ClassifiedLink(LinkKind.SHAZAM, url)

    async def resolve_track(self, link: ClassifiedLink) -> ResolvedTrack:
        """Resolve Shazam metadata without blocking the Telegram event loop."""
        return await asyncio.to_thread(self._resolve_track_sync, link)

    async def expand_collection(self, link: ClassifiedLink) -> list[TrackSearchItem]:
        """Shazam collection expansion is not supported by this bot."""
        raise unsupported_collection()

    def _resolve_track_sync(self, link: ClassifiedLink) -> ResolvedTrack:
        """Build the YouTube Music search query for one Shazam link."""
        fallback_query = self._shazam_query(link.url)
        if not fallback_query and _is_numeric_shazam_track_url(link.url):
            query, title, artist, album = self._shazam_numeric_track_query(link.url)
        else:
            query, title, artist, album = fallback_query, fallback_query, None, None
        if not query:
            from sync_me_maybe.music.providers.base import ProviderError

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

    def _shazam_query(self, url: str) -> str | None:
        """Extract a readable query from non-numeric Shazam URL slugs."""
        parsed = urlparse(url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if "track" in parts:
            index = parts.index("track")
            for part in parts[index + 1 :]:
                cleaned = clean_slug(part)
                if usable_slug_query(cleaned):
                    return cleaned
        return slug_query(url)

    def _shazam_numeric_track_query(
        self, url: str
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Fetch numeric Shazam URLs because their path lacks title text."""
        try:
            response = requests.get(
                url, timeout=self.timeout_seconds, headers={"User-Agent": "Mozilla/5.0"}
            )
            response.raise_for_status()
        except requests.RequestException:
            return None, None, None, None

        title, artist = _shazam_title_artist(response.text)
        if title and artist:
            return f"{artist} {title}", title, artist, None
        if title:
            return title, title, None, None

        redirected_query = self._shazam_query(response.url)
        return redirected_query, redirected_query, None, None


def _is_numeric_shazam_track_url(url: str) -> bool:
    """Return true when the URL path has /track/<number>."""
    parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if "track" not in parts:
        return False
    index = parts.index("track")
    return index + 1 < len(parts) and parts[index + 1].isdigit()


def _shazam_title_artist(html: str) -> tuple[str | None, str | None]:
    """Parse title/artist from public Shazam HTML metadata."""
    soup = BeautifulSoup(html, "html.parser")
    raw_title = (
        _soup_meta(soup, "og:title")
        or _soup_meta(soup, "twitter:title")
        or (soup.title.string if soup.title else None)
    )
    if not raw_title:
        return None, None

    cleaned = re.sub(r":\s*Song Lyrics, Music Videos.*$", "", raw_title, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*\|\s*Shazam\s*$", "", cleaned, flags=re.IGNORECASE).strip()
    if " - " not in cleaned:
        return clean_title(cleaned), None

    title, artist = cleaned.split(" - ", 1)
    return clean_title(title), clean_title(artist)


def _soup_meta(soup: BeautifulSoup, property_name: str) -> str | None:
    """Read one meta tag value from a BeautifulSoup document."""
    tag = soup.find("meta", attrs={"property": property_name}) or soup.find(
        "meta", attrs={"name": property_name}
    )
    if not tag:
        return None
    content = tag.get("content")
    return str(content) if content else None
