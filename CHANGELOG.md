# Changelog

All notable user-facing changes and version changes in this project are documented here.

This project maintains the changelog manually. Add future changes under `Unreleased` first,
then move them into a dated version section when `pyproject.toml` is bumped.

## Unreleased

### Added

- Added a final Telegram `Skipped/failed details` button that sends duplicate and failure details
  for finished requests.

### Fixed

- Fixed Spotify playlist and album expansion by falling back to public embed metadata when the
  main Spotify page does not expose tracks.

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
