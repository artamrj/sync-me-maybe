from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from uuid import uuid4

from telegram import Update
from telegram.ext import Application

from sync_me_maybe.auth import is_allowed
from sync_me_maybe.config import Settings
from sync_me_maybe.music.collections import CollectionResolver
from sync_me_maybe.music.downloader import YtDlpDownloader
from sync_me_maybe.music.resolver import LinkResolver
from sync_me_maybe.queueing.queue import DownloadQueue, JobKind, QueuedJob, UploadPayload
from sync_me_maybe.ui.messages import StatusStage


@dataclass
class RequestState:
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
    stage: StatusStage = StatusStage.QUEUED
    paths: list[str] = field(default_factory=list)
    job_ids: list[str] = field(default_factory=list)
    cancelled: bool = False
    cancel_event: threading.Event = field(default_factory=threading.Event)


@dataclass
class BufferedUpload:
    chat_id: int
    original_message_id: int
    user_id: int
    payload: UploadPayload


@dataclass
class UploadBatch:
    key: tuple[int, int]
    request: RequestState
    uploads: list[BufferedUpload]
    flush_task: asyncio.Task[None] | None = None


class BotRuntime:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.path_callbacks: dict[str, str] = {}
        self.queue = DownloadQueue()
        self.resolver = LinkResolver()
        self.collection_resolver = CollectionResolver(settings)
        self.batch_progress: dict[int, dict[str, int | str]] = {}
        self.requests: dict[str, RequestState] = {}
        self.upload_batches: dict[tuple[int, int], UploadBatch] = {}
        self.downloader = YtDlpDownloader(
            tmp_dir=settings.download_tmp_dir,
            cookies_file=settings.ytdlp_cookies_file,
            max_seconds=settings.max_download_seconds,
        )

    def allowed(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        return is_allowed(user_id, self.settings.allowed_telegram_user_ids)

    def remember_path(self, relative_path: str) -> str:
        token = uuid4().hex[:16]
        self.path_callbacks[token] = relative_path
        return f"path:{token}"

    def remember_results(self, request: RequestState) -> str:
        token = uuid4().hex[:16]
        shown = request.paths[:8]
        more = len(request.paths) - len(shown)
        suffix = f"\n...and {more} more" if more > 0 else ""
        self.path_callbacks[token] = "\n".join(shown) + suffix if shown else "No stored paths yet."
        return f"results:{token}"

    async def process_job(self, job: QueuedJob, application: Application) -> None:
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
