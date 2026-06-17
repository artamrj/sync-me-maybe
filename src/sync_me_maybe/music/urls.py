from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class LinkKind(StrEnum):
    YOUTUBE = "youtube"
    SPOTIFY = "spotify"
    APPLE_MUSIC = "apple_music"
    SHAZAM = "shazam"
    UNSUPPORTED = "unsupported"


class LinkScope(StrEnum):
    TRACK = "track"
    PLAYLIST = "playlist"
    ALBUM = "album"


@dataclass(frozen=True)
class ClassifiedLink:
    kind: LinkKind
    url: str
    scope: LinkScope = LinkScope.TRACK
    reason: str | None = None


URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def extract_first_url(text: str | None) -> str | None:
    urls = extract_urls(text)
    return urls[0] if urls else None


def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,;]")
        if url in seen:
            continue
        urls.append(url)
        seen.add(url)
    return urls


def classify_url(url: str) -> ClassifiedLink:
    from sync_me_maybe.music.providers.registry import build_providers

    for provider in build_providers():
        classified = provider.classify(url)
        if classified:
            return classified

    return ClassifiedLink(
        LinkKind.UNSUPPORTED,
        url,
        reason=(
            "Unsupported link. Send a YouTube Music, Spotify, Apple Music, or Shazam track link."
        ),
    )
