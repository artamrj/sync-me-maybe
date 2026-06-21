"""Retrying wrappers around Telegram API calls used by status updates."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TypeVar

from telegram import Bot, Message
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError, TimedOut

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


async def telegram_call(
    description: str, operation: Callable[[], Awaitable[T]], attempts: int = 3
) -> T | None:
    """Run one Telegram API call with small retries for transient failures."""
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except RetryAfter as exc:
            # Telegram tells us exactly how long to wait when rate limited.
            retry_after = exc.retry_after
            delay = (
                retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after
            ) + 0.5
            LOGGER.warning("Telegram rate limited %s; retrying in %.1fs", description, delay)
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError) as exc:
            # Network hiccups are common for long-running bots. Retry briefly,
            # then drop the status update instead of failing the whole job.
            if attempt >= attempts:
                LOGGER.warning(
                    "Telegram request failed after %s attempts for %s: %s",
                    attempts,
                    description,
                    exc,
                )
                return None
            delay = min(2**attempt, 8)
            LOGGER.warning("Telegram request timed out for %s; retrying in %ss", description, delay)
            await asyncio.sleep(delay)
        except BadRequest as exc:
            # Editing a message to identical text is harmless and should not
            # appear as a user-visible failure.
            if "message is not modified" in str(exc).lower():
                return None
            LOGGER.warning("Telegram rejected %s: %s", description, exc)
            return None
        except TelegramError as exc:
            LOGGER.warning("Telegram request failed for %s: %s", description, exc)
            return None
    return None


async def safe_edit_message(
    bot: Bot,
    chat_id: int,
    message_id: int,
    text: str,
    **kwargs,
) -> Message | bool | None:
    """Safely edit a message by chat/message ID."""
    if message_id <= 0:
        return None
    return await telegram_call(
        f"edit message {message_id}",
        lambda: bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text, **kwargs),
    )


async def safe_edit_status(message: Message, text: str, **kwargs) -> Message | bool | None:
    """Safely edit a message object returned by Telegram."""
    return await telegram_call(
        f"edit status message {message.message_id}", lambda: message.edit_text(text, **kwargs)
    )


async def safe_send_message(bot: Bot, chat_id: int, text: str, **kwargs) -> Message | None:
    """Safely send a new status message."""
    return await telegram_call(
        "send status message", lambda: bot.send_message(chat_id=chat_id, text=text, **kwargs)
    )


async def safe_send_sticker(
    bot: Bot, chat_id: int, sticker: str | None, **kwargs
) -> Message | None:
    """Safely send an optional sticker without making it critical to a job."""
    if not sticker:
        return None
    return await telegram_call(
        "send received sticker",
        lambda: bot.send_sticker(chat_id=chat_id, sticker=sticker, **kwargs),
    )


async def safe_chat_action(bot: Bot, chat_id: int, action: str) -> None:
    """Send typing/upload indicators without making them critical to the job."""
    await telegram_call(
        f"send chat action {action}",
        lambda: bot.send_chat_action(chat_id=chat_id, action=action),
        attempts=2,
    )
