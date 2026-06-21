from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from telegram.error import BadRequest, RetryAfter, TelegramError, TimedOut

from sync_me_maybe.library.storage import TrackInfo
from sync_me_maybe.music.downloader import DownloadedTrack, DownloadError
from sync_me_maybe.music.providers.base import ExpandedCollection, TrackSearchItem
from sync_me_maybe.music.resolver import ResolvedTrack
from sync_me_maybe.music.urls import classify_url
from sync_me_maybe.queueing.queue import JobKind, QueuedJob, UploadPayload
from sync_me_maybe.telegram_bot.callbacks import handle_callback
from sync_me_maybe.telegram_bot.handlers import (
    enqueue_buffered_link_batch,
    enqueue_link_batch,
    handle_message,
    job_detail,
    process_collection_job,
    process_link_job,
    retry_link_job,
    update_parent_progress,
)
from sync_me_maybe.telegram_bot.requests import (
    mark_request_cancelled,
    request_keyboard,
    request_position,
    update_request,
)
from sync_me_maybe.telegram_bot.runtime import (
    BotRuntime,
    BufferedUpload,
    RequestIssueDetail,
    RequestState,
    UploadBatch,
)
from sync_me_maybe.telegram_bot.safe_api import (
    safe_edit_message,
    safe_send_message,
    telegram_call,
)
from sync_me_maybe.telegram_bot.uploads import (
    audio_document_filename,
    buffer_upload,
    enqueue_upload_batch,
    process_upload_job,
    retry_upload_job,
    upload_job_from_buffered,
)
from sync_me_maybe.ui.messages import StatusStage


@pytest.mark.asyncio
async def test_safe_api_retries_rate_limit_timeout_and_ignores_bad_request() -> None:
    operation = AsyncMock(side_effect=[RetryAfter(0), "ok"])
    with patch("sync_me_maybe.telegram_bot.safe_api.asyncio.sleep", new=AsyncMock()) as sleep:
        assert await telegram_call("send", operation) == "ok"
    sleep.assert_awaited_once()

    timeout_operation = AsyncMock(side_effect=[TimedOut("slow"), "ok"])
    with patch("sync_me_maybe.telegram_bot.safe_api.asyncio.sleep", new=AsyncMock()):
        assert await telegram_call("edit", timeout_operation) == "ok"

    assert (
        await telegram_call(
            "same",
            AsyncMock(side_effect=BadRequest("Message is not modified")),
        )
        is None
    )
    assert await telegram_call("bad", AsyncMock(side_effect=TelegramError("boom"))) is None


@pytest.mark.asyncio
async def test_safe_wrappers_call_expected_bot_methods(fake_application: SimpleNamespace) -> None:
    bot = fake_application.bot
    bot.edit_message_text.return_value = "edited"
    bot.send_message.return_value = "sent"

    assert await safe_edit_message(bot, 1, 0, "skip") is None
    assert await safe_edit_message(bot, 1, 2, "text") == "edited"
    assert bot.edit_message_text.await_args.kwargs["message_id"] == 2
    assert await safe_send_message(bot, 1, "text") == "sent"


@pytest.mark.asyncio
async def test_request_helpers_position_keyboard_update_and_cancel(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
) -> None:
    request = RequestState("r1", 1, 10, "Batch", 2, source_urls=["https://source"])
    runtime.requests[request.id] = request
    job = QueuedJob(
        kind=JobKind.LINK,
        chat_id=1,
        original_message_id=2,
        status_message_id=10,
        user_id=42,
        source_label="source",
        request_id=request.id,
    )
    await runtime.queue.enqueue(job)
    request.job_ids.append(job.id)

    assert await request_position(runtime, request) == 1
    keyboard = request_keyboard(runtime, request)
    assert keyboard is not None
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "⛔ Stop" in labels
    assert "🔄 Refresh" in labels
    assert "🧾 Skipped/failed details" not in labels

    request.stage = StatusStage.DONE
    request.completed = 2
    keyboard = request_keyboard(runtime, request)
    assert keyboard is not None
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "⛔ Stop" not in labels
    assert "🔄 Refresh" not in labels
    assert "🧾 Skipped/failed details" not in labels

    request.completed = 1
    request.failed = 1
    request.issue_details.append(RequestIssueDetail("failed", "Song", reason="broken"))
    keyboard = request_keyboard(runtime, request)
    assert keyboard is not None
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "🧾 Skipped/failed details" in labels
    await update_request(runtime, fake_application, request)
    fake_application.bot.edit_message_text.assert_awaited()

    await mark_request_cancelled(runtime, fake_application, request)
    assert request.cancelled
    assert request.cancel_event.is_set()
    assert request.stage == StatusStage.CANCELLED


@pytest.mark.asyncio
async def test_callbacks_answer_health_path_refresh_cancel(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
) -> None:
    query = SimpleNamespace(data="path:t", answer=AsyncMock())
    runtime.path_callbacks["t"] = "Artist/Song.mp3"
    callback_message = SimpleNamespace(chat_id=1)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=callback_message,
        callback_query=query,
    )
    context = SimpleNamespace(application=fake_application)

    await handle_callback(update, context)
    query.answer.assert_awaited_with("Artist/Song.mp3", show_alert=True)

    query.data = "health"
    await handle_callback(update, context)
    assert query.answer.await_args.args[0].startswith("Health ok")

    runtime.issue_callbacks["i"] = "Skipped/failed details\n\n1. FAILED - Song"
    query.data = "issues:i"
    await handle_callback(update, context)
    fake_application.bot.send_message.assert_awaited()
    assert "FAILED - Song" in fake_application.bot.send_message.await_args.kwargs["text"]
    assert query.answer.await_args.args == ("Sent details.",)

    query.data = "issues:missing"
    await handle_callback(update, context)
    query.answer.assert_awaited_with("Details are no longer available in memory.", show_alert=True)

    request = RequestState("r1", 1, 10, "Request", 1)
    runtime.requests[request.id] = request
    query.data = "refresh:r1"
    await handle_callback(update, context)
    assert query.answer.await_args.args == ("Refreshed.",)

    job = QueuedJob(JobKind.LINK, 1, 2, 10, 42, "source", request_id="r1")
    await runtime.queue.enqueue(job)
    request.job_ids.append(job.id)
    query.data = "cancel:r1"
    await handle_callback(update, context)
    assert request.cancelled
    assert request.failed == 1
    assert query.answer.await_args.args == ("Stopped.",)


@pytest.mark.asyncio
async def test_callbacks_reject_unauthorized(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
) -> None:
    query = SimpleNamespace(data="health", answer=AsyncMock())
    update = SimpleNamespace(effective_user=SimpleNamespace(id=7), callback_query=query)
    await handle_callback(update, SimpleNamespace(application=fake_application))
    query.answer.assert_awaited_with("Not authorized.", show_alert=True)


@pytest.mark.asyncio
async def test_handle_message_routes_auth_upload_empty_single_and_batch(
    runtime: BotRuntime,
    fake_update: SimpleNamespace,
    fake_context: SimpleNamespace,
    fake_message: SimpleNamespace,
) -> None:
    fake_update.effective_user.id = 7
    await handle_message(fake_update, fake_context)
    fake_message.reply_text.assert_awaited_with("Not authorized.")

    fake_update.effective_user.id = 42
    fake_message.reply_text.reset_mock()
    await handle_message(fake_update, fake_context)
    fake_message.reply_text.assert_awaited_with(
        "Send an audio file, music link, playlist link, or album link."
    )

    fake_message.text = "https://youtu.be/abc"
    await handle_message(fake_update, fake_context)
    snapshot = await runtime.queue.snapshot()
    assert len(snapshot.pending) == 1
    assert snapshot.pending[0].kind == JobKind.LINK

    fake_message.text = (
        "https://youtu.be/one https://example.com/nope https://open.spotify.com/playlist/abc"
    )
    await handle_message(fake_update, fake_context)
    snapshot = await runtime.queue.snapshot()
    assert [job.kind for job in snapshot.pending][-2:] == [JobKind.LINK, JobKind.COLLECTION]


@pytest.mark.asyncio
async def test_enqueue_link_batch_records_unsupported_failures(
    runtime: BotRuntime,
    fake_update: SimpleNamespace,
) -> None:
    links = [
        (1, classify_url("https://youtu.be/abc")),
        (3, classify_url("https://open.spotify.com/playlist/abc")),
    ]
    await enqueue_link_batch(fake_update, runtime, links, ["Link 2 unsupported"], link_total=3)
    request = next(iter(runtime.requests.values()))
    snapshot = await runtime.queue.snapshot()
    assert request.total == 3
    assert request.failed == 1
    assert request.issue_details[0].status == "failed"
    assert request.issue_details[0].reason == "Link 2 unsupported"
    assert len(request.job_ids) == 2
    assert [job.kind for job in snapshot.pending] == [JobKind.LINK, JobKind.COLLECTION]


@pytest.mark.asyncio
async def test_fast_link_messages_share_one_status_message_and_request(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
    fake_context: SimpleNamespace,
    fake_update: SimpleNamespace,
    fake_message: SimpleNamespace,
) -> None:
    runtime = BotRuntime(replace(runtime.settings, upload_batch_window_seconds=30))
    fake_application.bot_data["runtime"] = runtime
    fake_context.application = fake_application
    fake_message.text = "https://youtu.be/one"

    await handle_message(fake_update, fake_context)
    batch = runtime.link_batches[(1, 42)]
    first_flush_task = batch.flush_task
    assert first_flush_task is not None

    fake_message.message_id = 3
    fake_message.text = "https://open.spotify.com/playlist/abc"
    await handle_message(fake_update, fake_context)
    await asyncio.sleep(0)

    assert fake_message.reply_text.await_count == 1
    assert first_flush_task.cancelled()
    assert len(runtime.link_batches) == 1
    assert batch.request.total == 2
    assert batch.request.current == "2 link(s) queued"
    assert len(batch.links) == 2

    batch.flush_task.cancel()
    runtime.link_batches.pop(batch.key)
    await enqueue_buffered_link_batch(runtime, fake_application, batch)
    snapshot = await runtime.queue.snapshot()
    assert [job.kind for job in snapshot.pending] == [JobKind.LINK, JobKind.COLLECTION]
    assert {job.request_id for job in snapshot.pending} == {batch.request.id}
    assert batch.request.job_ids == [job.id for job in snapshot.pending]


@pytest.mark.asyncio
async def test_process_link_job_success_failure_and_retry(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
    tmp_path: Path,
) -> None:
    classified = classify_url("https://youtu.be/abc")
    request = RequestState("r1", 1, 10, "youtube", 1)
    runtime.requests[request.id] = request
    job = QueuedJob(
        JobKind.LINK,
        1,
        2,
        10,
        42,
        "youtube",
        classified_link=classified,
        request_id=request.id,
    )
    request.job_ids.append(job.id)
    temp = runtime.settings.download_tmp_dir / "song.mp3"
    runtime.settings.download_tmp_dir.mkdir(parents=True, exist_ok=True)
    temp.write_text("audio", encoding="utf-8")
    runtime.resolver.resolve = AsyncMock(
        return_value=ResolvedTrack("src", "download", title="Song", artist="Artist")
    )
    runtime.downloader.download = AsyncMock(
        return_value=DownloadedTrack(temp, TrackInfo("Song", "Artist"))
    )

    await process_link_job(job, runtime, fake_application)
    assert request.stage == StatusStage.DONE
    assert request.completed == 1
    assert request.paths == ["Artist - Song.mp3"]
    assert request.issue_details == []

    skip_request = RequestState("r-skip", 1, 11, "youtube", 1)
    runtime.requests[skip_request.id] = skip_request
    skip_job = QueuedJob(
        JobKind.LINK,
        1,
        2,
        11,
        42,
        "youtube",
        classified_link=classified,
        request_id=skip_request.id,
    )
    skip_request.job_ids.append(skip_job.id)
    duplicate = runtime.settings.download_tmp_dir / "duplicate.mp3"
    duplicate.write_text("audio", encoding="utf-8")
    runtime.resolver.resolve = AsyncMock(
        return_value=ResolvedTrack("src", "download", title="Song", artist="Artist")
    )
    runtime.downloader.download = AsyncMock(
        return_value=DownloadedTrack(duplicate, TrackInfo("Song", "Artist"))
    )
    await process_link_job(skip_job, runtime, fake_application)
    assert skip_request.skipped == 1
    assert skip_request.issue_details[0].status == "skipped"
    assert skip_request.issue_details[0].path == "Artist - Song.mp3"
    assert skip_request.issue_details[0].metadata["Title"] == "Song"

    failing = QueuedJob(JobKind.LINK, 1, 2, 10, 42, "youtube", classified_link=classified)
    runtime.resolver.resolve = AsyncMock(side_effect=DownloadError("permanent", retryable=False))
    await process_link_job(failing, runtime, fake_application)
    assert "permanent" in fake_application.bot.edit_message_text.await_args.kwargs["text"]

    failed_request = RequestState("r-fail", 1, 13, "youtube", 1)
    runtime.requests[failed_request.id] = failed_request
    failing_with_request = QueuedJob(
        JobKind.LINK,
        1,
        2,
        13,
        42,
        "youtube",
        classified_link=classified,
        request_id=failed_request.id,
    )
    runtime.resolver.resolve = AsyncMock(side_effect=DownloadError("permanent", retryable=False))
    await process_link_job(failing_with_request, runtime, fake_application)
    assert failed_request.failed == 1
    assert failed_request.issue_details[0].status == "failed"
    assert failed_request.issue_details[0].reason == "permanent"

    retry_job = QueuedJob(JobKind.LINK, 1, 2, 10, 42, "youtube", classified_link=classified)
    with pytest.raises(Exception) as exc:
        await retry_link_job(
            retry_job,
            runtime,
            fake_application,
            None,
            "youtube",
            DownloadError("temporary", retryable=True),
        )
    assert exc.value.__class__.__name__ == "RetryJob"


@pytest.mark.asyncio
async def test_process_collection_job_enqueues_child_tracks(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
) -> None:
    classified = classify_url("https://open.spotify.com/playlist/abc")
    request = RequestState("r1", 1, 10, "spotify playlist", 1)
    runtime.requests[request.id] = request
    job = QueuedJob(
        JobKind.COLLECTION,
        1,
        2,
        10,
        42,
        "playlist",
        classified_link=classified,
        request_id=request.id,
    )
    runtime.collection_resolver.expand = AsyncMock(
        return_value=ExpandedCollection(
            [TrackSearchItem("One", "Artist"), TrackSearchItem("Two", "Artist")],
            owner="Owner",
            title="Playlist",
        )
    )

    await process_collection_job(job, runtime, fake_application)
    snapshot = await runtime.queue.snapshot()
    assert request.total == 2
    assert len(snapshot.pending) == 2
    assert snapshot.pending[0].resolved_track.search_query == "Artist One"
    assert snapshot.pending[0].resolved_track.collection_owner == "Owner"
    assert snapshot.pending[0].resolved_track.collection_title == "Playlist"


@pytest.mark.asyncio
async def test_update_parent_progress_edits_collection_status(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
) -> None:
    runtime.batch_progress[99] = {
        "source": "youtube playlist",
        "total": 2,
        "queued": 2,
        "completed": 0,
        "skipped": 0,
        "failed": 0,
    }
    job = QueuedJob(JobKind.LINK, 1, 2, 10, 42, "track", parent_status_message_id=99)
    await update_parent_progress(job, runtime, fake_application, "completed")
    assert runtime.batch_progress[99]["completed"] == 1
    assert "50%" in fake_application.bot.edit_message_text.await_args.kwargs["text"]
    detail_job = QueuedJob(
        JobKind.LINK,
        1,
        2,
        3,
        42,
        "track",
        batch_index=1,
        batch_total=2,
    )
    assert job_detail(detail_job, "Downloading") == "Track 1/2\nDownloading"


@pytest.mark.asyncio
async def test_upload_buffer_batch_enqueue_and_document_detection(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
    fake_update: SimpleNamespace,
    fake_message: SimpleNamespace,
) -> None:
    fake_message.document = SimpleNamespace(
        file_id="file",
        file_unique_id="unique",
        file_name="song.mp3",
        mime_type="audio/mpeg",
    )
    assert audio_document_filename(fake_update) == "song.mp3"

    fake_application.bot.send_message.return_value = SimpleNamespace(
        message_id=11,
        edit_text=AsyncMock(),
    )
    await buffer_upload(fake_update, runtime, fake_application)
    assert len((await runtime.queue.snapshot()).pending) == 1

    request = RequestState("batch", 1, 10, "Telegram uploads", 2)
    batch = UploadBatch(
        key=(1, 42),
        request=request,
        uploads=[
            BufferedUpload(1, 2, 42, UploadPayload("a", "ua", "a.mp3")),
            BufferedUpload(1, 3, 42, UploadPayload("b", "ub", "b.mp3")),
        ],
    )
    runtime.requests[request.id] = request
    await enqueue_upload_batch(runtime, fake_application, batch)
    assert len(request.job_ids) == 2


@pytest.mark.asyncio
async def test_fast_uploads_share_one_status_message_and_request(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
    fake_update: SimpleNamespace,
    fake_message: SimpleNamespace,
) -> None:
    runtime = BotRuntime(replace(runtime.settings, upload_batch_window_seconds=30))
    fake_application.bot_data["runtime"] = runtime
    first_status = SimpleNamespace(message_id=11, edit_text=AsyncMock())
    fake_message.reply_text.return_value = first_status
    fake_message.document = SimpleNamespace(
        file_id="file-a",
        file_unique_id="unique-a",
        file_name="a.mp3",
        mime_type="audio/mpeg",
    )

    await buffer_upload(fake_update, runtime, fake_application)
    batch = runtime.upload_batches[(1, 42)]
    first_flush_task = batch.flush_task
    assert first_flush_task is not None

    fake_message.message_id = 3
    fake_message.document = SimpleNamespace(
        file_id="file-b",
        file_unique_id="unique-b",
        file_name="b.mp3",
        mime_type="audio/mpeg",
    )
    await buffer_upload(fake_update, runtime, fake_application)
    await asyncio.sleep(0)

    assert fake_message.reply_text.await_count == 1
    assert first_flush_task.cancelled()
    assert len(runtime.upload_batches) == 1
    assert batch.request.total == 2
    assert batch.request.current == "2 file(s) queued"
    assert len(batch.uploads) == 2

    batch.flush_task.cancel()
    runtime.upload_batches.pop(batch.key)
    await enqueue_upload_batch(runtime, fake_application, batch)
    snapshot = await runtime.queue.snapshot()
    assert len(snapshot.pending) == 2
    assert {job.request_id for job in snapshot.pending} == {batch.request.id}
    assert batch.request.job_ids == [job.id for job in snapshot.pending]


@pytest.mark.asyncio
async def test_later_upload_resets_batch_flush_timer(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
    fake_update: SimpleNamespace,
    fake_message: SimpleNamespace,
) -> None:
    runtime = BotRuntime(replace(runtime.settings, upload_batch_window_seconds=30))
    fake_application.bot_data["runtime"] = runtime
    fake_message.document = SimpleNamespace(
        file_id="file-a",
        file_unique_id="unique-a",
        file_name="a.mp3",
        mime_type="audio/mpeg",
    )

    await buffer_upload(fake_update, runtime, fake_application)
    batch = runtime.upload_batches[(1, 42)]
    first_flush_task = batch.flush_task
    assert first_flush_task is not None

    fake_message.message_id = 3
    fake_message.document = SimpleNamespace(
        file_id="file-b",
        file_unique_id="unique-b",
        file_name="b.mp3",
        mime_type="audio/mpeg",
    )
    await buffer_upload(fake_update, runtime, fake_application)
    await asyncio.sleep(0)

    assert first_flush_task.cancelled()
    assert batch.flush_task is not None
    assert batch.flush_task is not first_flush_task
    assert not batch.flush_task.done()
    batch.flush_task.cancel()


@pytest.mark.asyncio
async def test_process_upload_job_skip_success_failure_and_retry(
    runtime: BotRuntime,
    fake_application: SimpleNamespace,
) -> None:
    runtime.settings.music_dir.mkdir(parents=True)
    existing = runtime.settings.music_dir / "song.mp3"
    existing.write_text("old", encoding="utf-8")
    request = RequestState("r1", 1, 10, "upload", 1)
    runtime.requests[request.id] = request
    skip_job = QueuedJob(
        JobKind.UPLOAD,
        1,
        2,
        10,
        42,
        "song.mp3",
        request_id=request.id,
        upload=UploadPayload("file", "unique", "song.mp3"),
    )
    await process_upload_job(skip_job, runtime, fake_application)
    assert request.skipped == 1
    assert request.paths == ["song.mp3"]
    assert request.issue_details[0].status == "skipped"
    assert request.issue_details[0].label == "song.mp3"
    assert request.issue_details[0].path == "song.mp3"

    request2 = RequestState("r2", 1, 12, "upload", 1)
    runtime.requests[request2.id] = request2
    success_job = QueuedJob(
        JobKind.UPLOAD,
        1,
        2,
        12,
        42,
        "new.mp3",
        request_id=request2.id,
        upload=UploadPayload("file2", "unique2", "new.mp3"),
    )
    file_ref = SimpleNamespace(
        download_to_drive=AsyncMock(
            side_effect=lambda custom_path: Path(custom_path).write_text(
                "audio",
                encoding="utf-8",
            )
        )
    )
    fake_application.bot.get_file.return_value = file_ref
    await process_upload_job(success_job, runtime, fake_application)
    assert request2.completed == 1
    assert (runtime.settings.music_dir / "new.mp3").exists()

    failed_request = RequestState("r3", 1, 14, "upload", 1)
    runtime.requests[failed_request.id] = failed_request
    failed_job = QueuedJob(
        JobKind.UPLOAD,
        1,
        2,
        14,
        42,
        "failed.mp3",
        request_id=failed_request.id,
        upload=UploadPayload("file3", "unique3", "failed.mp3"),
    )
    fake_application.bot.get_file.side_effect = DownloadError("permanent", retryable=False)
    await process_upload_job(failed_job, runtime, fake_application)
    assert failed_request.failed == 1
    assert failed_request.issue_details[0].status == "failed"
    assert failed_request.issue_details[0].reason == "Upload failed: permanent"
    fake_application.bot.get_file.side_effect = None

    with pytest.raises(Exception) as exc:
        await retry_upload_job(
            success_job,
            runtime,
            fake_application,
            request2,
            "new.mp3",
            DownloadError("temporary", retryable=True),
        )
    assert exc.value.__class__.__name__ == "RetryJob"


def test_upload_job_from_buffered_sets_display_title() -> None:
    request = RequestState("r", 1, 10, "upload", 2)
    job = upload_job_from_buffered(
        BufferedUpload(1, 2, 42, UploadPayload("file", "unique", "song.mp3")),
        request,
        1,
        2,
    )
    assert job.display_title == "File 1/2"
    assert job.upload and job.upload.filename == "song.mp3"
