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
from .lyrics import LrclibClient, Lyrics
from .matching import (
    STRICT,
    MatchTarget,
    Thresholds,
    YouTubeMatch,
    YouTubeMusicMatcher,
    clean_channel_name,
    primary_artist,
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

    @property
    def isrc(self) -> str | None:
        return (self.canonical.isrc if self.canonical else None) or self.resolved.isrc

    @property
    def video_id(self) -> str | None:
        return self.match.candidate.video_id if self.match else None


@dataclass(frozen=True)
class TrackExtras:
    """Cover art and lyrics, usually fetched while the audio downloads."""

    cover: CoverArt | None = None
    cover_url: str | None = None
    lyrics: Lyrics | None = None
    lyrics_tried: bool = False


@dataclass(frozen=True)
class FinalizedTrack:
    """What was written into the downloaded file."""

    tags: AudioTags
    canonical: CanonicalTrack | None = None
    lyrics: Lyrics | None = None
    tagged: bool = False


@dataclass(frozen=True)
class LyricsQuery:
    title: str
    artist: str
    album: str | None = None
    duration_seconds: int | None = None


class TrackEnricher:
    """Best-effort matching, tagging, cover art, and lyrics around the downloader."""

    def __init__(
        self,
        *,
        lookups_enabled: bool = True,
        lyrics_enabled: bool = True,
        metadata: DeezerMetadata | None = None,
        matcher: YouTubeMusicMatcher | None = None,
        lyrics: LrclibClient | None = None,
        timeout_seconds: int = 15,
    ) -> None:
        self.lookups_enabled = lookups_enabled
        self.lyrics_enabled = lyrics_enabled
        self.metadata = metadata or DeezerMetadata(timeout_seconds)
        self.matcher = matcher or YouTubeMusicMatcher()
        self.lyrics = lyrics or LrclibClient(timeout_seconds)
        self.timeout_seconds = timeout_seconds

    async def prepare(self, resolved: ResolvedTrack) -> PreparedTrack:
        """Swap blind search downloads for a scored YouTube Music match."""
        if not self.lookups_enabled or not is_search(resolved) or not resolved.title:
            return PreparedTrack(resolved)
        target = MatchTarget(resolved.title, resolved.artist, resolved.duration_seconds)
        # Title-only lookups (e.g. Shazam slugs) could attach another artist's tags.
        wants_canonical = bool(resolved.artist or resolved.isrc)
        canonical: CanonicalTrack | None = None
        if wants_canonical and not target.duration_seconds:
            # Matching is far more reliable with a duration, so wait for it.
            canonical = await asyncio.to_thread(self._lookup, target, resolved.isrc, STRICT)
            if canonical:
                target = replace(target, duration_seconds=canonical.duration_seconds)
            match = await asyncio.to_thread(self._match, target)
        elif wants_canonical:
            canonical, match = await asyncio.gather(
                asyncio.to_thread(self._lookup, target, resolved.isrc, STRICT),
                asyncio.to_thread(self._match, target),
            )
        else:
            match = await asyncio.to_thread(self._match, target)
        if match is None:
            LOGGER.info("No confident YouTube Music match for %r; using plain search", target)
            return PreparedTrack(resolved, canonical)
        LOGGER.info("Matched %r to %s (score %.0f)", target.title, match.url, match.score.total)
        return PreparedTrack(replace(resolved, download_url=match.url), canonical, match)

    def prefetch(self, prepared: PreparedTrack) -> asyncio.Task[TrackExtras] | None:
        """Start fetching cover art and lyrics so they overlap with the download."""
        if not self.lookups_enabled:
            return None
        cover_url = _cover_url(prepared.canonical, prepared.match)
        query = self._prefetch_lyrics_query(prepared)
        if not cover_url and not query:
            return None
        return asyncio.create_task(self._extras(cover_url, query))

    async def finalize(
        self,
        downloaded: DownloadedTrack,
        prepared: PreparedTrack,
        prefetched: asyncio.Task[TrackExtras] | None = None,
    ) -> FinalizedTrack:
        """Write tags and cover art into the temp file before it is stored."""
        canonical = prepared.canonical
        info = downloaded.info
        if canonical is None and self.lookups_enabled and info.title:
            target = MatchTarget(info.title, info.artist, downloaded.duration_seconds)
            canonical = await asyncio.to_thread(
                self._lookup, target, prepared.resolved.isrc, POST_DOWNLOAD
            )
        extras = await _settle(prefetched)
        tags = build_tags(downloaded, prepared.resolved, canonical)
        cover = extras.cover
        cover_url = _cover_url(canonical, prepared.match)
        if self.lookups_enabled and cover is None and cover_url and cover_url != extras.cover_url:
            cover = await asyncio.to_thread(self._cover, cover_url)
        lyrics = extras.lyrics
        if lyrics is None and not extras.lyrics_tried and self.lookups_enabled:
            query = _lyrics_query(
                tags.title,
                tags.artists[0] if tags.artists else None,
                tags.album,
                downloaded.duration_seconds,
            )
            if query and self.lyrics_enabled:
                lyrics = await asyncio.to_thread(self._lyrics, query)
        try:
            tagged = await asyncio.to_thread(write_tags, downloaded.temp_file, tags, cover)
        except Exception:  # noqa: BLE001 - untagged audio is still better than no audio.
            LOGGER.warning("Could not tag %s", downloaded.temp_file, exc_info=True)
            tagged = False
        return FinalizedTrack(tags=tags, canonical=canonical, lyrics=lyrics, tagged=tagged)

    def _prefetch_lyrics_query(self, prepared: PreparedTrack) -> LyricsQuery | None:
        if not self.lyrics_enabled:
            return None
        canonical, resolved = prepared.canonical, prepared.resolved
        if canonical:
            return _lyrics_query(
                canonical.title,
                canonical.artists[0] if canonical.artists else None,
                canonical.album,
                canonical.duration_seconds,
            )
        if resolved.artist:
            duration = resolved.duration_seconds or (
                prepared.match.candidate.duration_seconds if prepared.match else None
            )
            return _lyrics_query(
                resolved.title, primary_artist(resolved.artist), resolved.album, duration
            )
        return None

    async def _extras(self, cover_url: str | None, query: LyricsQuery | None) -> TrackExtras:
        cover_job = asyncio.to_thread(self._cover, cover_url) if cover_url else _none()
        lyrics_job = asyncio.to_thread(self._lyrics, query) if query else _none()
        cover, lyrics = await asyncio.gather(cover_job, lyrics_job)
        return TrackExtras(
            cover=cover, cover_url=cover_url, lyrics=lyrics, lyrics_tried=query is not None
        )

    def _lyrics(self, query: LyricsQuery) -> Lyrics | None:
        try:
            return self.lyrics.fetch(query.title, query.artist, query.album, query.duration_seconds)
        except Exception:  # noqa: BLE001 - lyrics are optional.
            LOGGER.warning("Lyrics lookup failed for %r", query, exc_info=True)
            return None

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


async def _none() -> None:
    return None


async def _settle(task: asyncio.Task[TrackExtras] | None) -> TrackExtras:
    if task is None:
        return TrackExtras()
    try:
        return await task
    except Exception:  # noqa: BLE001 - extras are optional.
        LOGGER.warning("Prefetching cover art/lyrics failed", exc_info=True)
        return TrackExtras()


def discard(task: asyncio.Task[TrackExtras] | None) -> None:
    """Cancel a prefetch whose download failed or was skipped."""
    if task is not None and not task.done():
        task.cancel()


def _cover_url(canonical: CanonicalTrack | None, match: YouTubeMatch | None) -> str | None:
    return (canonical.cover_url if canonical else None) or (
        match.candidate.thumbnail_url if match else None
    )


def _lyrics_query(
    title: str | None, artist: str | None, album: str | None, duration: int | None
) -> LyricsQuery | None:
    if not title or not artist:
        return None
    return LyricsQuery(title, artist, album, duration)


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
