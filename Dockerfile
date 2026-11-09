# yt-dlp needs a JavaScript runtime for full YouTube support; Deno is its default.
FROM denoland/deno:bin-2.9.8 AS deno

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:$PATH" \
    XDG_CACHE_HOME=/tmp/.cache \
    DENO_DIR=/tmp/.cache/deno

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir uv==0.11.21

COPY --from=deno /deno /usr/local/bin/deno

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-cache

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

RUN chmod 755 /usr/local/bin/docker-entrypoint.sh \
    && mkdir -p /music /tmp/sync-me-maybe /config \
    && chmod 775 /music /tmp/sync-me-maybe /config

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["sync-me-maybe"]
