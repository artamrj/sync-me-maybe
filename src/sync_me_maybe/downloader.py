from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp

from .filenames import sanitize_filename
from .resolver import ResolvedTrack
from .storage import TrackInfo


class DownloadError(RuntimeError):
    pass


@dataclass(frozen=True)
class DownloadedTrack:
    temp_file: Path
    info: TrackInfo


class YtDlpDownloader:
    def __init__(self, tmp_dir: Path, cookies_file: Path | None = None, max_seconds: int = 900) -> None:
        self.tmp_dir = tmp_dir
        self.cookies_file = cookies_file
        self.max_seconds = max_seconds

    async def download(self, resolved: ResolvedTrack) -> DownloadedTrack:
        return await asyncio.to_thread(self._download_sync, resolved)

    def _download_sync(self, resolved: ResolvedTrack) -> DownloadedTrack:
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        run_id = uuid.uuid4().hex
        output_template = str(self.tmp_dir / f"{run_id}.%(ext)s")

        options: dict[str, Any] = {
            "format": "bestaudio/best",
            "noplaylist": True,
            "outtmpl": output_template,
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
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

        start = time.monotonic()
        try:
            with yt_dlp.YoutubeDL(options) as ydl:
                info = ydl.extract_info(resolved.download_url, download=True)
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many concrete exception types.
            raise DownloadError(f"Download failed: {exc}") from exc

        if time.monotonic() - start > self.max_seconds:
            raise DownloadError("Download exceeded MAX_DOWNLOAD_SECONDS.")

        if isinstance(info, dict) and "entries" in info:
            entries = [entry for entry in info.get("entries") or [] if entry]
            if not entries:
                raise DownloadError("No matching YouTube Music result found.")
            info = entries[0]

        temp_file = self.tmp_dir / f"{run_id}.mp3"
        if not temp_file.exists():
            matches = list(self.tmp_dir.glob(f"{run_id}.*"))
            if not matches:
                raise DownloadError("Download completed but no output file was produced.")
            temp_file = matches[0]

        return DownloadedTrack(temp_file=temp_file, info=_track_info(info, resolved))


def _track_info(info: dict[str, Any], resolved: ResolvedTrack) -> TrackInfo:
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
    value = re.sub(r"\s*\[[^\]]*(official|lyrics?|audio|video)[^\]]*\]\s*", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s*\([^\)]*(official|lyrics?|audio|video)[^\)]*\)\s*", " ", value, flags=re.IGNORECASE)
    return sanitize_filename(value, "Unknown Title")
