"""Retry decisions and user-facing retry messages for queue jobs."""

from __future__ import annotations

from dataclasses import replace
from enum import StrEnum
from typing import Protocol

from telegram.error import NetworkError, TimedOut

from sync_me_maybe.queueing.queue import QueuedJob


class RetryDecision(StrEnum):
    """Possible outcomes after a queued job raises an exception."""

    RETRY = "retry"
    FAIL = "fail"
    CANCEL = "cancel"


class RetryableException(Protocol):
    """Protocol for exceptions that expose a retryable flag."""

    retryable: bool


def retry_delay_seconds(job: QueuedJob) -> int | None:
    """Return the configured backoff delay for the job's current attempt."""
    index = max(job.attempt - 1, 0)
    if index >= len(job.retry_backoff_seconds):
        return None
    return job.retry_backoff_seconds[index]


def next_attempt(job: QueuedJob) -> QueuedJob:
    """Return a copy of the job representing the next retry attempt."""
    return replace(job, attempt=job.attempt + 1)


def retry_detail(job: QueuedJob, delay_seconds: int, reason: str) -> str:
    """Render status text that explains when and why a retry will happen."""
    next_attempt_number = min(job.attempt + 1, job.max_attempts)
    return (
        f"Retry {next_attempt_number}/{job.max_attempts} "
        f"in {_format_delay(delay_seconds)}: {reason}"
    )


def retry_decision(job: QueuedJob, exc: BaseException) -> RetryDecision:
    """Choose whether a failed queue job should retry, fail, or cancel."""
    if is_cancelled_error(exc):
        return RetryDecision.CANCEL
    if not is_retryable_error(exc):
        return RetryDecision.FAIL
    if job.attempt >= job.max_attempts:
        return RetryDecision.FAIL
    if retry_delay_seconds(job) is None:
        return RetryDecision.FAIL
    return RetryDecision.RETRY


def is_retryable_error(exc: BaseException) -> bool:
    """Detect retryable errors, including wrapped causes from provider code."""
    if isinstance(exc, (TimedOut, NetworkError)):
        return True
    retryable = getattr(exc, "retryable", None)
    if isinstance(retryable, bool):
        return retryable
    cause = exc.__cause__
    if cause:
        return is_retryable_error(cause)
    return False


def is_cancelled_error(exc: BaseException) -> bool:
    """Detect user cancellations, including wrapped causes."""
    if "Cancelled by user" in str(exc):
        return True
    cause = exc.__cause__
    if cause:
        return is_cancelled_error(cause)
    return False


def _format_delay(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes, remainder = divmod(seconds, 60)
    if remainder == 0:
        return f"{minutes}m"
    return f"{minutes}m {remainder}s"
