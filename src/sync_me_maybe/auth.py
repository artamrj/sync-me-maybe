"""Small authorization helper for Telegram user allowlisting."""

from __future__ import annotations


def is_allowed(user_id: int | None, allowed_user_ids: set[int]) -> bool:
    """Return whether a Telegram user is present and included in the allowlist."""
    return user_id is not None and user_id in allowed_user_ids


def is_private_chat(chat_type: object | None) -> bool:
    """Return whether Telegram identified the effective chat as private."""
    value = getattr(chat_type, "value", chat_type)
    return value == "private"
