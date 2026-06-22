"""Spotify provider adapter.

Spotify is used only for public metadata. Actual audio is found by turning that
metadata into a YouTube Music search query.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from sync_me_maybe.music.filenames import clean_title
from sync_me_maybe.music.providers.base import (
    ExpandedCollection,
    ProviderError,
    ResolvedTrack,
    TrackSearchItem,
)
from sync_me_maybe.music.providers.common import slug_query
from sync_me_maybe.music.providers.public_scrape import PublicCollectionScraper, dedupe_tracks
from sync_me_maybe.music.urls import ClassifiedLink, LinkKind, LinkScope

SPOTIFY_PAGE_SIZE = 100
SPOTIFY_PLAYLIST_TRACKS_URL = "https://api.spotify.com/v1/playlists/{playlist_id}/tracks"


class SpotifyProvider:
    """Resolve Spotify links and expand public Spotify collections."""

    kind = LinkKind.SPOTIFY

    def __init__(self, timeout_seconds: int = 20, max_collection_tracks: int | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.max_collection_tracks = max_collection_tracks
        self.public_scraper = PublicCollectionScraper(timeout_seconds)

    def classify(self, url: str) -> ClassifiedLink | None:
        """Recognize Spotify track, playlist, and album URLs."""
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
            embed_url = spotify_embed_url(link.url)
            if embed_url:
                embed_collection = self.public_scraper.collection(embed_url)
                collection = merge_collection_metadata(embed_collection, collection)
        if not collection.tracks:
            raise ProviderError(
                "Could not expand this Spotify collection. "
                "Public extraction did not expose track data.",
                retryable=False,
            )
        if link.scope == LinkScope.PLAYLIST and len(collection.tracks) == SPOTIFY_PAGE_SIZE:
            collection = self._with_paginated_playlist_tracks(link.url, collection)
        return collection

    def _with_paginated_playlist_tracks(
        self, url: str, collection: ExpandedCollection
    ) -> ExpandedCollection:
        """Merge public Web API playlist pages when the embed result is capped at 100."""
        playlist_id = spotify_playlist_id(url)
        embed_url = spotify_embed_url(url)
        if not playlist_id or not embed_url:
            return collection

        token = spotify_embed_access_token(embed_url, self.timeout_seconds)
        if not token:
            return collection

        tracks = self._playlist_api_tracks(playlist_id, token)
        if len(tracks) <= len(collection.tracks):
            return collection

        return ExpandedCollection(
            dedupe_tracks([*collection.tracks, *tracks]),
            owner=collection.owner,
            title=collection.title,
        )

    def _playlist_api_tracks(self, playlist_id: str, token: str) -> list[TrackSearchItem]:
        """Read anonymous Spotify playlist pages; failures fall back to embed tracks."""
        tracks: list[TrackSearchItem] = []
        offset = 0
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "sync-me-maybe/0.1",
        }
        stop_after = (
            self.max_collection_tracks + 1 if self.max_collection_tracks is not None else None
        )

        while True:
            params: dict[str, str | int] = {
                "limit": SPOTIFY_PAGE_SIZE,
                "offset": offset,
                "market": "from_token",
            }
            try:
                response = requests.get(
                    SPOTIFY_PLAYLIST_TRACKS_URL.format(playlist_id=playlist_id),
                    params=params,
                    timeout=self.timeout_seconds,
                    headers=headers,
                )
                if response.status_code in {401, 403, 429}:
                    return []
                response.raise_for_status()
                payload = response.json()
            except Exception:  # noqa: BLE001 - public Spotify pagination is best-effort.
                return []

            items = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                break

            page_tracks = tracks_from_spotify_playlist_items(items, start_offset=offset)
            if not page_tracks:
                break
            tracks.extend(page_tracks)
            if stop_after is not None and len(tracks) >= stop_after:
                tracks = tracks[:stop_after]
                break
            if not payload.get("next"):
                break
            offset += len(items)

        return tracks

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


def spotify_embed_url(url: str) -> str | None:
    """Build the public Spotify embed URL for playlist and album pages."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "open.spotify.com":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] not in {"playlist", "album"}:
        return None
    return f"https://open.spotify.com/embed/{parts[0]}/{parts[1]}"


def spotify_playlist_id(url: str) -> str | None:
    """Return the playlist ID from an open.spotify.com playlist URL."""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    if host != "open.spotify.com":
        return None

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2 or parts[0] != "playlist":
        return None
    return parts[1]


def spotify_embed_access_token(embed_url: str, timeout_seconds: int) -> str | None:
    """Read Spotify's anonymous embed access token from public Next.js state."""
    try:
        response = requests.get(
            embed_url,
            timeout=timeout_seconds,
            headers={"User-Agent": "sync-me-maybe/0.1"},
        )
        response.raise_for_status()
    except requests.RequestException:
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    script = soup.find("script", attrs={"id": "__NEXT_DATA__"})
    if not script or not script.string:
        return None

    try:
        data = json.loads(script.string)
    except json.JSONDecodeError:
        return None

    token = _nested_get(
        data,
        "props",
        "pageProps",
        "state",
        "settings",
        "session",
        "accessToken",
    )
    return clean_title(token)


def tracks_from_spotify_playlist_items(
    items: list[Any], *, start_offset: int = 0
) -> list[TrackSearchItem]:
    """Convert Spotify Web API playlist items into search items."""
    tracks: list[TrackSearchItem] = []
    for index, item in enumerate(items, start=start_offset + 1):
        track = track_from_spotify_playlist_item(item, fallback_number=index)
        if track:
            tracks.append(track)
    return tracks


def track_from_spotify_playlist_item(
    item: Any, *, fallback_number: int | None = None
) -> TrackSearchItem | None:
    """Safely parse one Spotify Web API playlist item."""
    if not isinstance(item, dict):
        return None
    track = item.get("track")
    if not isinstance(track, dict):
        return None

    title = clean_title(track.get("name"))
    if not title:
        return None

    artists = track.get("artists")
    artist_names = [
        clean
        for artist in artists or []
        if isinstance(artist, dict) and (clean := clean_title(artist.get("name")))
    ]
    album_data = track.get("album")
    album = clean_title(album_data.get("name")) if isinstance(album_data, dict) else None
    external_urls = track.get("external_urls")
    source_url = (
        clean_title(external_urls.get("spotify")) if isinstance(external_urls, dict) else None
    ) or clean_title(track.get("uri"))

    return TrackSearchItem(
        title=title,
        artist=", ".join(artist_names) or None,
        album=album,
        track_number=_int_or_none(track.get("track_number")) or fallback_number,
        source_url=source_url,
    )


def _nested_get(data: Any, *keys: str) -> Any:
    """Read a nested dict path without assuming Spotify's shape is stable."""
    current = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def merge_collection_metadata(
    primary: ExpandedCollection, fallback: ExpandedCollection
) -> ExpandedCollection:
    """Use fallback display metadata when the track-bearing source lacks it."""
    return ExpandedCollection(
        primary.tracks,
        owner=primary.owner or fallback.owner,
        title=primary.title or fallback.title,
    )
