from __future__ import annotations

import asyncio
import unittest

from telegram.error import TimedOut

from sync_me_maybe.music.downloader import DownloadError
from sync_me_maybe.music.resolver import ResolveError
from sync_me_maybe.queueing.queue import DownloadQueue, JobKind, QueuedJob
from sync_me_maybe.queueing.retry import (
    RetryDecision,
    next_attempt,
    retry_decision,
    retry_delay_seconds,
    retry_detail,
)


def job(**overrides: object) -> QueuedJob:
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


class RetryPolicyTests(unittest.TestCase):
    def test_retryable_errors_retry_until_max_attempts(self) -> None:
        first = job()
        final = job(attempt=3)

        self.assertEqual(
            retry_decision(first, DownloadError("temporary yt-dlp error", retryable=True)),
            RetryDecision.RETRY,
        )
        self.assertEqual(
            retry_decision(first, TimedOut("telegram timed out")),
            RetryDecision.RETRY,
        )
        self.assertEqual(
            retry_decision(final, DownloadError("temporary yt-dlp error", retryable=True)),
            RetryDecision.FAIL,
        )

    def test_permanent_and_cancelled_errors_do_not_retry(self) -> None:
        current = job()

        self.assertEqual(
            retry_decision(current, ResolveError("Unsupported link.", retryable=False)),
            RetryDecision.FAIL,
        )
        self.assertEqual(
            retry_decision(current, DownloadError("Cancelled by user.", retryable=False)),
            RetryDecision.CANCEL,
        )
        self.assertEqual(
            retry_decision(
                current,
                DownloadError("No matching YouTube Music result found.", retryable=False),
            ),
            RetryDecision.FAIL,
        )

    def test_backoff_and_attempt_metadata(self) -> None:
        first = job()
        second = next_attempt(first)
        third = next_attempt(second)

        self.assertEqual(first.id, second.id)
        self.assertEqual(second.attempt, 2)
        self.assertEqual(retry_delay_seconds(first), 30)
        self.assertEqual(retry_delay_seconds(second), 120)
        self.assertEqual(retry_delay_seconds(third), 600)
        self.assertEqual(
            retry_detail(first, 30, "temporary"),
            "Retry 2/3 in 30s: temporary",
        )


class QueueRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_reenqueues_same_job_id_after_delay(self) -> None:
        queue = DownloadQueue()
        original = job(retry_backoff_seconds=(0,))

        queue.retry_later(original, 0)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        snapshot = await queue.snapshot()
        self.assertEqual([pending.id for pending in snapshot.pending], [original.id])

    async def test_cancel_request_cancels_delayed_retry(self) -> None:
        queue = DownloadQueue()
        original = job(request_id="request-1")

        queue.retry_later(original, 60)
        removed = await queue.cancel_request("request-1")
        snapshot = await queue.snapshot()

        self.assertEqual(removed, 1)
        self.assertEqual(snapshot.pending, ())


if __name__ == "__main__":
    unittest.main()
