"""Filesystem layout and safe file moves for the local music library."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sync_me_maybe.music.filenames import sanitize_filename


@dataclass(frozen=True)
class TrackInfo:
    """Metadata used to decide where a downloaded track should be stored."""

    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None


@dataclass(frozen=True)
class StoreResult:
    """Result returned after a file is stored or skipped as a duplicate."""

    path: Path
    relative_path: str
    skipped: bool


def track_destination(music_dir: Path, info: TrackInfo, extension: str = ".mp3") -> Path:
    """Build the final music-library path for a resolved/downloaded track."""
    artist = sanitize_filename(info.artist, "Unknown Artist")
    title = sanitize_filename(info.title, "Unknown Title")

    prefix = f"{info.track_number:02d} - " if info.track_number else ""
    filename = f"{prefix}{title}{extension if extension.startswith('.') else f'.{extension}'}"
    # Album-aware paths help Navidrome and similar scanners group tracks into
    # albums. Unknown albums are avoided so they do not create noisy folders.
    if _has_known_album(info.album):
        return music_dir / artist / sanitize_filename(info.album, "Unknown Album") / filename
    return music_dir / artist / filename


def _has_known_album(album: str | None) -> bool:
    if not album:
        return False
    normalized = album.strip().casefold()
    return bool(normalized) and normalized != "unknown album"


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
