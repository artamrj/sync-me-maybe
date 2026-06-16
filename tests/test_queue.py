from __future__ import annotations

import asyncio

from sync_me_maybe.queue import DownloadQueue, JobKind, QueuedJob, render_queue_snapshot
from sync_me_maybe.urls import ClassifiedLink, LinkKind


def make_job(label: str, message_id: int) -> QueuedJob:
    return QueuedJob(
        kind=JobKind.LINK,
        chat_id=1,
        original_message_id=message_id,
        status_message_id=message_id + 100,
        user_id=123,
        source_label=label,
        classified_link=ClassifiedLink(LinkKind.YOUTUBE, f"https://music.youtube.com/watch?v={message_id}"),
    )


def test_queue_positions_without_active_job() -> None:
    async def run() -> None:
        queue = DownloadQueue()

        assert await queue.enqueue(make_job("first", 1)) == 1
        assert await queue.enqueue(make_job("second", 2)) == 2

        snapshot = await queue.snapshot()
        assert snapshot.active is None
        assert [job.source_label for job in snapshot.pending] == ["first", "second"]

    asyncio.run(run())


def test_queue_worker_processes_fifo() -> None:
    async def run() -> None:
        queue = DownloadQueue()
        processed: list[str] = []
        done = asyncio.Event()

        async def processor(job: QueuedJob) -> None:
            processed.append(job.source_label)
            if len(processed) == 2:
                done.set()

        queue.start(processor)
        await queue.enqueue(make_job("first", 1))
        await queue.enqueue(make_job("second", 2))

        await asyncio.wait_for(done.wait(), timeout=1)
        await queue.stop()

        assert processed == ["first", "second"]

    asyncio.run(run())


def test_queue_snapshot_renders_active_and_pending() -> None:
    async def run() -> None:
        queue = DownloadQueue()
        started = asyncio.Event()
        release = asyncio.Event()

        async def processor(job: QueuedJob) -> None:
            started.set()
            await release.wait()

        queue.start(processor)
        await queue.enqueue(make_job("active", 1))
        await started.wait()
        await queue.enqueue(make_job("pending", 2))

        text = render_queue_snapshot(await queue.snapshot())
        release.set()
        await queue.stop()

        assert "Now: active" in text
        assert "Pending: 1" in text
        assert "1. pending" in text

    asyncio.run(run())


def test_empty_queue_snapshot_text() -> None:
    from sync_me_maybe.queue import QueueSnapshot

    assert render_queue_snapshot(QueueSnapshot(None, ())) == "✅ Queue is empty."


def test_cancel_request_removes_matching_pending_jobs() -> None:
    async def run() -> None:
        queue = DownloadQueue()
        keep = make_job("keep", 1)
        cancel_1 = make_job("cancel 1", 2)
        cancel_2 = make_job("cancel 2", 3)
        cancel_1.request_id = "req"
        cancel_2.request_id = "req"

        await queue.enqueue(keep)
        await queue.enqueue(cancel_1)
        await queue.enqueue(cancel_2)

        assert await queue.cancel_request("req") == 2
        snapshot = await queue.snapshot()
        assert [job.source_label for job in snapshot.pending] == ["keep"]

    asyncio.run(run())


def test_position_updates_after_request_cancellation() -> None:
    async def run() -> None:
        queue = DownloadQueue()
        first = make_job("first", 1)
        cancelled = make_job("cancel", 2)
        last = make_job("last", 3)
        cancelled.request_id = "req"

        await queue.enqueue(first)
        await queue.enqueue(cancelled)
        await queue.enqueue(last)

        assert await queue.position_of(last.id) == 3
        assert await queue.cancel_request("req") == 1
        assert await queue.position_of(last.id) == 2

    asyncio.run(run())
