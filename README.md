# sync-me-maybe

Comfortably synced. Telegram bot that downloads music from links and audio files directly to your NAS for imports into Navidrome.

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
HOST_MUSIC_DIR=/path/to/nas/navidrome/import
```

Find your Telegram user ID by running the bot and sending `/id`.

Start the service:

```sh
docker compose up --build
```

The Compose file mounts `${HOST_MUSIC_DIR}` into the container as `/music`. Mount your NAS on the host first using your preferred SMB/NFS/system mount setup, then point `HOST_MUSIC_DIR` at that mounted folder.

## Environment Variables

Required:

- `TELEGRAM_BOT_TOKEN`: Telegram bot token from BotFather.
- `ALLOWED_TELEGRAM_USER_IDS`: comma-separated Telegram user IDs allowed to use the bot.
- `HOST_MUSIC_DIR`: host path to the NAS/Navidrome import folder.

Optional:

- `MUSIC_DIR`: container music path, default `/music`.
- `DOWNLOAD_TMP_DIR`: temporary work directory, default `/tmp/sync-me-maybe`.
- `HOST_TMP_DIR`: host path for temporary files, default `./tmp`.
- `YTDLP_COOKIES_FILE`: optional cookies file path inside the container for YouTube Music.
- `MAX_DOWNLOAD_SECONDS`: soft timeout limit, default `900`.
- `MAX_COLLECTION_TRACKS`: maximum tracks to expand from one playlist or album, default `100`.
- `LOG_LEVEL`: default `INFO`.

## Bot Commands

- `/start`: show accepted inputs and authorization status.
- `/help`: show supported links and usage.
- `/id`: show your Telegram user ID for allowlist setup.
- `/health`: verify the bot can write to the music directory.
- `/queue`: show the active download and pending queue.

Incoming links and uploads are added to a global in-memory queue. The bot replies directly to the original file or link, shows the queue position, and edits that same status reply as the item moves through resolving, downloading, saving, and completion.
If a message contains multiple links, each supported link becomes its own queue item and unsupported links are reported individually.

## Local Development

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
```
