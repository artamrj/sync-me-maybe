from __future__ import annotations

import logging

from telegram import Update

from sync_me_maybe.config import ConfigError, Settings
from sync_me_maybe.telegram_bot.app import build_application


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    try:
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
