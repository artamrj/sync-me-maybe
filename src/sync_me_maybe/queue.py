from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from .urls import ClassifiedLink


class JobKind(StrEnum):
    LINK = "link"
    UPLOAD = "upload"
    COLLECTION = "collection"


@dataclass(frozen=True)
class UploadPayload:
    file_id: str
    file_unique_id: str
    filename: str


@dataclass
class QueuedJob:
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


@dataclass(frozen=True)
class QueueSnapshot:
    active: QueuedJob | None
    pending: tuple[QueuedJob, ...]


class DownloadQueue:
    def __init__(self) -> None:
        self._pending: deque[QueuedJob] = deque()
        self._active: QueuedJob | None = None
        self._condition = asyncio.Condition()
        self._worker: asyncio.Task[None] | None = None

    async def enqueue(self, job: QueuedJob) -> int:
        async with self._condition:
            self._pending.append(job)
            position = len(self._pending) + (1 if self._active else 0)
            self._condition.notify()
            return position

    async def snapshot(self) -> QueueSnapshot:
        async with self._condition:
            return QueueSnapshot(self._active, tuple(self._pending))

    def start(self, processor: Callable[[QueuedJob], Awaitable[None]]) -> None:
        if self._worker and not self._worker.done():
            return
        self._worker = asyncio.create_task(self._run(processor))

    async def stop(self) -> None:
        if not self._worker:
            return
        self._worker.cancel()
        try:
            await self._worker
        except asyncio.CancelledError:
            pass

    async def _run(self, processor: Callable[[QueuedJob], Awaitable[None]]) -> None:
        while True:
            async with self._condition:
                while not self._pending:
                    await self._condition.wait()
                job = self._pending.popleft()
                self._active = job

            try:
                await processor(job)
            finally:
                async with self._condition:
                    if self._active is job:
                        self._active = None


def render_queue_snapshot(snapshot: QueueSnapshot, limit: int = 5) -> str:
    if not snapshot.active and not snapshot.pending:
        return "Queue is empty."

    lines: list[str] = []
    if snapshot.active:
        lines.append(f"Now: {snapshot.active.source_label}")
    else:
        lines.append("Now: idle")

    lines.append(f"Pending: {len(snapshot.pending)}")
    for index, job in enumerate(snapshot.pending[:limit], start=1):
        lines.append(f"{index}. {job.source_label}")

    remaining = len(snapshot.pending) - limit
    if remaining > 0:
        lines.append(f"...and {remaining} more")
    return "\n".join(lines)
