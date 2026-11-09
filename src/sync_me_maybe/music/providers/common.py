"""Small parsing helpers shared by provider adapters."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import unquote, urlparse

GENERIC_SLUG_PARTS = {
    "album",
    "artist",
    "music",
    "song",
    "track",
    "us",
    "de",
    "gb",
    "fr",
    "es",
    "it",
}


def clean_slug(value: str | None) -> str | None:
    """Turn a URL path segment into readable search text."""
    from sync_me_maybe.music.filenames import clean_title

    if not value:
        return None
    value = unquote(value)
    value = re.sub(r"\.[a-z0-9]{2,5}$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"[_-]+", " ", value)
    value = re.sub(r"\b\d{4,}\b", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" /.-_")
    return clean_title(value) if value else None


def usable_slug_query(value: str | None) -> bool:
    """Reject slug fragments that are too generic to be useful search queries."""
    if not value:
        return False
    return not value.isdigit() and value.casefold() not in GENERIC_SLUG_PARTS


def slug_query(url: str) -> str | None:
    """Find the best usable search phrase from the end of a URL path."""
    parsed = urlparse(url)
    for part in reversed([unquote(part) for part in parsed.path.split("/") if part]):
        cleaned = clean_slug(part)
        if usable_slug_query(cleaned):
            return cleaned
    return None


def duration_from_ms(value: Any) -> int | None:
    """Convert a millisecond duration into whole seconds."""
    milliseconds = int_or_none(value)
    if not milliseconds or milliseconds <= 0:
        return None
    return round(milliseconds / 1000)


def duration_from_any(source: dict[str, Any]) -> int | None:
    """Read a track duration from loosely structured provider JSON.

    Handles ``duration_ms``/``durationInMillis`` (milliseconds), plain
    ``duration`` numbers (milliseconds when large), and ISO 8601 ``PT3M45S``.
    """
    for key in ("duration_ms", "durationMs", "durationInMillis"):
        if (seconds := duration_from_ms(source.get(key))) is not None:
            return seconds
    value = source.get("duration")
    if isinstance(value, dict):
        return duration_from_ms(value.get("totalMilliseconds") or value.get("milliseconds"))
    if isinstance(value, int | float) and value > 0:
        return round(value / 1000) if value > 10_000 else round(value)
    if isinstance(value, str):
        match = re.fullmatch(r"P(?:T)?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", value.strip())
        if match and any(match.groups()):
            hours, minutes, secs = (float(part or 0) for part in match.groups())
            return round(hours * 3600 + minutes * 60 + secs) or None
    return None


def int_or_none(value: Any) -> int | None:
    """Convert loose provider metadata into an integer when possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
