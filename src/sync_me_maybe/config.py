from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


def parse_user_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()

    user_ids: set[int] = set()
    for value in raw.replace(";", ",").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            user_ids.add(int(value))
        except ValueError as exc:
            raise ConfigError(f"Invalid Telegram user ID: {value}") from exc
    return user_ids


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    allowed_telegram_user_ids: set[int]
    music_dir: Path
    download_tmp_dir: Path
    ytdlp_cookies_file: Path | None = None
    max_download_seconds: int = 900
    max_collection_tracks: int = 100
    upload_batch_window_seconds: float = 2.0
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> "Settings":
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ConfigError("TELEGRAM_BOT_TOKEN is required")

        allowed = parse_user_ids(os.environ.get("ALLOWED_TELEGRAM_USER_IDS"))
        if not allowed:
            raise ConfigError("ALLOWED_TELEGRAM_USER_IDS must contain at least one Telegram user ID")

        cookies = os.environ.get("YTDLP_COOKIES_FILE", "").strip()
        max_seconds = os.environ.get("MAX_DOWNLOAD_SECONDS", "900").strip()
        try:
            max_download_seconds = int(max_seconds)
        except ValueError as exc:
            raise ConfigError("MAX_DOWNLOAD_SECONDS must be an integer") from exc
        max_collection_tracks_raw = os.environ.get("MAX_COLLECTION_TRACKS", "100").strip()
        try:
            max_collection_tracks = int(max_collection_tracks_raw)
        except ValueError as exc:
            raise ConfigError("MAX_COLLECTION_TRACKS must be an integer") from exc
        upload_batch_window_raw = os.environ.get("UPLOAD_BATCH_WINDOW_SECONDS", "2").strip()
        try:
            upload_batch_window_seconds = float(upload_batch_window_raw)
        except ValueError as exc:
            raise ConfigError("UPLOAD_BATCH_WINDOW_SECONDS must be a number") from exc
        if upload_batch_window_seconds < 0:
            raise ConfigError("UPLOAD_BATCH_WINDOW_SECONDS must be non-negative")

        music_dir = Path(os.environ.get("MUSIC_DIR", "./music"))

        return cls(
            telegram_bot_token=token,
            allowed_telegram_user_ids=allowed,
            music_dir=music_dir,
            download_tmp_dir=Path(os.environ.get("DOWNLOAD_TMP_DIR", "./tmp/sync-me-maybe")),
            ytdlp_cookies_file=Path(cookies) if cookies else None,
            max_download_seconds=max_download_seconds,
            max_collection_tracks=max_collection_tracks,
            upload_batch_window_seconds=upload_batch_window_seconds,
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        )
