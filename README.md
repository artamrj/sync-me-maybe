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
- Link downloads are stored as `Artist/Album/Track - Title.mp3` when album metadata exists.
- If album metadata is missing or unknown, tracks are stored directly under `Artist/Track - Title.mp3`.
- Existing target files are skipped instead of overwritten.
- Playlist and album links are expanded into individual track jobs, capped by `MAX_COLLECTION_TRACKS`.

## Setup

Create a Telegram bot with BotFather, copy `.env.example` to `.env`, and fill in the required values:

```sh
TELEGRAM_BOT_TOKEN=123456:replace-me
ALLOWED_TELEGRAM_USER_IDS=123456789
MUSIC_DIR=./music
DOWNLOAD_TMP_DIR=./tmp/sync-me-maybe
MAX_DOWNLOAD_SECONDS=900
MAX_COLLECTION_TRACKS=100
UPLOAD_BATCH_WINDOW_SECONDS=2
LOG_LEVEL=INFO
```

Find your Telegram user ID by running the bot and sending `/id`.

Install and run locally:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e .
sync-me-maybe
```

`ffmpeg` must be available on `PATH` for audio conversion.

## Environment Variables

- `TELEGRAM_BOT_TOKEN`: required Telegram bot token.
- `ALLOWED_TELEGRAM_USER_IDS`: required comma- or semicolon-separated Telegram user IDs allowed to use the bot.
- `MUSIC_DIR`: directory where completed files are stored. Defaults to `./music`.
- `DOWNLOAD_TMP_DIR`: directory for temporary downloads. Defaults to `./tmp/sync-me-maybe`.
- `YTDLP_COOKIES_FILE`: optional cookies file passed to `yt-dlp`.
- `MAX_DOWNLOAD_SECONDS`: maximum download duration before a job fails. Defaults to `900`.
- `MAX_COLLECTION_TRACKS`: maximum playlist or album tracks to enqueue. Defaults to `100`.
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
python -m venv .venv
. .venv/bin/activate
pip install -e .
```
