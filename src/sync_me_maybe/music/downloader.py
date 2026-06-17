"""Download resolved tracks with yt-dlp and normalize their metadata."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from sync_me_maybe.library.storage import TrackInfo

from .filenames import sanitize_filename
from .resolver import ResolvedTrack


class DownloadError(RuntimeError):
    """Download failure that tells queue retry logic whether retrying may help."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@dataclass(frozen=True)
class DownloadedTrack:
    """Temporary downloaded file plus metadata for final library storage."""

    temp_file: Path
    info: TrackInfo


class YtDlpDownloader:
    """Async-friendly wrapper around blocking yt-dlp downloads."""

    def __init__(
        self, tmp_dir: Path, cookies_file: Path | None = None, max_seconds: int = 900
    ) -> None:
        self.tmp_dir = tmp_dir
        self.cookies_file = cookies_file
        self.max_seconds = max_seconds

    async def download(
        self, resolved: ResolvedTrack, cancel_check: Callable[[], bool] | None = None
    ) -> DownloadedTrack:
        """Run the blocking yt-dlp workflow in a worker thread."""
        return await asyncio.to_thread(self._download_sync, resolved, cancel_check)

    def _download_sync(
        self, resolved: ResolvedTrack, cancel_check: Callable[[], bool] | None = None
    ) -> DownloadedTrack:
        """Perform the actual download and conversion work synchronously."""
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        output_template = str(self.tmp_dir / f"{run_id}.%(ext)s")
        start = time.monotonic()

        def cleanup_partial_files() -> None:
            """Remove every temp file created for this run ID."""
            for path in self.tmp_dir.glob(f"{run_id}*"):
                path.unlink(missing_ok=True)

        def check_cancel() -> None:
            if cancel_check and cancel_check():
                raise DownloadError("Cancelled by user.", retryable=False)

        def check_timeout() -> None:
            if time.monotonic() - start > self.max_seconds:
                raise DownloadError("Download exceeded MAX_DOWNLOAD_SECONDS.", retryable=True)

        def progress_hook(_: dict[str, Any]) -> None:
            # yt-dlp calls hooks during long transfers. This is where the bot can
            # notice cancellation or timeout instead of waiting for the process
            # to finish naturally.
            check_cancel()
            check_timeout()

        # yt-dlp writes to a unique run ID, then ffmpeg converts the best audio
        # stream to MP3 so downstream storage always sees a predictable format.
        options: dict[str, Any] = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "progress_hooks": [progress_hook],
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
        }
        if self.cookies_file:
            options["cookiefile"] = str(self.cookies_file)

        try:
            check_cancel()
            check_timeout()
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(resolved.download_url, download=True)
            check_cancel()
            check_timeout()
        except DownloadError:
            cleanup_partial_files()
            raise
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many concrete exception types.
            cleanup_partial_files()
            raise DownloadError(f"Download failed: {exc}", retryable=True) from exc

        # Search-based providers can return a result list. The first entry is
        # treated as the best match because providers build a precise query.
        if isinstance(info, dict) and "entries" in info:
            entries = [entry for entry in info.get("entries") or [] if entry]
            if not entries:
                raise DownloadError("No matching YouTube Music result found.", retryable=False)
            info = entries[0]

        temp_file = self.tmp_dir / f"{run_id}.mp3"
        if not temp_file.exists():
            matches = list(self.tmp_dir.glob(f"{run_id}.*"))
            if not matches:
                raise DownloadError(
                    "Download completed but no output file was produced.", retryable=False
                )
            temp_file = matches[0]

        return DownloadedTrack(temp_file=temp_file, info=_track_info(info, resolved))


def _track_info(info: dict[str, Any], resolved: ResolvedTrack) -> TrackInfo:
    """Merge provider metadata with yt-dlp metadata for storage naming."""
    title = resolved.title or _string(info.get("track")) or _string(info.get("title"))
    artist = resolved.artist or _string(info.get("artist")) or _string(info.get("uploader"))
    album = resolved.album or _string(info.get("album"))
    track_number = resolved.track_number or _int(info.get("track_number"))

    if title and not resolved.title:
        title = _strip_youtube_noise(title)

    return TrackInfo(title=title, artist=artist, album=album, track_number=track_number)


def _string(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _strip_youtube_noise(value: str) -> str:
    value = re.sub(
        r"\s*\[[^\]]*(official|lyrics?|audio|video)[^\]]*\]\s*", " ", value, flags=re.IGNORECASE
    )
    value = re.sub(
        r"\s*\([^\)]*(official|lyrics?|audio|video)[^\)]*\)\s*", " ", value, flags=re.IGNORECASE
    )
    return sanitize_filename(value, "Unknown Title")
