# NAS Deployment

This Compose setup is intentionally simple: host paths are only used in volume mounts, and the app always writes inside the container to `/music` and `/tmp/sync-me-maybe`.

## Example `.env`

```env
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

## Variables

| Variable | Required | Example | Why it is needed |
| --- | --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | `123456:replace-me` | Authenticates the bot with Telegram. Get it from BotFather. |
| `ALLOWED_TELEGRAM_USER_IDS` | Yes | `123456789,987654321` | Limits bot usage to trusted Telegram users. Send `/id` to the bot to find your ID. |
| `SYNC_ME_MAYBE_VERSION` | No | `latest` or `0.9.0` | Chooses the GHCR image tag. Use `latest` for newest main build or a version for pinned deploys. |
| `PUID` | No | `1026` | Runs the container as this NAS user ID so it can write to mounted folders. Find it with `id your-user`. |
| `PGID` | No | `100` | Runs the container with this NAS group ID. Find it with `id your-user`. |
| `HOST_MUSIC_DIR` | Yes | `/volume1/music/2-library/telegram-bot` | Host/NAS folder where finished music is stored. Compose mounts it to `/music`. |
| `HOST_TMP_DIR` | No | `/volume1/docker/sync-me-maybe/tmp` | Host/NAS folder for temporary downloads. Compose mounts it to `/tmp/sync-me-maybe`. |
| `MAX_DOWNLOAD_SECONDS` | No | `900` | Stops very long downloads from running forever. |
| `MAX_COLLECTION_TRACKS` | No | `100` | Caps playlist/album expansion size. |
| `UPLOAD_BATCH_WINDOW_SECONDS` | No | `2` | Groups audio files forwarded quickly into one progress message. Set `0` to disable batching. |
| `LOG_LEVEL` | No | `INFO` | Controls app log verbosity. Use `DEBUG` only while troubleshooting. |

## Important Path Rule

Do not set `MUSIC_DIR` or `DOWNLOAD_TMP_DIR` in the NAS `.env`.

Use:

```env
HOST_MUSIC_DIR=/volume1/music/2-library/telegram-bot
HOST_TMP_DIR=/volume1/docker/sync-me-maybe/tmp
```

The container paths are fixed by Compose:

```text
/music
/tmp/sync-me-maybe
```

## Synology Permissions

Find the user and group IDs:

```sh
id your-synology-user
```

Create the host folders:

```sh
mkdir -p /volume1/music/2-library/telegram-bot
mkdir -p /volume1/docker/sync-me-maybe/tmp
```

Make sure the `PUID`/`PGID` user can write to both folders.

## Start Or Update

```sh
docker compose pull
docker compose up -d
docker compose logs -f sync-me-maybe
```
