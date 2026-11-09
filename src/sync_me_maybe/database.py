"""Small SQLite wrapper shared by the library index and the bot state store.

One connection is shared between the event loop and worker threads, so every
statement runs under a lock. Queries are tiny, which keeps that cheap.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    path TEXT PRIMARY KEY,
    mtime REAL,
    isrc TEXT,
    video_id TEXT,
    artist_key TEXT,
    title_key TEXT,
    source_url TEXT,
    added_at REAL
);
CREATE INDEX IF NOT EXISTS tracks_isrc ON tracks(isrc);
CREATE INDEX IF NOT EXISTS tracks_video ON tracks(video_id);
CREATE INDEX IF NOT EXISTS tracks_key ON tracks(artist_key, title_key);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    request_id TEXT,
    not_before REAL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY,
    updated_at REAL NOT NULL,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS callbacks (
    token TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    owner_user_id INTEGER,
    updated_at REAL NOT NULL,
    data TEXT NOT NULL
);
"""


class Database:
    """Thread-safe SQLite access. ``path=None`` keeps everything in memory."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(path) if path else ":memory:", check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            if path is not None:
                self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self._lock:
            self._conn.execute(sql, params)

    def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                self._conn.executemany(sql, rows)
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            row: sqlite3.Row | None = self._conn.execute(sql, params).fetchone()
            return row

    def close(self) -> None:
        with self._lock:
            self._conn.close()
