"""Synced lyrics from LRCLIB, saved as sidecar files next to each track.

Navidrome reads ``.lrc`` (synced) and ``.txt`` (plain) files that share the
audio file's name, so no tag rewriting is needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .metadata import USER_AGENT

LOGGER = logging.getLogger(__name__)

LRCLIB_GET = "https://lrclib.net/api/get"


@dataclass(frozen=True)
class Lyrics:
    synced: str | None = None
    plain: str | None = None
    instrumental: bool = False


class LrclibClient:
    """Blocking LRCLIB client; call from a worker thread."""

    def __init__(self, timeout_seconds: int = 15, session: requests.Session | None = None) -> None:
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", USER_AGENT)

    def fetch(
        self,
        title: str,
        artist: str,
        album: str | None = None,
        duration_seconds: int | None = None,
    ) -> Lyrics | None:
        params: dict[str, Any] = {"track_name": title, "artist_name": artist}
        if album:
            params["album_name"] = album
        if duration_seconds:
            params["duration"] = duration_seconds
        response = self.session.get(LRCLIB_GET, params=params, timeout=self.timeout_seconds)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            return None
        lyrics = Lyrics(
            synced=_text(data.get("syncedLyrics")),
            plain=_text(data.get("plainLyrics")),
            instrumental=bool(data.get("instrumental")),
        )
        if not (lyrics.synced or lyrics.plain):
            return None
        return lyrics


def write_lyrics_sidecar(audio_path: Path, lyrics: Lyrics | None) -> Path | None:
    """Write ``<track>.lrc`` (synced) or ``<track>.txt`` (plain). Blocking."""
    if lyrics is None or lyrics.instrumental:
        return None
    if lyrics.synced:
        path, text = audio_path.with_suffix(".lrc"), lyrics.synced
    elif lyrics.plain:
        path, text = audio_path.with_suffix(".txt"), lyrics.plain
    else:
        return None
    if path.exists():
        return None
    path.write_text(text.rstrip() + "\n", encoding="utf-8")
    return path


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None
