# sync-me-maybe Architecture

This document describes the project from technical, functional, and file-ownership perspectives. It is based on the current source tree under `src/sync_me_maybe`.

## 1. Project Purpose

`sync-me-maybe` is a Telegram bot that accepts music-related input and stores audio files in a local music folder, suitable for Navidrome or any other library scanner.

The bot accepts:

- Telegram audio uploads.
- Telegram document uploads that look like audio.
- YouTube and YouTube Music track links.
- YouTube playlist links.
- Spotify track, playlist, and album links.
- Apple Music track, playlist, and album links.
- Shazam track links.
- Multiple supported links in one Telegram message.

Important provider behavior:

- YouTube and YouTube Music are used as actual download sources through `yt-dlp`.
- Spotify, Apple Music, and Shazam are not downloaded directly. They are converted into search queries, then downloaded from YouTube or YouTube Music search results.
- Spotify and Apple Music collections are expanded through public metadata extraction. This may fail if the public page does not expose track data.
- The project does not bypass DRM.

## 2. Top-Level Repository Layout

```text
.
├── ARCHITECTURE.md
├── LICENSE
├── README.md
├── pyproject.toml
├── uv.lock
└── src/
    └── sync_me_maybe/
        ├── __init__.py
        ├── auth.py
        ├── config.py
        ├── main.py
        ├── library/
        ├── music/
        ├── queueing/
        ├── telegram_bot/
        └── ui/
```

The package is installed from `src/` using setuptools. The console script is:

```toml
sync-me-maybe = "sync_me_maybe.main:main"
```

## 3. Python Package Areas

### `sync_me_maybe.main`

File: `src/sync_me_maybe/main.py`

This is the process entrypoint.

Responsibilities:

- Load settings from environment variables with `Settings.from_env()`.
- Configure Python logging.
- Create the configured music and temp directories.
- Build the Telegram application.
- Start Telegram polling with `run_polling(allowed_updates=Update.ALL_TYPES)`.

Failure behavior:

- Missing or invalid environment configuration exits with `SystemExit`.
- Directory permission problems exit with a human-readable permissions message.

### `sync_me_maybe.config`

File: `src/sync_me_maybe/config.py`

This module owns runtime configuration.

Main names:

- `ConfigError`: raised when configuration is invalid.
- `parse_user_ids(raw)`: parses comma- or semicolon-separated Telegram user IDs.
- `Settings`: frozen dataclass containing all runtime settings.

Settings fields:

- `telegram_bot_token: str`
- `allowed_telegram_user_ids: set[int]`
- `music_dir: Path`
- `download_tmp_dir: Path`
- `ytdlp_cookies_file: Path | None`
- `max_download_seconds: int`
- `max_collection_tracks: int`
- `upload_batch_window_seconds: float`
- `log_level: str`

Environment variables:

- `TELEGRAM_BOT_TOKEN`: required.
- `ALLOWED_TELEGRAM_USER_IDS`: required.
- `MUSIC_DIR`: default `./music`.
- `DOWNLOAD_TMP_DIR`: default `./tmp/sync-me-maybe`.
- `YTDLP_COOKIES_FILE`: optional.
- `MAX_DOWNLOAD_SECONDS`: default `900`.
- `MAX_COLLECTION_TRACKS`: default `1000`.
- `UPLOAD_BATCH_WINDOW_SECONDS`: default `2`.
- `LOG_LEVEL`: default `INFO`.

Validation:

- Bot token must be present.
- At least one allowed Telegram user ID must be present.
- Numeric limits must parse as integers or floats.
- Upload batch window must be non-negative.

### `sync_me_maybe.auth`

File: `src/sync_me_maybe/auth.py`

This module contains allowlist authorization logic.

Main name:

- `is_allowed(user_id, allowed_user_ids)`: returns true only when a Telegram user ID exists and is in the configured allowlist.

## 4. Telegram Bot Layer

Directory: `src/sync_me_maybe/telegram_bot`

This package owns Telegram-specific behavior: application setup, commands, callback buttons, message routing, upload handling, status updates, and safe API wrappers.

### `telegram_bot.app`

File: `src/sync_me_maybe/telegram_bot/app.py`

Main name:

- `build_application(settings) -> Application`

Responsibilities:

- Create a `BotRuntime`.
- Build the `python-telegram-bot` `Application`.
- Store runtime in `application.bot_data["runtime"]`.
- Register command handlers:
  - `/start`
  - `/help`
  - `/id`
  - `/health`
  - `/queue`
- Register callback query handler.
- Register a catch-all non-command message handler.
- Start the queue worker in `post_init`.
- Register Telegram bot command descriptions.
- Stop the queue worker in `post_shutdown`.

The bot runs in polling mode rather than webhook mode.

### `telegram_bot.runtime`

File: `src/sync_me_maybe/telegram_bot/runtime.py`

This module holds shared in-memory runtime state.

Main names:

- `RequestState`
- `BufferedUpload`
- `UploadBatch`
- `BotRuntime`

`RequestState` tracks one user-visible request. A request can represent one upload, many uploads, one link, many links, or an expanded collection.

Important `RequestState` fields:

- `id`: request ID used by callbacks and jobs.
- `chat_id`: Telegram chat.
- `status_message_id`: Telegram message being edited for status.
- `title`: display title.
- `total`: total expected items.
- `source_urls`: original URLs.
- `completed`: count of stored files.
- `skipped`: count of duplicate files.
- `failed`: count of failed jobs.
- `current`: current item or status detail.
- `detail`: extra error or progress detail.
- `collection_title`: playlist or album title shown in status messages when known.
- `collection_owner`: playlist or album owner shown in status messages when known.
- `source_label`: provider and link type shown in status messages when known.
- `stage`: current `StatusStage`.
- `paths`: stored/skipped relative paths.
- `issue_details`: skipped and failed details shown from final status buttons.
- `job_ids`: queue job IDs belonging to this request.
- `failed_jobs`: cloneable failed jobs used by the final rerun button.
- `cancelled`: cancellation flag.
- `cancel_event`: thread-safe event used by blocking downloader code.

`BotRuntime` owns:

- `settings`
- `path_callbacks`: in-memory token-to-path/result data for inline buttons.
- `issue_callbacks`: in-memory token-to-skipped/failed details.
- `rerun_failed_callbacks`: in-memory token-to-failed-job retry data.
- `queue`: the global `DownloadQueue`.
- `resolver`: `LinkResolver` for single-track links.
- `collection_resolver`: `CollectionResolver` for playlists/albums.
- `batch_progress`: legacy/secondary collection progress map.
- `requests`: request ID to `RequestState`.
- `upload_batches`: short-window upload grouping.
- `downloader`: `YtDlpDownloader`.

Important methods:

- `allowed(update)`: checks Telegram user allowlist.
- `remember_path(relative_path)`: creates a short callback token for path display.
- `remember_results(request)`: creates a callback token for multi-result display.
- `process_job(job, application)`: dispatches queue jobs by kind.

Runtime state is in-memory only. Restarting the process clears queue, request state, callback tokens, upload batches, and progress state.

### `telegram_bot.commands`

File: `src/sync_me_maybe/telegram_bot/commands.py`

Command handlers:

- `start(update, context)`: shows welcome text and authorization status.
- `help_command(update, context)`: explains accepted inputs.
- `user_id(update, context)`: returns the Telegram user ID.
- `health(update, context)`: checks write access to `MUSIC_DIR`.
- `queue_command(update, context)`: shows active and pending queue state.

Authorization:

- `/start`, `/help`, and `/id` are allowed for everyone.
- `/health` and `/queue` require the configured allowlist.

### `telegram_bot.handlers`

File: `src/sync_me_maybe/telegram_bot/handlers.py`

This is the main inbound message and link processing module.

Main inbound function:

- `handle_message(update, context)`

Routing behavior:

1. Read `BotRuntime` from `context.application.bot_data["runtime"]`.
2. Ignore empty message updates.
3. Reject unauthorized users with `Not authorized.`.
4. If the message contains Telegram audio or an audio-like document, send it to upload buffering.
5. Otherwise extract URLs from text or caption.
6. If no URLs are found, ask the user to send an audio file or supported link.
7. Classify each URL.
8. Unsupported URLs are reported.
9. Supported track links become link jobs.
10. Supported playlist/album links become collection jobs.
11. Multiple links become one aggregate request with multiple jobs.

Main enqueue functions:

- `enqueue_link(...)`
- `enqueue_collection(...)`
- `enqueue_link_batch(...)`

Main processor functions:

- `process_link_job(job, runtime, application)`
- `process_collection_job(job, runtime, application)`
- `update_parent_progress(job, runtime, application, outcome)`
- `job_detail(job, detail)`

Single link job flow:

1. Mark request as preparing.
2. Resolve source into a `ResolvedTrack`.
3. Mark request as downloading.
4. Download through `YtDlpDownloader`.
5. Compute final path with `track_destination`.
6. Mark request as saving.
7. Move completed file into the library with `store_completed_file`.
8. Update counts and paths.
9. Render final success, skipped, or failed state.

Collection job flow:

1. Mark request as expanding.
2. Expand playlist/album into `TrackSearchItem` objects.
3. Increase request total to account for child tracks.
4. Convert each track into a `ResolvedTrack` using `ytsearch1:<artist title>`.
5. Enqueue child `LINK` jobs.
6. Child jobs reuse the normal link pipeline.

Cancellation:

- The callback layer can set `RequestState.cancelled` and `cancel_event`.
- Pending queue jobs for the request are removed.
- Active downloader jobs receive `cancel_check` and can abort during progress hooks.
- Cancelled partial download files are deleted.

### `telegram_bot.uploads`

File: `src/sync_me_maybe/telegram_bot/uploads.py`

This module handles Telegram audio uploads and document uploads that look like audio.

Main names:

- `AUDIO_EXTENSIONS`
- `buffer_upload(update, runtime, application)`
- `flush_upload_batch_after_delay(runtime, application, key)`
- `enqueue_upload_batch(runtime, application, batch)`
- `enqueue_upload_request(...)`
- `upload_job_from_buffered(...)`
- `process_upload_job(job, runtime, application)`
- `audio_document_filename(update)`

Upload detection:

- Native Telegram `message.audio` is accepted.
- `message.document` is accepted when MIME type starts with `audio/` or filename suffix is one of:
  - `.aac`
  - `.aiff`
  - `.alac`
  - `.flac`
  - `.m4a`
  - `.mp3`
  - `.ogg`
  - `.opus`
  - `.wav`
  - `.wma`

Upload batching:

- Controlled by `UPLOAD_BATCH_WINDOW_SECONDS`.
- If set to `0`, each upload becomes a request immediately.
- If positive, uploads from the same `(chat_id, user_id)` inside the window are grouped into one request.
- A new upload cancels and resets the previous flush task.

Upload job flow:

1. Determine original filename.
2. Build `UploadPayload`.
3. Create or update an upload request.
4. Enqueue one `UPLOAD` job per file.
5. For each upload job:
   - Compute final path with `upload_destination`.
   - Skip immediately if destination exists.
   - Download Telegram file to temp path.
   - Move it into the music directory with `store_completed_file`.
   - Update request progress.

Unlike link downloads, Telegram uploads keep their original format and filename when possible.

### `telegram_bot.requests`

File: `src/sync_me_maybe/telegram_bot/requests.py`

This module renders and updates aggregate request state.

Main names:

- `request_position(runtime, request)`: finds the best queue position across all jobs in a request.
- `render_request_text(runtime, request)`: converts `RequestState` into a user-facing message.
- `request_keyboard(runtime, request)`: builds inline buttons for source, refresh, stop, path, and results.
- `update_request(runtime, application, request)`: edits the Telegram status message.
- `job_request(runtime, job)`: maps a job back to its request.
- `request_cancelled(request)`: cancellation helper.
- `mark_request_cancelled(...)`: sets cancellation state and updates Telegram.

The request layer is the bridge between queue/job state and user-visible Telegram status messages.

### `telegram_bot.callbacks`

File: `src/sync_me_maybe/telegram_bot/callbacks.py`

This module handles inline keyboard callbacks.

Supported callback data:

- `health`: run write probe against `MUSIC_DIR`.
- `path:<token>`: show stored relative path.
- `issues:<token>`: send skipped/failed details.
- `rerun_failed:<token>`: create a new request for only failed jobs.
- `refresh:<request_id>`: re-render current request status.
- `cancel:<request_id>`: cancel pending jobs and signal active job cancellation.

Callback data is resolved against in-memory runtime dictionaries. Old buttons stop working after process restart.

### `telegram_bot.safe_api`

File: `src/sync_me_maybe/telegram_bot/safe_api.py`

This module wraps Telegram API calls so transient Telegram failures do not crash processing.

Main names:

- `telegram_call(description, operation, attempts=3)`
- `safe_edit_message(...)`
- `safe_edit_status(...)`
- `safe_send_message(...)`
- `safe_chat_action(...)`

Handled Telegram failures:

- `RetryAfter`: waits for the Telegram-provided retry delay plus a small buffer.
- `TimedOut` and `NetworkError`: retries with exponential backoff.
- `BadRequest` with "message is not modified": ignored.
- Other `BadRequest` and `TelegramError`: logged and converted to `None`.

## 5. Queueing Layer

Directory: `src/sync_me_maybe/queueing`

### `queueing.queue`

File: `src/sync_me_maybe/queueing/queue.py`

This module owns serialized job execution.

Main names:

- `JobKind`
- `UploadPayload`
- `QueuedJob`
- `QueueSnapshot`
- `DownloadQueue`
- `render_queue_snapshot(snapshot, limit=5)`

`JobKind` values:

- `LINK`
- `UPLOAD`
- `COLLECTION`

`QueuedJob` carries all data needed to process one queued unit:

- Telegram chat/message IDs.
- User ID.
- Source label.
- Optional classified link.
- Optional upload payload.
- Optional pre-resolved track.
- Parent collection progress metadata.
- Aggregate request metadata.
- Display title.
- Retry metadata:
  - current attempt
  - maximum attempts
  - retry backoff seconds

`DownloadQueue` implementation:

- Uses an in-memory `collections.deque`.
- Uses one `asyncio.Condition`.
- Has one background worker task.
- Processes exactly one job at a time.
- Keeps an `_active` job for status and queue position.
- Tracks delayed retry tasks for jobs waiting on backoff.
- Catches unexpected job exceptions so the worker survives individual failures.

Queue operations:

- `enqueue(job) -> int`: append pending job and return queue position.
- `snapshot() -> QueueSnapshot`: read active and pending jobs.
- `position_of(job_id) -> int | None`: find active or pending position.
- `cancel_request(request_id) -> int`: remove pending and delayed retry jobs belonging to a request.
- `retry_later(job, delay_seconds)`: re-enqueue a job after retry backoff.
- `start(processor)`: start the worker.
- `stop()`: cancel the worker at shutdown.

The queue and delayed retries are not persisted. They are lost on restart.

## 6. Music Layer

Directory: `src/sync_me_maybe/music`

This package owns URL extraction, provider dispatch, metadata resolution, collection expansion, filename cleanup, and audio download.

### `music.urls`

File: `src/sync_me_maybe/music/urls.py`

Main names:

- `LinkKind`
- `LinkScope`
- `ClassifiedLink`
- `extract_first_url(text)`
- `extract_urls(text)`
- `classify_url(url)`

`LinkKind` values:

- `YOUTUBE`
- `SPOTIFY`
- `APPLE_MUSIC`
- `SHAZAM`
- `UNSUPPORTED`

`LinkScope` values:

- `TRACK`
- `PLAYLIST`
- `ALBUM`

URL extraction:

- Uses a regex for `http://` and `https://` URLs.
- Strips trailing punctuation like `.`, `,`, `;`, and `]`.
- Deduplicates URLs inside one message while preserving order.

Classification:

- `classify_url` delegates provider-specific host and scope detection to the provider registry.
- The current registry order is YouTube, Spotify, Apple Music, and Shazam.

- YouTube hosts:
  - `youtu.be`
  - `youtube.com`
  - `m.youtube.com`
  - `music.youtube.com`
- Spotify hosts:
  - `open.spotify.com`
  - `spotify.link`
- Apple hosts:
  - `music.apple.com`
  - `itunes.apple.com`
- Shazam hosts:
  - any host ending in `shazam.com`

Scope detection:

- YouTube is a playlist when query contains `list` but not `v`.
- Spotify path `/playlist/` means playlist.
- Spotify path `/album/` means album.
- Apple path `/album/` without query parameter `i` means album.
- Apple path `/playlist/` means playlist.
- Otherwise supported links default to track scope.

### `music.resolver`

File: `src/sync_me_maybe/music/resolver.py`

This module is a compatibility facade for resolving single-track links into download instructions.

Main names:

- `ResolvedTrack`
- `ResolveError`
- `LinkResolver`

`ResolvedTrack` fields:

- `source_url`
- `download_url`
- `search_query`
- `title`
- `artist`
- `album`
- `track_number`

Resolver behavior:

- `LinkResolver` delegates provider-specific resolution to the provider registry.
- YouTube links return `download_url=classified.url`.
- Spotify, Apple Music, and Shazam links become YouTube search URLs using `ytsearch1:<query>`.
- Spotify first tries `https://open.spotify.com/oembed`.
- Spotify falls back to page metadata or URL slug parsing.
- Apple Music uses album slug or general URL slug parsing.
- Shazam uses track slug parsing.
- Numeric Shazam track URLs can be fetched and parsed from page metadata.

Metadata cleanup:

- `clean_title` removes provider suffixes such as "on Spotify" or trailing provider names.
- Slug cleanup removes common URL noise, numeric IDs, repeated whitespace, underscores, and hyphens.

Failures:

- If no query can be built, `ResolveError` is raised.
- Unsupported links raise `ResolveError`.

### `music.collections`

File: `src/sync_me_maybe/music/collections.py`

This module is a compatibility facade for expanding playlist and album links into individual track search items.

Main names:

- `CollectionResolveError`
- `TrackSearchItem`
- `CollectionResolver`

`TrackSearchItem` fields:

- `title`
- `artist`
- `album`
- `track_number`
- `source_url`

`TrackSearchItem.search_query` joins artist and title when available.

Expansion behavior:

- Track-scoped links are rejected.
- `CollectionResolver` delegates provider-specific expansion to the provider registry.
- Empty collection and `MAX_COLLECTION_TRACKS` checks are enforced centrally here.
- YouTube playlists are expanded with `yt-dlp` flat playlist extraction.
- Spotify collections use public extraction and retry the public embed page when the main
  collection page does not expose tracks.
- Apple Music collections use public extraction.
- Shazam collections are not supported.

Public collection extraction attempts:

1. `yt-dlp` flat public metadata extraction.
2. HTML fetch with `requests`.
3. JSON extraction from `<script>` tags with JSON type or `__NEXT_DATA__`.
4. Fallback balanced JSON object extraction around track-related markers.
5. Recursive walk looking for track-like dictionaries.

Safety limit:

- If extracted track count exceeds `MAX_COLLECTION_TRACKS`, expansion fails.

Provider-local dedupe:

- Tracks are deduplicated by case-insensitive `(artist, title)`.

### `music.providers`

Directory: `src/sync_me_maybe/music/providers`

This package owns provider-specific classification, track resolution, and collection expansion.

Main files:

- `base.py`: provider protocol, shared provider errors, `ResolvedTrack`, and `TrackSearchItem`.
- `registry.py`: explicit provider construction and kind-based lookup.
- `youtube.py`: YouTube classification, direct track resolution, and playlist expansion.
- `spotify.py`: Spotify classification, oEmbed/page/slug track resolution, and collection expansion.
- `apple.py`: Apple Music classification, slug-based track resolution, and collection expansion.
- `shazam.py`: Shazam classification and track resolution.
- `public_scrape.py`: shared best-effort public collection extraction for Spotify and Apple Music.

Provider interface:

```python
class Provider:
    kind: LinkKind

    def classify(url) -> ClassifiedLink | None: ...
    async def resolve_track(link) -> ResolvedTrack: ...
    async def expand_collection(link) -> list[TrackSearchItem]: ...
```

Provider implementation details:

- Blocking metadata, scraping, and `yt-dlp` work is wrapped by provider methods with `asyncio.to_thread`.
- Unsupported provider capabilities raise provider errors that resolver facades convert into user-facing resolver errors.
- Spotify and Apple public-page scraping is isolated under provider code because those page structures are fragile.

### `music.downloader`

File: `src/sync_me_maybe/music/downloader.py`

This module downloads and converts audio through `yt-dlp`.

Main names:

- `DownloadError`
- `DownloadedTrack`
- `YtDlpDownloader`

Download behavior:

- Runs blocking `yt-dlp` work in a thread via `asyncio.to_thread`.
- Uses `bestaudio/best`.
- Disables playlist downloads with `noplaylist=True`.
- Outputs to a unique temp filename in `DOWNLOAD_TMP_DIR`.
- Converts to MP3 using `FFmpegExtractAudio`.
- Uses preferred MP3 quality `320`.
- Supports optional cookies file.
- Uses a 30-second socket timeout.
- Enforces `MAX_DOWNLOAD_SECONDS` through progress hooks.
- Supports cancellation through a callable `cancel_check`.

Temporary file behavior:

- Each download uses a UUID-based output template.
- Partial files matching that UUID are deleted on cancellation, timeout, or `yt-dlp` failure.
- Final output is expected as `.mp3`, but the code falls back to any matching extension if necessary.

Track metadata:

- `_track_info(info, resolved)` builds `TrackInfo`.
- Explicit resolved metadata wins over `yt-dlp` metadata.
- Title fallback order: resolved title, `info["track"]`, `info["title"]`.
- Artist fallback order: resolved artist, `info["artist"]`, `info["uploader"]`.
- Album fallback order: resolved album, `info["album"]`.
- Track number fallback order: resolved track number, `info["track_number"]`.
- YouTube noise such as "official video" or "lyrics" is stripped from fallback titles.

### `music.filenames`

File: `src/sync_me_maybe/music/filenames.py`

This module normalizes names for safe filesystem use.

Main names:

- `sanitize_filename(value, fallback="Unknown")`
- `clean_title(value)`

`sanitize_filename`:

- Normalizes Unicode with NFKC.
- Replaces invalid filename characters with `_`.
- Collapses whitespace.
- Strips trailing/leading spaces and dots.
- Limits length to 180 characters.
- Uses a fallback when empty.

Invalid characters include:

```text
< > : " / \ | ? * and ASCII control characters
```

`clean_title` removes provider-specific title suffixes.

## 7. Library Storage Layer

Directory: `src/sync_me_maybe/library`

### `library.storage`

File: `src/sync_me_maybe/library/storage.py`

This module owns final filesystem paths and moving completed files.

Main names:

- `TrackInfo`
- `StoreResult`
- `track_destination(music_dir, info, extension=".mp3")`
- `upload_destination(music_dir, filename)`
- `store_completed_file(source, destination, music_dir, skip_existing=True)`

Link download destination format:

```text
Artist - Title.mp3
Title.mp3
Owner - Collection/Artist - Title.mp3
Collection(<collection URL>)/Artist - Title.mp3
```

Rules:

- Artist is omitted from the filename when missing.
- Title fallback: `Unknown Title`.
- Collection folders use `Owner - Collection` when the owner is a real display name.
- URL-like collection owners are ignored.
- When a collection owner is missing or URL-like and the source URL is available, collection folders use `Collection(<collection URL>)`.

Upload destination format:

```text
<original-safe-filename>
```

Uploads are placed directly under `MUSIC_DIR`.

Duplicate behavior:

- `store_completed_file` skips existing destination files by default.
- If skipped, the temp source file is deleted.
- Existing files are not overwritten.

Return value:

- `StoreResult.path`: absolute or configured destination path.
- `StoreResult.relative_path`: path relative to `MUSIC_DIR` when possible.
- `StoreResult.skipped`: whether an existing file was skipped.

## 8. UI Rendering Layer

Directory: `src/sync_me_maybe/ui`

### `ui.messages`

File: `src/sync_me_maybe/ui/messages.py`

This module owns Telegram message text and inline keyboard construction.

Main names:

- `StatusStage`
- `RequestView`
- `render_welcome(authorized)`
- `render_help()`
- `render_status(stage, source, detail=None, position=None)`
- `render_success(relative_path, skipped=False)`
- `render_error(message)`
- `render_collection_progress(...)`
- `progress_bar(done, total, width=10)`
- `render_counters(total, completed, skipped, failed)`
- `render_request(view)`
- `status_keyboard(...)`

`StatusStage` values:

- `QUEUED`
- `THINKING`
- `DOWNLOADING`
- `SAVING`
- `EXPANDING`
- `DONE`
- `SKIPPED`
- `FAILED`
- `CANCELLED`

The UI layer is intentionally simple: it returns plain Telegram text and `InlineKeyboardMarkup`. It does not own business state.

Inline buttons can include:

- Open source URL.
- Stop request.
- Refresh status.
- Show stored path.
- Show multi-result paths.
- Health check.

## 9. End-to-End Runtime Lifecycle

Startup:

1. `sync_me_maybe.main.main()` runs.
2. `Settings.from_env()` reads and validates environment.
3. Logging is configured.
4. `MUSIC_DIR` and `DOWNLOAD_TMP_DIR` are created.
5. `build_application(settings)` creates the Telegram application.
6. `BotRuntime(settings)` creates shared runtime objects.
7. Telegram handlers are registered.
8. `Application.run_polling(...)` starts polling.
9. `post_init` starts the queue worker and registers bot commands.

Message handling:

1. Telegram update reaches `handle_message`.
2. Runtime allowlist is checked.
3. Message is classified as upload, link text, or unsupported input.
4. A `RequestState` is created.
5. One or more `QueuedJob` objects are enqueued.
6. The queue worker serially processes jobs.
7. Status message is edited as work progresses.
8. Final files are moved to `MUSIC_DIR`.
9. Final status stays compact and exposes paths, issue details, and reruns through optional buttons.

Shutdown:

1. `post_shutdown` calls `runtime.queue.stop()`.
2. Queue worker task is cancelled.
3. In-memory state disappears with the process.

## 10. Functional Flows

### Single Telegram Upload

```text
Telegram audio/document
→ handle_message
→ buffer_upload
→ enqueue_upload_request or upload batch
→ DownloadQueue.enqueue(UPLOAD)
→ process_upload_job
→ bot.get_file(file_id)
→ download_to_drive(temp_path)
→ upload_destination
→ store_completed_file
→ update_request
```

### Batched Telegram Uploads

```text
Multiple uploads from same chat/user
→ buffer_upload
→ runtime.upload_batches[(chat_id, user_id)]
→ delayed flush task
→ enqueue_upload_batch
→ one UPLOAD job per file
→ aggregate RequestState progress
```

### YouTube Track Link

```text
Message text
→ extract_urls
→ classify_url: YOUTUBE/TRACK
→ enqueue_link
→ process_link_job
→ LinkResolver.resolve returns direct URL
→ YtDlpDownloader.download
→ track_destination
→ store_completed_file
```

### Spotify, Apple Music, or Shazam Track Link

```text
Message text
→ extract_urls
→ classify_url
→ enqueue_link
→ process_link_job
→ LinkResolver.resolve
→ metadata or slug becomes search query
→ download_url = ytsearch1:<query>
→ YtDlpDownloader.download
→ track_destination
→ store_completed_file
```

### Playlist or Album Link

```text
Message text
→ extract_urls
→ classify_url with PLAYLIST or ALBUM scope
→ enqueue_collection
→ process_collection_job
→ CollectionResolver.expand
→ list[TrackSearchItem]
→ create child LINK jobs with prebuilt ResolvedTrack
→ each child uses normal link download flow
```

### Multiple Links in One Message

```text
Message text with N URLs
→ extract_urls
→ classify each URL
→ unsupported links counted as failed
→ supported links become LINK or COLLECTION jobs
→ one aggregate RequestState tracks total progress
```

## 11. Data Model Summary

Important dataclasses:

- `Settings`: environment-derived process configuration.
- `ClassifiedLink`: provider kind, URL, link scope, unsupported reason.
- `ResolvedTrack`: source URL, actual `yt-dlp` download URL, search query, metadata.
- `TrackSearchItem`: collection-expanded title/artist/album/track data.
- `DownloadedTrack`: temp file and `TrackInfo`.
- `TrackInfo`: final library metadata.
- `StoreResult`: final path, relative path, skipped flag.
- `UploadPayload`: Telegram file identifiers and filename.
- `QueuedJob`: one unit of queued work.
- `QueueSnapshot`: active and pending queue view.
- `RequestState`: aggregate user-visible request state.
- `BufferedUpload`: upload waiting inside a batch window.
- `UploadBatch`: batch state and delayed flush task.
- `RequestView`: render-only projection of request state.

## 12. External Dependencies

Declared in `pyproject.toml`:

- `beautifulsoup4`: HTML parsing for metadata and public collection extraction.
- `python-telegram-bot`: Telegram bot framework.
- `requests`: HTTP fetching for metadata and public pages.
- `yt-dlp`: YouTube/search download and some playlist metadata extraction.

Required system dependency:

- `ffmpeg`: required by `yt-dlp` postprocessor for MP3 extraction.

Development dependencies:

- `ruff`
- `mypy`

Python version:

- `>=3.11`

## 13. Build, Lint, and Type Configuration

File: `pyproject.toml`

Build system:

- `setuptools>=69`
- `wheel`
- `setuptools.build_meta`

Package discovery:

```toml
[tool.setuptools.packages.find]
where = ["src"]
```

Ruff:

- Line length: `100`
- Target Python: `py311`
- Selected rules: `E`, `F`, `I`, `UP`, `B`, `BLE`
- Ignored rule: `B008`
- Double quotes.
- Space indentation.

Mypy:

- Python version: `3.11`
- Files: `src/sync_me_maybe`
- `ignore_missing_imports = true`
- `warn_unused_configs = true`
- `check_untyped_defs = true`
- `no_implicit_optional = true`
- `disallow_untyped_defs = false`

## 14. Error Handling Strategy

Configuration errors:

- Raised as `ConfigError`.
- Converted to process exit in `main`.

Telegram API errors:

- Wrapped by `safe_api`.
- Retries transient rate limit and network failures.
- Logs permanent failures without crashing job processing.

Resolver errors:

- `ResolveError` for single-link metadata/query failures.
- Retryable resolver errors preserve a retryable flag from provider failures.
- Rendered as failed request state.

Collection errors:

- `CollectionResolveError` for unsupported or unexpandable collections.
- Retryable collection errors preserve a retryable flag from provider failures.
- Rendered as failed request state.

Download errors:

- `DownloadError` for `yt-dlp`, timeout, missing result, and cancellation failures.
- Temporary `yt-dlp` and timeout errors are retryable.
- Missing search results, missing output files, unsupported inputs, and cancellation are not retryable.
- Partial files are cleaned up.

Per-job retry behavior:

- Retryable failures are retried up to three attempts.
- Backoff delays are 30 seconds, 2 minutes, and 10 minutes.
- Request failure counters are updated only after the final failed attempt.
- Cancelled jobs and permanent failures are not retried.

Unexpected job errors:

- Caught inside processors or queue worker.
- Logged.
- The queue worker continues processing later jobs.

## 15. State, Persistence, and Concurrency

Persistent state:

- Only completed audio files in `MUSIC_DIR`.
- Temp files may briefly exist in `DOWNLOAD_TMP_DIR`.

In-memory state:

- Queue.
- Active job.
- Request progress.
- Callback tokens.
- Upload batch buffers.
- Collection progress.

Concurrency model:

- Telegram handlers are async.
- Queue processing is serialized by one queue worker.
- Blocking resolver/downloader/collection work is run in threads where needed.
- Upload batch flushes use `asyncio.Task`.
- Cancellation is coordinated with a boolean flag and `threading.Event`.

Consequences:

- Only one download/upload job is processed at a time.
- Queue positions are simple and predictable.
- Restarting the bot loses pending jobs and button callback memory.
- A long active job can delay all later jobs.

## 16. Security and Access Control

Authorization:

- Access is controlled by `ALLOWED_TELEGRAM_USER_IDS`.
- Message handling rejects unauthorized users.
- Callback handling rejects unauthorized users.
- Sensitive operational commands `/health` and `/queue` require authorization.

Filesystem safety:

- Filenames are sanitized before writing.
- Link downloads are placed under artist/album directories.
- Uploads are placed directly under `MUSIC_DIR` using sanitized original filenames.
- Existing files are skipped rather than overwritten.

Network behavior:

- The bot fetches public provider metadata using `requests`.
- The bot invokes `yt-dlp`, which may contact YouTube, provider pages, and search endpoints.
- Optional cookies can be supplied to `yt-dlp`.

## 17. Known Limitations

- Queue and request state are not persisted.
- Callback tokens are lost on restart.
- There is only one worker, so large playlists are processed serially.
- Spotify and Apple Music collection expansion is best-effort and can break when public page structures change.
- Search-based provider resolution can download an incorrect YouTube match.
- Telegram uploads are not transcoded or metadata-normalized.
- Provider refactor tests cover classification, resolver fallbacks, public scraping helpers, YouTube playlist expansion, and collection limits.
- `src/sync_me_maybe/__init__.py` reports `__version__ = "0.1.0"` while `pyproject.toml` declares project version `0.9.0`.

## 18. Extension Points

Add a new provider:

1. Add a `LinkKind` value in `music.urls`.
2. Add a provider module under `music.providers`.
3. Implement provider classification, `resolve_track`, and `expand_collection`.
4. Register the provider in `music.providers.registry`.
5. Update README/help text if user-facing support changes.

Add another storage layout:

1. Change or wrap `track_destination`.
2. Keep `store_completed_file` behavior consistent with duplicate handling.
3. Update README and this document.

Add persistent queueing:

1. Replace or extend `DownloadQueue`.
2. Persist `QueuedJob` and `RequestState`.
3. Rehydrate active/pending jobs at startup.
4. Redesign callback token storage or derive callback content from persisted request state.

Add parallel downloads:

1. Replace the single `_active` job with multiple active workers.
2. Change queue position semantics.
3. Make request updates safe against more concurrent edits.
4. Consider Telegram edit rate limits.

Add tests:

High-value areas:

- URL extraction and classification.
- Filename sanitization.
- Storage destination paths.
- Resolver slug and metadata parsing.
- Collection JSON walking and dedupe.
- Queue cancellation.
- Request rendering.

## 19. Quick File Ownership Map

```text
src/sync_me_maybe/main.py
  Process startup and Telegram polling.

src/sync_me_maybe/config.py
  Environment parsing and validation.

src/sync_me_maybe/auth.py
  Telegram allowlist check.

src/sync_me_maybe/telegram_bot/app.py
  Application construction and handler registration.

src/sync_me_maybe/telegram_bot/runtime.py
  Shared runtime objects and in-memory state.

src/sync_me_maybe/telegram_bot/commands.py
  Slash command handlers.

src/sync_me_maybe/telegram_bot/handlers.py
  Message routing, link jobs, collection jobs.

src/sync_me_maybe/telegram_bot/uploads.py
  Telegram upload detection, batching, and processing.

src/sync_me_maybe/telegram_bot/requests.py
  Aggregate request rendering and update helpers.

src/sync_me_maybe/telegram_bot/callbacks.py
  Inline keyboard callback behavior.

src/sync_me_maybe/telegram_bot/safe_api.py
  Telegram API retry and error wrappers.

src/sync_me_maybe/queueing/queue.py
  In-memory serialized job queue.

src/sync_me_maybe/music/urls.py
  URL extraction and classification dispatch.

src/sync_me_maybe/music/resolver.py
  Single-link provider resolution facade.

src/sync_me_maybe/music/collections.py
  Playlist and album provider expansion facade.

src/sync_me_maybe/music/providers/
  Provider-specific classification, metadata lookup, scraping, and collection expansion.

src/sync_me_maybe/music/downloader.py
  yt-dlp download and MP3 conversion.

src/sync_me_maybe/music/filenames.py
  Title cleanup and filesystem-safe filenames.

src/sync_me_maybe/library/storage.py
  Final destination paths and file moves.

src/sync_me_maybe/ui/messages.py
  Telegram text rendering and inline keyboards.
```

## 20. Architecture Summary

`sync-me-maybe` is structured as a small layered bot:

```text
Telegram updates
→ telegram_bot handlers/commands/callbacks
→ runtime request state
→ in-memory serialized queue
→ music resolver / collection resolver / downloader
→ library storage
→ Telegram status updates
```

The central design choice is a single in-memory runtime with a single queue worker. This keeps the implementation understandable and avoids concurrent file writes or conflicting Telegram status edits. The tradeoff is that all work is serialized and volatile across restarts.
