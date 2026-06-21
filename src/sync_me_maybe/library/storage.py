"""Filesystem layout and safe file moves for the local music library."""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from sync_me_maybe.music.filenames import sanitize_filename


@dataclass(frozen=True)
class TrackInfo:
    """Metadata used to decide where a downloaded track should be stored."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    collection_owner: str | None = None
    collection_title: str | None = None
    collection_url: str | None = None


@dataclass(frozen=True)
class StoreResult:
    """Result returned after a file is stored or skipped as a duplicate."""

    path: Path
    relative_path: str
    skipped: bool


def track_destination(music_dir: Path, info: TrackInfo, extension: str = ".mp3") -> Path:
    """Build the final music-library path for a resolved/downloaded track."""
    artist = sanitize_filename(info.artist, "")
    title = sanitize_filename(info.title, "Unknown Title")
    suffix = extension if extension.startswith(".") else f".{extension}"
    stem = f"{artist} - {title}" if artist else title
    collection_title = sanitize_filename(info.collection_title, "")
    if collection_title:
        collection_owner = (
            sanitize_filename(info.collection_owner, "")
            if trustworthy_collection_owner(info.collection_owner)
            else ""
        )
        if collection_owner:
            folder = f"{collection_owner} - {collection_title}"
        elif info.collection_url:
            folder = sanitize_filename(f"{info.collection_title}({info.collection_url})", "")
        else:
            folder = collection_title
        return music_dir / folder / f"{stem}{suffix}"

    return music_dir / f"{stem}{suffix}"


def trustworthy_collection_owner(value: str | None) -> bool:
    """Return whether provider owner metadata is a display name, not a URL."""
    if not value:
        return False
    value = value.strip()
    if not value:
        return False
    if "://" in value:
        return False
    parsed = urlparse(f"https://{value}")
    host = parsed.netloc.lower()
    if "." in host and parsed.path not in {"", "/"}:
        return False
    return not bool(re.match(r"^(?:www\.)?[\w-]+\.[\w.-]+(?:/|$)", value, re.IGNORECASE))


def upload_destination(music_dir: Path, filename: str | None) -> Path:
    """Build a safe destination path for a Telegram-uploaded audio file."""
    safe_name = sanitize_filename(filename, "telegram-audio")
    return music_dir / safe_name


def store_completed_file(
    source: Path, destination: Path, music_dir: Path, skip_existing: bool = True
) -> StoreResult:
    """Move a completed temp file into the music library.

    Existing destination files are treated as already synced. The temporary
    source is removed in that case so repeated uploads/downloads do not pile up.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    if skip_existing and destination.exists():
        source.unlink(missing_ok=True)
        return StoreResult(destination, _relative(destination, music_dir), True)

    shutil.move(str(source), str(destination))
    return StoreResult(destination, _relative(destination, music_dir), False)


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()
