"""Canonical track metadata from the public Deezer API.

Deezer needs no API key and returns album, album artist, track position,
release date, ISRC, and 1000px cover art. Results are only used when they score
as a confident match against what the provider told us.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

from .matching import (
    STRICT,
    Candidate,
    MatchTarget,
    Thresholds,
    best_candidate,
    primary_artist,
)

LOGGER = logging.getLogger(__name__)

DEEZER_API = "https://api.deezer.com"
MAX_GENRES = 3
USER_AGENT = "sync-me-maybe (+https://github.com/artamrj/sync-me-maybe)"


@dataclass(frozen=True)
class CanonicalTrack:
    """Trusted metadata for tagging a downloaded file."""

    title: str
    artists: tuple[str, ...]
    album: str | None = None
    album_artist: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    release_date: str | None = None
    duration_seconds: int | None = None
    isrc: str | None = None
    genres: tuple[str, ...] = ()
    cover_url: str | None = None
    source: str = "deezer"
    extra: dict[str, Any] = field(default_factory=dict, compare=False)

    @property
    def artist(self) -> str:
        return ", ".join(self.artists)


class DeezerMetadata:
    """Small, blocking Deezer client. Call from a worker thread."""

    def __init__(self, timeout_seconds: int = 15, session: requests.Session | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)

    def lookup(
        self, target: MatchTarget, isrc: str | None = None, thresholds: Thresholds = STRICT
    ) -> CanonicalTrack | None:
        """Find a confident match by ISRC first, then by search."""
        track_id: int | None = None
        if isrc:
            data = self._get(f"/track/isrc:{isrc}")
            if data and data.get("id"):
                track_id = int(data["id"])
        if track_id is None:
            track_id = self._search(target, thresholds)
        if track_id is None:
            return None
        return self.track(track_id)

    def track(self, track_id: int) -> CanonicalTrack | None:
        """Fetch full track details plus album artist and genres."""
        data = self._get(f"/track/{track_id}")
        if not data or not data.get("title"):
            return None
        album = _dict(data.get("album"))
        album_data = self._get(f"/album/{album['id']}") if album.get("id") else None
        return canonical_from_deezer(data, album_data)

    def _search(self, target: MatchTarget, thresholds: Thresholds) -> int | None:
        artist = primary_artist(target.artist)
        queries = []
        if artist:
            queries.append(f'artist:"{artist}" track:"{target.title}"')
        queries.append(" ".join(part for part in [artist, target.title] if part))
        for query in queries:
            data = self._get("/search", params={"q": query, "limit": 10})
            items = data.get("data") if data else None
            candidates = [c for item in items or [] if (c := candidate_from_deezer(item))]
            match = best_candidate(target, candidates, thresholds)
            if match:
                return int(match[0].extra["id"])
        return None

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        response = self.session.get(
            f"{DEEZER_API}{path}", params=params, timeout=self.timeout_seconds
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None
        if "error" in data:
            # Deezer reports "not found" and quota errors with HTTP 200.
            LOGGER.debug("Deezer %s returned %s", path, data["error"])
            return None
        return data


def candidate_from_deezer(item: Any) -> Candidate | None:
    if not isinstance(item, dict) or not item.get("id") or not item.get("title"):
        return None
    artist = _dict(item.get("artist"))
    duration = item.get("duration")
    return Candidate(
        title=str(item["title"]),
        artists=(str(artist["name"]),) if artist.get("name") else (),
        duration_seconds=int(duration) if isinstance(duration, int | float) else None,
        extra={"id": item["id"]},
    )


def canonical_from_deezer(
    track: dict[str, Any], album: dict[str, Any] | None = None
) -> CanonicalTrack:
    """Map Deezer track and album JSON onto ``CanonicalTrack``."""
    contributors = [
        str(c["name"])
        for c in track.get("contributors") or []
        if isinstance(c, dict) and c.get("name")
    ]
    main_artist = _dict(track.get("artist"))
    artists = tuple(dict.fromkeys(contributors or [str(main_artist.get("name") or "")]))
    artists = tuple(a for a in artists if a)
    track_album = _dict(track.get("album"))
    album = album or {}
    album_artist = _dict(album.get("artist"))
    genres = _dict(album.get("genres"))
    return CanonicalTrack(
        title=str(track["title"]),
        artists=artists,
        album=_str(album.get("title")) or _str(track_album.get("title")),
        album_artist=_str(album_artist.get("name")) or (artists[0] if artists else None),
        track_number=_int(track.get("track_position")),
        disc_number=_int(track.get("disk_number")),
        release_date=_str(track.get("release_date")) or _str(album.get("release_date")),
        duration_seconds=_int(track.get("duration")),
        isrc=_str(track.get("isrc")),
        genres=tuple(
            str(g["name"])
            for g in genres.get("data") or []
            if isinstance(g, dict) and g.get("name")
        )[:MAX_GENRES],
        cover_url=_str(album.get("cover_xl")) or _str(track_album.get("cover_xl")),
    )


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None
