# Changelog

All notable user-facing changes and version changes in this project are documented here.

This project maintains the changelog manually. Add future changes under `Unreleased` first,
then move them into a dated version section when `pyproject.toml` is bumped.

## Unreleased

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
