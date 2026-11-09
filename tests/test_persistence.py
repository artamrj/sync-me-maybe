from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sync_me_maybe.config import Settings
from sync_me_maybe.database import Database
from sync_me_maybe.library.playlists import PlaylistEntry, PlaylistState
from sync_me_maybe.music.resolver import ResolvedTrack
from sync_me_maybe.music.urls import classify_url
from sync_me_maybe.queueing.queue import DownloadQueue, JobKind, QueuedJob, UploadPayload
from sync_me_maybe.telegram_bot.persistence import (
    StateStore,
    job_from_dict,
    job_to_dict,
    request_from_dict,
    request_to_dict,
)
from sync_me_maybe.telegram_bot.runtime import BotRuntime, RequestIssueDetail, RequestState
from sync_me_maybe.ui.messages import StatusStage


def link_job(**overrides: object) -> QueuedJob:
    values: dict[str, object] = {
        "kind": JobKind.LINK,
        "chat_id": 1,
        "original_message_id": 2,
        "status_message_id": 3,
        "user_id": 42,
        "source_label": "spotify",
        "classified_link": classify_url("https://open.spotify.com/playlist/abc"),
        "resolved_track": ResolvedTrack(
            "src", "ytsearch1:A Song", title="Song", artist="A", duration_seconds=200
        ),
        "request_id": "req",
        "request_index": 4,
        "attempt": 2,
    }
    values.update(overrides)
    return QueuedJob(**values)  # type: ignore[arg-type]


def test_job_round_trip_keeps_links_tracks_and_uploads() -> None:
    job = link_job()
    restored = job_from_dict(job_to_dict(job))
    assert restored == job

    upload = link_job(
        kind=JobKind.UPLOAD,
        classified_link=None,
        resolved_track=None,
        upload=UploadPayload("file", "unique", "song.flac"),
    )
    assert job_from_dict(job_to_dict(upload)) == upload


def test_request_round_trip_keeps_progress_details_and_playlists() -> None:
    request = RequestState(
        id="req",
        chat_id=1,
        status_message_id=10,
        title="Spotify playlist",
        total=3,
        owner_user_id=42,
        completed=1,
        failed=1,
        stage=StatusStage.DOWNLOADING,
        download_started_at=datetime.now(UTC),
        issue_details=[RequestIssueDetail("failed", "Song", reason="boom", metadata={"A": "b"})],
        failed_jobs=[link_job()],
        playlists={
            "url": PlaylistState("Road Trip", "url", {1: PlaylistEntry("A/Song.m4a", "Song")})
        },
        expanded_collections=["url"],
        cancelled=True,
    )

    restored = request_from_dict(request_to_dict(request))

    assert restored.stage == StatusStage.DOWNLOADING
    assert restored.issue_details == request.issue_details
    assert restored.failed_jobs == request.failed_jobs
    assert restored.playlists == request.playlists
    assert restored.expanded_collections == ["url"]
    assert restored.download_started_at == request.download_started_at
    assert restored.cancel_event.is_set()


def test_state_store_saves_loads_and_prunes() -> None:
    store = StateStore(Database())
    store.save_job(link_job(id="a"))
    store.save_job(link_job(id="b"), not_before=123.0)
    store.db.execute("INSERT INTO jobs (id, data) VALUES ('broken', '{}')")
    assert [(job.id, when) for job, when in store.load_jobs()] == [("a", None), ("b", 123.0)]
    store.delete_job("a")
    assert [job.id for job, _ in store.load_jobs()] == ["b"]

    store.save_request(RequestState("old", 1, 2, "Old", 1))
    store.save_request(RequestState("req", 1, 2, "Active", 1))
    store.save_callback("tok", "path", 42, "A/Song.m4a")
    store.db.execute("UPDATE requests SET updated_at = 0")
    store.db.execute("UPDATE callbacks SET updated_at = 0")
    store.prune()
    # "req" still has a queued job, so it is kept even though it is old.
    assert [request.id for request in store.load_requests()] == ["req"]
    assert store.load_callbacks() == []


@pytest.mark.asyncio
async def test_runtime_restores_requests_buttons_and_jobs_from_disk(tmp_path: Path) -> None:
    settings = Settings(
        telegram_bot_token="1:x",
        allowed_telegram_user_ids={42},
        music_dir=tmp_path / "music",
        download_tmp_dir=tmp_path / "tmp",
        data_dir=tmp_path / "data",
        metadata_lookup=False,
    )
    first = BotRuntime(settings)
    request = RequestState("req", 1, 10, "Playlist", 2, owner_user_id=42)
    request.failed_jobs.append(link_job())
    request.expanded_collections.append("https://open.spotify.com/playlist/abc")
    first.requests[request.id] = request
    first.save_request(request)
    path_token = first.remember_path("A/Song.m4a", 42)
    assert first.remember_path("A/Song.m4a", 42) == path_token  # stable across re-renders
    rerun_token = first.remember_failed_jobs(request)
    await first.queue.enqueue(link_job(id="child"))
    await first.queue.enqueue(
        link_job(
            id="collection",
            kind=JobKind.COLLECTION,
            classified_link=classify_url("https://open.spotify.com/playlist/abc"),
        )
    )
    first.db.close()

    second = BotRuntime(settings)
    jobs = second.restore_state()

    assert [job.id for job, _ in jobs] == ["child"]  # expanded collection is not re-run
    assert second.requests["req"].title == "Playlist"
    assert second.path_callbacks[path_token.removeprefix("path:")].text == "A/Song.m4a"
    owner, rerun = second.rerun_failed_callbacks[rerun_token.removeprefix("rerun_failed:")]
    assert owner == 42 and rerun.jobs[0].resolved_track == link_job().resolved_track
    second.db.close()


@pytest.mark.asyncio
async def test_queue_persists_until_finished_and_restores_retry_delays() -> None:
    store = StateStore(Database())
    queue = DownloadQueue(store=store)
    await queue.enqueue(link_job(id="one"))
    assert [job.id for job, _ in store.load_jobs()] == ["one"]

    done = asyncio.Event()

    async def processor(job: QueuedJob) -> None:
        done.set()

    queue.start(processor)
    await asyncio.wait_for(done.wait(), 1)
    await asyncio.sleep(0.01)
    await queue.stop()
    assert store.load_jobs() == []

    restored = DownloadQueue(store=store)
    later = link_job(id="later", request_id="r2")
    await restored.restore([(link_job(id="now"), None), (later, time.time() + 60)])
    assert [job.id for job in (await restored.snapshot()).pending] == ["now"]
    assert [job.id for job, when in store.load_jobs() if when] == ["later"]
    assert await restored.cancel_request("r2") == 1
    assert [job.id for job, _ in store.load_jobs()] == []


@pytest.mark.asyncio
async def test_queue_runs_jobs_in_parallel_and_reports_positions() -> None:
    queue = DownloadQueue(workers=2)
    started: list[str] = []
    release = asyncio.Event()

    async def processor(job: QueuedJob) -> None:
        started.append(job.id)
        await release.wait()

    for job_id in ["a", "b", "c"]:
        await queue.enqueue(link_job(id=job_id))
    queue.start(processor)
    for _ in range(50):
        if len(started) == 2:
            break
        await asyncio.sleep(0.01)

    snapshot = await queue.snapshot()
    assert started == ["a", "b"]
    assert [job.id for job in snapshot.active] == ["a", "b"]
    assert await queue.position_of("a") == 0
    assert await queue.position_of("c") == 3
    release.set()
    for _ in range(50):
        if len(started) == 3:
            break
        await asyncio.sleep(0.01)
    await queue.stop()
    assert started == ["a", "b", "c"]


@pytest.mark.asyncio
async def test_post_init_resumes_saved_jobs_and_marks_request(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sync_me_maybe.telegram_bot.app import post_init, post_shutdown

    settings = Settings(
        telegram_bot_token="1:x",
        allowed_telegram_user_ids={42},
        music_dir=tmp_path / "music",
        download_tmp_dir=tmp_path / "tmp",
        data_dir=tmp_path / "data",
        metadata_lookup=False,
    )
    before = BotRuntime(settings)
    request = RequestState("req", 1, 10, "Playlist", 2, owner_user_id=42)
    before.save_request(request)
    await before.queue.enqueue(link_job(id="pending"))
    before.db.close()

    runtime = BotRuntime(settings)
    processed: list[str] = []

    async def process_job(job: QueuedJob, _: object) -> None:
        processed.append(job.id)

    runtime.process_job = process_job  # type: ignore[method-assign]
    bot = SimpleNamespace(edit_message_text=AsyncMock(), set_my_commands=AsyncMock())
    application = SimpleNamespace(bot=bot, bot_data={"runtime": runtime})

    await post_init(application)  # type: ignore[arg-type]
    for _ in range(50):
        if processed:
            break
        await asyncio.sleep(0.01)
    await post_shutdown(application)  # type: ignore[arg-type]

    assert processed == ["pending"]
    assert "Resumed after restart." in bot.edit_message_text.await_args.kwargs["text"]
