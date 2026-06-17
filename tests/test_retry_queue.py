from __future__ import annotations

import asyncio

import pytest
from telegram.error import TimedOut

from sync_me_maybe.music.downloader import DownloadError
from sync_me_maybe.music.resolver import ResolveError
from sync_me_maybe.queueing.queue import DownloadQueue, JobKind, QueuedJob, RetryJob
from sync_me_maybe.queueing.retry import (
    RetryDecision,
    next_attempt,
    retry_decision,
    retry_delay_seconds,
    retry_detail,
)


def make_job(**overrides: object) -> QueuedJob:
    values = {
        "kind": JobKind.LINK,
        "chat_id": 1,
        "original_message_id": 2,
        "status_message_id": 3,
        "user_id": 4,
        "source_label": "source",
    }
    values.update(overrides)
    return QueuedJob(**values)  # type: ignore[arg-type]


def test_retryable_errors_retry_until_limits() -> None:
    first = make_job()
    final = make_job(attempt=3)

    assert retry_decision(first, DownloadError("temporary", retryable=True)) == RetryDecision.RETRY
    assert retry_decision(first, TimedOut("telegram timed out")) == RetryDecision.RETRY
    assert retry_decision(final, DownloadError("temporary", retryable=True)) == RetryDecision.FAIL
    assert (
        retry_decision(
            make_job(retry_backoff_seconds=()),
            DownloadError("temporary", retryable=True),
        )
        == RetryDecision.FAIL
    )


def test_permanent_nested_and_cancelled_errors() -> None:
    current = make_job()
    wrapped = RuntimeError("wrapper")
    wrapped.__cause__ = DownloadError("temporary", retryable=True)
    cancelled = RuntimeError("wrapper")
    cancelled.__cause__ = DownloadError("Cancelled by user.", retryable=False)

    assert (
        retry_decision(current, ResolveError("Unsupported", retryable=False)) == RetryDecision.FAIL
    )
    assert retry_decision(current, wrapped) == RetryDecision.RETRY
    assert retry_decision(current, cancelled) == RetryDecision.CANCEL


def test_backoff_and_attempt_metadata() -> None:
    first = make_job()
    second = next_attempt(first)
    third = next_attempt(second)

    assert first.id == second.id
    assert second.attempt == 2
    assert retry_delay_seconds(first) == 30
    assert retry_delay_seconds(second) == 120
    assert retry_delay_seconds(third) == 600
    assert retry_detail(first, 30, "temporary") == "Retry 2/3 in 30s: temporary"
    assert retry_detail(make_job(attempt=3), 600, "temporary").startswith("Retry 3/3")


@pytest.mark.asyncio
async def test_queue_enqueue_snapshot_and_position() -> None:
    queue = DownloadQueue()
    first = make_job(id="first")
    second = make_job(id="second")

    assert await queue.enqueue(first) == 1
    assert await queue.enqueue(second) == 2
    snapshot = await queue.snapshot()
    assert snapshot.pending == (first, second)
    assert await queue.position_of("first") == 1
    assert await queue.position_of("missing") is None


@pytest.mark.asyncio
async def test_queue_worker_processes_jobs_and_survives_failure() -> None:
    queue = DownloadQueue()
    processed: list[str] = []
    calls = 0

    async def processor(job: QueuedJob) -> None:
        nonlocal calls
        calls += 1
        processed.append(job.id)
        if calls == 1:
            raise RuntimeError("first failure")

    queue.start(processor)
    await queue.enqueue(make_job(id="one"))
    await queue.enqueue(make_job(id="two"))
    for _ in range(20):
        if processed == ["one", "two"]:
            break
        await asyncio.sleep(0.01)
    await queue.stop()

    assert processed == ["one", "two"]


@pytest.mark.asyncio
async def test_queue_retry_and_cancel_request() -> None:
    queue = DownloadQueue()
    original = make_job(id="retry", request_id="request-1")

    queue.retry_later(original, 0)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert [job.id for job in (await queue.snapshot()).pending] == ["retry"]

    delayed = make_job(id="delayed", request_id="request-2")
    queue.retry_later(delayed, 60)
    assert await queue.cancel_request("request-2") == 1
    assert await queue.cancel_request("request-2") == 0


@pytest.mark.asyncio
async def test_queue_retries_processor_retryjob_after_delay() -> None:
    queue = DownloadQueue()
    attempts = 0

    async def processor(job: QueuedJob) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RetryJob(next_attempt(job), "temporary", 0)

    queue.start(processor)
    await queue.enqueue(make_job())
    for _ in range(20):
        if attempts >= 2:
            break
        await asyncio.sleep(0.01)
    await queue.stop()

    assert attempts == 2
