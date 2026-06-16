FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip \
    && pip install .

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /music /tmp/sync-me-maybe \
    && chown -R appuser:appuser /music /tmp/sync-me-maybe

USER appuser

CMD ["sync-me-maybe"]
