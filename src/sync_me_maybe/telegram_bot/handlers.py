from __future__ import annotations

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
from sync_me_maybe.queueing.queue import JobKind, QueuedJob
from sync_me_maybe.telegram_bot.requests import (
    job_request,
    mark_request_cancelled,
    render_request_text,
    request_cancelled,
    request_keyboard,
    update_request,
)
from sync_me_maybe.telegram_bot.runtime import BotRuntime, RequestState
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
    runtime: BotRuntime = context.application.bot_data["runtime"]
    message = update.effective_message
    if message is None:
        return
    if not runtime.allowed(update):
        await message.reply_text("Not authorized.")
        return

    if message.audio or audio_document_filename(update):
        await buffer_upload(update, runtime, context.application)
        return

    text = message.text or message.caption
    urls = extract_urls(text)
    if not urls:
        await message.reply_text("Send an audio file, music link, playlist link, or album link.")
        return

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


async def enqueue_link(
    update: Update,
    runtime: BotRuntime,
    classified,
    link_index: int = 1,
    link_total: int = 1,
) -> None:
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
    runtime.requests[request_id] = request

    for request_index, (link_index, classified) in enumerate(classified_links, start=1):
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
        resolved = (
            job.resolved_track
            if isinstance(job.resolved_track, ResolvedTrack)
            else runtime.resolver.resolve(classified)
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
        await update_parent_progress(job, runtime, application, "failed")
        if request:
            if request_cancelled(request) or "Cancelled by user" in str(exc):
                await mark_request_cancelled(runtime, application, request)
            else:
                request.stage = StatusStage.FAILED
                request.failed += 1
                request.current = job.display_title or job.source_label
                request.detail = str(exc)
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
        LOGGER.exception("Link handling failed")
        await update_parent_progress(job, runtime, application, "failed")
        if request:
            request.stage = StatusStage.FAILED
            request.failed += 1
            request.current = job.display_title or job.source_label
            request.detail = f"Download failed: {exc}"
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
        if result.skipped:
            request.skipped += 1
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
        tracks = await runtime.collection_resolver.expand(classified)
        if request_cancelled(request):
            return
    except CollectionResolveError as exc:
        if request:
            request.stage = StatusStage.FAILED
            request.failed += 1
            request.current = job.display_title or source
            request.detail = str(exc)
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
            resolved = ResolvedTrack(
                source_url=track.source_url or classified.url,
                download_url=f"ytsearch1:{track.search_query}",
                search_query=track.search_query,
                title=track.title,
                artist=track.artist,
                album=track.album,
                track_number=track.track_number,
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


async def update_parent_progress(
    job: QueuedJob, runtime: BotRuntime, application: Application, outcome: str
) -> None:
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
    if job.batch_index and job.batch_total:
        return f"Track {job.batch_index}/{job.batch_total}\n{detail}"
    return detail
