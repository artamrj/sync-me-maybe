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


@dataclass(frozen=True)
class ClassifiedLink:
    kind: LinkKind
    url: str
    reason: str | None = None


URL_RE = re.compile(r"https?://[^\s<>()]+", re.IGNORECASE)


def extract_first_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,;]")


def classify_url(url: str) -> ClassifiedLink:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path.lower()

    if host.startswith("www."):
        host = host[4:]

    if host in {"youtu.be", "youtube.com", "m.youtube.com", "music.youtube.com"}:
        if "list" in parse_qs(parsed.query) and "v" not in parse_qs(parsed.query):
            return ClassifiedLink(LinkKind.UNSUPPORTED, url, "Playlists are not supported in v1.")
        return ClassifiedLink(LinkKind.YOUTUBE, url)

    if host in {"open.spotify.com", "spotify.link"}:
        if "/playlist/" in path or "/album/" in path:
            return ClassifiedLink(LinkKind.UNSUPPORTED, url, "Spotify playlists and albums are not supported in v1.")
        return ClassifiedLink(LinkKind.SPOTIFY, url)

    if host in {"music.apple.com", "itunes.apple.com"}:
        if "/album/" in path and "i" not in parse_qs(parsed.query):
            return ClassifiedLink(LinkKind.UNSUPPORTED, url, "Apple Music albums are not supported in v1.")
        return ClassifiedLink(LinkKind.APPLE_MUSIC, url)

    if host.endswith("shazam.com"):
        return ClassifiedLink(LinkKind.SHAZAM, url)

    return ClassifiedLink(LinkKind.UNSUPPORTED, url, "Unsupported link. Send a YouTube Music, Spotify, Apple Music, or Shazam track link.")
