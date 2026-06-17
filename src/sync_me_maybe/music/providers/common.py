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


def int_or_none(value: Any) -> int | None:
    """Convert loose provider metadata into an integer when possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
