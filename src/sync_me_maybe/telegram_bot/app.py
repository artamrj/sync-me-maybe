from __future__ import annotations

from telegram import BotCommand
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from sync_me_maybe.config import Settings
from sync_me_maybe.telegram_bot.callbacks import handle_callback
from sync_me_maybe.telegram_bot.commands import health, help_command, queue_command, start, user_id
from sync_me_maybe.telegram_bot.handlers import handle_message
from sync_me_maybe.telegram_bot.runtime import BotRuntime


def build_application(settings: Settings) -> Application:
    runtime = BotRuntime(settings)
    application = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
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
