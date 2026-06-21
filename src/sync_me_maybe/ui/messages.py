"""User-facing Telegram message and keyboard rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


class StatusStage(StrEnum):
    """Status labels shown in Telegram messages."""

    QUEUED = "⏳ Queued"
    THINKING = "🧠 Preparing"
    DOWNLOADING = "⬇️ Downloading"
    SAVING = "💾 Saving"
    EXPANDING = "🧩 Expanding"
    DONE = "✅ Done"
    SKIPPED = "⏭️ Skipped"
    FAILED = "❌ Failed"
    CANCELLED = "⛔ Cancelled"


@dataclass
class RequestView:
    """Presentation model used to render aggregate request status."""

    title: str
    stage: StatusStage
    total: int = 1
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    current: str | None = None
    queue_position: int | None = None
    detail: str | None = None
    paths: list[str] = field(default_factory=list)
    collection_title: str | None = None
    collection_owner: str | None = None


def render_welcome(authorized: bool) -> str:
    """Render the /start message."""
    status = "✅ Authorized" if authorized else "🔒 Not authorized"
    return (
        "🎧 sync-me-maybe\n\n"
        f"Status: {status}\n"
        "Send music links, playlist links, album links, or an audio file.\n"
        "You can also upload an audio file.\n\n"
        "Commands: /help /id /health /queue"
    )


def render_help() -> str:
    """Render the /help message."""
    return (
        "🎧 How to use sync-me-maybe\n\n"
        "1. Send one music link or one audio file.\n"
        "2. I resolve, download, and save it to your music folder.\n"
        "3. Duplicates are skipped automatically.\n\n"
        "Supported links: YouTube Music, Spotify, Apple Music, and Shazam tracks.\n"
        "Playlists/albums are supported for YouTube, Spotify, and Apple Music "
        "when public track data is available."
    )


def render_status(
    stage: StatusStage, source: str, detail: str | None = None, position: int | None = None
) -> str:
    """Render a simple one-job status message."""
    lines = [
        f"{stage.value}",
        "",
        f"🎼 Source: {source}",
    ]
    if position is not None:
        lines.append(f"📍 Queue: #{position}")
    if detail:
        lines.extend(["", detail])
    return "\n".join(lines)


def render_success(relative_path: str, skipped: bool = False) -> str:
    """Render completion text for stored or duplicate files."""
    heading = StatusStage.SKIPPED.value if skipped else StatusStage.DONE.value
    return f"{heading}\n\n📍 Path: {relative_path}"


def render_error(message: str) -> str:
    """Render a failed status message."""
    return f"{StatusStage.FAILED.value}\n\n{message}"


def render_collection_progress(
    source: str,
    total: int | None = None,
    queued: int = 0,
    completed: int = 0,
    skipped: int = 0,
    failed: int = 0,
) -> str:
    """Render progress for collection expansion and child track processing."""
    lines = [StatusStage.EXPANDING.value, "", f"🎼 Source: {source}"]
    if total is None:
        lines.append("Finding tracks...")
    else:
        done = completed + skipped + failed
        lines.extend(
            [
                progress_bar(done, total),
                f"🎵 Tracks: {total}",
                render_counters(total, completed, skipped, failed),
                f"⏳ Queued: {queued}",
            ]
        )
    return "\n".join(lines)


def progress_bar(done: int, total: int, width: int = 10) -> str:
    """Render a fixed-width text progress bar."""
    if total <= 0:
        return "░" * width + " 0%"
    ratio = max(0.0, min(1.0, done / total))
    filled = round(ratio * width)
    percent = round(ratio * 100)
    return f"{'█' * filled}{'░' * (width - filled)} {percent}%"


def render_counters(total: int, completed: int, skipped: int, failed: int) -> str:
    """Render stored/skipped/failed/waiting counters."""
    waiting = max(total - completed - skipped - failed, 0)
    return f"✅ {completed} saved  ⏭️ {skipped} skipped  ❌ {failed} failed  ⏳ {waiting} left"


def render_request(view: RequestView) -> str:
    """Render the aggregate request status used for batches and collections."""
    done = view.completed + view.skipped + view.failed
    lines = [
        f"🎧 {view.title}",
        _status_line(view),
        "",
    ]
    collection_label = _collection_label(view.title)
    if view.collection_title and collection_label:
        lines.append(f"{collection_label} name: {view.collection_title}")
    if view.collection_owner:
        lines.append(f"By: {view.collection_owner}")
    if len(lines) > 3:
        lines.append("")
    lines.extend(
        [
            progress_bar(done, view.total),
            "",
            render_counters(view.total, view.completed, view.skipped, view.failed),
        ]
    )
    if view.queue_position is not None:
        if view.queue_position == 0:
            lines.append("Queue: active")
        else:
            lines.append(f"Queue: #{view.queue_position}")
    if view.current:
        lines.extend(["", f"{_active_label(view)}: {view.current}"])
    if view.detail and view.detail != view.current:
        prefix = "Problem" if view.stage == StatusStage.FAILED else "Note"
        lines.append(f"{prefix}: {view.detail}")
    return "\n".join(lines)


def _status_line(view: RequestView) -> str:
    """Render the primary status without duplicating the active item."""
    if view.stage == StatusStage.EXPANDING:
        return "🧩 Finding tracks..."
    return view.stage.value


def _collection_label(title: str) -> str | None:
    normalized = title.casefold()
    if "playlist" in normalized:
        return "Playlist"
    if "album" in normalized:
        return "Album"
    return None


def _active_label(view: RequestView) -> str:
    title = view.title.casefold()
    if "upload" in title:
        return "Item"
    return "Track"


def status_keyboard(
    *,
    source_url: str | None = None,
    relative_path: str | None = None,
    path_callback_data: str | None = None,
    issue_callback_data: str | None = None,
    rerun_failed_callback_data: str | None = None,
    refresh_callback_data: str | None = None,
    cancel_callback_data: str | None = None,
    include_health: bool = False,
) -> InlineKeyboardMarkup | None:
    """Build optional inline buttons for source links and status actions."""
    rows: list[list[InlineKeyboardButton]] = []
    if source_url:
        rows.append([InlineKeyboardButton("🔗 Open source", url=source_url)])
    action_row: list[InlineKeyboardButton] = []
    if cancel_callback_data:
        action_row.append(InlineKeyboardButton("⛔ Stop", callback_data=cancel_callback_data))
    if refresh_callback_data:
        action_row.append(InlineKeyboardButton("🔄 Refresh", callback_data=refresh_callback_data))
    if relative_path:
        action_row.append(
            InlineKeyboardButton("📍 Show path", callback_data=path_callback_data or "path")
        )
    if issue_callback_data:
        action_row.append(
            InlineKeyboardButton("🧾 Skipped/failed details", callback_data=issue_callback_data)
        )
    if rerun_failed_callback_data:
        action_row.append(
            InlineKeyboardButton("🔁 Rerun failed", callback_data=rerun_failed_callback_data)
        )
    if action_row:
        rows.append(action_row)
    if include_health:
        rows.append([InlineKeyboardButton("🩺 Health", callback_data="health")])
    return InlineKeyboardMarkup(rows) if rows else None
