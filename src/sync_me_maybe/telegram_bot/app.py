"""Telegram application wiring and lifecycle hooks."""

from __future__ import annotations

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from sync_me_maybe.config import Settings
from sync_me_maybe.telegram_bot.callbacks import handle_callback
from sync_me_maybe.telegram_bot.commands import (
    guests_command,
    health,
    help_command,
    queue_command,
    start,
    user_id,
)
from sync_me_maybe.telegram_bot.handlers import handle_message
from sync_me_maybe.telegram_bot.runtime import BotRuntime


def build_application(settings: Settings) -> Application:
    """Create the python-telegram-bot application and register all handlers."""
    runtime = BotRuntime(settings)
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    # bot_data is the shared place python-telegram-bot exposes to handlers. The
    # runtime object keeps queue, resolver, downloader, and in-memory state.
    application.bot_data["runtime"] = runtime
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("id", user_id))
    application.add_handler(CommandHandler("health", health))
    application.add_handler(CommandHandler("queue", queue_command))
    application.add_handler(CommandHandler("guests", guests_command))
    application.add_handler(CallbackQueryHandler(handle_callback))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    return application


async def post_init(application: Application) -> None:
    """Start background services once Telegram has initialized the app."""
    runtime: BotRuntime = application.bot_data["runtime"]
    runtime.queue.start(lambda job: runtime.process_job(job, application))
    await application.bot.set_my_commands(
        [
            BotCommand("start", "Open sync-me-maybe"),
            BotCommand("help", "Show supported links and usage"),
            BotCommand("id", "Show your Telegram user ID"),
            BotCommand("health", "Check music folder access"),
            BotCommand("queue", "Show active and pending downloads"),
            BotCommand("guests", "Manage temporary guest access"),
        ]
    )


async def post_shutdown(application: Application) -> None:
    """Stop background queue work during graceful shutdown."""
    runtime: BotRuntime = application.bot_data["runtime"]
    await runtime.queue.stop()
