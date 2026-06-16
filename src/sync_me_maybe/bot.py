from __future__ import annotations

import logging
from pathlib import Path
from uuid import uuid4

from telegram import BotCommand, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .auth import is_allowed
from .collections import CollectionResolveError, CollectionResolver
from .config import ConfigError, Settings
from .downloader import DownloadError, YtDlpDownloader
from .resolver import ResolvedTrack
from .queue import DownloadQueue, JobKind, QueuedJob, UploadPayload, render_queue_snapshot
from .resolver import LinkResolver, ResolveError
from .storage import store_completed_file, track_destination, upload_destination
from .ui import StatusStage, render_collection_progress, render_error, render_help, render_status, render_success, render_welcome, status_keyboard
from .urls import LinkKind, LinkScope, classify_url, extract_urls

LOGGER = logging.getLogger(__name__)

AUDIO_EXTENSIONS = {".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}


class BotRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path_callbacks: dict[str, str] = {}
        self.queue = DownloadQueue()
        self.resolver = LinkResolver()
        self.collection_resolver = CollectionResolver(settings)
        self.batch_progress: dict[int, dict[str, int | str]] = {}
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

    async def process_job(self, job: QueuedJob, application: Application) -> None:
        if job.kind == JobKind.LINK:
            await _process_link_job(job, self, application)
            return
        if job.kind == JobKind.UPLOAD:
            await _process_upload_job(job, self, application)
            return
        if job.kind == JobKind.COLLECTION:
            await _process_collection_job(job, self, application)
            return
        raise RuntimeError(f"Unknown job kind: {job.kind}")


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


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    if not runtime.allowed(update):
        await update.effective_message.reply_text("Not authorized.")
        return

    await update.effective_message.reply_text(render_queue_snapshot(await runtime.queue.snapshot()))


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
        await _enqueue_upload(update, runtime)
        return

    text = message.text or message.caption
    urls = extract_urls(text)
    if not urls:
        await message.reply_text("Send an audio file, music link, playlist link, or album link.")
        return

    total = len(urls)
    for index, url in enumerate(urls, start=1):
        classified = classify_url(url)
        if classified.kind == LinkKind.UNSUPPORTED:
            detail = f"Link {index} of {total}: " if total > 1 else ""
            await message.reply_text(
                f"{detail}{classified.reason or 'Unsupported link.'}",
                reply_to_message_id=message.message_id,
                allow_sending_without_reply=True,
            )
            continue

        if classified.scope == LinkScope.TRACK:
            await _enqueue_link(update, runtime, classified, link_index=index, link_total=total)
        else:
            await _enqueue_collection(update, runtime, classified, link_index=index, link_total=total)


async def _enqueue_upload(update: Update, runtime: BotRuntime) -> None:
    message = update.effective_message
    telegram_file = message.audio or message.document
    if telegram_file is None:
        await message.reply_text(
            render_error("Could not read uploaded audio."),
            reply_to_message_id=message.message_id,
            allow_sending_without_reply=True,
        )
        return

    filename = telegram_file.file_name or getattr(telegram_file, "title", None) or f"telegram-audio-{telegram_file.file_unique_id}"
    status_message = await message.reply_text(
        render_status(StatusStage.QUEUED, "Telegram upload", filename),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    job = QueuedJob(
        kind=JobKind.UPLOAD,
        chat_id=message.chat_id,
        original_message_id=message.message_id,
        status_message_id=status_message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        source_label=filename,
        upload=UploadPayload(
            file_id=telegram_file.file_id,
            file_unique_id=telegram_file.file_unique_id,
            filename=filename,
        ),
    )
    position = await runtime.queue.enqueue(job)
    await status_message.edit_text(render_status(StatusStage.QUEUED, "Telegram upload", filename, position=position))


async def _process_upload_job(job: QueuedJob, runtime: BotRuntime, application: Application) -> None:
    bot = application.bot
    assert job.upload is not None
    filename = job.upload.filename
    destination = upload_destination(runtime.settings.music_dir, filename)
    if destination.exists():
        relative_path = destination.relative_to(runtime.settings.music_dir).as_posix()
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_success(relative_path, skipped=True),
            reply_markup=status_keyboard(
                relative_path=relative_path,
                path_callback_data=runtime.remember_path(relative_path),
            ),
        )
        return

    runtime.settings.download_tmp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = runtime.settings.download_tmp_dir / f"{job.upload.file_unique_id}-{Path(destination).name}"
    try:
        await bot.send_chat_action(chat_id=job.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_status(StatusStage.DOWNLOADING, "Telegram upload", filename),
        )
        file_ref = await bot.get_file(job.upload.file_id)
        await file_ref.download_to_drive(custom_path=temp_path)
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_status(StatusStage.SAVING, "Telegram upload", filename),
        )
        result = store_completed_file(temp_path, destination, runtime.settings.music_dir)
    except Exception as exc:  # noqa: BLE001 - Telegram file APIs raise several exception types.
        temp_path.unlink(missing_ok=True)
        LOGGER.exception("Upload handling failed")
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_error(f"Upload failed: {exc}"),
        )
        return

    await bot.edit_message_text(
        chat_id=job.chat_id,
        message_id=job.status_message_id,
        text=render_success(result.relative_path, skipped=result.skipped),
        reply_markup=status_keyboard(
            relative_path=result.relative_path,
            path_callback_data=runtime.remember_path(result.relative_path),
        ),
    )


async def _enqueue_link(
    update: Update,
    runtime: BotRuntime,
    classified,
    link_index: int = 1,
    link_total: int = 1,
) -> None:
    message = update.effective_message
    detail = f"Link {link_index} of {link_total}" if link_total > 1 else None
    status_message = await message.reply_text(
        render_status(StatusStage.QUEUED, classified.kind.value, detail),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    source_label = classified.kind.value if link_total == 1 else f"{classified.kind.value} link {link_index}/{link_total}"
    job = QueuedJob(
        kind=JobKind.LINK,
        chat_id=message.chat_id,
        original_message_id=message.message_id,
        status_message_id=status_message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        source_label=source_label,
        classified_link=classified,
    )
    position = await runtime.queue.enqueue(job)
    await status_message.edit_text(render_status(StatusStage.QUEUED, classified.kind.value, detail, position=position))


async def _enqueue_collection(
    update: Update,
    runtime: BotRuntime,
    classified,
    link_index: int = 1,
    link_total: int = 1,
) -> None:
    message = update.effective_message
    detail = f"Link {link_index} of {link_total}" if link_total > 1 else None
    source = f"{classified.kind.value} {classified.scope.value}"
    status_message = await message.reply_text(
        render_status(StatusStage.QUEUED, source, detail),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    source_label = source if link_total == 1 else f"{source} link {link_index}/{link_total}"
    job = QueuedJob(
        kind=JobKind.COLLECTION,
        chat_id=message.chat_id,
        original_message_id=message.message_id,
        status_message_id=status_message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        source_label=source_label,
        classified_link=classified,
    )
    position = await runtime.queue.enqueue(job)
    await status_message.edit_text(render_status(StatusStage.QUEUED, source, detail, position=position))


async def _process_link_job(job: QueuedJob, runtime: BotRuntime, application: Application) -> None:
    bot = application.bot
    assert job.classified_link is not None
    classified = job.classified_link
    source = f"{classified.kind.value} {classified.scope.value}" if job.batch_total else classified.kind.value
    try:
        await bot.send_chat_action(chat_id=job.chat_id, action=ChatAction.TYPING)
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_status(StatusStage.THINKING, source, _job_detail(job, "Preparing search.")),
        )
        resolved = job.resolved_track if isinstance(job.resolved_track, ResolvedTrack) else runtime.resolver.resolve(classified)
        await bot.send_chat_action(chat_id=job.chat_id, action=ChatAction.UPLOAD_DOCUMENT)
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_status(StatusStage.DOWNLOADING, source, _job_detail(job, resolved.search_query or "Direct YouTube Music link.")),
        )
        downloaded = await runtime.downloader.download(resolved)
        destination = track_destination(runtime.settings.music_dir, downloaded.info, ".mp3")
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_status(StatusStage.SAVING, source, _job_detail(job, destination.name)),
        )
        result = store_completed_file(downloaded.temp_file, destination, runtime.settings.music_dir)
    except (ResolveError, DownloadError) as exc:
        await _update_parent_progress(job, runtime, application, "failed")
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_error(str(exc)),
            reply_markup=status_keyboard(source_url=classified.url),
        )
        return
    except Exception as exc:  # noqa: BLE001 - keep bot alive and report actionable failure.
        LOGGER.exception("Link handling failed")
        await _update_parent_progress(job, runtime, application, "failed")
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_error(f"Download failed: {exc}"),
            reply_markup=status_keyboard(source_url=classified.url),
        )
        return

    await bot.edit_message_text(
        chat_id=job.chat_id,
        message_id=job.status_message_id,
        text=render_success(result.relative_path, skipped=result.skipped),
        reply_markup=status_keyboard(
            source_url=classified.url,
            relative_path=result.relative_path,
            path_callback_data=runtime.remember_path(result.relative_path),
        ),
    )
    await _update_parent_progress(job, runtime, application, "skipped" if result.skipped else "completed")


async def _process_collection_job(job: QueuedJob, runtime: BotRuntime, application: Application) -> None:
    bot = application.bot
    assert job.classified_link is not None
    classified = job.classified_link
    source = f"{classified.kind.value} {classified.scope.value}"

    try:
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_collection_progress(source),
        )
        tracks = await runtime.collection_resolver.expand(classified)
    except CollectionResolveError as exc:
        await bot.edit_message_text(
            chat_id=job.chat_id,
            message_id=job.status_message_id,
            text=render_error(str(exc)),
            reply_markup=status_keyboard(source_url=classified.url),
        )
        return

    runtime.batch_progress[job.status_message_id] = {
        "source": source,
        "total": len(tracks),
        "queued": len(tracks),
        "completed": 0,
        "skipped": 0,
        "failed": 0,
    }
    await bot.edit_message_text(
        chat_id=job.chat_id,
        message_id=job.status_message_id,
        text=render_collection_progress(source, total=len(tracks), queued=len(tracks)),
        reply_markup=status_keyboard(source_url=classified.url),
    )

    for index, track in enumerate(tracks, start=1):
        detail = f"Track {index}/{len(tracks)}"
        status_message = await bot.send_message(
            chat_id=job.chat_id,
            text=render_status(StatusStage.QUEUED, source, detail),
            reply_to_message_id=job.original_message_id,
            allow_sending_without_reply=True,
        )
        resolved = ResolvedTrack(
            source_url=track.source_url or classified.url,
            download_url=f"ytsearch1:{track.search_query}",
            search_query=track.search_query,
            title=track.title,
            artist=track.artist,
            album=track.album,
            track_number=track.track_number,
        )
        child = QueuedJob(
            kind=JobKind.LINK,
            chat_id=job.chat_id,
            original_message_id=job.original_message_id,
            status_message_id=status_message.message_id,
            user_id=job.user_id,
            source_label=f"{source} track {index}/{len(tracks)}",
            classified_link=classified,
            resolved_track=resolved,
            parent_status_message_id=job.status_message_id,
            batch_index=index,
            batch_total=len(tracks),
        )
        position = await runtime.queue.enqueue(child)
        await status_message.edit_text(render_status(StatusStage.QUEUED, source, detail, position=position))


async def _update_parent_progress(job: QueuedJob, runtime: BotRuntime, application: Application, outcome: str) -> None:
    if not job.parent_status_message_id:
        return
    progress = runtime.batch_progress.get(job.parent_status_message_id)
    if not progress:
        return
    progress[outcome] = int(progress.get(outcome, 0)) + 1
    await application.bot.edit_message_text(
        chat_id=job.chat_id,
        message_id=job.parent_status_message_id,
        text=render_collection_progress(
            str(progress["source"]),
            total=int(progress["total"]),
            queued=int(progress["queued"]),
            completed=int(progress["completed"]),
            skipped=int(progress["skipped"]),
            failed=int(progress["failed"]),
        ),
    )


def _job_detail(job: QueuedJob, detail: str) -> str:
    if job.batch_index and job.batch_total:
        return f"Track {job.batch_index}/{job.batch_total}\n{detail}"
    return detail


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
    application = Application.builder().token(settings.telegram_bot_token).post_init(post_init).post_shutdown(post_shutdown).build()
    application.bot_data["runtime"] = runtime
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", user_id))
    application.add_handler(CommandHandler("health", health))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    return application


async def post_init(application: Application) -> None:
    runtime: BotRuntime = application.bot_data["runtime"]
    runtime.queue.start(lambda job: runtime.process_job(job, application))
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open sync-me-maybe"),
            BotCommand("help", "Show supported links and usage"),
            BotCommand("id", "Show your Telegram user ID"),
            BotCommand("health", "Check music folder access"),
            BotCommand("queue", "Show active and pending downloads"),
        ]
    )


async def post_shutdown(application: Application) -> None:
    runtime: BotRuntime = application.bot_data["runtime"]
    await runtime.queue.stop()


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
