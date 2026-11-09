# Changelog

All notable user-facing changes and version changes in this project are documented here.

This project maintains the changelog manually. Add future changes under `Unreleased` first,
then move them into a dated version section when `pyproject.toml` is bumped.

## Unreleased

## 0.12.0 - 2026-11-09

### Added

- Added scored YouTube Music matching for Spotify, Apple Music, Shazam, and playlist/album
  tracks. Candidates are compared on title, artist, and duration, alternate versions (live,
  cover, remix, sped up, ...) are penalized, and plain YouTube search is only a last resort.
- Added best-effort track metadata from the public Deezer API (by ISRC or scored search) and
  tag writing for every link download: title, artists, album, album artist, track/disc
  number, date, ISRC, genre, source URL, and embedded cover art (m4a, mp3, opus, ogg, flac).
- Added iTunes lookup for Apple Music track links, so `album/...?i=<id>` links search for the
  actual song instead of the album name.
- Added `AUDIO_FORMAT` (`m4a`, `opus`, `mp3`) and `METADATA_LOOKUP` settings.
- Added a Docker entrypoint that runs the bot as `PUID:PGID`.
- Added Deno to the Docker image so `yt-dlp` has the JavaScript runtime YouTube now requires.

### Changed

- Link downloads now default to `m4a` and keep YouTube's original audio stream instead of
  re-encoding to MP3 320, roughly halving file size at the same quality. Set
  `AUDIO_FORMAT=mp3` to keep the previous behavior.
- Duplicate detection for link downloads now recognizes the same track in another audio
  format and runs before downloading when provider metadata already fixes the filename.
- Direct YouTube links no longer repeat the artist in the filename
  (`Artist - Artist - Song`), and `- Topic`/`VEVO` channel suffixes are removed from artists.
- Docker containers now drop root privileges. Files created by older root-running images may
  need a one-time `chown` to `PUID:PGID` (see README).
- Updated `yt-dlp` to `2026.10.26` with its `default` extras and added `ytmusicapi` and
  `mutagen` dependencies.
- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.11.0` to `0.12.0`.

### Fixed

- Fixed `PUID`/`PGID` being passed to the container but never applied.
- Stopped `yt-dlp` download progress bars from filling container logs.

## 0.11.0 - 2026-07-20

### Added

- Added owner-managed, one-time Telegram invite links for revocable guest music submissions in
  private bot chats.
- Added `/guests` controls for creating and invalidating invites and listing or revoking active
  guests.

### Security

- Bound status callbacks and stored detail/path tokens to the user who submitted each request.

### Changed

- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.10.5` to `0.11.0`.

## 0.10.5 - 2026-06-22

### Fixed

- Kept single-track title and artist metadata in completed Telegram status messages, so Shazam
  and other track links no longer finish with only the provider name.

### Changed

- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.10.4` to `0.10.5`.

## 0.10.4 - 2026-06-22

### Fixed

- Added best-effort Spotify playlist pagination so large public playlists are no longer limited
  to the first 100 embed tracks when Spotify exposes an anonymous public token.

### Changed

- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.10.3` to `0.10.4`.

## 0.10.3 - 2026-06-22

### Fixed

- Made the received sticker delay non-blocking so queue work starts immediately while the first
  status message waits 5 seconds.
- Kept collection downloads visually in `Downloading` between child tracks instead of returning
  to `Preparing`.
- Added a small leading spacer before the remaining-time line to improve Telegram readability.

### Changed

- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.10.2` to `0.10.3`.

## 0.10.2 - 2026-06-22

### Fixed

- Made the received sticker act as a temporary acknowledgement that is deleted once the first
  Telegram status message is sent, so the status message becomes the lasting UI.

### Changed

- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.10.1` to `0.10.2`.

## 0.10.1 - 2026-06-22

### Fixed

- Changed the received acknowledgement behavior so the optional sticker is sent immediately and
  normal status text starts at `Preparing` instead of showing a visible `Received` message.
- Changed the Apple Music status icon from green apple to red apple.

### Changed

- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.10.0` to `0.10.1`.

## 0.10.0 - 2026-06-22

### Added

- Added a Telegram status lifecycle with instant `Received`, source-specific icons, optional
  `RECEIVED_STICKER_ID` acknowledgement stickers, queue-position messaging, and best-effort
  remaining time estimates.

### Changed

- Improved Telegram status text so collection names become the headline, source platform/type is
  shown separately, and active collection tracks use real metadata instead of index-only labels.
- Redesigned Telegram aggregate status messages around `Received -> Preparing -> Queued ->
  Downloading -> Completed`, with compact final summaries and no generic `Source:` or
  `Queue: active` lines.
- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.9.6` to `0.10.0`.

## 0.9.6 - 2026-06-21

### Added

- Added a final Telegram `Skipped/failed details` button that sends duplicate and failure details
  for finished requests.
- Added a final Telegram `Rerun failed` button that queues only failed jobs again.

### Fixed

- Fixed Spotify playlist and album expansion by falling back to public embed metadata when the
  main Spotify page does not expose tracks.
- Fixed collection folder naming so URL-like owners are ignored and ownerless collections fall
  back to `Collection(<collection URL>)`.

### Changed

- Improved Telegram progress text with clearer counters, active item labels, and playlist/album
  metadata when available.
- Removed the final inline paths block from Telegram status text; paths remain available through
  buttons where applicable.
- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.9.5` to `0.9.6`.

## 0.9.5 - 2026-06-21

### Added

- Added playlist and album folder storage using `Owner - Collection Name` when collection metadata is available.

### Changed

- Bumped `pyproject.toml`, `uv.lock`, and package `__version__` from `0.9.1` to `0.9.5`.

### Removed

- Removed the multi-item `Show results` status button and its callback handling.

## 0.9.1 - 2026-06-21

### Fixed

- Fixed CI formatting failure by applying Ruff formatting to `tests/test_providers.py`.

### Versioning

- Bumped `pyproject.toml` and package `__version__` from `0.9.0` to `0.9.1` for this
  bugfix release.

## 0.9.0 - 2026-06-21

### Added

- Added Telegram bot support for music downloads from YouTube and YouTube Music links.
- Added support for Spotify, Apple Music, and Shazam links by resolving provider metadata to
  matching YouTube or YouTube Music results.
- Added playlist and album expansion for supported collection links.
- Added Telegram upload handling, upload batching, request stage tracking, cancellation, and queue
  status updates.
- Added retry handling with backoff for queued job processing.
- Added Docker deployment support, Docker Compose configuration, and a GitHub Actions release
  pipeline that publishes GHCR images.
- Added architecture documentation covering project behavior, extension points, and known
  limitations.
- Added test coverage for providers, downloads, queueing, Telegram flows, configuration, storage,
  and UI message rendering.

### Changed

- Increased `MAX_COLLECTION_TRACKS` to `1000`.
- Simplified link download filenames to `Artist - Title.mp3` when artist metadata exists, otherwise
  `Title.mp3`.
- Updated environment variable defaults and documentation for local and NAS deployments.
- Updated Docker image tagging so release images use the exact project version from
  `pyproject.toml` and `latest`.
- Refactored provider, queueing, Telegram, and test structure for maintainability.

### Fixed

- Fixed environment variable documentation and user/group settings for NAS compatibility.
- Fixed CI workflow naming and dependency action versions.
- Fixed repository ignore rules for local `sync-me-maybe` runtime output.
- Fixed project description punctuation.

### Versioning

- `pyproject.toml` started at `0.1.0` in commit `b80ebb5`.
- `pyproject.toml` was bumped to `0.9.0` in commit `7cda742`.
- Docker image version tags are read from `pyproject.toml`; tags do not include a `v` prefix.
- Future version bumps must update this changelog in the same change.

## 0.1.0 - 2026-06-16

### Added

- Added the initial packaged `sync-me-maybe` Telegram bot project.
- Added Python package metadata, CLI entry point, and initial runtime structure.
- Added the initial package `__version__` value of `0.1.0`.

## Initial repository - 2026-06-16

### Added

- Created the initial repository in commit `2729dff`.
- Added the first README content for the project.
