from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .auth import is_allowed
from .config import ConfigError, Settings
from .downloader import DownloadError, YtDlpDownloader
from .resolver import LinkResolver, ResolveError
from .storage import store_completed_file, track_destination, upload_destination
from .ui import StatusStage, render_error, render_help, render_status, render_success, render_welcome, status_keyboard
from .urls import LinkKind, classify_url, extract_first_url

LOGGER = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}


class BotRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path_callbacks: dict[str, str] = {}
        self.resolver = LinkResolver()
        self.downloader = YtDlpDownloader(
            tmp_dir=settings.download_tmp_dir,
            cookies_file=settings.ytdlp_cookies_file,
            max_seconds=settings.max_download_seconds,
        )

    def allowed(self, update: Update) -> bool:
        return is_allowed(update.effective_user.id if update.effective_user else None, self.settings.allowed_telegram_user_ids)

    def remember_path(self, relative_path: str) -> str:
        token = uuid4().hex[:16]
        self.path_callbacks[token] = relative_path
        return f"path:{token}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    await update.effective_message.reply_text(
        render_welcome(runtime.allowed(update)),
        reply_markup=status_keyboard(include_health=runtime.allowed(update)),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(render_help())


async def user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    await update.effective_message.reply_text(f"Telegram user ID: {user.id if user else 'unknown'}")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    if not runtime.allowed(update):
        await update.effective_message.reply_text("Not authorized.")
        return

    try:
        runtime.settings.music_dir.mkdir(parents=True, exist_ok=True)
        probe = runtime.settings.music_dir / ".sync-me-maybe-health"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        await update.effective_message.reply_text(f"Health check failed: cannot write to music dir: {exc}")
        return

    await update.effective_message.reply_text("Health check ok: music dir is writable.")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    query = update.callback_query
    if not query:
        return

    if not runtime.allowed(update):
        await query.answer("Not authorized.", show_alert=True)
        return

    data = query.data or ""
    if data == "health":
        try:
            runtime.settings.music_dir.mkdir(parents=True, exist_ok=True)
            probe = runtime.settings.music_dir / ".sync-me-maybe-health"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
        except OSError as exc:
            await query.answer(f"Health failed: {exc}", show_alert=True)
            return
        await query.answer("Health ok: music dir is writable.", show_alert=True)
        return

    if data.startswith("path:"):
        token = data.removeprefix("path:")
        await query.answer(runtime.path_callbacks.get(token, "Path is no longer available in memory."), show_alert=True)
        return

    await query.answer()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if not runtime.allowed(update):
        await message.reply_text("Not authorized.")
        return

    if message.audio or _audio_document_filename(update):
        await _handle_upload(update, context, runtime)
        return

    text = message.text or message.caption
    url = extract_first_url(text)
    if not url:
        await message.reply_text("Send an audio file or a single-track YouTube Music, Spotify, Apple Music, or Shazam link.")
        return

    classified = classify_url(url)
    if classified.kind == LinkKind.UNSUPPORTED:
        await message.reply_text(classified.reason or "Unsupported link.")
        return

    await _handle_link(update, context, runtime, classified)


async def _handle_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: BotRuntime) -> None:
    message = update.effective_message
    status_message = await message.reply_text(render_status(StatusStage.QUEUED, "Telegram upload"))
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)

    telegram_file = message.audio or message.document
    if telegram_file is None:
        await status_message.edit_text(render_error("Could not read uploaded audio."))
        return

    filename = telegram_file.file_name or getattr(telegram_file, "title", None) or f"telegram-audio-{telegram_file.file_unique_id}"
    destination = upload_destination(runtime.settings.music_dir, filename)
    if destination.exists():
        relative_path = destination.relative_to(runtime.settings.music_dir).as_posix()
        await status_message.edit_text(
            render_success(relative_path, skipped=True),
            reply_markup=status_keyboard(
                relative_path=relative_path,
                path_callback_data=runtime.remember_path(relative_path),
            ),
        )
        return

    runtime.settings.download_tmp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = runtime.settings.download_tmp_dir / f"{telegram_file.file_unique_id}-{Path(destination).name}"

    try:
        await status_message.edit_text(render_status(StatusStage.DOWNLOADING, "Telegram upload", filename))
        file_ref = await telegram_file.get_file()
        await file_ref.download_to_drive(custom_path=temp_path)
        await status_message.edit_text(render_status(StatusStage.SAVING, "Telegram upload", filename))
        result = store_completed_file(temp_path, destination, runtime.settings.music_dir)
    except Exception as exc:  # noqa: BLE001 - Telegram file APIs raise several exception types.
        temp_path.unlink(missing_ok=True)
        LOGGER.exception("Upload handling failed")
        await status_message.edit_text(render_error(f"Upload failed: {exc}"))
        return

    await status_message.edit_text(
        render_success(result.relative_path, skipped=result.skipped),
        reply_markup=status_keyboard(
            relative_path=result.relative_path,
            path_callback_data=runtime.remember_path(result.relative_path),
        ),
    )


async def _handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE, runtime: BotRuntime, classified) -> None:
    message = update.effective_message
    status_message = await message.reply_text(render_status(StatusStage.QUEUED, classified.kind.value))
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        await status_message.edit_text(render_status(StatusStage.THINKING, classified.kind.value, "Resolving track metadata."))
        resolved = runtime.resolver.resolve(classified)
        await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        await status_message.edit_text(
            render_status(StatusStage.DOWNLOADING, classified.kind.value, resolved.search_query or "Direct YouTube Music link.")
        )
        downloaded = await runtime.downloader.download(resolved)
        destination = track_destination(runtime.settings.music_dir, downloaded.info, ".mp3")
        await status_message.edit_text(render_status(StatusStage.SAVING, classified.kind.value, destination.name))
        result = store_completed_file(downloaded.temp_file, destination, runtime.settings.music_dir)
    except (ResolveError, DownloadError) as exc:
        await status_message.edit_text(render_error(str(exc)), reply_markup=status_keyboard(source_url=classified.url))
        return
    except Exception as exc:  # noqa: BLE001 - keep bot alive and report actionable failure.
        LOGGER.exception("Link handling failed")
        await status_message.edit_text(render_error(f"Download failed: {exc}"), reply_markup=status_keyboard(source_url=classified.url))
        return

    await status_message.edit_text(
        render_success(result.relative_path, skipped=result.skipped),
        reply_markup=status_keyboard(
            source_url=classified.url,
            relative_path=result.relative_path,
            path_callback_data=runtime.remember_path(result.relative_path),
        ),
    )


def _audio_document_filename(update: Update) -> str | None:
    document = update.effective_message.document if update.effective_message else None
    if not document:
        return None
    filename = document.file_name or ""
    mime_type = document.mime_type or ""
    suffix = Path(filename).suffix.lower()
    if mime_type.startswith("audio/") or suffix in AUDIO_EXTENSIONS:
        return filename
    return None


def build_application(settings: Settings) -> Application:
    runtime = BotRuntime(settings)
    application = Application.builder().token(settings.telegram_bot_token).post_init(post_init).build()
    application.bot_data["runtime"] = runtime
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", user_id))
    application.add_handler(CommandHandler("health", health))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    return application


async def post_init(application: Application) -> None:
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open sync-me-maybe"),
            BotCommand("help", "Show supported links and usage"),
            BotCommand("id", "Show your Telegram user ID"),
            BotCommand("health", "Check music folder access"),
        ]
    )


def main() -> None:
    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO))
    settings.music_dir.mkdir(parents=True, exist_ok=True)
    settings.download_tmp_dir.mkdir(parents=True, exist_ok=True)
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
