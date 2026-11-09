"""Write playlist files that Navidrome imports automatically.

Playlist tracks are stored once in the normal library layout. The playlist
itself is an extended M3U file under ``MUSIC_DIR/Playlists`` whose entries are
paths relative to the playlist file, so the whole music folder can be moved or
mounted elsewhere without breaking it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from sync_me_maybe.library.storage import trustworthy_collection_owner
from sync_me_maybe.music.filenames import sanitize_filename

PLAYLISTS_DIRNAME = "Playlists"


@dataclass
class PlaylistEntry:
    """One stored or already-present track at a playlist position."""

    relative_path: str
    title: str | None = None
    artist: str | None = None
    duration_seconds: int | None = None


@dataclass
class PlaylistState:
    """Playlist name plus the tracks known so far, keyed by playlist position."""

    name: str
    source_url: str | None = None
    entries: dict[int, PlaylistEntry] = field(default_factory=dict)


def playlist_name(title: str | None, owner: str | None, fallback: str = "Playlist") -> str:
    """Return ``Owner - Title`` when the owner is a real display name."""
    title = (title or "").strip() or fallback
    if trustworthy_collection_owner(owner):
        return f"{owner.strip()} - {title}" if owner else title
    return title


def playlist_path(music_dir: Path, name: str) -> Path:
    return music_dir / PLAYLISTS_DIRNAME / f"{sanitize_filename(name, 'Playlist')}.m3u8"


def write_playlist(music_dir: Path, playlist: PlaylistState) -> Path | None:
    """Write (or rewrite) the playlist file in position order. Blocking."""
    if not playlist.entries:
        return None
    path = playlist_path(music_dir, playlist.name)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", f"#PLAYLIST:{playlist.name}"]
    for _, entry in sorted(playlist.entries.items()):
        label = " - ".join(part for part in [entry.artist, entry.title] if part)
        duration = entry.duration_seconds if entry.duration_seconds else -1
        lines.append(f"#EXTINF:{duration},{label or Path(entry.relative_path).stem}")
        relative = os.path.relpath(music_dir / entry.relative_path, path.parent)
        lines.append(Path(relative).as_posix())
    temp = path.with_suffix(".m3u8.tmp")
    temp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp.replace(path)
    return path
