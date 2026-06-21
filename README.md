# sync-me-maybe

Telegram bot that downloads music links and Telegram audio uploads into a local music folder for Navidrome or any other library scanner.

## What It Accepts

- Telegram audio files and audio-like document uploads
- YouTube and YouTube Music track and playlist links
- Spotify track, playlist, and album links
- Apple Music track, playlist, and album links
- Shazam links
- Multiple links in one message

Spotify, Apple Music, and Shazam links are used as search inputs. The bot does not bypass DRM or download directly from those providers; it resolves a matching YouTube or YouTube Music result and stores that audio. Spotify and Apple Music playlists and albums use tokenless public extraction and may fail if the public page does not expose track data.

## Storage Behavior

- Link downloads are converted to MP3 at up to 320 kbps via `yt-dlp` and `ffmpeg`.
- Telegram uploads are stored in their original format with their original filename when available.
- Link downloads are stored directly under `MUSIC_DIR` as `Artist - Title.mp3` when artist metadata exists.
- If artist metadata is missing, link downloads are stored as `Title.mp3`.
- Existing target files are skipped instead of overwritten.
- Playlist and album links are expanded into individual track jobs, capped by `MAX_COLLECTION_TRACKS`.
- Playlist and album downloads are stored under `Owner - Collection Name` folders when provider metadata exposes a real owner. If the owner is missing or looks like a URL, the folder falls back to `Collection Name(<collection URL>)`.
- Finished Telegram requests with failed jobs show a `Rerun failed` button that queues only the failed items again.
- Telegram progress messages show collection name and owner when known, compact saved/skipped/failed/left counters, queue state, and the active track or item.

## Setup

Create a Telegram bot with BotFather, copy `.env.example` to `.env`, and fill in the required values:

```sh
TELEGRAM_BOT_TOKEN=123456:replace-me
ALLOWED_TELEGRAM_USER_IDS=123456789
MUSIC_DIR=./music
DOWNLOAD_TMP_DIR=./tmp/sync-me-maybe
MAX_DOWNLOAD_SECONDS=900
MAX_COLLECTION_TRACKS=1000
UPLOAD_BATCH_WINDOW_SECONDS=2
LOG_LEVEL=INFO
```

Find your Telegram user ID by running the bot and sending `/id`.

Install and run locally:

```sh
uv sync
uv run sync-me-maybe
```

`ffmpeg` must be available on `PATH` for audio conversion.

## Docker Deployment

Published images are available from GitHub Container Registry:

```sh
ghcr.io/artamrj/sync-me-maybe:latest
```

For a NAS, VPS, or home server deployment, copy `.env.example` to `.env` and set the Telegram token, allowed user IDs, host paths, and host UID/GID. On Linux or Synology hosts, get the UID and GID for the account that should own downloaded music:

```sh
id
```

Then start the service:

```sh
docker compose pull
docker compose up -d
docker compose logs -f
```

The compose file passes `PUID` and `PGID` into the container and mounts:

- `${MUSIC_DIR_HOST}` to `/music`
- `${DOWNLOAD_TMP_DIR_HOST}` to `/tmp/sync-me-maybe`
- `${YTDLP_COOKIES_FILE_HOST}` to `/config/cookies.txt` for optional yt-dlp cookies

Use writable host folders for the configured UID/GID. The app sets `MUSIC_DIR=/music` and `DOWNLOAD_TMP_DIR=/tmp/sync-me-maybe` inside the container, so the container remains stateless except for mounted music, temporary downloads, and optional cookies. To use cookies, set `YTDLP_COOKIES_FILE=/config/cookies.txt` and point `YTDLP_COOKIES_FILE_HOST` at the host cookies file.

The compose file intentionally does not use Docker's `user: ${PUID}:${PGID}` setting because some NAS runtimes reject that switch with `operation not permitted`. If your NAS reports `uid=1000(arta) gid=10(admin)`, use `PUID=1000` and `PGID=10`.

Tags:

- `latest`: latest image published by the release pipeline.
- `<project version>`: exact version from `pyproject.toml`, without a `v` prefix.

See [CHANGELOG.md](CHANGELOG.md) for release history and versioning notes.

## Environment Variables

- `TELEGRAM_BOT_TOKEN`: required Telegram bot token.
- `ALLOWED_TELEGRAM_USER_IDS`: required comma- or semicolon-separated Telegram user IDs allowed to use the bot.
- `MUSIC_DIR`: directory where completed files are stored. Defaults to `./music`.
- `DOWNLOAD_TMP_DIR`: directory for temporary downloads. Defaults to `./tmp/sync-me-maybe`.
- `YTDLP_COOKIES_FILE`: optional cookies file passed to `yt-dlp`.
- `MAX_DOWNLOAD_SECONDS`: maximum download duration before a job fails. Defaults to `900`.
- `MAX_COLLECTION_TRACKS`: maximum playlist or album tracks to enqueue. Defaults to `1000`.
- `UPLOAD_BATCH_WINDOW_SECONDS`: seconds to group quickly forwarded audio uploads. Defaults to `2`; set `0` to disable batching.
- `LOG_LEVEL`: Python logging level. Defaults to `INFO`.

## Bot Commands

- `/start`: show accepted inputs and authorization status.
- `/help`: show supported links and usage.
- `/id`: show your Telegram user ID for allowlist setup.
- `/health`: verify the bot can write to the music directory.
- `/queue`: show the active download and pending queue.

Incoming links and uploads are added to a global in-memory queue. The bot replies directly to the original file or link, shows the queue position, and edits that same status reply as the item moves through resolving, downloading, saving, and completion.

If a message contains multiple links, each supported link becomes its own queue item and unsupported links are reported individually.

## Development

```sh
uv sync --group dev
uv run sync-me-maybe
uv run ruff format .
uv run ruff check .
uv run mypy src
```
