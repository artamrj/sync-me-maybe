# sync-me-maybe

Telegram bot that downloads music links and Telegram audio uploads into a local music folder for Navidrome or any other library scanner.

## What It Accepts

- Telegram audio files and audio-like document uploads
- YouTube and YouTube Music track and playlist links
- Spotify track, playlist, and album links
- Apple Music track, playlist, and album links
- Shazam links
- Multiple links in one message

Spotify, Apple Music, and Shazam links are used as search inputs. The bot does not bypass DRM or download directly from those providers; it resolves a matching YouTube or YouTube Music result and stores that audio. Spotify and Apple Music playlists and albums use tokenless public extraction and may fail if the public page does not expose track data. Large Spotify playlists can page beyond the first 100 public embed tracks when Spotify exposes an anonymous public token.

## Matching and Tags

- Search-based links (Spotify, Apple Music, Shazam, and playlist/album tracks) are matched against YouTube Music's official song results first. Candidates are scored on title, artist, and duration; live, cover, remix, sped-up, and similar versions are penalized unless the original title asks for them. Music videos are tried next with stricter rules, and plain YouTube search is the last resort.
- Spotify track links read the public embed page for the title, every credited artist, and the duration.
- Apple Music track links use Apple's public iTunes lookup to get the real song title and artist instead of the album name in the URL.
- Track metadata is looked up in the public Deezer API (no account or key needed), by ISRC when the provider exposes one, otherwise by a scored search. Only confident matches are used.
- Every link download is tagged before it is stored: title, artists, album, album artist, track and disc number, release date, ISRC, genre, source URL, and embedded cover art. Library scanners such as Navidrome organize music by these tags.
- Direct YouTube links get the same lookup after download, but only when the duration also matches within a few seconds.
- Synced lyrics are fetched from [LRCLIB](https://lrclib.net) and saved next to the song as `.lrc` (or `.txt` for plain lyrics), which Navidrome shows in its player. Set `LYRICS=false` to skip them.
- Cover art and lyrics are fetched while the audio downloads, and metadata lookup and matching run together when the song duration is already known.
- Lookups are best-effort. If Deezer or YouTube Music is unreachable, the bot falls back to the previous search behavior and still tags the file with what it knows. Set `METADATA_LOOKUP=false` to disable all lookups.

## Storage Behavior

- Link downloads use `AUDIO_FORMAT`, which defaults to `m4a`. `m4a` and `opus` keep YouTube's original audio stream without re-encoding (typically AAC ~128 kbps or Opus ~130-160 kbps), so files are about half the size of the old MP3 320 output at the same quality. `mp3` keeps the previous 320 kbps transcode.
- Telegram uploads are stored in their original format with their original filename when available.
- Link downloads are stored once, in `Album Artist/Album/01 - Title.<ext>`. Songs without a known album go to `Artist/Title.<ext>`. Files from versions before 0.13 stay where they are.
- Playlist and album links are expanded into individual track jobs, capped by `MAX_COLLECTION_TRACKS`.
- Playlist links also write `Playlists/<Owner - Playlist>.m3u8` with paths relative to the playlist file. Navidrome imports these automatically (its default `AutoImportPlaylists=true` and `PlaylistsPath` already include this folder), so a song that appears in several playlists is stored only once. Sending the same playlist again refreshes its file in the playlist's order.
- Duplicates are skipped before downloading. A library index in `DATA_DIR` remembers every song by ISRC, YouTube video ID, and artist + title, and a background scan at startup indexes everything already in `MUSIC_DIR` (older downloads, uploads, and files added by hand, using tags or `Artist - Title` filenames). The index ignores featured-artist and remaster notes but keeps versions such as live or remix apart.
- Existing target files are never overwritten; the same name in another audio format also counts as already present.
- Two downloads run at the same time by default (`MAX_PARALLEL_DOWNLOADS`), with a short pause after each real download (`DOWNLOAD_PAUSE_SECONDS`) to stay polite to YouTube. Skipped songs never wait.
- When `NAVIDROME_URL`, `NAVIDROME_USER`, and `NAVIDROME_PASSWORD` are set, the bot asks Navidrome to rescan shortly after new songs or playlists are saved. Scans are batched, so a large playlist triggers one scan. The user must be a Navidrome admin.
- Finished Telegram requests with failed jobs show a `Rerun failed` button that queues only the failed items again.
- The queue, request status messages, and their buttons (`Rerun failed`, details, paths) are saved in `DATA_DIR` and survive a restart. Unfinished requests continue where they stopped and their status message says "Resumed after restart." Finished requests and buttons are kept for 30 days.
- Allowlisted owners can create one-time guest links that grant revocable, in-memory access to submit music in a private bot chat.
- Telegram status messages follow a clear `Preparing -> Queued -> Downloading -> Completed` lifecycle, use source-specific icons, show playlist/album or single-track title and artist when known, and keep final statuses compact. If `RECEIVED_STICKER_ID` is configured, the bot shows that sticker for 5 seconds while work continues, then replaces it with the latest status message.

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
DATA_DIR=./data
AUDIO_FORMAT=m4a
METADATA_LOOKUP=true
LYRICS=true
MAX_PARALLEL_DOWNLOADS=2
DOWNLOAD_PAUSE_SECONDS=3
NAVIDROME_URL=
NAVIDROME_USER=
NAVIDROME_PASSWORD=
RECEIVED_STICKER_ID=
LOG_LEVEL=INFO
```

Find your Telegram user ID by running the bot and sending `/id`.

Install and run locally:

```sh
uv sync
uv run sync-me-maybe
```

`ffmpeg` must be available on `PATH` for audio extraction. For reliable YouTube downloads, also install [Deno](https://deno.com/) (`yt-dlp` uses it as its JavaScript runtime); the Docker image already includes it.

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
- `${DATA_DIR_HOST}` to `/data` for the bot database (library index, queue, and buttons)
- `${YTDLP_COOKIES_FILE_HOST}` to `/config/cookies.txt` for optional yt-dlp cookies

Use writable host folders for the configured UID/GID. The app sets `MUSIC_DIR=/music`, `DOWNLOAD_TMP_DIR=/tmp/sync-me-maybe`, and `DATA_DIR=/data` inside the container, so the container remains stateless except for mounted music, temporary downloads, the database, and optional cookies. Keep `/data` on a local disk rather than a network share, since SQLite needs reliable file locking. If Navidrome runs in the same compose project, `NAVIDROME_URL=http://navidrome:4533` works. To use cookies, set `YTDLP_COOKIES_FILE=/config/cookies.txt` and point `YTDLP_COOKIES_FILE_HOST` at the host cookies file.

The compose file intentionally does not use Docker's `user: ${PUID}:${PGID}` setting because some NAS runtimes reject that switch with `operation not permitted`. Instead, the container starts as root and its entrypoint drops to `PUID:PGID` before starting the bot, so new music files are owned by that host user. If the runtime does not allow switching users, the bot logs a warning and keeps running as root. If your NAS reports `uid=1000(arta) gid=10(admin)`, use `PUID=1000` and `PGID=10`.

Upgrading to 0.13: add the `/data` volume from `compose.yaml` (or set `DATA_DIR_HOST`). Without it the database lives inside the container and is lost when the container is recreated; the bot still works but rescans the library and forgets queued work.

Upgrading from 0.11 or earlier: older images ran as root, so files and playlist folders they created may be owned by root. Give them to the configured user once, for example `sudo chown -R 1000:10 /path/to/music`, then run `/health`.

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
- `AUDIO_FORMAT`: output format for link downloads: `m4a` (default), `opus`, or `mp3`. `m4a` plays in nearly every client; `opus` is slightly more efficient; `mp3` re-encodes at 320 kbps.
- `METADATA_LOOKUP`: `true` (default) or `false`. Enables YouTube Music matching, Deezer metadata lookup, and cover-art and lyrics downloads. Files are still tagged with provider metadata when disabled.
- `DATA_DIR`: folder for the SQLite database (library index, queue, status buttons). Defaults to `./data`.
- `LYRICS`: `true` (default) or `false`. Saves synced lyrics from LRCLIB next to each new song.
- `MAX_PARALLEL_DOWNLOADS`: downloads processed at the same time, 1-8. Defaults to `2`. Use `1` on a VPS or datacenter IP that YouTube rate-limits.
- `DOWNLOAD_PAUSE_SECONDS`: pause after each real download, per worker. Defaults to `3`.
- `NAVIDROME_URL`, `NAVIDROME_USER`, `NAVIDROME_PASSWORD`: optional Navidrome server URL and admin login used to trigger a library scan after new files are saved. All three are required when `NAVIDROME_URL` is set.
- `PUID` / `PGID` (Docker only): host user and group the bot runs as and that owns new files.
- `RECEIVED_STICKER_ID`: optional Telegram sticker file ID shown for 5 seconds as a temporary acknowledgement after the user sends a supported link or upload.
- `LOG_LEVEL`: Python logging level. Defaults to `INFO`.

## Bot Commands

- `/start`: show accepted inputs and authorization status.
- `/help`: show supported links and usage.
- `/id`: show your Telegram user ID for allowlist setup.
- `/health`: verify the bot can write to the music directory, and show the library index size and whether Navidrome is reachable.
- `/queue`: show the active downloads and pending queue.
- `/guests`: create one-time guest invitations, list active guests, and revoke guest access (allowlisted owners only).

## Temporary Guest Access

Every user configured in `ALLOWED_TELEGRAM_USER_IDS` is an owner. Open `/guests` and select
`Create invite` to receive a one-time Telegram link. The recipient must open that link in a
private chat with the bot. Once accepted, the guest can submit the same supported links and audio
uploads as an owner, but cannot inspect the global queue, run health checks, or manage guests.

Guest access lasts until an owner revokes it from `/guests` or the bot restarts. Unused invitation
links are also kept only in memory and become invalid after a restart. Revoking a guest blocks new
requests immediately but does not cancel work that is already queued or downloading.

Incoming links and uploads are added to a global queue immediately (saved in `DATA_DIR`). The bot optionally shows the configured received sticker for 5 seconds without blocking the queue, removes that temporary sticker once the status exists, then edits the status reply as the item moves through preparing, queued, downloading, and completed states. The queued state is shown only when another job is ahead.

If a message contains multiple links, each supported link becomes its own queue item and unsupported links are reported individually.

## Development

```sh
uv sync --group dev
uv run sync-me-maybe
uv run ruff format .
uv run ruff check .
uv run mypy src
```
