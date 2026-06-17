from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from sync_me_maybe.music.filenames import sanitize_filename


@dataclass(frozen=True)
class TrackInfo:
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None


@dataclass(frozen=True)
class StoreResult:
    path: Path
    relative_path: str
    skipped: bool


def track_destination(music_dir: Path, info: TrackInfo, extension: str = ".mp3") -> Path:
    artist = sanitize_filename(info.artist, "Unknown Artist")
    title = sanitize_filename(info.title, "Unknown Title")

    prefix = f"{info.track_number:02d} - " if info.track_number else ""
    filename = f"{prefix}{title}{extension if extension.startswith('.') else f'.{extension}'}"
    if _has_known_album(info.album):
        return music_dir / artist / sanitize_filename(info.album, "Unknown Album") / filename
    return music_dir / artist / filename


def _has_known_album(album: str | None) -> bool:
    if not album:
        return False
    normalized = album.strip().casefold()
    return bool(normalized) and normalized != "unknown album"


def upload_destination(music_dir: Path, filename: str | None) -> Path:
    safe_name = sanitize_filename(filename, "telegram-audio")
    return music_dir / safe_name


def store_completed_file(
    source: Path, destination: Path, music_dir: Path, skip_existing: bool = True
) -> StoreResult:
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
