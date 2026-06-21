"""Inbound Telegram message handling for music links and collections."""

from __future__ import annotations

import asyncio
import logging
from uuid import uuid4

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ContextTypes

from sync_me_maybe.library.storage import store_completed_file, track_destination
from sync_me_maybe.music.collections import CollectionResolveError
from sync_me_maybe.music.downloader import DownloadError
from sync_me_maybe.music.resolver import ResolvedTrack, ResolveError
from sync_me_maybe.music.urls import LinkKind, LinkScope, classify_url, extract_urls
from sync_me_maybe.queueing.queue import JobKind, QueuedJob, RetryJob
from sync_me_maybe.queueing.retry import (
    RetryDecision,
    next_attempt,
    retry_decision,
    retry_delay_seconds,
    retry_detail,
)
from sync_me_maybe.telegram_bot.requests import (
    job_request,
    mark_request_cancelled,
    render_request_text,
    request_cancelled,
    request_keyboard,
    update_request,
)
from sync_me_maybe.telegram_bot.runtime import (
    BotRuntime,
    BufferedLink,
    LinkBatch,
    RequestIssueDetail,
    RequestState,
    issue_metadata_from_track,
)
from sync_me_maybe.telegram_bot.safe_api import (
    safe_chat_action,
    safe_edit_message,
    safe_edit_status,
    safe_send_message,
)
from sync_me_maybe.telegram_bot.uploads import audio_document_filename, buffer_upload
from sync_me_maybe.ui.messages import (
    RequestView,
    StatusStage,
    render_collection_progress,
    render_error,
    render_request,
    render_status,
    render_success,
    status_keyboard,
)

LOGGER = logging.getLogger(__name__)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route a non-command Telegram message to upload, link, or collection flow."""
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    if not runtime.allowed(update):
        await message.reply_text("Not authorized.")
        return

    # Telegram can send audio either as a dedicated audio object or as a generic
    # document with an audio MIME type/extension.
    if message.audio or audio_document_filename(update):
        await buffer_upload(update, runtime, context.application)
        return

    text = message.text or message.caption
    urls = extract_urls(text)
    if not urls:
        await message.reply_text("Send an audio file, music link, playlist link, or album link.")
        return

    # Classify every URL first so a single Telegram reply can report unsupported
    # links while still queueing the supported ones from the same message.
    classified_links = []
    unsupported: list[str] = []
    total = len(urls)
    for index, url in enumerate(urls, start=1):
        classified = classify_url(url)
        if classified.kind == LinkKind.UNSUPPORTED:
            detail = f"Link {index} of {total}: " if total > 1 else ""
            unsupported.append(f"{detail}{classified.reason or 'Unsupported link.'}")
            continue
        classified_links.append((index, classified))

    if not classified_links:
        await message.reply_text(
            render_error("\n".join(unsupported) if unsupported else "Unsupported link."),
            reply_to_message_id=message.message_id,
            allow_sending_without_reply=True,
        )
        return

    if runtime.settings.upload_batch_window_seconds > 0:
        await buffer_link_request(
            update,
            runtime,
            context.application,
            classified_links,
            unsupported,
            link_total=len(urls),
        )
        return

    if len(classified_links) == 1 and not unsupported:
        index, classified = classified_links[0]
        if classified.scope == LinkScope.TRACK:
            await enqueue_link(update, runtime, classified, link_index=index, link_total=len(urls))
        else:
            await enqueue_collection(
                update, runtime, classified, link_index=index, link_total=len(urls)
            )
        return

    await enqueue_link_batch(update, runtime, classified_links, unsupported, link_total=len(urls))


async def buffer_link_request(
    update: Update,
    runtime: BotRuntime,
    application: Application,
    classified_links,
    unsupported: list[str],
    link_total: int,
) -> None:
    """Collect nearby link messages and enqueue them as one visible request."""
    message = update.effective_message
    assert message is not None
    user_id = message.from_user.id if message.from_user else 0
    key = (message.chat_id, user_id)
    buffered_links = [
        BufferedLink(message.chat_id, message.message_id, user_id, link_index, classified)
        for link_index, classified in classified_links
    ]
    batch = runtime.link_batches.get(key)
    if not batch:
        request_id = uuid4().hex
        title = link_batch_title(len(buffered_links))
        detail = "\n".join(unsupported) if unsupported else None
        status_message = await message.reply_text(
            render_request(
                RequestView(
                    title=title,
                    stage=StatusStage.QUEUED,
                    total=len(buffered_links) + len(unsupported),
                    failed=len(unsupported),
                    detail=detail,
                )
            ),
            reply_to_message_id=message.message_id,
            allow_sending_without_reply=True,
        )
        request = RequestState(
            id=request_id,
            chat_id=message.chat_id,
            status_message_id=status_message.message_id,
            title=title,
            total=len(buffered_links) + len(unsupported),
            failed=len(unsupported),
            detail=detail,
            source_urls=[link.classified_link.url for link in buffered_links],
        )
        add_unsupported_details(request, unsupported)
        runtime.requests[request_id] = request
        batch = LinkBatch(key=key, request=request, links=buffered_links, unsupported=unsupported)
        runtime.link_batches[key] = batch
    else:
        batch.links.extend(buffered_links)
        batch.unsupported.extend(unsupported)
        batch.request.title = link_batch_title(len(batch.links))
        batch.request.total = len(batch.links) + len(batch.unsupported)
        batch.request.failed = len(batch.unsupported)
        batch.request.current = f"{len(batch.links)} link(s) queued"
        batch.request.detail = "\n".join(batch.unsupported) if batch.unsupported else None
        batch.request.source_urls.extend(link.classified_link.url for link in buffered_links)
        add_unsupported_details(batch.request, unsupported)
        if batch.flush_task:
            batch.flush_task.cancel()

    await update_request(runtime, application, batch.request)
    batch.flush_task = asyncio.create_task(flush_link_batch_after_delay(runtime, application, key))


async def flush_link_batch_after_delay(
    runtime: BotRuntime, application: Application, key: tuple[int, int]
) -> None:
    """Wait for the batch window, then enqueue collected link jobs."""
    try:
        await asyncio.sleep(runtime.settings.upload_batch_window_seconds)
    except asyncio.CancelledError:
        return
    batch = runtime.link_batches.pop(key, None)
    if not batch:
        return
    await enqueue_buffered_link_batch(runtime, application, batch)


async def enqueue_buffered_link_batch(
    runtime: BotRuntime, application: Application, batch: LinkBatch
) -> None:
    """Turn a buffered cross-message link batch into queue jobs."""
    if batch.request.cancelled:
        return
    batch.request.title = link_batch_title(len(batch.links))
    batch.request.total = len(batch.links) + len(batch.unsupported)
    batch.request.failed = len(batch.unsupported)
    batch.request.current = f"{len(batch.links)} link(s) queued"
    batch.request.detail = "\n".join(batch.unsupported) if batch.unsupported else None
    add_unsupported_details(batch.request, batch.unsupported)
    for request_index, buffered in enumerate(batch.links, start=1):
        classified = buffered.classified_link
        source = (
            classified.kind.value
            if classified.scope == LinkScope.TRACK
            else f"{classified.kind.value} {classified.scope.value}"
        )
        source_label = f"{source} link {request_index}/{len(batch.links)}"
        job = QueuedJob(
            kind=JobKind.LINK if classified.scope == LinkScope.TRACK else JobKind.COLLECTION,
            chat_id=buffered.chat_id,
            original_message_id=buffered.original_message_id,
            status_message_id=batch.request.status_message_id,
            user_id=buffered.user_id,
            source_label=source_label,
            classified_link=classified,
            request_id=batch.request.id,
            request_status_message_id=batch.request.status_message_id,
            request_total=len(batch.links),
            request_index=request_index,
            display_title=source_label,
        )
        await runtime.queue.enqueue(job)
        batch.request.job_ids.append(job.id)
    await update_request(runtime, application, batch.request)


def link_batch_title(count: int) -> str:
    """Render a compact title for one or more queued music links."""
    return "Music link" if count == 1 else f"{count} music link(s)"


def add_unsupported_details(request: RequestState, unsupported: list[str]) -> None:
    """Record unsupported links as failed issue details once per reason."""
    known = {
        (detail.status, detail.label, detail.reason)
        for detail in request.issue_details
        if detail.status == "failed"
    }
    for reason in unsupported:
        item = ("failed", "Unsupported link", reason)
        if item in known:
            continue
        request.issue_details.append(
            RequestIssueDetail(status="failed", label="Unsupported link", reason=reason)
        )
        known.add(item)


def add_link_issue_detail(
    request: RequestState,
    job: QueuedJob,
    status: str,
    *,
    path: str | None = None,
    reason: str | None = None,
    resolved: object | None = None,
    downloaded_info: object | None = None,
) -> None:
    """Record skipped/failed detail for one link job."""
    request.issue_details.append(
        RequestIssueDetail(
            status=status,
            label=job.display_title or job.source_label,
            source_url=job.classified_link.url if job.classified_link else None,
            path=path,
            reason=reason,
            metadata={
                **issue_metadata_from_track(resolved),
                **issue_metadata_from_track(downloaded_info),
            },
        )
    )


async def enqueue_link(
    update: Update,
    runtime: BotRuntime,
    classified,
    link_index: int = 1,
    link_total: int = 1,
) -> None:
    """Create a one-track request and enqueue its download job."""
    message = update.effective_message
    assert message is not None
    detail = f"Link {link_index} of {link_total}" if link_total > 1 else None
    request_id = uuid4().hex
    title = classified.kind.value
    status_message = await message.reply_text(
        render_request(RequestView(title=title, stage=StatusStage.QUEUED, current=detail)),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    # A RequestState owns the user-facing Telegram status message; the QueuedJob
    # below owns the actual work item processed by the background queue.
    request = RequestState(
        id=request_id,
        chat_id=message.chat_id,
        status_message_id=status_message.message_id,
        title=title,
        total=1,
        current=detail,
        source_urls=[classified.url],
    )
    runtime.requests[request_id] = request
    source_label = (
        classified.kind.value
        if link_total == 1
        else f"{classified.kind.value} link {link_index}/{link_total}"
    )
    job = QueuedJob(
        kind=JobKind.LINK,
        chat_id=message.chat_id,
        original_message_id=message.message_id,
        status_message_id=status_message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        source_label=source_label,
        classified_link=classified,
        request_id=request_id,
        request_status_message_id=status_message.message_id,
        request_total=1,
        request_index=1,
        display_title=source_label,
    )
    await runtime.queue.enqueue(job)
    request.job_ids.append(job.id)
    await safe_edit_status(
        status_message,
        await render_request_text(runtime, request),
        reply_markup=request_keyboard(runtime, request),
    )


async def enqueue_collection(
    update: Update,
    runtime: BotRuntime,
    classified,
    link_index: int = 1,
    link_total: int = 1,
) -> None:
    """Create a collection request and enqueue the expansion job."""
    message = update.effective_message
    assert message is not None
    detail = f"Link {link_index} of {link_total}" if link_total > 1 else None
    source = f"{classified.kind.value} {classified.scope.value}"
    request_id = uuid4().hex
    status_message = await message.reply_text(
        render_request(RequestView(title=source, stage=StatusStage.QUEUED, current=detail)),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    request = RequestState(
        id=request_id,
        chat_id=message.chat_id,
        status_message_id=status_message.message_id,
        title=source,
        total=1,
        current=detail,
        source_urls=[classified.url],
    )
    runtime.requests[request_id] = request
    source_label = source if link_total == 1 else f"{source} link {link_index}/{link_total}"
    job = QueuedJob(
        kind=JobKind.COLLECTION,
        chat_id=message.chat_id,
        original_message_id=message.message_id,
        status_message_id=status_message.message_id,
        user_id=message.from_user.id if message.from_user else 0,
        source_label=source_label,
        classified_link=classified,
        request_id=request_id,
        request_status_message_id=status_message.message_id,
        request_total=1,
        request_index=1,
        display_title=source_label,
    )
    await runtime.queue.enqueue(job)
    request.job_ids.append(job.id)
    await safe_edit_status(
        status_message,
        await render_request_text(runtime, request),
        reply_markup=request_keyboard(runtime, request),
    )


async def enqueue_link_batch(
    update: Update, runtime: BotRuntime, classified_links, unsupported: list[str], link_total: int
) -> None:
    """Create one aggregate request for a message containing multiple links."""
    message = update.effective_message
    assert message is not None
    request_id = uuid4().hex
    title = f"{len(classified_links)} music link(s)"
    detail = "\n".join(unsupported) if unsupported else None
    status_message = await message.reply_text(
        render_request(
            RequestView(
                title=title,
                stage=StatusStage.QUEUED,
                total=link_total,
                failed=len(unsupported),
                detail=detail,
            )
        ),
        reply_to_message_id=message.message_id,
        allow_sending_without_reply=True,
    )
    request = RequestState(
        id=request_id,
        chat_id=message.chat_id,
        status_message_id=status_message.message_id,
        title=title,
        total=link_total,
        failed=len(unsupported),
        detail=detail,
        source_urls=[classified.url for _, classified in classified_links],
    )
    add_unsupported_details(request, unsupported)
    runtime.requests[request_id] = request

    for request_index, (link_index, classified) in enumerate(classified_links, start=1):
        # Track links download directly; playlist/album links first expand into
        # child jobs and then those child jobs download like normal links.
        source = (
            classified.kind.value
            if classified.scope == LinkScope.TRACK
            else f"{classified.kind.value} {classified.scope.value}"
        )
        source_label = f"{source} link {link_index}/{link_total}"
        job = QueuedJob(
            kind=JobKind.LINK if classified.scope == LinkScope.TRACK else JobKind.COLLECTION,
            chat_id=message.chat_id,
            original_message_id=message.message_id,
            status_message_id=status_message.message_id,
            user_id=message.from_user.id if message.from_user else 0,
            source_label=source_label,
            classified_link=classified,
            request_id=request_id,
            request_status_message_id=status_message.message_id,
            request_total=len(classified_links),
            request_index=request_index,
            display_title=source_label,
        )
        await runtime.queue.enqueue(job)
        request.job_ids.append(job.id)

    await safe_edit_status(
        status_message,
        await render_request_text(runtime, request),
        reply_markup=request_keyboard(runtime, request),
    )


async def process_link_job(job: QueuedJob, runtime: BotRuntime, application: Application) -> None:
    """Resolve, download, and store one link or one expanded collection track."""
    bot = application.bot
    assert job.classified_link is not None
    classified = job.classified_link
    source = (
        f"{classified.kind.value} {classified.scope.value}"
        if job.batch_total
        else classified.kind.value
    )
    request = job_request(runtime, job)
    if request_cancelled(request):
        return
    resolved: ResolvedTrack | None = None
    try:
        await safe_chat_action(bot, job.chat_id, ChatAction.TYPING)
        if request:
            request.stage = StatusStage.THINKING
            request.current = job.display_title or job.source_label
            request.detail = "Preparing search."
            await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_status(StatusStage.THINKING, source, job_detail(job, "Preparing search.")),
            )
        # Collection child jobs already carry a ResolvedTrack. Plain link jobs
        # resolve provider metadata here before downloading.
        resolved = (
            job.resolved_track
            if isinstance(job.resolved_track, ResolvedTrack)
            else await runtime.resolver.resolve(classified)
        )
        await safe_chat_action(bot, job.chat_id, ChatAction.UPLOAD_DOCUMENT)
        if request:
            request.stage = StatusStage.DOWNLOADING
            request.current = job_detail(job, resolved.search_query or "Direct YouTube Music link.")
            request.detail = None
            await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_status(
                    StatusStage.DOWNLOADING,
                    source,
                    job_detail(job, resolved.search_query or "Direct YouTube Music link."),
                ),
            )
        downloaded = await runtime.downloader.download(
            resolved, cancel_check=request.cancel_event.is_set if request else None
        )
        if request_cancelled(request):
            # If cancellation happened just after the blocking download returned,
            # remove the temp file before surfacing the cancellation.
            downloaded.temp_file.unlink(missing_ok=True)
            raise DownloadError("Cancelled by user.")
        destination = track_destination(runtime.settings.music_dir, downloaded.info, ".mp3")
        if request:
            request.stage = StatusStage.SAVING
            request.current = job_detail(job, destination.name)
            await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_status(StatusStage.SAVING, source, job_detail(job, destination.name)),
            )
        result = store_completed_file(downloaded.temp_file, destination, runtime.settings.music_dir)
    except (ResolveError, DownloadError) as exc:
        if await retry_link_job(job, runtime, application, request, source, exc):
            return
        await update_parent_progress(job, runtime, application, "failed")
        if request:
            if request_cancelled(request) or "Cancelled by user" in str(exc):
                await mark_request_cancelled(runtime, application, request)
            else:
                request.stage = StatusStage.FAILED
                request.failed += 1
                request.current = job.display_title or job.source_label
                request.detail = str(exc)
                add_link_issue_detail(
                    request, job, "failed", reason=str(exc), resolved=resolved
                )
                await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_error(str(exc)),
                reply_markup=status_keyboard(source_url=classified.url),
            )
        return
    except Exception as exc:  # noqa: BLE001 - keep bot alive and report actionable failure.
        if await retry_link_job(job, runtime, application, request, source, exc):
            return
        LOGGER.exception("Link handling failed")
        await update_parent_progress(job, runtime, application, "failed")
        if request:
            request.stage = StatusStage.FAILED
            request.failed += 1
            request.current = job.display_title or job.source_label
            request.detail = f"Download failed: {exc}"
            add_link_issue_detail(
                request,
                job,
                "failed",
                reason=f"Download failed: {exc}",
                resolved=resolved,
            )
            await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_error(f"Download failed: {exc}"),
                reply_markup=status_keyboard(source_url=classified.url),
            )
        return

    if request:
        # Aggregate requests stay on one Telegram message. Each completed job
        # updates counters and paths until the whole request reaches a terminal
        # status.
        if result.skipped:
            request.skipped += 1
            add_link_issue_detail(
                request,
                job,
                "skipped",
                path=result.relative_path,
                resolved=resolved,
                downloaded_info=downloaded.info,
            )
        else:
            request.completed += 1
        request.paths.append(result.relative_path)
        done = request.completed + request.skipped + request.failed
        request.stage = StatusStage.DONE if done >= request.total else StatusStage.QUEUED
        request.current = job.display_title or result.relative_path
        request.detail = None
        await update_request(runtime, application, request)
    else:
        await safe_edit_message(
            bot,
            job.chat_id,
            job.status_message_id,
            render_success(result.relative_path, skipped=result.skipped),
            reply_markup=status_keyboard(
                source_url=classified.url,
                relative_path=result.relative_path,
                path_callback_data=runtime.remember_path(result.relative_path),
            ),
        )
    await update_parent_progress(
        job, runtime, application, "skipped" if result.skipped else "completed"
    )


async def process_collection_job(
    job: QueuedJob, runtime: BotRuntime, application: Application
) -> None:
    """Expand one playlist/album and enqueue child track download jobs."""
    bot = application.bot
    assert job.classified_link is not None
    classified = job.classified_link
    source = f"{classified.kind.value} {classified.scope.value}"
    request = job_request(runtime, job)
    if request_cancelled(request):
        return

    try:
        if request:
            request.stage = StatusStage.EXPANDING
            request.current = job.display_title or source
            request.detail = "Detecting tracks."
            await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot, job.chat_id, job.status_message_id, render_collection_progress(source)
            )
        if request_cancelled(request):
            return
        collection = await runtime.collection_resolver.expand(classified)
        tracks = collection.tracks
        if request_cancelled(request):
            return
    except CollectionResolveError as exc:
        if await retry_collection_job(job, runtime, application, request, source, exc):
            return
        if request:
            request.stage = StatusStage.FAILED
            request.failed += 1
            request.current = job.display_title or source
            request.detail = str(exc)
            request.issue_details.append(
                RequestIssueDetail(
                    status="failed",
                    label=job.display_title or source,
                    source_url=classified.url,
                    reason=str(exc),
                )
            )
            await update_request(runtime, application, request)
        else:
            await safe_edit_message(
                bot,
                job.chat_id,
                job.status_message_id,
                render_error(str(exc)),
                reply_markup=status_keyboard(source_url=classified.url),
            )
        return

    if request:
        # The collection expansion job itself counted as one item initially. Once
        # tracks are known, the request total grows to include every child track.
        request.total += max(len(tracks) - 1, 0)
        request.stage = StatusStage.QUEUED
        request.current = f"{len(tracks)} track(s) detected"
        request.detail = None
        await update_request(runtime, application, request)

        for index, track in enumerate(tracks, start=1):
            if request_cancelled(request):
                await mark_request_cancelled(runtime, application, request)
                break
            detail = f"Track {index}/{len(tracks)}"
            # Child jobs use ytsearch URLs, so the downloader can handle them the
            # same way it handles Spotify/Apple/Shazam single-track links.
            resolved = ResolvedTrack(
                source_url=track.source_url or classified.url,
                download_url=f"ytsearch1:{track.search_query}",
                search_query=track.search_query,
                title=track.title,
                artist=track.artist,
                album=track.album,
                track_number=track.track_number,
                collection_owner=collection.owner,
                collection_title=collection.title,
            )
            child = QueuedJob(
                kind=JobKind.LINK,
                chat_id=job.chat_id,
                original_message_id=job.original_message_id,
                status_message_id=request.status_message_id,
                user_id=job.user_id,
                source_label=f"{source} track {index}/{len(tracks)}",
                classified_link=classified,
                resolved_track=resolved,
                request_id=request.id,
                request_status_message_id=request.status_message_id,
                request_total=len(tracks),
                request_index=index,
                display_title=detail,
            )
            await runtime.queue.enqueue(child)
            request.job_ids.append(child.id)
        await update_request(runtime, application, request)
        return

    # Older/simple collection flow uses one parent progress message plus separate
    # child status messages when no aggregate RequestState exists.
    runtime.batch_progress[job.status_message_id] = {
        "source": source,
        "total": len(tracks),
        "queued": len(tracks),
        "completed": 0,
        "skipped": 0,
        "failed": 0,
    }
    await safe_edit_message(
        bot,
        job.chat_id,
        job.status_message_id,
        render_collection_progress(source, total=len(tracks), queued=len(tracks)),
        reply_markup=status_keyboard(source_url=classified.url),
    )

    for index, track in enumerate(tracks, start=1):
        detail = f"Track {index}/{len(tracks)}"
        status_message = await safe_send_message(
            bot,
            job.chat_id,
            render_status(StatusStage.QUEUED, source, detail),
            reply_to_message_id=job.original_message_id,
            allow_sending_without_reply=True,
        )
        status_message_id = status_message.message_id if status_message else 0
        if status_message_id == 0:
            LOGGER.warning(
                "Queueing collection track %s/%s without a Telegram status message",
                index,
                len(tracks),
            )
        resolved = ResolvedTrack(
            source_url=track.source_url or classified.url,
            download_url=f"ytsearch1:{track.search_query}",
            search_query=track.search_query,
            title=track.title,
            artist=track.artist,
            album=track.album,
            track_number=track.track_number,
            collection_owner=collection.owner,
            collection_title=collection.title,
        )
        child = QueuedJob(
            kind=JobKind.LINK,
            chat_id=job.chat_id,
            original_message_id=job.original_message_id,
            status_message_id=status_message_id,
            user_id=job.user_id,
            source_label=f"{source} track {index}/{len(tracks)}",
            classified_link=classified,
            resolved_track=resolved,
            parent_status_message_id=job.status_message_id,
            batch_index=index,
            batch_total=len(tracks),
        )
        position = await runtime.queue.enqueue(child)
        if status_message:
            await safe_edit_status(
                status_message, render_status(StatusStage.QUEUED, source, detail, position=position)
            )


async def retry_link_job(
    job: QueuedJob,
    runtime: BotRuntime,
    application: Application,
    request: RequestState | None,
    source: str,
    exc: BaseException,
) -> bool:
    """Update status and raise RetryJob when a link failure should retry."""
    decision = retry_decision(job, exc)
    if decision != RetryDecision.RETRY:
        return False
    delay = retry_delay_seconds(job)
    if delay is None:
        return False
    detail = retry_detail(job, delay, str(exc))
    retry_job = next_attempt(job)
    if request:
        request.stage = StatusStage.QUEUED
        request.current = job.display_title or job.source_label
        request.detail = detail
        await update_request(runtime, application, request)
    else:
        await safe_edit_message(
            application.bot,
            job.chat_id,
            job.status_message_id,
            render_status(StatusStage.QUEUED, source, detail),
        )
    # Raising this special exception hands control back to DownloadQueue, which
    # schedules the delayed retry without treating it as an unexpected failure.
    raise RetryJob(retry_job, str(exc), delay)


async def retry_collection_job(
    job: QueuedJob,
    runtime: BotRuntime,
    application: Application,
    request: RequestState | None,
    source: str,
    exc: BaseException,
) -> bool:
    """Update status and raise RetryJob when collection expansion should retry."""
    decision = retry_decision(job, exc)
    if decision != RetryDecision.RETRY:
        return False
    delay = retry_delay_seconds(job)
    if delay is None:
        return False
    detail = retry_detail(job, delay, str(exc))
    retry_job = next_attempt(job)
    if request:
        request.stage = StatusStage.QUEUED
        request.current = job.display_title or source
        request.detail = detail
        await update_request(runtime, application, request)
    else:
        await safe_edit_message(
            application.bot,
            job.chat_id,
            job.status_message_id,
            render_status(StatusStage.QUEUED, source, detail),
            reply_markup=status_keyboard(
                source_url=job.classified_link.url if job.classified_link else None
            ),
        )
    raise RetryJob(retry_job, str(exc), delay)


async def update_parent_progress(
    job: QueuedJob, runtime: BotRuntime, application: Application, outcome: str
) -> None:
    """Update the parent collection message after a child track finishes."""
    if not job.parent_status_message_id:
        return
    progress = runtime.batch_progress.get(job.parent_status_message_id)
    if not progress:
        return
    progress[outcome] = int(progress.get(outcome, 0)) + 1
    await safe_edit_message(
        application.bot,
        job.chat_id,
        job.parent_status_message_id,
        render_collection_progress(
            str(progress["source"]),
            total=int(progress["total"]),
            queued=int(progress["queued"]),
            completed=int(progress["completed"]),
            skipped=int(progress["skipped"]),
            failed=int(progress["failed"]),
        ),
    )


def job_detail(job: QueuedJob, detail: str) -> str:
    """Prefix details with track position for collection child jobs."""
    if job.batch_index and job.batch_total:
        return f"Track {job.batch_index}/{job.batch_total}\n{detail}"
    return detail
