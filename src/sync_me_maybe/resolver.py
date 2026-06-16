from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .filenames import clean_title
from .urls import ClassifiedLink, LinkKind

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedTrack:
    source_url: str
    download_url: str
    search_query: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None


class ResolveError(RuntimeError):
    pass


class LinkResolver:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def resolve(self, classified: ClassifiedLink) -> ResolvedTrack:
        if classified.kind == LinkKind.YOUTUBE:
            return ResolvedTrack(source_url=classified.url, download_url=classified.url)

        if classified.kind in {LinkKind.SPOTIFY, LinkKind.APPLE_MUSIC, LinkKind.SHAZAM}:
            query, title, artist, album = self._metadata_query(classified.url)
            if not query:
                raise ResolveError("Could not resolve enough metadata to search YouTube Music.")
            return ResolvedTrack(
                source_url=classified.url,
                download_url=f"ytsearch1:{query}",
                search_query=query,
                title=title,
                artist=artist,
                album=album,
            )

        raise ResolveError(classified.reason or "Unsupported link.")

    def _metadata_query(self, url: str) -> tuple[str | None, str | None, str | None, str | None]:
        parsed = urlparse(url)
        if parsed.netloc.lower().replace("www.", "") == "open.spotify.com":
            spotify = self._spotify_oembed(url)
            if spotify:
                return spotify

        title, artist, album = self._page_metadata(url)
        parts = [artist, title] if artist else [title]
        query = " ".join(part for part in parts if part)
        return query or None, title, artist, album

    def _spotify_oembed(self, url: str) -> tuple[str | None, str | None, str | None, str | None] | None:
        try:
            response = requests.get(
                "https://open.spotify.com/oembed",
                params={"url": url},
                timeout=self.timeout_seconds,
                headers={"User-Agent": "sync-me-maybe/0.1"},
            )
            response.raise_for_status()
            title = clean_title(response.json().get("title"))
        except Exception as exc:  # noqa: BLE001 - metadata fallback should be best-effort.
            LOGGER.debug("Spotify oEmbed resolution failed: %s", exc)
            return None

        if not title:
            return None
        return title, title, None, None

    def _page_metadata(self, url: str) -> tuple[str | None, str | None, str | None]:
        try:
            response = requests.get(url, timeout=self.timeout_seconds, headers={"User-Agent": "sync-me-maybe/0.1"})
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ResolveError(f"Could not fetch link metadata: {exc}") from exc

        soup = BeautifulSoup(response.text, "html.parser")
        title = self._meta(soup, "og:title") or (soup.title.string if soup.title else None)
        artist = self._meta(soup, "music:musician") or self._meta(soup, "og:site_name")
        album = self._meta(soup, "music:album")
        return clean_title(title), clean_title(artist), clean_title(album)

    @staticmethod
    def _meta(soup: BeautifulSoup, property_name: str) -> str | None:
        tag = soup.find("meta", attrs={"property": property_name}) or soup.find("meta", attrs={"name": property_name})
        if not tag:
            return None
        content = tag.get("content")
        return str(content) if content else None
