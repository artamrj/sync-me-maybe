from __future__ import annotations

from enum import StrEnum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


class StatusStage(StrEnum):
    QUEUED = "Queued"
    THINKING = "Thinking"
    DOWNLOADING = "Downloading"
    SAVING = "Saving"


def render_welcome(authorized: bool) -> str:
    status = "Authorized" if authorized else "Not authorized"
    return (
        "sync-me-maybe\n\n"
        f"Status: {status}\n"
        "Send a single-track YouTube Music, Spotify, Apple Music, or Shazam link.\n"
        "You can also upload an audio file.\n\n"
        "Commands: /help /id /health"
    )


def render_help() -> str:
    return (
        "How to use sync-me-maybe\n\n"
        "1. Send one music link or one audio file.\n"
        "2. I resolve, download, and save it to your music folder.\n"
        "3. Duplicates are skipped automatically.\n\n"
        "Supported links: YouTube Music, Spotify tracks, Apple Music tracks, Shazam.\n"
        "Playlists and albums are not supported in v1."
    )


def render_status(stage: StatusStage, source: str, detail: str | None = None) -> str:
    lines = [
        f"{stage.value}",
        "",
        f"Source: {source}",
    ]
    if detail:
        lines.extend(["", detail])
    return "\n".join(lines)


def render_success(relative_path: str, skipped: bool = False) -> str:
    heading = "Skipped duplicate" if skipped else "Stored"
    return f"{heading}\n\nPath: {relative_path}"


def render_error(message: str) -> str:
    return f"Failed\n\n{message}"


def status_keyboard(
    *,
    source_url: str | None = None,
    relative_path: str | None = None,
    path_callback_data: str | None = None,
    include_health: bool = False,
) -> InlineKeyboardMarkup | None:
    rows: list[list[InlineKeyboardButton]] = []
    if source_url:
        rows.append([InlineKeyboardButton("Open source", url=source_url)])
    if relative_path:
        rows.append([InlineKeyboardButton("Show path", callback_data=path_callback_data or "path")])
    if include_health:
        rows.append([InlineKeyboardButton("Health", callback_data="health")])
    return InlineKeyboardMarkup(rows) if rows else None
