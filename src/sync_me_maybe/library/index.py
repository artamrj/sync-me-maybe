"""Library index: answers "is this song already in the music folder?" quickly.

Tracks are indexed by ISRC, YouTube video ID, and a normalized artist + title
key. Downloads add themselves after they are stored, and a background scan
indexes everything else in ``MUSIC_DIR`` (older downloads, uploads, and files
added by hand) from tags, falling back to ``Artist - Title`` filenames.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from mutagen import File as MutagenFile
from mutagen import MutagenError

from sync_me_maybe.database import Database
from sync_me_maybe.music.matching import normalize, primary_artist

LOGGER = logging.getLogger(__name__)

INDEXED_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".flac", ".aac", ".wav", ".wma", ".aiff"}
_FEAT_BRACKETS = re.compile(r"\s*[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\b[^\)\]]*[\)\]]", re.I)
_FEAT_SUFFIX = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.I)
_REMASTER = re.compile(
    r"\s*(?:[\(\[][^\)\]]*remaster[^\)\]]*[\)\]]|-\s*(?:\d{4}\s+)?remaster(?:ed)?(?:\s+\d{4})?.*$)",
    re.I,
)
_NUMBERED = re.compile(r"^(?:\d+-)?\d+\s*[-.]\s*(.+)$")


def artist_key(artist: str | None) -> str:
    return normalize(primary_artist(artist))


def title_key(title: str | None) -> str:
    """Normalize a title, ignoring featured-artist and remaster suffixes.

    Version words such as "live" or "remix" are kept so different versions of
    a song are not mistaken for duplicates.
    """
    if not title:
        return ""
    value = _FEAT_BRACKETS.sub("", title)
    value = _REMASTER.sub("", value)
    value = _FEAT_SUFFIX.sub("", value)
    return normalize(value) or normalize(title)


def youtube_video_id(url: str | None) -> str | None:
    """Return the video ID of a YouTube watch/short link, if any."""
    if not url or url.startswith("ytsearch"):
        return None
    parsed = urlparse(url)
    host = parsed.netloc.lower().removeprefix("www.").removeprefix("m.")
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        return video_id or None
    if host in {"youtube.com", "music.youtube.com"}:
        if parsed.path.startswith("/shorts/"):
            return parsed.path.split("/")[2] or None
        values = parse_qs(parsed.query).get("v")
        return values[0] if values else None
    return None


@dataclass(frozen=True)
class IndexedTags:
    artist: str | None
    title: str | None
    isrc: str | None


class LibraryIndex:
    """SQLite-backed index of audio files below ``music_dir``."""

    def __init__(self, db: Database, music_dir: Path) -> None:
        self.db = db
        self.music_dir = music_dir

    def lookup(
        self,
        *,
        isrc: str | None = None,
        video_id: str | None = None,
        artist: str | None = None,
        title: str | None = None,
    ) -> str | None:
        """Return the relative path of an existing copy, checking strongest IDs first."""
        queries: list[tuple[str, tuple[str, ...]]] = []
        if isrc:
            queries.append(("isrc = ?", (isrc.upper(),)))
        if video_id:
            queries.append(("video_id = ?", (video_id,)))
        akey, tkey = artist_key(artist), title_key(title)
        if akey and tkey:
            queries.append(("artist_key = ? AND title_key = ?", (akey, tkey)))
        for where, params in queries:
            for row in self.db.fetchall(f"SELECT path FROM tracks WHERE {where}", params):
                path = str(row["path"])
                if (self.music_dir / path).exists():
                    return path
                self.db.execute("DELETE FROM tracks WHERE path = ?", (path,))
        return None

    def add(
        self,
        relative_path: str,
        *,
        isrc: str | None = None,
        video_id: str | None = None,
        artist: str | None = None,
        title: str | None = None,
        source_url: str | None = None,
    ) -> None:
        full = self.music_dir / relative_path
        mtime = full.stat().st_mtime if full.exists() else None
        self.db.execute(
            """
            INSERT INTO tracks
                (path, mtime, isrc, video_id, artist_key, title_key, source_url, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime = excluded.mtime,
                isrc = COALESCE(excluded.isrc, tracks.isrc),
                video_id = COALESCE(excluded.video_id, tracks.video_id),
                artist_key = excluded.artist_key,
                title_key = excluded.title_key,
                source_url = COALESCE(excluded.source_url, tracks.source_url)
            """,
            (
                relative_path,
                mtime,
                isrc.upper() if isrc else None,
                video_id,
                artist_key(artist),
                title_key(title),
                source_url,
                time.time(),
            ),
        )

    def count(self) -> int:
        row = self.db.fetchone("SELECT COUNT(*) AS n FROM tracks")
        return int(row["n"]) if row else 0

    def scan(self) -> int:
        """Index new or changed audio files and forget deleted ones. Blocking."""
        if not self.music_dir.exists():
            return 0
        known = {
            str(row["path"]): row["mtime"]
            for row in self.db.fetchall("SELECT path, mtime FROM tracks")
        }
        seen: set[str] = set()
        batch: list[tuple[object, ...]] = []
        changed = 0
        for path in _audio_files(self.music_dir):
            relative = path.relative_to(self.music_dir).as_posix()
            seen.add(relative)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if relative in known and known[relative] == mtime:
                continue
            tags = read_index_tags(path)
            batch.append(
                (
                    relative,
                    mtime,
                    tags.isrc.upper() if tags.isrc else None,
                    artist_key(tags.artist),
                    title_key(tags.title),
                    time.time(),
                )
            )
            changed += 1
            if len(batch) >= 500:
                self._upsert_scanned(batch)
                batch = []
        if batch:
            self._upsert_scanned(batch)
        stale = [(path,) for path in known if path not in seen]
        if stale:
            self.db.executemany("DELETE FROM tracks WHERE path = ?", stale)
        LOGGER.info(
            "Library index: %s file(s) indexed, %s removed, %s total",
            changed,
            len(stale),
            self.count(),
        )
        return changed

    def _upsert_scanned(self, rows: list[tuple[object, ...]]) -> None:
        self.db.executemany(
            """
            INSERT INTO tracks (path, mtime, isrc, artist_key, title_key, added_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(path) DO UPDATE SET
                mtime = excluded.mtime,
                isrc = COALESCE(excluded.isrc, tracks.isrc),
                artist_key = excluded.artist_key,
                title_key = excluded.title_key
            """,
            rows,
        )


def _audio_files(root: Path) -> Iterator[Path]:
    for directory, subdirs, files in os.walk(root):
        # Skip hidden folders and NAS metadata folders such as Synology's @eaDir.
        subdirs[:] = [d for d in subdirs if not d.startswith((".", "@"))]
        for name in files:
            if name.startswith("."):
                continue
            path = Path(directory) / name
            if path.suffix.lower() in INDEXED_EXTENSIONS:
                yield path


def read_index_tags(path: Path) -> IndexedTags:
    """Read artist/title/ISRC from tags, falling back to the filename."""
    artist = title = isrc = None
    try:
        audio = MutagenFile(path, easy=True)
    except (MutagenError, OSError, ValueError):
        audio = None
    if audio is not None and audio.tags is not None:
        artist = _first(audio.tags.get("artist")) or _first(audio.tags.get("albumartist"))
        title = _first(audio.tags.get("title"))
        isrc = _first(audio.tags.get("isrc"))
        if isrc is None and path.suffix.lower() in {".m4a", ".mp4", ".aac"}:
            isrc = _mp4_isrc(path)
    if not title:
        artist_from_name, title = _parse_filename(path)
        artist = artist or artist_from_name
    return IndexedTags(artist, title, isrc)


def _mp4_isrc(path: Path) -> str | None:
    """EasyMP4 has no ISRC mapping; read the iTunes freeform atom directly."""
    try:
        audio = MutagenFile(path)
    except (MutagenError, OSError, ValueError):
        return None
    values = (audio.tags or {}).get("----:com.apple.iTunes:ISRC") if audio else None
    if not values:
        return None
    raw = values[0]
    text = bytes(raw).decode("utf-8", "ignore").strip()
    return text or None


def _parse_filename(path: Path) -> tuple[str | None, str | None]:
    stem = path.stem
    numbered = _NUMBERED.match(stem)
    if numbered:
        # "01 - Title" inside Artist/Album/ folders.
        parent = path.parent.parent
        return (parent.name if parent != path.parent else None), numbered.group(1)
    if " - " in stem:
        artist, title = stem.split(" - ", 1)
        return artist.strip() or None, title.strip() or None
    return None, stem


def _first(value: object) -> str | None:
    if isinstance(value, list) and value:
        text = str(value[0]).strip()
        return text or None
    return None
