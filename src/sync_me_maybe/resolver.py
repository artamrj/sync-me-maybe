from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from .filenames import clean_title
from .urls import ClassifiedLink, LinkKind

LOGGER = logging.getLogger(__name__)
GENERIC_SLUG_PARTS = {"album", "artist", "music", "song", "track", "us", "de", "gb", "fr", "es", "it"}


@dataclass(frozen=True)
class ResolvedTrack:
    source_url: str
    download_url: str
    search_query: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None


class ResolveError(RuntimeError):
    pass


class LinkResolver:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds

    def resolve(self, classified: ClassifiedLink) -> ResolvedTrack:
        if classified.kind == LinkKind.YOUTUBE:
            return ResolvedTrack(source_url=classified.url, download_url=classified.url)

        if classified.kind in {LinkKind.SPOTIFY, LinkKind.APPLE_MUSIC, LinkKind.SHAZAM}:
            query, title, artist, album = self._query_for(classified)
            if not query:
                raise ResolveError("Could not build a YouTube Music search query from this link.")
            return ResolvedTrack(
                source_url=classified.url,
                download_url=f"ytsearch1:{query}",
                search_query=query,
                title=title,
                artist=artist,
                album=album,
            )

        raise ResolveError(classified.reason or "Unsupported link.")

    def _query_for(self, classified: ClassifiedLink) -> tuple[str | None, str | None, str | None, str | None]:
        if classified.kind == LinkKind.SPOTIFY:
            spotify = self._spotify_oembed(classified.url)
            if spotify:
                return spotify
            return self._best_effort_metadata_or_slug(classified.url, self._slug_query(classified.url))

        if classified.kind == LinkKind.APPLE_MUSIC:
            fallback_query = self._apple_music_query(classified.url)
            return fallback_query, fallback_query, None, None

        if classified.kind == LinkKind.SHAZAM:
            fallback_query = self._shazam_query(classified.url)
            if not fallback_query and _is_numeric_shazam_track_url(classified.url):
                return self._shazam_numeric_track_query(classified.url)
            return fallback_query, fallback_query, None, None

        return None, None, None, None

    def _best_effort_metadata_or_slug(self, url: str, fallback_query: str | None) -> tuple[str | None, str | None, str | None, str | None]:
        try:
            title, artist, album = self._page_metadata(url)
        except ResolveError as exc:
            LOGGER.debug("Metadata lookup failed, using URL fallback: %s", exc)
            return fallback_query, fallback_query, None, None

        parts = [artist, title] if artist else [title]
        metadata_query = " ".join(part for part in parts if part)
        query = metadata_query or fallback_query
        return query or None, title or fallback_query, artist, album

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

    def _apple_music_query(self, url: str) -> str | None:
        parsed = urlparse(url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if "album" in parts:
            index = parts.index("album")
            if index + 1 < len(parts):
                return _clean_slug(parts[index + 1])
        return self._slug_query(url)

    def _shazam_query(self, url: str) -> str | None:
        parsed = urlparse(url)
        parts = [unquote(part) for part in parsed.path.split("/") if part]
        if "track" in parts:
            index = parts.index("track")
            for part in parts[index + 1 :]:
                cleaned = _clean_slug(part)
                if _usable_slug_query(cleaned):
                    return cleaned
        return self._slug_query(url)

    def _shazam_numeric_track_query(self, url: str) -> tuple[str | None, str | None, str | None, str | None]:
        try:
            response = requests.get(url, timeout=self.timeout_seconds, headers={"User-Agent": "Mozilla/5.0"})
            response.raise_for_status()
        except requests.RequestException as exc:
            LOGGER.debug("Shazam numeric track lookup failed: %s", exc)
            return None, None, None, None

        title, artist = _shazam_title_artist(response.text)
        if title and artist:
            return f"{artist} {title}", title, artist, None
        if title:
            return title, title, None, None

        redirected_query = self._shazam_query(response.url)
        return redirected_query, redirected_query, None, None

    def _slug_query(self, url: str) -> str | None:
        parsed = urlparse(url)
        for part in reversed([unquote(part) for part in parsed.path.split("/") if part]):
            cleaned = _clean_slug(part)
            if _usable_slug_query(cleaned):
                return cleaned
        return None

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


def _clean_slug(value: str | None) -> str | None:
    if not value:
        return None
    value = unquote(value)
    value = re.sub(r"\.[a-z0-9]{2,5}$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[_-]+", " ", value)
    value = re.sub(r"\b\d{4,}\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" /.-_")
    return clean_title(value) if value else None


def _usable_slug_query(value: str | None) -> bool:
    if not value:
        return False
    return not value.isdigit() and value.casefold() not in GENERIC_SLUG_PARTS


def _is_numeric_shazam_track_url(url: str) -> bool:
    parts = [unquote(part) for part in urlparse(url).path.split("/") if part]
    if "track" not in parts:
        return False
    index = parts.index("track")
    return index + 1 < len(parts) and parts[index + 1].isdigit()


def _shazam_title_artist(html: str) -> tuple[str | None, str | None]:
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
    tag = soup.find("meta", attrs={"property": property_name}) or soup.find("meta", attrs={"name": property_name})
    if not tag:
        return None
    content = tag.get("content")
    return str(content) if content else None
