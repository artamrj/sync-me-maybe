"""Telegram slash command handlers."""

from __future__ import annotations

from telegram import Update
from telegram.constants import ChatType
from telegram.ext import ContextTypes
from telegram.helpers import create_deep_linked_url

from sync_me_maybe.queueing.queue import render_queue_snapshot
from sync_me_maybe.telegram_bot.runtime import BotRuntime
from sync_me_maybe.ui.messages import render_help, render_welcome, status_keyboard


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the welcome message and whether this Telegram user is authorized."""
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    args = getattr(context, "args", None) or []
    if args and args[0].startswith("invite_"):
        chat = update.effective_chat
        if chat is None or chat.type != ChatType.PRIVATE:
            await message.reply_text("Guest invitations can only be accepted in a private chat.")
            return
        user = update.effective_user
        if user is None:
            await message.reply_text("Could not identify your Telegram account.")
            return
        if runtime.owner(update):
            await message.reply_text("You already have permanent owner access.")
            return
        token = args[0].removeprefix("invite_")
        grant = runtime.redeem_guest_invite(token, user.id, user.full_name)
        if grant is None:
            await message.reply_text("This guest invitation is invalid or no longer available.")
            return
        await message.reply_text(
            "Guest access granted. Send a supported music link or audio file in this chat."
        )
        return
    await message.reply_text(
        render_welcome(runtime.allowed(update)),
        reply_markup=status_keyboard(include_health=runtime.owner(update)),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show supported inputs and the basic workflow."""
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(render_help())


async def user_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reply with the Telegram user ID used for allowlist configuration."""
    message = update.effective_message
    if message is None:
        return
    user = update.effective_user
    await message.reply_text(f"Telegram user ID: {user.id if user else 'unknown'}")


async def health(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Verify that the configured music directory is writable."""
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    if not runtime.owner(update):
        await message.reply_text("Not authorized.")
        return

    try:
        # The probe creates and deletes a small file, which checks real write
        # permission rather than only whether the directory path exists.
        runtime.settings.music_dir.mkdir(parents=True, exist_ok=True)
        probe = runtime.settings.music_dir / ".sync-me-maybe-health"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        await message.reply_text(f"Health check failed: cannot write to music dir: {exc}")
        return

    await message.reply_text("Health check ok: music dir is writable.")


async def queue_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the active queue item and a short pending list."""
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    if not runtime.owner(update):
        await message.reply_text("Not authorized.")
        return

    await message.reply_text(render_queue_snapshot(await runtime.queue.snapshot()))


async def guests_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show owner-only guest and pending-invite management controls."""
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    if not runtime.owner(update):
        await message.reply_text("Not authorized.")
        return
    text, keyboard = render_guest_management(runtime)
    await message.reply_text(text, reply_markup=keyboard)


def render_guest_management(runtime: BotRuntime):
    """Render active guests and unused invites with owner action buttons."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    lines = ["👥 Guest access", ""]
    rows = [[InlineKeyboardButton("➕ Create invite", callback_data="guests:create")]]
    if runtime.guest_grants:
        lines.append("Active guests:")
        for grant in sorted(runtime.guest_grants.values(), key=lambda item: item.user_id):
            lines.append(f"• {grant.display_name} ({grant.user_id})")
            rows.append(
                [
                    InlineKeyboardButton(
                        f"Revoke {grant.display_name}",
                        callback_data=f"guests:revoke:{grant.user_id}",
                    )
                ]
            )
    else:
        lines.append("Active guests: none")
    lines.append("")
    lines.append(f"Unused invites: {len(runtime.guest_invites)}")
    for index, token in enumerate(runtime.guest_invites, start=1):
        rows.append(
            [
                InlineKeyboardButton(
                    f"Invalidate invite {index}", callback_data=f"guests:invalidate:{token}"
                )
            ]
        )
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def guest_invite_url(bot_username: str, token: str) -> str:
    """Build the Telegram deep link used to redeem one guest invitation."""
    return create_deep_linked_url(bot_username, f"invite_{token}")
