"""Telegram application wiring and lifecycle hooks."""

from __future__ import annotations

import asyncio
import logging

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from sync_me_maybe.config import Settings
from sync_me_maybe.queueing.queue import QueuedJob
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
from sync_me_maybe.telegram_bot.requests import update_request
from sync_me_maybe.telegram_bot.runtime import BotRuntime

LOGGER = logging.getLogger(__name__)


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
    """Restore saved state and start background services once Telegram is ready."""
    runtime: BotRuntime = application.bot_data["runtime"]
    restored_jobs = runtime.restore_state()
    await runtime.queue.restore(restored_jobs)
    runtime.queue.start(lambda job: runtime.process_job(job, application))
    runtime.background_tasks.add(asyncio.create_task(scan_library(runtime)))
    if restored_jobs:
        LOGGER.info("Resumed %s queued job(s) after restart", len(restored_jobs))
        await refresh_resumed_requests(runtime, application, restored_jobs)
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
    """Stop background work; pending jobs stay saved for the next start."""
    runtime: BotRuntime = application.bot_data["runtime"]
    await runtime.queue.stop()
    for task in runtime.background_tasks:
        task.cancel()
    if runtime.navidrome is not None:
        await runtime.navidrome.flush()
    runtime.db.close()


async def scan_library(runtime: BotRuntime) -> None:
    """Index the existing music folder so duplicates are skipped before downloading."""
    try:
        await asyncio.to_thread(runtime.library.scan)
    except Exception:  # noqa: BLE001 - the bot works without a complete index.
        LOGGER.exception("Library scan failed")


async def refresh_resumed_requests(
    runtime: BotRuntime, application: Application, jobs: list[tuple[QueuedJob, float | None]]
) -> None:
    """Tell users that unfinished requests continue after a restart."""
    request_ids = {job.request_id for job, _ in jobs if job.request_id}
    for request_id in request_ids:
        request = runtime.requests.get(request_id)
        if request is None or request.cancelled:
            continue
        request.detail = "Resumed after restart."
        await update_request(runtime, application, request, force=True)
