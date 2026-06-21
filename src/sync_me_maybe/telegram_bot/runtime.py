"""Shared in-memory state for one running bot process."""

from __future__ import annotations

import asyncio
import copy
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from telegram import Update
from telegram.ext import Application

from sync_me_maybe.auth import is_allowed
from sync_me_maybe.config import Settings
from sync_me_maybe.music.collections import CollectionResolver
from sync_me_maybe.music.downloader import YtDlpDownloader
from sync_me_maybe.music.resolver import LinkResolver
from sync_me_maybe.music.urls import ClassifiedLink
from sync_me_maybe.queueing.queue import DownloadQueue, JobKind, QueuedJob, UploadPayload
from sync_me_maybe.ui.messages import StatusStage


@dataclass
class RequestIssueDetail:
    """Skipped or failed item detail shown from a final status button."""

    status: str
    label: str
    source_url: str | None = None
    path: str | None = None
    reason: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


@dataclass
class RequestState:
    """User-visible request status shared by queue jobs and callbacks."""

    id: str
    chat_id: int
    status_message_id: int
    title: str
    total: int
    source_urls: list[str] = field(default_factory=list)
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    current: str | None = None
    detail: str | None = None
    collection_title: str | None = None
    collection_owner: str | None = None
    source_label: str | None = None
    stage: StatusStage = StatusStage.QUEUED
    paths: list[str] = field(default_factory=list)
    issue_details: list[RequestIssueDetail] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    failed_jobs: list[QueuedJob] = field(default_factory=list)
    cancelled: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class FailedJobRerun:
    """Jobs and display context needed by a rerun-failed callback."""

    title: str
    jobs: list[QueuedJob]


@dataclass
class BufferedUpload:
    """Upload metadata held briefly while nearby files are batched together."""

    chat_id: int
    original_message_id: int
    user_id: int
    payload: UploadPayload


@dataclass
class BufferedLink:
    """Link metadata held briefly while nearby link messages are batched."""

    chat_id: int
    original_message_id: int
    user_id: int
    link_index: int
    classified_link: ClassifiedLink


@dataclass
class UploadBatch:
    """Pending group of uploads from the same chat/user within the batch window."""

    key: tuple[int, int]
    request: RequestState
    uploads: list[BufferedUpload]
    flush_task: asyncio.Task[None] | None = None


@dataclass
class LinkBatch:
    """Pending group of links from the same chat/user within the batch window."""

    key: tuple[int, int]
    request: RequestState
    links: list[BufferedLink]
    unsupported: list[str] = field(default_factory=list)
    flush_task: asyncio.Task[None] | None = None


class BotRuntime:
    """Container for services and mutable process-local bot state."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        # Callback data in Telegram buttons must be short, so paths are remembered
        # in memory and buttons carry only generated tokens.
        self.path_callbacks: dict[str, str] = {}
        self.issue_callbacks: dict[str, str] = {}
        self.rerun_failed_callbacks: dict[str, FailedJobRerun] = {}
        self.queue = DownloadQueue()
        self.resolver = LinkResolver()
        self.collection_resolver = CollectionResolver(settings)
        self.batch_progress: dict[int, dict[str, int | str]] = {}
        self.requests: dict[str, RequestState] = {}
        self.upload_batches: dict[tuple[int, int], UploadBatch] = {}
        self.link_batches: dict[tuple[int, int], LinkBatch] = {}
        self.downloader = YtDlpDownloader(
            tmp_dir=settings.download_tmp_dir,
            cookies_file=settings.ytdlp_cookies_file,
            max_seconds=settings.max_download_seconds,
        )

    def allowed(self, update: Update) -> bool:
        """Check whether the effective Telegram user can use protected actions."""
        user_id = update.effective_user.id if update.effective_user else None
        return is_allowed(user_id, self.settings.allowed_telegram_user_ids)

    def remember_path(self, relative_path: str) -> str:
        """Store one path for later display through an inline keyboard callback."""
        token = uuid4().hex[:16]
        self.path_callbacks[token] = relative_path
        return f"path:{token}"

    def remember_issue_details(self, request: RequestState) -> str:
        """Store formatted skipped/failed details for later callback delivery."""
        token = uuid4().hex[:16]
        self.issue_callbacks[token] = render_issue_details(request.issue_details)
        return f"issues:{token}"

    def remember_failed_jobs(self, request: RequestState) -> str:
        """Store cloneable failed jobs for a rerun callback."""
        token = uuid4().hex[:16]
        self.rerun_failed_callbacks[token] = FailedJobRerun(
            title=request.title,
            jobs=[clone_job(job) for job in request.failed_jobs],
        )
        return f"rerun_failed:{token}"

    async def process_job(self, job: QueuedJob, application: Application) -> None:
        """Dispatch queued work to the handler module that owns that job kind."""
        # Imports stay inside the method to avoid circular imports: handlers need
        # BotRuntime, and BotRuntime needs to call back into those handlers.
        if job.kind == JobKind.LINK:
            from sync_me_maybe.telegram_bot.handlers import process_link_job

            await process_link_job(job, self, application)
            return
        if job.kind == JobKind.UPLOAD:
            from sync_me_maybe.telegram_bot.uploads import process_upload_job

            await process_upload_job(job, self, application)
            return
        if job.kind == JobKind.COLLECTION:
            from sync_me_maybe.telegram_bot.handlers import process_collection_job

            await process_collection_job(job, self, application)
            return
        raise RuntimeError(f"Unknown job kind: {job.kind}")


def render_issue_details(details: list[RequestIssueDetail]) -> str:
    """Render skipped/failed details for a normal Telegram chat message."""
    lines = ["Skipped/failed details"]
    for index, detail in enumerate(details, start=1):
        lines.extend(["", f"{index}. {detail.status.upper()} - {detail.label}"])
        if detail.reason:
            lines.append(f"Reason: {detail.reason}")
        if detail.source_url:
            lines.append(f"Source: {detail.source_url}")
        for key, value in detail.metadata.items():
            lines.append(f"{key}: {value}")
        if detail.path:
            lines.append(f"Path: {detail.path}")
    return "\n".join(lines)


def clone_job(job: QueuedJob, **overrides: object) -> QueuedJob:
    """Copy a queued job with a new ID, reset attempts, and optional overrides."""
    cloned = copy.copy(job)
    cloned.id = uuid4().hex
    cloned.attempt = 1
    cloned.enqueued_at = datetime.now(UTC)
    for key, value in overrides.items():
        setattr(cloned, key, value)
    return cloned


def issue_metadata_from_track(track: object | None) -> dict[str, str]:
    """Extract useful display metadata from resolved/downloaded track objects."""
    metadata: dict[str, str] = {}
    field_labels = (
        ("title", "Title"),
        ("artist", "Artist"),
        ("album", "Album"),
        ("track_number", "Track"),
        ("search_query", "Search"),
        ("collection_owner", "Collection owner"),
        ("collection_title", "Collection title"),
    )
    for attr, label in field_labels:
        value = getattr(track, attr, None)
        if value is not None and str(value):
            metadata[label] = str(value)
    owner = metadata.pop("Collection owner", None)
    title = metadata.pop("Collection title", None)
    if owner and title:
        metadata["Collection"] = f"{owner} - {title}"
    elif title:
        metadata["Collection"] = title
    elif owner:
        metadata["Collection"] = owner
    return metadata
