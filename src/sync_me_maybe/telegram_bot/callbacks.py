"""Inline keyboard callback handling for status messages."""

from __future__ import annotations

from uuid import uuid4

from telegram import Update
from telegram.ext import ContextTypes

from sync_me_maybe.telegram_bot.commands import guest_invite_url, render_guest_management
from sync_me_maybe.telegram_bot.requests import update_request
from sync_me_maybe.telegram_bot.runtime import BotRuntime, FailedJobRerun, RequestState, clone_job
from sync_me_maybe.telegram_bot.safe_api import safe_send_message
from sync_me_maybe.ui.messages import StatusStage

DETAIL_MESSAGE_LIMIT = 3900


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route button presses from bot status messages."""
    runtime: BotRuntime = context.application.bot_data["runtime"]
    query = update.callback_query
    if not query:
        return

    if not runtime.allowed(update):
        await query.answer("Not authorized.", show_alert=True)
        return

    data = query.data or ""
    user_id = update.effective_user.id if update.effective_user else None
    if data.startswith("guests:"):
        if not runtime.owner(update) or user_id is None:
            await query.answer("Owner access required.", show_alert=True)
            return
        await handle_guest_callback(runtime, context, update, data, user_id)
        return

    if data == "health":
        # Health is exposed as both a command and a button so users can check
        # storage access from the welcome/status keyboard.
        if not runtime.owner(update):
            await query.answer("Owner access required.", show_alert=True)
            return
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
        # Path callbacks intentionally read from memory. If the process restarted,
        # the file may still exist but the short callback token is gone.
        token = data.removeprefix("path:")
        value = runtime.path_callbacks.get(token)
        if value is None:
            await query.answer("Path is no longer available in memory.", show_alert=True)
            return
        if isinstance(value, str):
            text = value
        else:
            if value.owner_user_id is not None and value.owner_user_id != user_id:
                await query.answer("This action belongs to another user.", show_alert=True)
                return
            text = value.text
        await query.answer(text, show_alert=True)
        return

    if data.startswith("issues:"):
        token = data.removeprefix("issues:")
        value = runtime.issue_callbacks.get(token)
        if not value:
            await query.answer("Details are no longer available in memory.", show_alert=True)
            return
        if isinstance(value, str):
            details = value
        else:
            if value.owner_user_id is not None and value.owner_user_id != user_id:
                await query.answer("This action belongs to another user.", show_alert=True)
                return
            details = value.text
        chat = getattr(update, "effective_chat", None)
        chat_id = chat.id if chat else None
        if chat_id is None and update.effective_message:
            chat_id = update.effective_message.chat_id
        if chat_id is None:
            await query.answer("Cannot find chat for details.", show_alert=True)
            return
        for chunk in split_telegram_message(details):
            await safe_send_message(context.application.bot, chat_id, chunk)
        await query.answer("Sent details.")
        return

    if data.startswith("rerun_failed:"):
        token = data.removeprefix("rerun_failed:")
        stored_rerun = runtime.rerun_failed_callbacks.get(token)
        if not stored_rerun:
            await query.answer(
                "Failed retry data is no longer available in memory.", show_alert=True
            )
            return
        if isinstance(stored_rerun, FailedJobRerun):
            rerun = stored_rerun
        else:
            owner_user_id, rerun = stored_rerun
            if owner_user_id is not None and owner_user_id != user_id:
                await query.answer("This action belongs to another user.", show_alert=True)
                return
        await rerun_failed_jobs(runtime, context, update, rerun)
        await query.answer("Queued failed item(s) again.")
        return

    if data.startswith("refresh:"):
        request_id = data.removeprefix("refresh:")
        request = runtime.requests.get(request_id)
        if not request:
            await query.answer("This status is no longer available in memory.", show_alert=True)
            return
        if request.owner_user_id is not None and request.owner_user_id != user_id:
            await query.answer("This action belongs to another user.", show_alert=True)
            return
        await update_request(runtime, context.application, request)
        await query.answer("Refreshed.")
        return

    if data.startswith("cancel:"):
        request_id = data.removeprefix("cancel:")
        request = runtime.requests.get(request_id)
        if not request:
            await query.answer("This request is no longer available in memory.", show_alert=True)
            return
        if request.owner_user_id is not None and request.owner_user_id != user_id:
            await query.answer("This action belongs to another user.", show_alert=True)
            return
        if request.cancelled or request.stage in {
            StatusStage.DONE,
            StatusStage.FAILED,
            StatusStage.CANCELLED,
        }:
            await query.answer("This request is already finished.", show_alert=True)
            return
        # Cancellation has two parts: remove pending queue work and set the
        # request event so active blocking download/upload code can stop itself.
        removed = await runtime.queue.cancel_request(request_id)
        request.cancelled = True
        request.cancel_event.set()
        request.stage = StatusStage.CANCELLED
        request.failed += removed
        request.detail = f"Stopped by user. Cancelled {removed} pending item(s)."
        await update_request(runtime, context.application, request)
        await query.answer("Stopped.")
        return

    await query.answer()


async def handle_guest_callback(
    runtime: BotRuntime,
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    data: str,
    owner_user_id: int,
) -> None:
    """Create, revoke, or invalidate process-local guest access."""
    query = update.callback_query
    assert query is not None
    if data == "guests:create":
        invite = runtime.create_guest_invite(owner_user_id)
        username = getattr(context.application.bot, "username", None)
        if not username:
            runtime.guest_invites.pop(invite.token, None)
            await query.answer("Bot username is unavailable.", show_alert=True)
            return
        url = guest_invite_url(username, invite.token)
        chat = update.effective_chat
        if chat is None:
            runtime.guest_invites.pop(invite.token, None)
            await query.answer("Cannot find this chat.", show_alert=True)
            return
        await context.application.bot.send_message(
            chat_id=chat.id,
            text=(
                f"One-time guest invitation:\n{url}\n\nIt remains valid until used or invalidated."
            ),
        )
        await query.answer("Invite created.")
    elif data.startswith("guests:revoke:"):
        raw_user_id = data.removeprefix("guests:revoke:")
        try:
            guest_user_id = int(raw_user_id)
        except ValueError:
            await query.answer("Invalid guest.", show_alert=True)
            return
        removed_grant = runtime.guest_grants.pop(guest_user_id, None)
        await query.answer("Guest revoked." if removed_grant else "Guest is no longer active.")
    elif data.startswith("guests:invalidate:"):
        token = data.removeprefix("guests:invalidate:")
        removed_invite = runtime.guest_invites.pop(token, None)
        await query.answer(
            "Invite invalidated." if removed_invite else "Invite is no longer active."
        )
    else:
        await query.answer()
        return
    text, keyboard = render_guest_management(runtime)
    await query.edit_message_text(text=text, reply_markup=keyboard)


async def rerun_failed_jobs(
    runtime: BotRuntime,
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    rerun: FailedJobRerun,
) -> None:
    """Create a new request and enqueue clones of previously failed jobs."""
    failed_jobs = rerun.jobs
    first = failed_jobs[0]
    chat = getattr(update, "effective_chat", None)
    chat_id = chat.id if chat else first.chat_id
    message = await context.application.bot.send_message(
        chat_id=chat_id,
        text="Queueing failed item(s) again...",
        reply_to_message_id=first.original_message_id,
        allow_sending_without_reply=True,
    )
    request = RequestState(
        id=uuid4().hex,
        chat_id=chat_id,
        status_message_id=message.message_id,
        title=f"Rerun failed: {rerun.title}",
        total=len(failed_jobs),
        owner_user_id=update.effective_user.id if update.effective_user else None,
        source_urls=[
            job.classified_link.url
            for job in failed_jobs
            if job.classified_link and job.classified_link.url
        ],
    )
    runtime.requests[request.id] = request
    for index, failed_job in enumerate(failed_jobs, start=1):
        job = clone_job(
            failed_job,
            chat_id=chat_id,
            status_message_id=request.status_message_id,
            request_id=request.id,
            request_status_message_id=request.status_message_id,
            request_total=len(failed_jobs),
            request_index=index,
            display_title=failed_job.display_title or failed_job.source_label,
        )
        await runtime.queue.enqueue(job)
        request.job_ids.append(job.id)
    await update_request(runtime, context.application, request)


def split_telegram_message(text: str, limit: int = DETAIL_MESSAGE_LIMIT) -> list[str]:
    """Split text into Telegram-safe chunks, preferring line boundaries."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + (1 if current else 0)
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
            continue
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.extend(line[index : index + limit] for index in range(0, len(line), limit))
            continue
        current.append(line)
        current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks
