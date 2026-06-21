"""Telegram audio upload buffering, queueing, and storage."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application

from sync_me_maybe.library.storage import store_completed_file, upload_destination
from sync_me_maybe.music.downloader import DownloadError
from sync_me_maybe.queueing.queue import JobKind, QueuedJob, RetryJob, UploadPayload
from sync_me_maybe.queueing.retry import (
    RetryDecision,
    next_attempt,
    retry_decision,
    retry_delay_seconds,
    retry_detail,
)
from sync_me_maybe.telegram_bot.requests import (
    job_request,
    mark_request_cancelled,
    render_request_text,
    request_cancelled,
    request_keyboard,
    update_request,
)
from sync_me_maybe.telegram_bot.runtime import (
    BotRuntime,
    BufferedUpload,
    RequestIssueDetail,
    RequestState,
    UploadBatch,
    clone_job,
)
from sync_me_maybe.telegram_bot.safe_api import (
    safe_chat_action,
    safe_edit_message,
    safe_edit_status,
    safe_send_sticker,
)
from sync_me_maybe.ui.messages import (
    RequestView,
    StatusStage,
    render_error,
    render_request,
    render_status,
    render_success,
    status_keyboard,
)

LOGGER = logging.getLogger(__name__)
AUDIO_EXTENSIONS = {
    ".aac",
    ".aiff",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
    ".wma",
}


async def buffer_upload(update: Update, runtime: BotRuntime, application: Application) -> None:
    """Collect an uploaded audio file and enqueue it immediately or after batching."""
    message = update.effective_message
    if message is None:
        return
    telegram_file = message.audio or message.document
    if telegram_file is None:
        await message.reply_text(
            render_error("Could not read uploaded audio."),
            reply_to_message_id=message.message_id,
            allow_sending_without_reply=True,
        )
        return

    # Telegram audio uploads have different metadata depending on client and file
    # type, so choose the best available human-readable filename.
    filename = (
        telegram_file.file_name
        or getattr(telegram_file, "title", None)
        or f"telegram-audio-{telegram_file.file_unique_id}"
    )
    payload = UploadPayload(
        file_id=telegram_file.file_id,
        file_unique_id=telegram_file.file_unique_id,
        filename=filename,
    )
    user_id = message.from_user.id if message.from_user else 0
    if runtime.settings.upload_batch_window_seconds <= 0:
        # A zero window disables grouping and makes each file a separate request.
        await enqueue_upload_request(
            runtime,
            message.chat_id,
            message.message_id,
            user_id,
            [BufferedUpload(message.chat_id, message.message_id, user_id, payload)],
            application,
        )
        return

    # Group only uploads from the same chat and user. This avoids mixing files
    # from different people in a shared chat.
    key = (message.chat_id, user_id)
    batch = runtime.upload_batches.get(key)
    if not batch:
        request_id = uuid4().hex
        status_message = await message.reply_text(
            render_request(
                RequestView(title="Telegram upload", stage=StatusStage.RECEIVED, current=filename)
            ),
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
            stage=StatusStage.RECEIVED,
        )
        await send_received_sticker(runtime, application, message.chat_id, message.message_id)
        runtime.requests[request_id] = request
        batch = UploadBatch(
            key=key,
            request=request,
            uploads=[BufferedUpload(message.chat_id, message.message_id, user_id, payload)],
        )
        runtime.upload_batches[key] = batch
    else:
        # A new file arrived before the window closed, so extend the batch and
        # restart the timer.
        batch.uploads.append(BufferedUpload(message.chat_id, message.message_id, user_id, payload))
        batch.request.title = "Telegram uploads"
        batch.request.total = len(batch.uploads)
        batch.request.current = f"{len(batch.uploads)} file(s) queued"
        if batch.flush_task:
            batch.flush_task.cancel()

    await update_request(runtime, application, batch.request)
    batch.flush_task = asyncio.create_task(
        flush_upload_batch_after_delay(runtime, application, key)
    )


async def flush_upload_batch_after_delay(
    runtime: BotRuntime, application: Application, key: tuple[int, int]
) -> None:
    """Wait for the batch window, then enqueue the collected uploads."""
    try:
        await asyncio.sleep(runtime.settings.upload_batch_window_seconds)
    except asyncio.CancelledError:
        return
    batch = runtime.upload_batches.pop(key, None)
    if not batch:
        return
    await enqueue_upload_batch(runtime, application, batch)


async def enqueue_upload_batch(
    runtime: BotRuntime, application: Application, batch: UploadBatch
) -> None:
    """Turn a buffered upload batch into queue jobs."""
    if batch.request.cancelled:
        return
    batch.request.title = "Telegram upload" if len(batch.uploads) == 1 else "Telegram uploads"
    batch.request.total = len(batch.uploads)
    batch.request.current = (
        f"{len(batch.uploads)} file(s) queued"
        if len(batch.uploads) > 1
        else batch.uploads[0].payload.filename
    )
    for index, buffered in enumerate(batch.uploads, start=1):
        job = upload_job_from_buffered(buffered, batch.request, index, len(batch.uploads))
        await runtime.queue.enqueue(job)
        batch.request.job_ids.append(job.id)
    batch.request.stage = StatusStage.QUEUED
    await update_request(runtime, application, batch.request)


async def enqueue_upload_request(
    runtime: BotRuntime,
    chat_id: int,
    original_message_id: int,
    user_id: int,
    uploads: list[BufferedUpload],
    application: Application,
) -> None:
    """Create and enqueue upload jobs without the delayed batch buffer."""
    first = uploads[0]
    request_id = uuid4().hex
    status_message = await application.bot.send_message(
        chat_id=chat_id,
        text=render_request(
            RequestView(
                title="Telegram upload" if len(uploads) == 1 else "Telegram uploads",
                stage=StatusStage.RECEIVED,
                current=first.payload.filename,
                total=len(uploads),
            )
        ),
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
        stage=StatusStage.RECEIVED,
    )
    await safe_send_sticker(
        application.bot,
        chat_id,
        runtime.settings.received_sticker_id,
        reply_to_message_id=original_message_id,
        allow_sending_without_reply=True,
    )
    runtime.requests[request_id] = request
    for index, buffered in enumerate(uploads, start=1):
        job = upload_job_from_buffered(buffered, request, index, len(uploads))
        await runtime.queue.enqueue(job)
        request.job_ids.append(job.id)
    request.stage = StatusStage.QUEUED
    await safe_edit_status(
        status_message,
        await render_request_text(runtime, request),
        reply_markup=request_keyboard(runtime, request),
    )


def upload_job_from_buffered(
    buffered: BufferedUpload, request: RequestState, index: int, total: int
) -> QueuedJob:
    """Build a queue job from buffered Telegram upload metadata."""
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


async def send_received_sticker(
    runtime: BotRuntime, application: Application, chat_id: int, reply_to_message_id: int
) -> None:
    """Send the optional received sticker configured for instant acknowledgements."""
    await safe_send_sticker(
        application.bot,
        chat_id,
        runtime.settings.received_sticker_id,
        reply_to_message_id=reply_to_message_id,
        allow_sending_without_reply=True,
    )


async def process_upload_job(job: QueuedJob, runtime: BotRuntime, application: Application) -> None:
    """Download one Telegram-uploaded file and store it in the music directory."""
    bot = application.bot
    assert job.upload is not None
    filename = job.upload.filename
    request = job_request(runtime, job)
    if request_cancelled(request):
        return
    if request:
        request.stage = StatusStage.DOWNLOADING
        request.download_started_at = request.download_started_at or datetime.now(UTC)
        request.current = filename
        request.detail = None
        await update_request(runtime, application, request)
    destination = upload_destination(runtime.settings.music_dir, filename)
    if destination.exists():
        # Uploads keep their original filename, so an existing destination means
        # the same file/name has already been synced.
        relative_path = destination.relative_to(runtime.settings.music_dir).as_posix()
        if request:
            request.skipped += 1
            request.paths.append(relative_path)
            request.issue_details.append(
                RequestIssueDetail(status="skipped", label=filename, path=relative_path)
            )
            set_upload_request_stage(request)
            await update_request(runtime, application, request)
        else:
            await safe_edit_message(
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
    # Download to temp storage first. Only after Telegram's file transfer
    # completes do we move the file into the final music library path.
    temp_path = (
        runtime.settings.download_tmp_dir / f"{job.upload.file_unique_id}-{Path(destination).name}"
    )
    try:
        await safe_chat_action(bot, job.chat_id, ChatAction.UPLOAD_DOCUMENT)
        if not request:
            await safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_status(StatusStage.DOWNLOADING, "Telegram upload", filename),
            )
        if request_cancelled(request):
            raise DownloadError("Cancelled by user.")
        file_ref = await bot.get_file(job.upload.file_id)
        await file_ref.download_to_drive(custom_path=temp_path)
        if request_cancelled(request):
            raise DownloadError("Cancelled by user.")
        if request:
            request.stage = StatusStage.SAVING
            await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_status(StatusStage.SAVING, "Telegram upload", filename),
            )
        result = store_completed_file(temp_path, destination, runtime.settings.music_dir)
    except Exception as exc:  # noqa: BLE001 - Telegram file APIs raise several exception types.
        temp_path.unlink(missing_ok=True)
        if await retry_upload_job(job, runtime, application, request, filename, exc):
            return
        LOGGER.exception("Upload handling failed")
        if request:
            if request_cancelled(request):
                await mark_request_cancelled(runtime, application, request)
            else:
                request.stage = StatusStage.FAILED
                request.failed += 1
                request.failed_jobs.append(clone_job(job))
                request.detail = f"Upload failed: {exc}"
                request.issue_details.append(
                    RequestIssueDetail(
                        status="failed",
                        label=filename,
                        reason=f"Upload failed: {exc}",
                    )
                )
                await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot, job.chat_id, job.status_message_id, render_error(f"Upload failed: {exc}")
            )
        return

    if request:
        if result.skipped:
            request.skipped += 1
            request.issue_details.append(
                RequestIssueDetail(status="skipped", label=filename, path=result.relative_path)
            )
        else:
            request.completed += 1
        request.paths.append(result.relative_path)
        request.current = filename
        set_upload_request_stage(request)
        await update_request(runtime, application, request)
    else:
        await safe_edit_message(
            bot,
            job.chat_id,
            job.status_message_id,
            render_success(result.relative_path, skipped=result.skipped),
            reply_markup=status_keyboard(
                relative_path=result.relative_path,
                path_callback_data=runtime.remember_path(result.relative_path),
            ),
        )


def set_upload_request_stage(request: RequestState) -> None:
    """Set the visible upload stage from aggregate counters."""
    done = request.completed + request.skipped + request.failed
    if done < request.total:
        request.stage = StatusStage.QUEUED
        return
    request.download_started_at = None
    if request.failed and not request.completed and not request.skipped:
        request.stage = StatusStage.FAILED
        return
    if request.completed:
        request.stage = StatusStage.DONE
        return
    request.stage = StatusStage.SKIPPED


def audio_document_filename(update: Update) -> str | None:
    """Return a filename when a Telegram document looks like audio."""
    document = update.effective_message.document if update.effective_message else None
    if not document:
        return None
    filename = document.file_name or ""
    mime_type = document.mime_type or ""
    suffix = Path(filename).suffix.lower()
    if mime_type.startswith("audio/") or suffix in AUDIO_EXTENSIONS:
        return filename
    return None


async def retry_upload_job(
    job: QueuedJob,
    runtime: BotRuntime,
    application: Application,
    request: RequestState | None,
    filename: str,
    exc: BaseException,
) -> bool:
    """Update status and raise RetryJob when an upload failure should retry."""
    decision = retry_decision(job, exc)
    if decision != RetryDecision.RETRY:
        return False
    delay = retry_delay_seconds(job)
    if delay is None:
        return False
    detail = retry_detail(job, delay, str(exc))
    retry_job = next_attempt(job)
    if request:
        request.stage = StatusStage.QUEUED
        request.current = filename
        request.detail = detail
        await update_request(runtime, application, request)
    else:
        await safe_edit_message(
            application.bot,
            job.chat_id,
            job.status_message_id,
            render_status(StatusStage.QUEUED, "Telegram upload", detail),
        )
    raise RetryJob(retry_job, str(exc), delay)
