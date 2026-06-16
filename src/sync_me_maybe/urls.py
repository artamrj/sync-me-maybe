from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import parse_qs, urlparse


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
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if host.startswith("www."):
        host = host[4:]

    if host in {"youtu.be", "youtube.com", "m.youtube.com", "music.youtube.com"}:
        if "list" in parse_qs(parsed.query) and "v" not in parse_qs(parsed.query):
            return ClassifiedLink(LinkKind.YOUTUBE, url, LinkScope.PLAYLIST)
        return ClassifiedLink(LinkKind.YOUTUBE, url)

    if host in {"open.spotify.com", "spotify.link"}:
        if "/playlist/" in path:
            return ClassifiedLink(LinkKind.SPOTIFY, url, LinkScope.PLAYLIST)
        if "/album/" in path:
            return ClassifiedLink(LinkKind.SPOTIFY, url, LinkScope.ALBUM)
        return ClassifiedLink(LinkKind.SPOTIFY, url)

    if host in {"music.apple.com", "itunes.apple.com"}:
        if "/album/" in path and "i" not in parse_qs(parsed.query):
            return ClassifiedLink(LinkKind.APPLE_MUSIC, url, LinkScope.ALBUM)
        if "/playlist/" in path:
            return ClassifiedLink(LinkKind.APPLE_MUSIC, url, LinkScope.PLAYLIST)
        return ClassifiedLink(LinkKind.APPLE_MUSIC, url)

    if host.endswith("shazam.com"):
        return ClassifiedLink(LinkKind.SHAZAM, url)

    return ClassifiedLink(LinkKind.UNSUPPORTED, url, reason="Unsupported link. Send a YouTube Music, Spotify, Apple Music, or Shazam track link.")
