from __future__ import annotations

from telegram.ext import Application

from sync_me_maybe.queueing.queue import QueuedJob
from sync_me_maybe.telegram_bot.runtime import BotRuntime, RequestState
from sync_me_maybe.telegram_bot.safe_api import safe_edit_message
from sync_me_maybe.ui.messages import RequestView, StatusStage, render_request, status_keyboard


async def request_position(runtime: BotRuntime, request: RequestState) -> int | None:
    positions = [
        position
        for job_id in request.job_ids
        if (position := await runtime.queue.position_of(job_id)) is not None
    ]
    if not positions:
        return None
    return min(positions)


async def render_request_text(runtime: BotRuntime, request: RequestState) -> str:
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
        )
    )


def request_keyboard(runtime: BotRuntime, request: RequestState):
    source_url = request.source_urls[0] if len(request.source_urls) == 1 else None
    relative_path = request.paths[0] if request.total == 1 and request.paths else None
    done = request.completed + request.skipped + request.failed
    is_terminal = (
        request.cancelled
        or request.stage in {StatusStage.DONE, StatusStage.FAILED, StatusStage.CANCELLED}
        or done >= request.total
    )
    return status_keyboard(
        source_url=source_url,
        relative_path=relative_path,
        path_callback_data=runtime.remember_path(relative_path) if relative_path else None,
        refresh_callback_data=None if is_terminal else f"refresh:{request.id}",
        cancel_callback_data=None if is_terminal else f"cancel:{request.id}",
        results_callback_data=runtime.remember_results(request)
        if request.paths and request.total > 1
        else None,
    )


async def update_request(
    runtime: BotRuntime, application: Application, request: RequestState
) -> None:
    if request.cancelled:
        request.stage = StatusStage.CANCELLED
    await safe_edit_message(
        application.bot,
        request.chat_id,
        request.status_message_id,
        await render_request_text(runtime, request),
        reply_markup=request_keyboard(runtime, request),
    )


def job_request(runtime: BotRuntime, job: QueuedJob) -> RequestState | None:
    if not job.request_id:
        return None
    return runtime.requests.get(job.request_id)


def request_cancelled(request: RequestState | None) -> bool:
    return bool(request and request.cancelled)


async def mark_request_cancelled(
    runtime: BotRuntime,
    application: Application,
    request: RequestState,
    detail: str = "Stopped by user.",
) -> None:
    request.cancelled = True
    request.cancel_event.set()
    request.stage = StatusStage.CANCELLED
    request.detail = detail
    await update_request(runtime, application, request)
