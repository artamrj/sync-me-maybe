"""Helpers for rendering and updating aggregate request status messages."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime

from telegram import Message
from telegram.ext import Application

from sync_me_maybe.queueing.queue import QueuedJob
from sync_me_maybe.telegram_bot.runtime import BotRuntime, RequestState
from sync_me_maybe.telegram_bot.safe_api import (
    safe_delete_message,
    safe_edit_message,
    safe_send_message,
)
from sync_me_maybe.ui.messages import RequestView, StatusStage, render_request, status_keyboard


async def request_position(runtime: BotRuntime, request: RequestState) -> int | None:
    """Return the best visible queue position among jobs in one request."""
    positions = [
        position
        for job_id in request.job_ids
        if (position := await runtime.queue.position_of(job_id)) is not None
    ]
    if not positions:
        return None
    return min(positions)


async def render_request_text(runtime: BotRuntime, request: RequestState) -> str:
    """Render the current RequestState into Telegram message text."""
    done = request.completed + request.skipped + request.failed
    elapsed_seconds = None
    if request.download_started_at and done > 0:
        elapsed_seconds = max(
            round((datetime.now(UTC) - request.download_started_at).total_seconds()), 1
        )
    return render_request(
        RequestView(
            title=request.title,
            stage=request.stage,
            total=max(request.total, 1),
            completed=request.completed,
            skipped=request.skipped,
            failed=request.failed,
            current=request.current,
            queue_position=await request_position(runtime, request),
            detail=request.detail,
            paths=request.paths,
            track_title=request.track_title,
            track_artist=request.track_artist,
            collection_title=request.collection_title,
            collection_owner=request.collection_owner,
            source_label=request.source_label,
            elapsed_seconds=elapsed_seconds,
        )
    )


def request_keyboard(runtime: BotRuntime, request: RequestState):
    """Build the action keyboard appropriate for the request's current state."""
    source_url = request.source_urls[0] if len(request.source_urls) == 1 else None
    relative_path = request.paths[0] if request.total == 1 and request.paths else None
    # Finished requests should not show refresh/cancel buttons, but can still
    # expose source URLs and single stored paths.
    is_terminal = request_is_terminal(request)
    return status_keyboard(
        source_url=source_url,
        relative_path=relative_path,
        path_callback_data=(
            runtime.remember_path(relative_path, request.owner_user_id) if relative_path else None
        ),
        issue_callback_data=(
            runtime.remember_issue_details(request)
            if is_terminal and request.issue_details
            else None
        ),
        rerun_failed_callback_data=(
            runtime.remember_failed_jobs(request) if is_terminal and request.failed_jobs else None
        ),
        refresh_callback_data=None if is_terminal else f"refresh:{request.id}",
        cancel_callback_data=None if is_terminal else f"cancel:{request.id}",
    )


def request_is_terminal(request: RequestState) -> bool:
    done = request.completed + request.skipped + request.failed
    return (
        request.cancelled
        or request.stage in {StatusStage.DONE, StatusStage.FAILED, StatusStage.CANCELLED}
        or done >= request.total
    )


async def update_request(
    runtime: BotRuntime, application: Application, request: RequestState, *, force: bool = False
) -> None:
    """Persist a RequestState and edit its Telegram status message.

    Edits are throttled per message: with parallel downloads several tracks can
    finish within a second, so intermediate updates are coalesced into one
    trailing edit. Final states are always sent immediately.
    """
    if request.cancelled:
        request.stage = StatusStage.CANCELLED
    runtime.save_request(request)
    if request.status_message_id <= 0:
        return
    interval = runtime.status_edit_interval
    wait = request.last_edit_at + interval - time.monotonic()
    if force or interval <= 0 or wait <= 0 or request_is_terminal(request):
        if request.pending_edit and not request.pending_edit.done():
            request.pending_edit.cancel()
        request.pending_edit = None
        await _edit_request(runtime, application, request)
        return
    if request.pending_edit is None or request.pending_edit.done():
        request.pending_edit = asyncio.create_task(
            _delayed_edit(runtime, application, request, wait)
        )


async def _delayed_edit(
    runtime: BotRuntime, application: Application, request: RequestState, delay: float
) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    request.pending_edit = None
    await _edit_request(runtime, application, request)


async def _edit_request(
    runtime: BotRuntime, application: Application, request: RequestState
) -> None:
    async with request.edit_lock:
        # Render inside the lock so the last edit always shows the latest state.
        request.last_edit_at = time.monotonic()
        await safe_edit_message(
            application.bot,
            request.chat_id,
            request.status_message_id,
            await render_request_text(runtime, request),
            reply_markup=request_keyboard(runtime, request),
        )


async def send_request_status(
    runtime: BotRuntime,
    application: Application,
    request: RequestState,
    reply_to_message_id: int,
) -> Message | None:
    """Send the first visible status message for a request."""
    status_message = await safe_send_message(
        application.bot,
        request.chat_id,
        await render_request_text(runtime, request),
        reply_to_message_id=reply_to_message_id,
        allow_sending_without_reply=True,
        reply_markup=request_keyboard(runtime, request),
    )
    message_id = getattr(status_message, "message_id", 0)
    if isinstance(message_id, int):
        request.status_message_id = message_id
        request.last_edit_at = time.monotonic()
    runtime.save_request(request)
    return status_message


async def schedule_initial_request_status(
    runtime: BotRuntime,
    application: Application,
    request: RequestState,
    reply_to_message_id: int,
    sticker_message: object | None,
    delay_seconds: float = 3.0,
) -> Message | None:
    """Send the first status immediately or after a temporary sticker delay."""
    if sticker_message is None:
        return await send_request_status(runtime, application, request, reply_to_message_id)

    async def delayed_status() -> None:
        await asyncio.sleep(delay_seconds)
        await send_request_status(runtime, application, request, reply_to_message_id)
        message_id = getattr(sticker_message, "message_id", 0)
        if isinstance(message_id, int):
            await safe_delete_message(application.bot, request.chat_id, message_id)

    asyncio.create_task(delayed_status())
    return None


def job_request(runtime: BotRuntime, job: QueuedJob) -> RequestState | None:
    """Find the aggregate request that owns a queued job."""
    if not job.request_id:
        return None
    return runtime.requests.get(job.request_id)


def request_cancelled(request: RequestState | None) -> bool:
    """Return whether a request exists and has been cancelled."""
    return bool(request and request.cancelled)


async def mark_request_cancelled(
    runtime: BotRuntime,
    application: Application,
    request: RequestState,
    detail: str = "Stopped by user.",
) -> None:
    """Mark a request cancelled and update its Telegram status message."""
    request.cancelled = True
    request.cancel_event.set()
    request.stage = StatusStage.CANCELLED
    request.detail = detail
    await update_request(runtime, application, request)
