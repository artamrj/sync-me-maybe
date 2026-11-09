"""Persist queue jobs, request status, and button data across restarts.

Everything is stored as JSON in the shared SQLite database. Restoring is
best-effort: rows that no longer parse are dropped instead of blocking startup.
Guest invites and grants are intentionally not persisted (see README).
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, fields
from datetime import datetime
from typing import Any

from sync_me_maybe.database import Database
from sync_me_maybe.library.playlists import PlaylistEntry, PlaylistState
from sync_me_maybe.music.providers.base import ResolvedTrack
from sync_me_maybe.music.urls import ClassifiedLink, LinkKind, LinkScope
from sync_me_maybe.queueing.queue import JobKind, QueuedJob, UploadPayload
from sync_me_maybe.ui.messages import StatusStage

LOGGER = logging.getLogger(__name__)

# Finished requests and their buttons are kept this long after their last update.
RETENTION_SECONDS = 30 * 24 * 3600


def job_to_dict(job: QueuedJob) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for item in fields(job):
        value = getattr(job, item.name)
        if item.name == "enqueued_at":
            value = value.isoformat()
        elif item.name == "classified_link" and value is not None:
            value = asdict(value)
        elif item.name == "upload" and value is not None:
            value = asdict(value)
        elif item.name == "resolved_track":
            value = asdict(value) if isinstance(value, ResolvedTrack) else None
        elif item.name == "retry_backoff_seconds":
            value = list(value)
        data[item.name] = value
    return data


def job_from_dict(data: dict[str, Any]) -> QueuedJob:
    values = dict(data)
    known = {item.name for item in fields(QueuedJob)}
    values = {key: value for key, value in values.items() if key in known}
    values["kind"] = JobKind(values["kind"])
    values["enqueued_at"] = datetime.fromisoformat(values["enqueued_at"])
    link = values.get("classified_link")
    if link:
        values["classified_link"] = ClassifiedLink(
            kind=LinkKind(link["kind"]),
            url=link["url"],
            scope=LinkScope(link.get("scope") or LinkScope.TRACK),
            reason=link.get("reason"),
        )
    if values.get("upload"):
        values["upload"] = UploadPayload(**values["upload"])
    if values.get("resolved_track"):
        resolved_fields = {item.name for item in fields(ResolvedTrack)}
        values["resolved_track"] = ResolvedTrack(
            **{k: v for k, v in values["resolved_track"].items() if k in resolved_fields}
        )
    if "retry_backoff_seconds" in values:
        values["retry_backoff_seconds"] = tuple(values["retry_backoff_seconds"])
    return QueuedJob(**values)


# RequestState fields that are runtime-only and never stored.
_REQUEST_RUNTIME_FIELDS = {"cancel_event", "edit_lock", "pending_edit", "last_edit_at"}


def request_to_dict(request: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for item in fields(request):
        if item.name in _REQUEST_RUNTIME_FIELDS:
            continue
        value = getattr(request, item.name)
        if item.name == "download_started_at":
            value = value.isoformat() if value else None
        elif item.name == "stage":
            value = value.name
        elif item.name == "issue_details":
            value = [asdict(detail) for detail in value]
        elif item.name == "failed_jobs":
            value = [job_to_dict(job) for job in value]
        elif item.name == "playlists":
            value = {url: asdict(playlist) for url, playlist in value.items()}
        data[item.name] = value
    return data


def request_from_dict(data: dict[str, Any]) -> Any:
    from sync_me_maybe.telegram_bot.runtime import RequestIssueDetail, RequestState

    known = {item.name for item in fields(RequestState)} - _REQUEST_RUNTIME_FIELDS
    values = {key: value for key, value in data.items() if key in known}
    if values.get("download_started_at"):
        values["download_started_at"] = datetime.fromisoformat(values["download_started_at"])
    values["stage"] = StatusStage[values.get("stage") or "QUEUED"]
    values["issue_details"] = [RequestIssueDetail(**d) for d in values.get("issue_details", [])]
    values["failed_jobs"] = [job_from_dict(job) for job in values.get("failed_jobs", [])]
    values["playlists"] = {
        url: PlaylistState(
            name=playlist["name"],
            source_url=playlist.get("source_url"),
            entries={
                int(position): PlaylistEntry(**entry)
                for position, entry in (playlist.get("entries") or {}).items()
            },
        )
        for url, playlist in (values.get("playlists") or {}).items()
    }
    request = RequestState(**values)
    if request.cancelled:
        request.cancel_event.set()
    return request


class StateStore:
    """SQLite persistence for jobs, requests, and inline-button callbacks."""

    def __init__(self, db: Database) -> None:
        self.db = db

    # Queue jobs -----------------------------------------------------------

    def save_job(self, job: QueuedJob, not_before: float | None = None) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO jobs (id, request_id, not_before, data) VALUES (?, ?, ?, ?)",
            (job.id, job.request_id, not_before, json.dumps(job_to_dict(job))),
        )

    def delete_job(self, job_id: str) -> None:
        self.db.execute("DELETE FROM jobs WHERE id = ?", (job_id,))

    def load_jobs(self) -> list[tuple[QueuedJob, float | None]]:
        jobs: list[tuple[QueuedJob, float | None]] = []
        for row in self.db.fetchall("SELECT id, not_before, data FROM jobs ORDER BY rowid"):
            try:
                jobs.append((job_from_dict(json.loads(row["data"])), row["not_before"]))
            except (KeyError, TypeError, ValueError):
                LOGGER.warning("Dropping unreadable persisted job %s", row["id"], exc_info=True)
                self.delete_job(str(row["id"]))
        return jobs

    # Requests -------------------------------------------------------------

    def save_request(self, request: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO requests (id, updated_at, data) VALUES (?, ?, ?)",
            (request.id, time.time(), json.dumps(request_to_dict(request))),
        )

    def load_requests(self) -> list[Any]:
        requests = []
        for row in self.db.fetchall("SELECT id, data FROM requests"):
            try:
                requests.append(request_from_dict(json.loads(row["data"])))
            except (KeyError, TypeError, ValueError):
                LOGGER.warning("Dropping unreadable persisted request %s", row["id"], exc_info=True)
                self.db.execute("DELETE FROM requests WHERE id = ?", (row["id"],))
        return requests

    # Callback buttons -----------------------------------------------------

    def save_callback(self, token: str, kind: str, owner_user_id: int | None, payload: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO callbacks (token, kind, owner_user_id, updated_at, data)"
            " VALUES (?, ?, ?, ?, ?)",
            (token, kind, owner_user_id, time.time(), json.dumps(payload)),
        )

    def load_callbacks(self) -> list[tuple[str, str, int | None, Any]]:
        rows = self.db.fetchall("SELECT token, kind, owner_user_id, data FROM callbacks")
        return [
            (str(row["token"]), str(row["kind"]), row["owner_user_id"], json.loads(row["data"]))
            for row in rows
        ]

    # Housekeeping ---------------------------------------------------------

    def prune(self, max_age_seconds: float = RETENTION_SECONDS) -> None:
        """Forget old finished requests and buttons; queued jobs are never pruned."""
        cutoff = time.time() - max_age_seconds
        self.db.execute(
            "DELETE FROM requests WHERE updated_at < ? AND id NOT IN"
            " (SELECT request_id FROM jobs WHERE request_id IS NOT NULL)",
            (cutoff,),
        )
        self.db.execute("DELETE FROM callbacks WHERE updated_at < ?", (cutoff,))
