from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from sync_me_maybe.queueing.queue import render_queue_snapshot
from sync_me_maybe.telegram_bot.runtime import BotRuntime
from sync_me_maybe.ui.messages import render_help, render_welcome, status_keyboard


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(
        render_welcome(runtime.allowed(update)),
        reply_markup=status_keyboard(include_health=runtime.allowed(update)),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(render_help())


async def user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    if message is None:
        return
    user = update.effective_user
    await message.reply_text(f"Telegram user ID: {user.id if user else 'unknown'}")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    if not runtime.allowed(update):
        await message.reply_text("Not authorized.")
        return

    try:
        runtime.settings.music_dir.mkdir(parents=True, exist_ok=True)
        probe = runtime.settings.music_dir / ".sync-me-maybe-health"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        await message.reply_text(f"Health check failed: cannot write to music dir: {exc}")
        return

    await message.reply_text("Health check ok: music dir is writable.")


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    if not runtime.allowed(update):
        await message.reply_text("Not authorized.")
        return

    await message.reply_text(render_queue_snapshot(await runtime.queue.snapshot()))
