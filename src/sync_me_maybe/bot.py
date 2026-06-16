from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar
from uuid import uuid4

from telegram import Bot, BotCommand, Message, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .auth import is_allowed
from .collections import CollectionResolveError, CollectionResolver
from .config import ConfigError, Settings
from .downloader import DownloadError, YtDlpDownloader
from .resolver import ResolvedTrack
from .queue import DownloadQueue, JobKind, QueuedJob, UploadPayload, render_queue_snapshot
from .resolver import LinkResolver, ResolveError
from .storage import store_completed_file, track_destination, upload_destination
from .ui import RequestView, StatusStage, render_collection_progress, render_error, render_help, render_request, render_status, render_success, render_welcome, status_keyboard
from .urls import LinkKind, LinkScope, classify_url, extract_urls

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")

AUDIO_EXTENSIONS = {".aac", ".aiff", ".alac", ".flac", ".m4a", ".mp3", ".ogg", ".opus", ".wav", ".wma"}


@dataclass
class RequestState:
    id: str
    chat_id: int
    status_message_id: int
    title: str
    total: int
    source_urls: list[str] = field(default_factory=list)
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    current: str | None = None
    detail: str | None = None
    stage: StatusStage = StatusStage.QUEUED
    paths: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    cancelled: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class BufferedUpload:
    chat_id: int
    original_message_id: int
    user_id: int
    payload: UploadPayload


@dataclass
class UploadBatch:
    key: tuple[int, int]
    request: RequestState
    uploads: list[BufferedUpload]
    flush_task: asyncio.Task[None] | None = None


class BotRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path_callbacks: dict[str, str] = {}
        self.queue = DownloadQueue()
        self.resolver = LinkResolver()
        self.collection_resolver = CollectionResolver(settings)
        self.batch_progress: dict[int, dict[str, int | str]] = {}
        self.requests: dict[str, RequestState] = {}
        self.upload_batches: dict[tuple[int, int], UploadBatch] = {}
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

    def remember_results(self, request: RequestState) -> str:
        token = uuid4().hex[:16]
        shown = request.paths[:8]
        more = len(request.paths) - len(shown)
        suffix = f"\n...and {more} more" if more > 0 else ""
        self.path_callbacks[token] = "\n".join(shown) + suffix if shown else "No stored paths yet."
        return f"results:{token}"

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


async def _telegram_call(description: str, operation: Callable[[], Awaitable[T]], attempts: int = 3) -> T | None:
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except RetryAfter as exc:
            delay = float(exc.retry_after) + 0.5
            LOGGER.warning("Telegram rate limited %s; retrying in %.1fs", description, delay)
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError) as exc:
            if attempt >= attempts:
                LOGGER.warning("Telegram request failed after %s attempts for %s: %s", attempts, description, exc)
                return None
            delay = min(2**attempt, 8)
            LOGGER.warning("Telegram request timed out for %s; retrying in %ss", description, delay)
            await asyncio.sleep(delay)
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return None
            LOGGER.warning("Telegram rejected %s: %s", description, exc)
            return None
        except TelegramError as exc:
            LOGGER.warning("Telegram request failed for %s: %s", description, exc)
            return None
    return None


async def _safe_edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    **kwargs,
) -> Message | bool | None:
    if message_id <= 0:
        return None
    return await _telegram_call(
        f"edit message {message_id}",
        lambda: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs),
    )


async def _safe_edit_status(message: Message, text: str, **kwargs) -> Message | bool | None:
    return await _telegram_call(f"edit status message {message.message_id}", lambda: message.edit_text(text, **kwargs))


async def _safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> Message | None:
    return await _telegram_call("send status message", lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs))


async def _safe_chat_action(bot: Bot, chat_id: int, action: str) -> None:
    await _telegram_call(f"send chat action {action}", lambda: bot.send_chat_action(chat_id=chat_id, action=action), attempts=2)


async def _request_position(runtime: BotRuntime, request: RequestState) -> int | None:
    positions = [position for job_id in request.job_ids if (position := await runtime.queue.position_of(job_id)) is not None]
    if not positions:
        return None
    return min(positions)


async def _render_request_text(runtime: BotRuntime, request: RequestState) -> str:
    return render_request(
        RequestView(
            title=request.title,
            stage=request.stage,
            total=max(request.total, 1),
            completed=request.completed,
            skipped=request.skipped,
            failed=request.failed,
            current=request.current,
            queue_position=await _request_position(runtime, request),
            detail=request.detail,
            paths=request.paths,
        )
    )


def _request_keyboard(runtime: BotRuntime, request: RequestState):
    source_url = request.source_urls[0] if len(request.source_urls) == 1 else None
    relative_path = request.paths[0] if request.total == 1 and request.paths else None
    done = request.completed + request.skipped + request.failed
    is_terminal = request.cancelled or request.stage in {StatusStage.DONE, StatusStage.FAILED, StatusStage.CANCELLED} or done >= request.total
    return status_keyboard(
        source_url=source_url,
        relative_path=relative_path,
        path_callback_data=runtime.remember_path(relative_path) if relative_path else None,
        refresh_callback_data=None if is_terminal else f"refresh:{request.id}",
        cancel_callback_data=None if is_terminal else f"cancel:{request.id}",
        results_callback_data=runtime.remember_results(request) if request.paths and request.total > 1 else None,
    )


async def _update_request(runtime: BotRuntime, application: Application, request: RequestState) -> None:
    if request.cancelled:
        request.stage = StatusStage.CANCELLED
    await _safe_edit_message(
        application.bot,
        request.chat_id,
        request.status_message_id,
        await _render_request_text(runtime, request),
        reply_markup=_request_keyboard(runtime, request),
    )


def _job_request(runtime: BotRuntime, job: QueuedJob) -> RequestState | None:
    if not job.request_id:
        return None
    return runtime.requests.get(job.request_id)


def _request_cancelled(request: RequestState | None) -> bool:
    return bool(request and request.cancelled)


async def _mark_request_cancelled(runtime: BotRuntime, application: Application, request: RequestState, detail: str = "Stopped by user.") -> None:
    request.cancelled = True
    request.cancel_event.set()
    request.stage = StatusStage.CANCELLED
    request.detail = detail
    await _update_request(runtime, application, request)


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

    if data.startswith("results:"):
        token = data.removeprefix("results:")
        await query.answer(runtime.path_callbacks.get(token, "Results are no longer available in memory."), show_alert=True)
        return

    if data.startswith("refresh:"):
        request_id = data.removeprefix("refresh:")
        request = runtime.requests.get(request_id)
        if not request:
            await query.answer("This status is no longer available in memory.", show_alert=True)
            return
        await _update_request(runtime, context.application, request)
        await query.answer("Refreshed.")
        return

    if data.startswith("cancel:"):
        request_id = data.removeprefix("cancel:")
        request = runtime.requests.get(request_id)
        if not request:
            await query.answer("This request is no longer available in memory.", show_alert=True)
            return
        if request.cancelled or request.stage in {StatusStage.DONE, StatusStage.FAILED, StatusStage.CANCELLED}:
            await query.answer("This request is already finished.", show_alert=True)
            return
        removed = await runtime.queue.cancel_request(request_id)
        request.cancelled = True
        request.cancel_event.set()
        request.stage = StatusStage.CANCELLED
        request.failed += removed
        request.detail = f"Stopped by user. Cancelled {removed} pending item(s)."
        await _update_request(runtime, context.application, request)
        await query.answer("Stopped.")
        return

    await query.answer()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if not runtime.allowed(update):
        await message.reply_text("Not authorized.")
        return

    if message.audio or _audio_document_filename(update):
        await _buffer_upload(update, runtime, context.application)
        return

    text = message.text or message.caption
    urls = extract_urls(text)
    if not urls:
        await message.reply_text("Send an audio file, music link, playlist link, or album link.")
        return

    classified_links = []
    unsupported: list[str] = []
    total = len(urls)
    for index, url in enumerate(urls, start=1):
        classified = classify_url(url)
        if classified.kind == LinkKind.UNSUPPORTED:
            detail = f"Link {index} of {total}: " if total > 1 else ""
            unsupported.append(f"{detail}{classified.reason or 'Unsupported link.'}")
            continue
        classified_links.append((index, classified))

    if not classified_links:
        await message.reply_text(
            render_error("\n".join(unsupported) if unsupported else "Unsupported link."),
            reply_to_message_id=message.message_id,
            allow_sending_without_reply=True,
        )
        return

    if len(classified_links) == 1 and not unsupported:
        index, classified = classified_links[0]
        if classified.scope == LinkScope.TRACK:
            await _enqueue_link(update, runtime, classified, link_index=index, link_total=len(urls))
        else:
            await _enqueue_collection(update, runtime, classified, link_index=index, link_total=len(urls))
        return

    await _enqueue_link_batch(update, runtime, classified_links, unsupported, link_total=len(urls))


async def _buffer_upload(update: Update, runtime: BotRuntime, application: Application) -> None:
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
    payload = UploadPayload(
        file_id=telegram_file.file_id,
        file_unique_id=telegram_file.file_unique_id,
        filename=filename,
    )
    user_id = message.from_user.id if message.from_user else 0
    if runtime.settings.upload_batch_window_seconds <= 0:
        await _enqueue_upload_request(
            runtime,
            message.chat_id,
            message.message_id,
            user_id,
            [BufferedUpload(message.chat_id, message.message_id, user_id, payload)],
            application,
        )
        return

    key = (message.chat_id, user_id)
    batch = runtime.upload_batches.get(key)
    if not batch:
        request_id = uuid4().hex
        status_message = await message.reply_text(
            render_request(RequestView(title="Telegram upload", stage=StatusStage.QUEUED, current=filename)),
            reply_to_message_id=message.message_id,
            allow_sending_without_reply=True,
        )
        request = RequestState(
            id=request_id,
            chat_id=message.chat_id,
            status_message_id=status_message.message_id,
            title="Telegram upload",
            total=1,
            current=filename,
        )
        runtime.requests[request_id] = request
        batch = UploadBatch(
            key=key,
            request=request,
            uploads=[BufferedUpload(message.chat_id, message.message_id, user_id, payload)],
        )
        runtime.upload_batches[key] = batch
    else:
        batch.uploads.append(BufferedUpload(message.chat_id, message.message_id, user_id, payload))
        batch.request.title = "Telegram uploads"
        batch.request.total = len(batch.uploads)
        batch.request.current = f"{len(batch.uploads)} file(s) queued"
        if batch.flush_task:
            batch.flush_task.cancel()

    await _update_request(runtime, application, batch.request)
    batch.flush_task = asyncio.create_task(_flush_upload_batch_after_delay(runtime, application, key))


async def _flush_upload_batch_after_delay(runtime: BotRuntime, application: Application, key: tuple[int, int]) -> None:
    try:
        await asyncio.sleep(runtime.settings.upload_batch_window_seconds)
    except asyncio.CancelledError:
        return
    batch = runtime.upload_batches.pop(key, None)
    if not batch:
        return
    await _enqueue_upload_batch(runtime, application, batch)


async def _enqueue_upload_batch(runtime: BotRuntime, application: Application, batch: UploadBatch) -> None:
    if batch.request.cancelled:
        return
    batch.request.title = "Telegram upload" if len(batch.uploads) == 1 else "Telegram uploads"
    batch.request.total = len(batch.uploads)
    batch.request.current = f"{len(batch.uploads)} file(s) queued" if len(batch.uploads) > 1 else batch.uploads[0].payload.filename
    for index, buffered in enumerate(batch.uploads, start=1):
        job = _upload_job_from_buffered(buffered, batch.request, index, len(batch.uploads))
        await runtime.queue.enqueue(job)
        batch.request.job_ids.append(job.id)
    await _update_request(runtime, application, batch.request)


async def _enqueue_upload_request(
    runtime: BotRuntime,
    chat_id: int,
    original_message_id: int,
    user_id: int,
    uploads: list[BufferedUpload],
    application: Application,
) -> None:
    first = uploads[0]
    request_id = uuid4().hex
    status_message = await application.bot.send_message(
        chat_id=chat_id,
        text=render_request(RequestView(title="Telegram upload" if len(uploads) == 1 else "Telegram uploads", stage=StatusStage.QUEUED, current=first.payload.filename, total=len(uploads))),
        reply_to_message_id=original_message_id,
        allow_sending_without_reply=True,
    )
    request = RequestState(
        id=request_id,
        chat_id=chat_id,
        status_message_id=status_message.message_id,
        title="Telegram upload" if len(uploads) == 1 else "Telegram uploads",
        total=len(uploads),
        current=first.payload.filename,
    )
    runtime.requests[request_id] = request
    for index, buffered in enumerate(uploads, start=1):
        job = _upload_job_from_buffered(buffered, request, index, len(uploads))
        await runtime.queue.enqueue(job)
        request.job_ids.append(job.id)
    await _safe_edit_status(status_message, await _render_request_text(runtime, request), reply_markup=_request_keyboard(runtime, request))


def _upload_job_from_buffered(buffered: BufferedUpload, request: RequestState, index: int, total: int) -> QueuedJob:
    return QueuedJob(
        kind=JobKind.UPLOAD,
        chat_id=buffered.chat_id,
        original_message_id=buffered.original_message_id,
        status_message_id=request.status_message_id,
        user_id=buffered.user_id,
        source_label=buffered.payload.filename,
        request_id=request.id,
        request_status_message_id=request.status_message_id,
        request_total=total,
        request_index=index,
        display_title=f"File {index}/{total}" if total > 1 else buffered.payload.filename,
        upload=buffered.payload,
    )


async def _process_upload_job(job: QueuedJob, runtime: BotRuntime, application: Application) -> None:
    bot = application.bot
    assert job.upload is not None
    filename = job.upload.filename
    request = _job_request(runtime, job)
    if _request_cancelled(request):
        return
    if request:
        request.stage = StatusStage.DOWNLOADING
        request.current = filename
        request.detail = None
        await _update_request(runtime, application, request)
    destination = upload_destination(runtime.settings.music_dir, filename)
    if destination.exists():
        relative_path = destination.relative_to(runtime.settings.music_dir).as_posix()
        if request:
            request.stage = StatusStage.SKIPPED
            request.skipped += 1
            request.paths.append(relative_path)
            await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_success(relative_path, skipped=True),
                reply_markup=status_keyboard(
                    relative_path=relative_path,
                    path_callback_data=runtime.remember_path(relative_path),
                ),
            )
        return

    runtime.settings.download_tmp_dir.mkdir(parents=True, exist_ok=True)
    temp_path = runtime.settings.download_tmp_dir / f"{job.upload.file_unique_id}-{Path(destination).name}"
    try:
        await _safe_chat_action(bot, job.chat_id, ChatAction.UPLOAD_DOCUMENT)
        if not request:
            await _safe_edit_message(bot, job.chat_id, job.status_message_id, render_status(StatusStage.DOWNLOADING, "Telegram upload", filename))
        if _request_cancelled(request):
            raise DownloadError("Cancelled by user.")
        file_ref = await bot.get_file(job.upload.file_id)
        await file_ref.download_to_drive(custom_path=temp_path)
        if _request_cancelled(request):
            raise DownloadError("Cancelled by user.")
        if request:
            request.stage = StatusStage.SAVING
            await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(bot, job.chat_id, job.status_message_id, render_status(StatusStage.SAVING, "Telegram upload", filename))
        result = store_completed_file(temp_path, destination, runtime.settings.music_dir)
    except Exception as exc:  # noqa: BLE001 - Telegram file APIs raise several exception types.
        temp_path.unlink(missing_ok=True)
        LOGGER.exception("Upload handling failed")
        if request:
            if _request_cancelled(request):
                await _mark_request_cancelled(runtime, application, request)
            else:
                request.stage = StatusStage.FAILED
                request.failed += 1
                request.detail = f"Upload failed: {exc}"
                await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(bot, job.chat_id, job.status_message_id, render_error(f"Upload failed: {exc}"))
        return

    if request:
        request.stage = StatusStage.SKIPPED if result.skipped else StatusStage.DONE
        if result.skipped:
            request.skipped += 1
        else:
            request.completed += 1
        request.paths.append(result.relative_path)
        request.current = filename
        await _update_request(runtime, application, request)
    else:
        await _safe_edit_message(
            bot,
            job.chat_id,
            job.status_message_id,
            render_success(result.relative_path, skipped=result.skipped),
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
    request_id = uuid4().hex
    title = classified.kind.value
    status_message = await message.reply_text(
        render_request(RequestView(title=title, stage=StatusStage.QUEUED, current=detail)),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    request = RequestState(
        id=request_id,
        chat_id=message.chat_id,
        status_message_id=status_message.message_id,
        title=title,
        total=1,
        current=detail,
        source_urls=[classified.url],
    )
    runtime.requests[request_id] = request
    source_label = classified.kind.value if link_total == 1 else f"{classified.kind.value} link {link_index}/{link_total}"
    job = QueuedJob(
        kind=JobKind.LINK,
        chat_id=message.chat_id,
        original_message_id=message.message_id,
        status_message_id=status_message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        source_label=source_label,
        classified_link=classified,
        request_id=request_id,
        request_status_message_id=status_message.message_id,
        request_total=1,
        request_index=1,
        display_title=source_label,
    )
    position = await runtime.queue.enqueue(job)
    request.job_ids.append(job.id)
    await _safe_edit_status(status_message, await _render_request_text(runtime, request), reply_markup=_request_keyboard(runtime, request))


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
    request_id = uuid4().hex
    status_message = await message.reply_text(
        render_request(RequestView(title=source, stage=StatusStage.QUEUED, current=detail)),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    request = RequestState(
        id=request_id,
        chat_id=message.chat_id,
        status_message_id=status_message.message_id,
        title=source,
        total=1,
        current=detail,
        source_urls=[classified.url],
    )
    runtime.requests[request_id] = request
    source_label = source if link_total == 1 else f"{source} link {link_index}/{link_total}"
    job = QueuedJob(
        kind=JobKind.COLLECTION,
        chat_id=message.chat_id,
        original_message_id=message.message_id,
        status_message_id=status_message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        source_label=source_label,
        classified_link=classified,
        request_id=request_id,
        request_status_message_id=status_message.message_id,
        request_total=1,
        request_index=1,
        display_title=source_label,
    )
    position = await runtime.queue.enqueue(job)
    request.job_ids.append(job.id)
    await _safe_edit_status(status_message, await _render_request_text(runtime, request), reply_markup=_request_keyboard(runtime, request))


async def _enqueue_link_batch(update: Update, runtime: BotRuntime, classified_links, unsupported: list[str], link_total: int) -> None:
    message = update.effective_message
    request_id = uuid4().hex
    title = f"{len(classified_links)} music link(s)"
    detail = "\n".join(unsupported) if unsupported else None
    status_message = await message.reply_text(
        render_request(RequestView(title=title, stage=StatusStage.QUEUED, total=link_total, failed=len(unsupported), detail=detail)),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    request = RequestState(
        id=request_id,
        chat_id=message.chat_id,
        status_message_id=status_message.message_id,
        title=title,
        total=link_total,
        failed=len(unsupported),
        detail=detail,
        source_urls=[classified.url for _, classified in classified_links],
    )
    runtime.requests[request_id] = request

    for request_index, (link_index, classified) in enumerate(classified_links, start=1):
        source = classified.kind.value if classified.scope == LinkScope.TRACK else f"{classified.kind.value} {classified.scope.value}"
        source_label = f"{source} link {link_index}/{link_total}"
        job = QueuedJob(
            kind=JobKind.LINK if classified.scope == LinkScope.TRACK else JobKind.COLLECTION,
            chat_id=message.chat_id,
            original_message_id=message.message_id,
            status_message_id=status_message.message_id,
            user_id=message.from_user.id if message.from_user else 0,
            source_label=source_label,
            classified_link=classified,
            request_id=request_id,
            request_status_message_id=status_message.message_id,
            request_total=len(classified_links),
            request_index=request_index,
            display_title=source_label,
        )
        await runtime.queue.enqueue(job)
        request.job_ids.append(job.id)

    await _safe_edit_status(status_message, await _render_request_text(runtime, request), reply_markup=_request_keyboard(runtime, request))


async def _process_link_job(job: QueuedJob, runtime: BotRuntime, application: Application) -> None:
    bot = application.bot
    assert job.classified_link is not None
    classified = job.classified_link
    source = f"{classified.kind.value} {classified.scope.value}" if job.batch_total else classified.kind.value
    request = _job_request(runtime, job)
    if _request_cancelled(request):
        return
    try:
        await _safe_chat_action(bot, job.chat_id, ChatAction.TYPING)
        if request:
            request.stage = StatusStage.THINKING
            request.current = job.display_title or job.source_label
            request.detail = "Preparing search."
            await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(bot, job.chat_id, job.status_message_id, render_status(StatusStage.THINKING, source, _job_detail(job, "Preparing search.")))
        resolved = job.resolved_track if isinstance(job.resolved_track, ResolvedTrack) else runtime.resolver.resolve(classified)
        await _safe_chat_action(bot, job.chat_id, ChatAction.UPLOAD_DOCUMENT)
        if request:
            request.stage = StatusStage.DOWNLOADING
            request.current = _job_detail(job, resolved.search_query or "Direct YouTube Music link.")
            request.detail = None
            await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_status(StatusStage.DOWNLOADING, source, _job_detail(job, resolved.search_query or "Direct YouTube Music link.")),
            )
        downloaded = await runtime.downloader.download(resolved, cancel_check=request.cancel_event.is_set if request else None)
        if _request_cancelled(request):
            downloaded.temp_file.unlink(missing_ok=True)
            raise DownloadError("Cancelled by user.")
        destination = track_destination(runtime.settings.music_dir, downloaded.info, ".mp3")
        if request:
            request.stage = StatusStage.SAVING
            request.current = _job_detail(job, destination.name)
            await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(bot, job.chat_id, job.status_message_id, render_status(StatusStage.SAVING, source, _job_detail(job, destination.name)))
        result = store_completed_file(downloaded.temp_file, destination, runtime.settings.music_dir)
    except (ResolveError, DownloadError) as exc:
        await _update_parent_progress(job, runtime, application, "failed")
        if request:
            if _request_cancelled(request) or "Cancelled by user" in str(exc):
                await _mark_request_cancelled(runtime, application, request)
            else:
                request.stage = StatusStage.FAILED
                request.failed += 1
                request.current = job.display_title or job.source_label
                request.detail = str(exc)
                await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_error(str(exc)),
                reply_markup=status_keyboard(source_url=classified.url),
            )
        return
    except Exception as exc:  # noqa: BLE001 - keep bot alive and report actionable failure.
        LOGGER.exception("Link handling failed")
        await _update_parent_progress(job, runtime, application, "failed")
        if request:
            request.stage = StatusStage.FAILED
            request.failed += 1
            request.current = job.display_title or job.source_label
            request.detail = f"Download failed: {exc}"
            await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_error(f"Download failed: {exc}"),
                reply_markup=status_keyboard(source_url=classified.url),
            )
        return

    if request:
        if result.skipped:
            request.skipped += 1
        else:
            request.completed += 1
        request.paths.append(result.relative_path)
        done = request.completed + request.skipped + request.failed
        request.stage = StatusStage.DONE if done >= request.total else StatusStage.QUEUED
        request.current = job.display_title or result.relative_path
        request.detail = None
        await _update_request(runtime, application, request)
    else:
        await _safe_edit_message(
            bot,
            job.chat_id,
            job.status_message_id,
            render_success(result.relative_path, skipped=result.skipped),
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
    request = _job_request(runtime, job)
    if _request_cancelled(request):
        return

    try:
        if request:
            request.stage = StatusStage.EXPANDING
            request.current = job.display_title or source
            request.detail = "Detecting tracks."
            await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(bot, job.chat_id, job.status_message_id, render_collection_progress(source))
        if _request_cancelled(request):
            return
        tracks = await runtime.collection_resolver.expand(classified)
        if _request_cancelled(request):
            return
    except CollectionResolveError as exc:
        if request:
            request.stage = StatusStage.FAILED
            request.failed += 1
            request.current = job.display_title or source
            request.detail = str(exc)
            await _update_request(runtime, application, request)
        else:
            await _safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_error(str(exc)),
                reply_markup=status_keyboard(source_url=classified.url),
            )
        return

    if request:
        request.total += max(len(tracks) - 1, 0)
        request.stage = StatusStage.QUEUED
        request.current = f"{len(tracks)} track(s) detected"
        request.detail = None
        await _update_request(runtime, application, request)

        for index, track in enumerate(tracks, start=1):
            if _request_cancelled(request):
                await _mark_request_cancelled(runtime, application, request)
                break
            detail = f"Track {index}/{len(tracks)}"
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
                status_message_id=request.status_message_id,
                user_id=job.user_id,
                source_label=f"{source} track {index}/{len(tracks)}",
                classified_link=classified,
                resolved_track=resolved,
                request_id=request.id,
                request_status_message_id=request.status_message_id,
                request_total=len(tracks),
                request_index=index,
                display_title=detail,
            )
            await runtime.queue.enqueue(child)
            request.job_ids.append(child.id)
        await _update_request(runtime, application, request)
        return

    runtime.batch_progress[job.status_message_id] = {
        "source": source,
        "total": len(tracks),
        "queued": len(tracks),
        "completed": 0,
        "skipped": 0,
        "failed": 0,
    }
    await _safe_edit_message(
        bot,
        job.chat_id,
        job.status_message_id,
        render_collection_progress(source, total=len(tracks), queued=len(tracks)),
        reply_markup=status_keyboard(source_url=classified.url),
    )

    for index, track in enumerate(tracks, start=1):
        detail = f"Track {index}/{len(tracks)}"
        status_message = await _safe_send_message(
            bot,
            job.chat_id,
            render_status(StatusStage.QUEUED, source, detail),
            reply_to_message_id=job.original_message_id,
            allow_sending_without_reply=True,
        )
        status_message_id = status_message.message_id if status_message else 0
        if status_message_id == 0:
            LOGGER.warning("Queueing collection track %s/%s without a Telegram status message", index, len(tracks))
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
            status_message_id=status_message_id,
            user_id=job.user_id,
            source_label=f"{source} track {index}/{len(tracks)}",
            classified_link=classified,
            resolved_track=resolved,
            parent_status_message_id=job.status_message_id,
            batch_index=index,
            batch_total=len(tracks),
        )
        position = await runtime.queue.enqueue(child)
        if status_message:
            await _safe_edit_status(status_message, render_status(StatusStage.QUEUED, source, detail, position=position))


async def _update_parent_progress(job: QueuedJob, runtime: BotRuntime, application: Application, outcome: str) -> None:
    if not job.parent_status_message_id:
        return
    progress = runtime.batch_progress.get(job.parent_status_message_id)
    if not progress:
        return
    progress[outcome] = int(progress.get(outcome, 0)) + 1
    await _safe_edit_message(
        application.bot,
        job.chat_id,
        job.parent_status_message_id,
        render_collection_progress(
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
    try:
        settings.music_dir.mkdir(parents=True, exist_ok=True)
        settings.download_tmp_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise SystemExit(
            "Cannot write to the configured music/temp directory. "
            "For NAS deployments, set HOST_MUSIC_DIR to the host path, keep MUSIC_DIR=/music, "
            "set PUID/PGID to a Synology user/group that can write to the mounted folder, "
            "and check permissions on HOST_MUSIC_DIR and HOST_TMP_DIR. "
            f"Original error: {exc}"
        ) from exc
    build_application(settings).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
