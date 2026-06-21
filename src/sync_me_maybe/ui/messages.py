"""User-facing Telegram message and keyboard rendering."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from telegram import InlineKeyboardButton, InlineKeyboardMarkup


class StatusStage(StrEnum):
    """Status labels shown in Telegram messages."""

    RECEIVED = "📥 Received"
    QUEUED = "🟡 Status     Queued"
    THINKING = "🟣 Status     Preparing"
    DOWNLOADING = "🔵 Status     Downloading"
    SAVING = "🔵 Status     Saving"
    EXPANDING = "🟣 Status     Preparing"
    DONE = "🟢 Status     Completed"
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
    source_label: str | None = None
    elapsed_seconds: int | None = None


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


def progress_bar(done: int, total: int, width: int = 10, *, show_count: bool = False) -> str:
    """Render a fixed-width text progress bar."""
    if total <= 0:
        bar = "░" * width + " 0%"
        return f"{bar}  • 0/0" if show_count else bar
    ratio = max(0.0, min(1.0, done / total))
    filled = round(ratio * width)
    percent = round(ratio * 100)
    bar = f"{'█' * filled}{'░' * (width - filled)} {percent}%"
    return f"{bar}  • {done}/{total}" if show_count else bar


def render_counters(total: int, completed: int, skipped: int, failed: int) -> str:
    """Render stored/skipped/failed/waiting counters."""
    return f"📥 {completed} saved • ⏭️ {skipped} skipped • ❌ {failed} failed"


def render_request(view: RequestView) -> str:
    """Render the aggregate request status used for batches and collections."""
    done = view.completed + view.skipped + view.failed
    visible_stage = _visible_stage(view)
    lines = [_status_line(view), _context_line(view)]

    if visible_stage == StatusStage.RECEIVED:
        return "\n".join(lines)

    if visible_stage == StatusStage.QUEUED:
        lines.extend(["", _queue_line(view)])
        detected = _detected_line(view)
        if detected:
            lines.append(detected)
        return "\n".join(lines)

    if visible_stage in {StatusStage.THINKING, StatusStage.EXPANDING}:
        lines.extend(["", _preparing_detail(view)])
        return "\n".join(lines)

    if visible_stage in {StatusStage.DOWNLOADING, StatusStage.SAVING}:
        lines.extend(["", progress_bar(done, view.total, show_count=True)])
        eta = _eta_line(view, done)
        if eta:
            lines.append(eta)
        lines.extend(["", render_counters(view.total, view.completed, view.skipped, view.failed)])
        if view.current and not _looks_like_index_label(view.current):
            lines.extend(["", f"{_active_label(view)}: {view.current}"])
        return "\n".join(lines)

    lines.extend(["", render_counters(view.total, view.completed, view.skipped, view.failed)])
    if view.detail:
        prefix = "Problem" if view.stage == StatusStage.FAILED else "Note"
        lines.append(f"{prefix}: {view.detail}")
    return "\n".join(lines)


def _visible_stage(view: RequestView) -> StatusStage:
    """Collapse internal queue states into user-visible lifecycle states."""
    if view.stage in {
        StatusStage.DONE,
        StatusStage.SKIPPED,
        StatusStage.FAILED,
        StatusStage.CANCELLED,
        StatusStage.DOWNLOADING,
        StatusStage.SAVING,
        StatusStage.THINKING,
        StatusStage.EXPANDING,
    }:
        return view.stage
    if view.queue_position is not None and view.queue_position > 1:
        return StatusStage.QUEUED
    if view.stage == StatusStage.QUEUED:
        return StatusStage.THINKING
    return StatusStage.RECEIVED


def _status_line(view: RequestView) -> str:
    """Render the primary status without duplicating the active item."""
    visible_stage = _visible_stage(view)
    if visible_stage == StatusStage.SKIPPED:
        return StatusStage.DONE.value
    if visible_stage == StatusStage.FAILED:
        return "🔴 Status     Failed"
    if visible_stage == StatusStage.CANCELLED:
        return "⚫ Status     Cancelled"
    return visible_stage.value


def _context_line(view: RequestView) -> str:
    source = _source_name(view)
    icon = _source_icon(source)
    title = _display_subject(view)
    owner = (view.collection_owner or "").strip()
    if title and owner:
        return f"{icon} {source} “{title}” by {owner}"
    if title:
        return f"{icon} {source} “{title}”"
    return f"{icon} {source}"


def _source_name(view: RequestView) -> str:
    if view.source_label:
        return view.source_label
    normalized_title = view.title.casefold()
    if "upload" in normalized_title:
        return "File"
    return view.title


def _source_icon(source: str) -> str:
    normalized = source.casefold()
    if "spotify" in normalized:
        return "🎵"
    if "apple music" in normalized:
        return "🍎"
    if "youtube" in normalized:
        return "📺"
    if "shazam" in normalized:
        return "🎶"
    if "file" in normalized or "upload" in normalized:
        return "📁"
    return "🎧"


def _display_subject(view: RequestView) -> str | None:
    if view.collection_title:
        return view.collection_title
    if "file" in _source_name(view).casefold() and view.current:
        return view.current
    return None


def _queue_line(view: RequestView) -> str:
    position = view.queue_position or 1
    return f"⏳ Waiting in queue · position #{position}"


def _detected_line(view: RequestView) -> str | None:
    if view.total <= 1:
        return None
    source = _source_name(view).casefold()
    if "playlist" in source or "album" in source:
        unit = "tracks"
    elif "file" in source or "upload" in source:
        unit = "files"
    else:
        unit = "items"
    return f"📦 {view.total} {unit} detected"


def _preparing_detail(view: RequestView) -> str:
    if view.detail:
        return view.detail
    source = _source_name(view).casefold()
    if "playlist" in source or "album" in source:
        return "🔍 Reading playlist..."
    if "file" in source or "upload" in source:
        return "🔍 Reading file..."
    return "🔍 Reading link..."


def _eta_line(view: RequestView, done: int) -> str | None:
    remaining = max(view.total - done, 0)
    if view.total <= 1 or done <= 0 or remaining <= 0 or not view.elapsed_seconds:
        return None
    seconds = round((view.elapsed_seconds / done) * remaining)
    if seconds <= 0:
        return None
    return f"⏳ ~{_format_duration(seconds)} remaining"


def _format_duration(seconds: int) -> str:
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _looks_like_index_label(value: str) -> bool:
    lower = value.casefold()
    return lower.startswith(("track ", "file ", "link ")) and "/" in lower


def _active_label(view: RequestView) -> str:
    source = _source_name(view).casefold()
    if "file" in source or "upload" in source:
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
