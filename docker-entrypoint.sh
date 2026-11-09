#!/bin/sh
# Run the bot as PUID:PGID so library files are owned by the host user.
#
# Compose does not use Docker's `user:` option because some NAS runtimes reject
# it. Instead the container starts as root and drops privileges here. If the
# runtime does not allow switching users, the bot keeps running as root.
set -eu

if [ "$(id -u)" = "0" ] && [ -n "${PUID:-}" ] && [ "${PUID}" != "0" ]; then
    PGID="${PGID:-$PUID}"
    tmp_dir="${DOWNLOAD_TMP_DIR:-/tmp/sync-me-maybe}"
    mkdir -p "$tmp_dir" "${XDG_CACHE_HOME:-/tmp/.cache}"
    # Only app-owned scratch folders are chowned; the music library is never
    # changed recursively.
    chown "$PUID:$PGID" "$tmp_dir" "${XDG_CACHE_HOME:-/tmp/.cache}" 2>/dev/null || true

    if setpriv --reuid="$PUID" --regid="$PGID" --clear-groups true 2>/dev/null; then
        export HOME="${XDG_CACHE_HOME:-/tmp/.cache}"
        exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
    fi
    echo "sync-me-maybe: could not switch to PUID=$PUID PGID=$PGID; running as root" >&2
fi

exec "$@"
