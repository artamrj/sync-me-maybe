"""Shared in-memory state for one running bot process."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import uuid4

from telegram import Update
from telegram.ext import Application

from sync_me_maybe.auth import is_allowed, is_private_chat
from sync_me_maybe.config import Settings
from sync_me_maybe.database import Database
from sync_me_maybe.library.index import LibraryIndex
from sync_me_maybe.library.navidrome import NavidromeClient
from sync_me_maybe.library.playlists import PlaylistState
from sync_me_maybe.music.collections import CollectionResolver
from sync_me_maybe.music.downloader import YtDlpDownloader
from sync_me_maybe.music.enrichment import TrackEnricher
from sync_me_maybe.music.resolver import LinkResolver
from sync_me_maybe.music.urls import ClassifiedLink
from sync_me_maybe.queueing.queue import DownloadQueue, JobKind, QueuedJob, UploadPayload
from sync_me_maybe.telegram_bot.persistence import StateStore, job_from_dict, job_to_dict
from sync_me_maybe.ui.messages import StatusStage

LOGGER = logging.getLogger(__name__)


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
    owner_user_id: int | None = None
    source_urls: list[str] = field(default_factory=list)
    completed: int = 0
    skipped: int = 0
    failed: int = 0
    current: str | None = None
    detail: str | None = None
    track_title: str | None = None
    track_artist: str | None = None
    collection_title: str | None = None
    collection_owner: str | None = None
    source_label: str | None = None
    download_started_at: datetime | None = None
    stage: StatusStage = StatusStage.QUEUED
    paths: list[str] = field(default_factory=list)
    issue_details: list[RequestIssueDetail] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    failed_jobs: list[QueuedJob] = field(default_factory=list)
    cancelled: bool = False
    # Playlist files being built, keyed by collection URL.
    playlists: dict[str, PlaylistState] = field(default_factory=dict)
    # Collection URLs already expanded into child jobs (avoids re-expanding after restart).
    expanded_collections: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    # Status-message edit throttling (runtime only, never persisted).
    edit_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False, compare=False)
    pending_edit: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)
    last_edit_at: float = 0.0


@dataclass
class FailedJobRerun:
    """Jobs and display context needed by a rerun-failed callback."""

    title: str
    jobs: list[QueuedJob]


@dataclass(frozen=True)
class GuestInvite:
    """One unused, process-local guest invitation."""

    token: str
    created_by: int


@dataclass(frozen=True)
class GuestGrant:
    """One active, process-local guest authorization."""

    user_id: int
    display_name: str
    granted_by: int


@dataclass(frozen=True)
class OwnedText:
    """Callback text restricted to the user who created the request."""

    owner_user_id: int | None
    text: str


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
        self.path_callbacks: dict[str, OwnedText | str] = {}
        self.issue_callbacks: dict[str, OwnedText | str] = {}
        self.rerun_failed_callbacks: dict[
            str, tuple[int | None, FailedJobRerun] | FailedJobRerun
        ] = {}
        self.guest_invites: dict[str, GuestInvite] = {}
        self.guest_grants: dict[int, GuestGrant] = {}
        self.db = Database(settings.database_path)
        self.store = StateStore(self.db)
        self.library = LibraryIndex(self.db, settings.music_dir)
        self.queue = DownloadQueue(workers=settings.max_parallel_downloads, store=self.store)
        self.navidrome = (
            NavidromeClient(
                settings.navidrome_url or "",
                settings.navidrome_user or "",
                settings.navidrome_password or "",
            )
            if settings.navidrome_enabled
            else None
        )
        # Minimum seconds between edits of one status message. Two workers can
        # finish tracks back to back; this keeps Telegram rate limits happy.
        self.status_edit_interval = 2.5
        self.background_tasks: set[asyncio.Task[None]] = set()
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
            audio_format=settings.audio_format,
        )
        self.enricher = TrackEnricher(
            lookups_enabled=settings.metadata_lookup, lyrics_enabled=settings.lyrics
        )

    def allowed(self, update: Update) -> bool:
        """Check whether the effective Telegram user can use protected actions."""
        user_id = update.effective_user.id if update.effective_user else None
        if is_allowed(user_id, self.settings.allowed_telegram_user_ids):
            return True
        chat = getattr(update, "effective_chat", None)
        return bool(
            user_id in self.guest_grants
            and chat is not None
            and is_private_chat(getattr(chat, "type", None))
        )

    def owner(self, update: Update) -> bool:
        """Return whether the effective user is permanently allowlisted."""
        user_id = update.effective_user.id if update.effective_user else None
        return is_allowed(user_id, self.settings.allowed_telegram_user_ids)

    def create_guest_invite(self, owner_user_id: int) -> GuestInvite:
        """Create and retain a cryptographically random, single-use invite."""
        token = secrets.token_urlsafe(18)
        invite = GuestInvite(token=token, created_by=owner_user_id)
        self.guest_invites[token] = invite
        return invite

    def redeem_guest_invite(self, token: str, user_id: int, display_name: str) -> GuestGrant | None:
        """Consume an invite and grant access, or return None if it is invalid."""
        invite = self.guest_invites.pop(token, None)
        if invite is None:
            return None
        grant = GuestGrant(user_id=user_id, display_name=display_name, granted_by=invite.created_by)
        self.guest_grants[user_id] = grant
        return grant

    def remember_path(self, relative_path: str, owner_user_id: int | None = None) -> str:
        """Store one path for later display through an inline keyboard callback.

        Tokens are derived from their content, so re-rendering a status message
        reuses the same token instead of creating a new one on every edit.
        """
        token = hashlib.sha1(f"{owner_user_id}:{relative_path}".encode()).hexdigest()[:16]
        value = OwnedText(owner_user_id, relative_path)
        if self.path_callbacks.get(token) != value:
            self.path_callbacks[token] = value
            self._save_callback(token, "path", owner_user_id, relative_path)
        return f"path:{token}"

    def remember_issue_details(self, request: RequestState) -> str:
        """Store formatted skipped/failed details for later callback delivery."""
        token = request.id
        value = OwnedText(request.owner_user_id, render_issue_details(request.issue_details))
        if self.issue_callbacks.get(token) != value:
            self.issue_callbacks[token] = value
            self._save_callback(token, "issues", request.owner_user_id, value.text)
        return f"issues:{token}"

    def remember_failed_jobs(self, request: RequestState) -> str:
        """Store cloneable failed jobs for a rerun callback."""
        token = request.id
        rerun = FailedJobRerun(
            title=request.title,
            jobs=[clone_job(job) for job in request.failed_jobs],
        )
        self.rerun_failed_callbacks[token] = (request.owner_user_id, rerun)
        self._save_callback(
            token,
            "rerun_failed",
            request.owner_user_id,
            {"title": rerun.title, "jobs": [job_to_dict(job) for job in rerun.jobs]},
        )
        return f"rerun_failed:{token}"

    def save_request(self, request: RequestState) -> None:
        """Persist request status so buttons and progress survive a restart."""
        try:
            self.store.save_request(request)
        except Exception:  # noqa: BLE001 - persistence must never break the bot.
            LOGGER.warning("Could not persist request %s", request.id, exc_info=True)

    def restore_state(self) -> list[tuple[QueuedJob, float | None]]:
        """Load persisted requests and buttons; return jobs to re-queue."""
        try:
            self.store.prune()
            for token, kind, owner, payload in self.store.load_callbacks():
                if kind == "path":
                    self.path_callbacks[token] = OwnedText(owner, str(payload))
                elif kind == "issues":
                    self.issue_callbacks[token] = OwnedText(owner, str(payload))
                elif kind == "rerun_failed":
                    rerun_jobs = [job_from_dict(data) for data in payload.get("jobs", [])]
                    self.rerun_failed_callbacks[token] = (
                        owner,
                        FailedJobRerun(title=str(payload.get("title") or ""), jobs=rerun_jobs),
                    )
            for request in self.store.load_requests():
                self.requests[request.id] = request
            jobs = self.store.load_jobs()
        except Exception:  # noqa: BLE001 - a broken state file must not stop the bot.
            LOGGER.exception("Could not restore saved state; starting fresh")
            return []
        expanded = {
            (request.id, url)
            for request in self.requests.values()
            for url in request.expanded_collections
        }
        restored: list[tuple[QueuedJob, float | None]] = []
        for job, not_before in jobs:
            link = job.classified_link
            if (
                job.kind == JobKind.COLLECTION
                and link is not None
                and (job.request_id, link.url) in expanded
            ):
                # Its child jobs were already queued (and persisted) before the restart.
                self.store.delete_job(job.id)
                continue
            restored.append((job, not_before))
        return restored

    def _save_callback(self, token: str, kind: str, owner: int | None, payload: object) -> None:
        try:
            self.store.save_callback(token, kind, owner, payload)
        except Exception:  # noqa: BLE001 - persistence must never break the bot.
            LOGGER.warning("Could not persist %s button %s", kind, token, exc_info=True)

    def request_library_scan(self) -> None:
        """Ask Navidrome to rescan soon (debounced), when configured."""
        if self.navidrome is not None:
            self.navidrome.request_scan()

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
