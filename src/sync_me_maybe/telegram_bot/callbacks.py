"""Inline keyboard callback handling for status messages."""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from sync_me_maybe.telegram_bot.requests import update_request
from sync_me_maybe.telegram_bot.runtime import BotRuntime
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
    if data == "health":
        # Health is exposed as both a command and a button so users can check
        # storage access from the welcome/status keyboard.
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
        await query.answer(
            runtime.path_callbacks.get(token, "Path is no longer available in memory."),
            show_alert=True,
        )
        return

    if data.startswith("issues:"):
        token = data.removeprefix("issues:")
        details = runtime.issue_callbacks.get(token)
        if not details:
            await query.answer("Details are no longer available in memory.", show_alert=True)
            return
        message = update.effective_message or query.message
        if not message:
            await query.answer("Cannot find chat for details.", show_alert=True)
            return
        chat_id = message.chat_id
        for chunk in split_telegram_message(details):
            await safe_send_message(context.application.bot, chat_id, chunk)
        await query.answer("Sent details.")
        return

    if data.startswith("refresh:"):
        request_id = data.removeprefix("refresh:")
        request = runtime.requests.get(request_id)
        if not request:
            await query.answer("This status is no longer available in memory.", show_alert=True)
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
