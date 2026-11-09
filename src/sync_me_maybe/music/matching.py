"""Score search candidates against known track metadata.

Search-based providers (Spotify, Apple Music, Shazam, playlist children) know the
title, artist, and often the duration of the track the user wants. This module
compares YouTube Music and metadata-service candidates against that target so
the bot downloads the official audio instead of the first search hit.
"""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

LOGGER = logging.getLogger(__name__)

# Words that mark a different version of a song. They are penalized when a
# candidate contains them but the requested title does not.
VARIANT_WORDS = (
    "live",
    "cover",
    "karaoke",
    "instrumental",
    "remix",
    "sped up",
    "slowed",
    "nightcore",
    "reverb",
    "8d",
    "acapella",
    "a cappella",
    "mashup",
    "tribute",
)

_ARTIST_SPLIT = re.compile(r"\s*(?:,|;|&|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b)\s*", re.I)
_BRACKETS = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]\s*")
_DASH_SUFFIX = re.compile(r"\s+-\s+.*$")
_FEAT_SUFFIX = re.compile(r"\s+(?:feat\.?|ft\.?|featuring)\s.*$", re.I)
_YOUTUBE_NOISE = re.compile(
    r"\s*[\(\[][^\)\]]*(official|lyrics?|audio|video|visuali[sz]er|hd|4k|mv)[^\)\]]*[\)\]]\s*",
    re.I,
)
_CHANNEL_SUFFIX = re.compile(r"\s*(?:-\s*topic|vevo|official)\s*$", re.I)


@dataclass(frozen=True)
class MatchTarget:
    """What the user asked for."""

    title: str
    artist: str | None = None
    duration_seconds: int | None = None


@dataclass(frozen=True)
class Candidate:
    """One search result that might be the requested track."""

    title: str
    artists: tuple[str, ...] = ()
    duration_seconds: int | None = None
    video_id: str | None = None
    official: bool = False
    thumbnail_url: str | None = None
    extra: dict[str, Any] = field(default_factory=dict, compare=False)


@dataclass(frozen=True)
class MatchScore:
    """Score breakdown for one candidate; ``total`` is 0-100."""

    total: float
    title: float
    artist: float | None
    duration: float | None
    accepted: bool


@dataclass(frozen=True)
class Thresholds:
    """Minimum scores a candidate needs before it is trusted."""

    min_total: float = 72.0
    min_title: float = 70.0
    min_artist: float = 65.0
    max_duration_diff: int = 15
    require_duration: bool = False


STRICT = Thresholds(min_total=82.0, min_title=80.0, min_artist=75.0, max_duration_diff=7)


def normalize(value: str | None) -> str:
    """Lowercase, strip accents and punctuation for fuzzy comparison."""
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(char for char in value if not unicodedata.combining(char))
    value = value.casefold().replace("&", " and ")
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def base_title(value: str | None) -> str:
    """Drop bracketed and dash suffixes such as ``(Remastered 2011)``."""
    if not value:
        return ""
    stripped = _BRACKETS.sub(" ", value)
    stripped = _FEAT_SUFFIX.sub("", _DASH_SUFFIX.sub("", stripped)).strip()
    return normalize(stripped or value)


def primary_artist(value: str | None) -> str | None:
    """Return the first credited artist from a joined artist string."""
    if not value:
        return None
    parts = [part.strip() for part in _ARTIST_SPLIT.split(value) if part and part.strip()]
    return parts[0] if parts else value.strip() or None


def clean_channel_name(value: str | None) -> str | None:
    """Turn ``Artist - Topic`` or ``ArtistVEVO`` into ``Artist``."""
    if not value:
        return None
    cleaned = _CHANNEL_SUFFIX.sub("", value).strip()
    return cleaned or value.strip() or None


def clean_video_title(title: str, artists: Iterable[str] = ()) -> str:
    """Remove ``(Official Video)`` noise and a leading ``Artist -`` prefix."""
    cleaned = _YOUTUBE_NOISE.sub(" ", title).strip()
    if " - " in cleaned:
        prefix, rest = cleaned.split(" - ", 1)
        prefix_norm = normalize(prefix)
        if any(prefix_norm and similarity(prefix_norm, normalize(a)) >= 80 for a in artists):
            cleaned = rest
    return re.sub(r"\s+", " ", cleaned).strip() or title


def similarity(left: str, right: str) -> float:
    """0-100 similarity tolerant of word order and extra words."""
    if not left or not right:
        return 0.0
    if left == right:
        return 100.0
    ratio = SequenceMatcher(None, left, right).ratio() * 100
    left_tokens, right_tokens = set(left.split()), set(right.split())
    shorter, longer = sorted((left_tokens, right_tokens), key=len)
    if shorter and shorter <= longer:
        # Every word of one side appears in the other ("Song" vs "Song Remaster").
        coverage = len(shorter) / len(longer)
        ratio = max(ratio, 60 + 40 * coverage)
    return ratio


def title_score(target: str, candidate: str) -> float:
    full = similarity(normalize(target), normalize(candidate))
    base = similarity(base_title(target), base_title(candidate))
    return max(full, base)


def artist_score(target: str | None, candidates: Iterable[str]) -> float | None:
    if not target:
        return None
    names = [normalize(name) for name in candidates if name]
    if not names:
        return 0.0
    wanted = normalize(primary_artist(target))
    joined = " ".join(names)
    if wanted and wanted in joined:
        return 100.0
    return max(similarity(wanted, name) for name in names)


def duration_score(target: int | None, candidate: int | None) -> float | None:
    if not target or not candidate:
        return None
    return 100 * math.exp(-0.1 * abs(target - candidate))


def variant_penalty(target_title: str, candidate_title: str) -> float:
    wanted = f" {normalize(target_title)} "
    found = f" {normalize(candidate_title)} "
    penalty = 0.0
    for word in VARIANT_WORDS:
        token = f" {word} "
        if token in found and token not in wanted:
            penalty += 35
    return penalty


def score(
    target: MatchTarget, candidate: Candidate, thresholds: Thresholds = Thresholds()
) -> MatchScore:
    """Score one candidate and decide whether it is trustworthy."""
    title = title_score(target.title, candidate.title)
    artist = artist_score(target.artist, candidate.artists)
    duration = duration_score(target.duration_seconds, candidate.duration_seconds)

    weighted = [(title, 0.45)]
    if artist is not None:
        weighted.append((artist, 0.35))
    if duration is not None:
        weighted.append((duration, 0.20))
    total = sum(value * weight for value, weight in weighted) / sum(w for _, w in weighted)
    total -= variant_penalty(target.title, candidate.title)
    if candidate.official:
        total += 3
    total = max(0.0, min(100.0, total))

    accepted = (
        title >= thresholds.min_title
        and (artist is None or artist >= thresholds.min_artist)
        and total >= thresholds.min_total
    )
    if target.duration_seconds and candidate.duration_seconds:
        if abs(target.duration_seconds - candidate.duration_seconds) > thresholds.max_duration_diff:
            accepted = False
    elif thresholds.require_duration:
        accepted = False
    return MatchScore(total, title, artist, duration, accepted)


def best_candidate(
    target: MatchTarget, candidates: Iterable[Candidate], thresholds: Thresholds = Thresholds()
) -> tuple[Candidate, MatchScore] | None:
    """Return the highest scoring accepted candidate, if any."""
    best: tuple[Candidate, MatchScore] | None = None
    for candidate in candidates:
        result = score(target, candidate, thresholds)
        if not result.accepted:
            continue
        if best is None or result.total > best[1].total:
            best = (candidate, result)
    return best


def large_thumbnail(url: str | None, size: int = 1200) -> str | None:
    """Ask Google's image CDN for a larger square thumbnail."""
    if not url:
        return None
    return re.sub(r"=w\d+-h\d+", f"=w{size}-h{size}", url)


@dataclass(frozen=True)
class YouTubeMatch:
    """A YouTube video chosen for download plus why it was chosen."""

    url: str
    candidate: Candidate
    score: MatchScore


class YouTubeMusicMatcher:
    """Search YouTube Music and pick the best-scoring official audio."""

    def __init__(self, client: Any | None = None, limit: int = 10) -> None:
        self._client = client
        self.limit = limit

    @property
    def client(self) -> Any:
        if self._client is None:
            from ytmusicapi import YTMusic

            self._client = YTMusic()
        return self._client

    def find(self, target: MatchTarget) -> YouTubeMatch | None:
        """Search songs first, then videos with stricter rules. Blocking."""
        query = " ".join(part for part in [primary_artist(target.artist), target.title] if part)
        songs = self._search(query, "songs")
        match = best_candidate(target, songs)
        if match is None:
            videos = self._search(query, "videos")
            match = best_candidate(target, videos, STRICT)
        if match is None:
            return None
        candidate, result = match
        return YouTubeMatch(
            url=f"https://www.youtube.com/watch?v={candidate.video_id}",
            candidate=candidate,
            score=result,
        )

    def _search(self, query: str, kind: str) -> list[Candidate]:
        results = self.client.search(query, filter=kind, limit=self.limit) or []
        return [c for item in results if (c := candidate_from_ytmusic(item))]


def candidate_from_ytmusic(item: Any) -> Candidate | None:
    """Convert one ytmusicapi search result into a scoring candidate."""
    if not isinstance(item, dict) or not item.get("videoId") or not item.get("title"):
        return None
    artists = tuple(
        name
        for artist in item.get("artists") or []
        if isinstance(artist, dict) and (name := clean_channel_name(artist.get("name")))
    )
    official = item.get("resultType") == "song"
    title = str(item["title"])
    if not official:
        title = clean_video_title(title, artists)
    thumbnails = [t for t in item.get("thumbnails") or [] if isinstance(t, dict) and t.get("url")]
    thumbnail = large_thumbnail(thumbnails[-1]["url"]) if official and thumbnails else None
    duration = item.get("duration_seconds")
    return Candidate(
        title=title,
        artists=artists,
        duration_seconds=int(duration) if isinstance(duration, int | float) else None,
        video_id=str(item["videoId"]),
        official=official,
        thumbnail_url=thumbnail,
    )
