"""Process entrypoint for running the Telegram music bot."""

from __future__ import annotations

import logging

from telegram import Update

from sync_me_maybe.config import ConfigError, Settings
from sync_me_maybe.telegram_bot.app import build_application


def main() -> None:
    """Load configuration, prepare storage directories, and start polling."""
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    try:
        # Create both directories before Telegram starts so permission problems
        # are reported immediately instead of during the first download.
        settings.music_dir.mkdir(parents=True, exist_ok=True)
        settings.download_tmp_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise SystemExit(
            "Cannot write to the configured music/temp directory. "
            "Check MUSIC_DIR and DOWNLOAD_TMP_DIR permissions, or set them to writable paths. "
            f"Original error: {exc}"
        ) from exc
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
