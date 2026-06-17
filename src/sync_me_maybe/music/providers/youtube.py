"""YouTube and YouTube Music provider adapter."""

from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import parse_qs, urlparse

import yt_dlp

from sync_me_maybe.config import Settings
from sync_me_maybe.music.filenames import clean_title
from sync_me_maybe.music.providers.base import (
    ProviderError,
    ResolvedTrack,
    TrackSearchItem,
    unsupported_collection,
)
from sync_me_maybe.music.urls import ClassifiedLink, LinkKind, LinkScope


class YouTubeProvider:
    """Handles direct YouTube downloads and YouTube playlist expansion."""

    kind = LinkKind.YOUTUBE

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings

    def classify(self, url: str) -> ClassifiedLink | None:
        """Recognize YouTube hosts and distinguish playlists from tracks."""
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host not in {"youtu.be", "youtube.com", "m.youtube.com", "music.youtube.com"}:
            return None
        query = parse_qs(parsed.query)
        if "list" in query and "v" not in query:
            return ClassifiedLink(LinkKind.YOUTUBE, url, LinkScope.PLAYLIST)
        return ClassifiedLink(LinkKind.YOUTUBE, url)

    async def resolve_track(self, link: ClassifiedLink) -> ResolvedTrack:
        """Use YouTube links directly because yt-dlp can download them."""
        return ResolvedTrack(source_url=link.url, download_url=link.url)

    async def expand_collection(self, link: ClassifiedLink) -> list[TrackSearchItem]:
        """Read a playlist as metadata-only entries in a worker thread."""
        if link.scope == LinkScope.TRACK:
            raise unsupported_collection()
        return await asyncio.to_thread(self._playlist_sync, link.url)

    def _playlist_sync(self, url: str) -> list[TrackSearchItem]:
        """Extract playlist entries without downloading media."""
        options: dict[str, Any] = {
            "extract_flat": "in_playlist",
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": False,
        }
        if self.settings and self.settings.ytdlp_cookies_file:
            options["cookiefile"] = str(self.settings.ytdlp_cookies_file)

        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp has many concrete error types.
            raise ProviderError(f"Could not read YouTube playlist: {exc}", retryable=True) from exc

        tracks: list[TrackSearchItem] = []
        for index, entry in enumerate((info or {}).get("entries") or [], start=1):
            if not entry:
                continue
            title = clean_title(entry.get("title"))
            if not title:
                continue
            tracks.append(
                TrackSearchItem(
                    title=title,
                    artist=clean_title(entry.get("uploader")),
                    track_number=index,
                    source_url=entry.get("url") or entry.get("webpage_url"),
                )
            )
        return tracks
