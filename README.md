# sync-me-maybe

Comfortably synced. Telegram bot that downloads music from links and audio files directly to your NAS for imports into Navidrome!

## What It Accepts

- Telegram audio files and audio-like document uploads
- YouTube and YouTube Music track and playlist links
- Spotify track, playlist, and album links
- Apple Music track, playlist, and album links
- Shazam links
- Multiple links in one message

Spotify, Apple Music, and Shazam links are used as search inputs. The bot does not bypass DRM or download directly from those providers; it resolves a matching YouTube/YouTube Music result and stores that audio. Spotify and Apple Music playlists/albums use tokenless public extraction and may fail if the public page does not expose track data.

## Storage Behavior

- Link downloads are converted to MP3 at up to 320 kbps via `yt-dlp` and `ffmpeg`.
- Telegram uploads are stored in their original format with their original filename when available.
- Link downloads are stored as `Artist/Album/Track - Title.mp3` when album metadata exists. If the album is missing or unknown, tracks are stored directly under `Artist/Track - Title.mp3`.
- Existing target files are skipped instead of overwritten.
- Playlist and album links are expanded into individual track jobs, capped by `MAX_COLLECTION_TRACKS`.

## Setup

Create a Telegram bot with BotFather, copy `.env.example` to `.env`, and fill in:

```sh
TELEGRAM_BOT_TOKEN=123456:replace-me
ALLOWED_TELEGRAM_USER_IDS=123456789
SYNC_ME_MAYBE_VERSION=latest
PUID=1026
PGID=100
HOST_MUSIC_DIR=/volume1/music/2-library/telegram-bot
HOST_TMP_DIR=/volume1/docker/sync-me-maybe/tmp
MAX_DOWNLOAD_SECONDS=900
MAX_COLLECTION_TRACKS=100
UPLOAD_BATCH_WINDOW_SECONDS=2
LOG_LEVEL=INFO
```

Find your Telegram user ID by running the bot and sending `/id`.
On Synology, find the right user and group IDs with:

```sh
id your-synology-user
```

Start the service on your NAS or server:

```sh
docker compose up -d
```

The Compose file pulls `ghcr.io/artamrj/sync-me-maybe:${SYNC_ME_MAYBE_VERSION:-latest}` and mounts `${HOST_MUSIC_DIR}` into the container as `/music`. Mount your NAS on the host first using your preferred SMB/NFS/system mount setup, then point `HOST_MUSIC_DIR` at that mounted folder.

Do not set `MUSIC_DIR` or `DOWNLOAD_TMP_DIR` for the NAS Compose deployment. Host paths belong only in `HOST_MUSIC_DIR` and `HOST_TMP_DIR`; the container paths are fixed as `/music` and `/tmp/sync-me-maybe`.

For a full NAS `.env` reference with examples and explanations, see [DEPLOYMENT.md](DEPLOYMENT.md).

Create the host folders before starting:

```sh
mkdir -p /volume1/music/2-library/telegram-bot
mkdir -p /volume1/docker/sync-me-maybe/tmp
```

Use `SYNC_ME_MAYBE_VERSION=latest` if you want the newest image published from `main`. Use `SYNC_ME_MAYBE_VERSION=0.9.0` for a pinned release.

To update a NAS deployment:

```sh
docker compose pull
docker compose up -d
```

## Environment Variables

See [DEPLOYMENT.md](DEPLOYMENT.md) for every variable, why it exists, and an example value.

## Bot Commands

- `/start`: show accepted inputs and authorization status.
- `/help`: show supported links and usage.
- `/id`: show your Telegram user ID for allowlist setup.
- `/health`: verify the bot can write to the music directory.
- `/queue`: show the active download and pending queue.

Incoming links and uploads are added to a global in-memory queue. The bot replies directly to the original file or link, shows the queue position, and edits that same status reply as the item moves through resolving, downloading, saving, and completion.
If a message contains multiple links, each supported link becomes its own queue item and unsupported links are reported individually.

## Local Development

The default Compose file pulls the published GHCR image. For local source builds, add the dev override:

```sh
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build
```

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## Container Images

GitHub Actions publishes images to GitHub Container Registry:

- `ghcr.io/artamrj/sync-me-maybe:0.9.0`: tag matching the project version in `pyproject.toml`.
- `ghcr.io/artamrj/sync-me-maybe:latest`: latest successful build from `main`.
