"""Pick the right YouTube audio and tag the downloaded file.

``TrackEnricher`` wraps the download step of a link job:

1. ``prepare`` runs before download. For search-based tracks it looks up
   canonical metadata (Deezer) and replaces the blind ``ytsearch1:`` query
   with the best-scoring YouTube Music result.
2. ``finalize`` runs after download. It writes tags and cover art so library
   scanners such as Navidrome group the file correctly.

Every network step is best-effort: failures are logged and the job continues
with the previous behavior instead of failing the download.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

import requests

from .downloader import DownloadedTrack
from .matching import (
    STRICT,
    MatchTarget,
    Thresholds,
    YouTubeMatch,
    YouTubeMusicMatcher,
    clean_channel_name,
)
from .metadata import USER_AGENT, CanonicalTrack, DeezerMetadata
from .resolver import ResolvedTrack
from .tagging import AudioTags, CoverArt, cover_from_bytes, write_tags

LOGGER = logging.getLogger(__name__)

MAX_COVER_BYTES = 10 * 1024 * 1024
# Post-download lookups have no provider metadata to lean on, so they must also
# agree on duration before their tags are trusted.
POST_DOWNLOAD = replace(STRICT, require_duration=True, max_duration_diff=5)


@dataclass(frozen=True)
class PreparedTrack:
    """A resolved track plus what enrichment learned before downloading."""

    resolved: ResolvedTrack
    canonical: CanonicalTrack | None = None
    match: YouTubeMatch | None = None


class TrackEnricher:
    """Best-effort matching and tagging around ``YtDlpDownloader``."""

    def __init__(
        self,
        *,
        lookups_enabled: bool = True,
        metadata: DeezerMetadata | None = None,
        matcher: YouTubeMusicMatcher | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        self.lookups_enabled = lookups_enabled
        self.metadata = metadata or DeezerMetadata(timeout_seconds)
        self.matcher = matcher or YouTubeMusicMatcher()
        self.timeout_seconds = timeout_seconds

    async def prepare(self, resolved: ResolvedTrack) -> PreparedTrack:
        """Swap blind search downloads for a scored YouTube Music match."""
        if not self.lookups_enabled or not is_search(resolved) or not resolved.title:
            return PreparedTrack(resolved)
        target = MatchTarget(resolved.title, resolved.artist, resolved.duration_seconds)
        # Title-only lookups (e.g. Shazam slugs) could attach another artist's tags.
        canonical = (
            await asyncio.to_thread(self._lookup, target, resolved.isrc, STRICT)
            if resolved.artist or resolved.isrc
            else None
        )
        if canonical and not target.duration_seconds:
            target = replace(target, duration_seconds=canonical.duration_seconds)
        match = await asyncio.to_thread(self._match, target)
        if match is None:
            LOGGER.info("No confident YouTube Music match for %r; using plain search", target)
            return PreparedTrack(resolved, canonical)
        LOGGER.info("Matched %r to %s (score %.0f)", target.title, match.url, match.score.total)
        return PreparedTrack(replace(resolved, download_url=match.url), canonical, match)

    async def finalize(self, downloaded: DownloadedTrack, prepared: PreparedTrack) -> bool:
        """Write tags and cover art into the temp file before it is stored."""
        canonical = prepared.canonical
        info = downloaded.info
        if canonical is None and self.lookups_enabled and info.title:
            target = MatchTarget(info.title, info.artist, downloaded.duration_seconds)
            canonical = await asyncio.to_thread(
                self._lookup, target, prepared.resolved.isrc, POST_DOWNLOAD
            )
        tags = build_tags(downloaded, prepared.resolved, canonical)
        cover: CoverArt | None = None
        cover_url = (canonical.cover_url if canonical else None) or (
            prepared.match.candidate.thumbnail_url if prepared.match else None
        )
        if cover_url and self.lookups_enabled:
            cover = await asyncio.to_thread(self._cover, cover_url)
        try:
            return await asyncio.to_thread(write_tags, downloaded.temp_file, tags, cover)
        except Exception:  # noqa: BLE001 - untagged audio is still better than no audio.
            LOGGER.warning("Could not tag %s", downloaded.temp_file, exc_info=True)
            return False

    def _lookup(
        self, target: MatchTarget, isrc: str | None, thresholds: Thresholds
    ) -> CanonicalTrack | None:
        try:
            return self.metadata.lookup(target, isrc, thresholds)
        except Exception:  # noqa: BLE001 - metadata is optional.
            LOGGER.warning("Metadata lookup failed for %r", target, exc_info=True)
            return None

    def _match(self, target: MatchTarget) -> YouTubeMatch | None:
        try:
            return self.matcher.find(target)
        except Exception:  # noqa: BLE001 - fall back to plain YouTube search.
            LOGGER.warning("YouTube Music search failed for %r", target, exc_info=True)
            return None

    def _cover(self, url: str) -> CoverArt | None:
        try:
            response = requests.get(
                url, timeout=self.timeout_seconds, headers={"User-Agent": USER_AGENT}
            )
            response.raise_for_status()
            if len(response.content) > MAX_COVER_BYTES:
                return None
            return cover_from_bytes(response.content)
        except Exception:  # noqa: BLE001 - cover art is optional.
            LOGGER.warning("Could not fetch cover art %s", url, exc_info=True)
            return None


def is_search(resolved: ResolvedTrack) -> bool:
    return resolved.download_url.startswith("ytsearch")


def build_tags(
    downloaded: DownloadedTrack, resolved: ResolvedTrack, canonical: CanonicalTrack | None
) -> AudioTags:
    """Merge trusted canonical metadata over provider/yt-dlp metadata."""
    info = downloaded.info
    if canonical:
        return AudioTags(
            title=canonical.title,
            artists=canonical.artists or ((info.artist,) if info.artist else ()),
            album=canonical.album or info.album,
            album_artist=canonical.album_artist,
            track_number=canonical.track_number,
            disc_number=canonical.disc_number,
            date=_valid_date(canonical.release_date),
            isrc=canonical.isrc or resolved.isrc,
            genres=canonical.genres,
            source_url=resolved.source_url,
        )
    artist = clean_channel_name(info.artist)
    return AudioTags(
        title=info.title,
        artists=(artist,) if artist else (),
        album=info.album,
        album_artist=artist,
        isrc=resolved.isrc,
        source_url=resolved.source_url,
    )


def _valid_date(value: str | None) -> str | None:
    if not value or value.startswith("0000"):
        return None
    return value
