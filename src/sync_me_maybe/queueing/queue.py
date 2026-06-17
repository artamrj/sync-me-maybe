"""In-memory FIFO queue that serializes downloads and delayed retries."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from sync_me_maybe.music.urls import ClassifiedLink

LOGGER = logging.getLogger(__name__)


class JobKind(StrEnum):
    """Kinds of work the queue worker knows how to dispatch."""

    LINK = "link"
    UPLOAD = "upload"
    COLLECTION = "collection"


@dataclass(frozen=True)
class UploadPayload:
    """Telegram file identifiers needed to fetch an uploaded audio file later."""

    file_id: str
    file_unique_id: str
    filename: str


@dataclass
class QueuedJob:
    """One unit of work visible in the queue and status messages."""

    kind: JobKind
    chat_id: int
    original_message_id: int
    status_message_id: int
    user_id: int
    source_label: str
    id: str = field(default_factory=lambda: uuid4().hex)
    enqueued_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    classified_link: ClassifiedLink | None = None
    upload: UploadPayload | None = None
    resolved_track: object | None = None
    parent_status_message_id: int | None = None
    batch_index: int | None = None
    batch_total: int | None = None
    request_id: str | None = None
    request_status_message_id: int | None = None
    request_total: int | None = None
    request_index: int | None = None
    display_title: str | None = None
    attempt: int = 1
    max_attempts: int = 3
    retry_backoff_seconds: tuple[int, ...] = (30, 120, 600)


class RetryJob(RuntimeError):
    """Internal signal used by processors to requeue a job after a delay."""

    def __init__(self, job: QueuedJob, reason: str, delay_seconds: int) -> None:
        super().__init__(reason)
        self.job = job
        self.reason = reason
        self.delay_seconds = delay_seconds


@dataclass(frozen=True)
class QueueSnapshot:
    """Read-only view used by the /queue command."""

    active: QueuedJob | None
    pending: tuple[QueuedJob, ...]


class DownloadQueue:
    """Single-worker async queue for download and upload processing."""

    def __init__(self) -> None:
        self._pending: deque[QueuedJob] = deque()
        self._active: QueuedJob | None = None
        self._condition = asyncio.Condition()
        self._worker: asyncio.Task[None] | None = None
        self._delayed_retries: dict[str, asyncio.Task[None]] = {}
        self._delayed_retry_jobs: dict[str, QueuedJob] = {}

    async def enqueue(self, job: QueuedJob) -> int:
        """Append a job and return its visible queue position."""
        async with self._condition:
            self._pending.append(job)
            position = len(self._pending) + (1 if self._active else 0)
            self._condition.notify()
            return position

    async def snapshot(self) -> QueueSnapshot:
        """Return the active job plus pending jobs without exposing internals."""
        async with self._condition:
            return QueueSnapshot(self._active, tuple(self._pending))

    async def position_of(self, job_id: str) -> int | None:
        """Return the current queue position for a job ID, or None if finished."""
        async with self._condition:
            if self._active and self._active.id == job_id:
                return 0
            for index, job in enumerate(self._pending, start=1):
                if job.id == job_id:
                    return index + (1 if self._active else 0)
            return None

    async def cancel_request(self, request_id: str) -> int:
        """Remove pending and delayed jobs that belong to one user request."""
        async with self._condition:
            # Active work is not forcibly interrupted here; request cancellation
            # is also carried by RequestState.cancel_event for blocking work.
            kept: deque[QueuedJob] = deque()
            removed = 0
            while self._pending:
                job = self._pending.popleft()
                if job.request_id == request_id:
                    removed += 1
                else:
                    kept.append(job)
            self._pending = kept
            for job_id, task in list(self._delayed_retries.items()):
                delayed_job = self._delayed_retry_jobs.get(job_id)
                if delayed_job and delayed_job.request_id == request_id:
                    task.cancel()
                    self._delayed_retries.pop(job_id, None)
                    self._delayed_retry_jobs.pop(job_id, None)
                    removed += 1
            return removed

    def retry_later(self, job: QueuedJob, delay_seconds: int) -> None:
        """Schedule a failed job to return to the queue after its backoff."""
        async def reenqueue() -> None:
            try:
                await asyncio.sleep(delay_seconds)
                # Re-enter the same FIFO queue after sleeping so retries do not
                # block unrelated jobs while waiting for network recovery.
                async with self._condition:
                    self._pending.append(job)
                    self._condition.notify()
            except asyncio.CancelledError:
                raise
            finally:
                self._delayed_retries.pop(job.id, None)
                self._delayed_retry_jobs.pop(job.id, None)

        task = asyncio.create_task(reenqueue())
        old_task = self._delayed_retries.pop(job.id, None)
        if old_task:
            old_task.cancel()
        self._delayed_retries[job.id] = task
        self._delayed_retry_jobs[job.id] = job

    def start(self, processor: Callable[[QueuedJob], Awaitable[None]]) -> None:
        """Start the background worker if it is not already running."""
        if self._worker and not self._worker.done():
            return
        self._worker = asyncio.create_task(self._run(processor))

    async def stop(self) -> None:
        """Cancel the worker and delayed retry tasks during bot shutdown."""
        if not self._worker:
            return
        self._worker.cancel()
        for task in self._delayed_retries.values():
            task.cancel()
        self._delayed_retries.clear()
        self._delayed_retry_jobs.clear()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001 - do not crash shutdown because the worker already failed.
            LOGGER.exception("Download queue worker stopped after an error")

    async def _run(self, processor: Callable[[QueuedJob], Awaitable[None]]) -> None:
        """Continuously process one queued job at a time."""
        while True:
            async with self._condition:
                # The condition lets enqueue/retry wake the worker without busy
                # polling while the queue is empty.
                while not self._pending:
                    await self._condition.wait()
                job = self._pending.popleft()
                self._active = job

            try:
                await processor(job)
            except RetryJob as exc:
                # Retry is an expected control path, not an unexpected worker
                # crash. The job is stored separately until its delay expires.
                self.retry_later(exc.job, exc.delay_seconds)
                LOGGER.info(
                    "Retrying queue job %s attempt %s/%s in %ss: %s",
                    exc.job.source_label,
                    exc.job.attempt,
                    exc.job.max_attempts,
                    exc.delay_seconds,
                    exc.reason,
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep queue worker alive after one failed job.
                LOGGER.exception("Download queue job failed unexpectedly: %s", job.source_label)
            finally:
                async with self._condition:
                    if self._active is job:
                        self._active = None


def render_queue_snapshot(snapshot: QueueSnapshot, limit: int = 5) -> str:
    """Render a compact queue summary for the Telegram /queue command."""
    if not snapshot.active and not snapshot.pending:
        return "✅ Queue is empty."

    lines: list[str] = []
    if snapshot.active:
        lines.append(f"⬇️ Now: {snapshot.active.source_label}")
    else:
        lines.append("💤 Now: idle")

    lines.append(f"⏳ Pending: {len(snapshot.pending)}")
    for index, job in enumerate(snapshot.pending[:limit], start=1):
        lines.append(f"{index}. {job.source_label}")

    remaining = len(snapshot.pending) - limit
    if remaining > 0:
        lines.append(f"...and {remaining} more")
    return "\n".join(lines)
