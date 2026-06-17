from __future__ import annotations

import re
import unicodedata

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WHITESPACE = re.compile(r"\s+")


def sanitize_filename(value: str | None, fallback: str = "Unknown") -> str:
    if not value:
        value = fallback
    value = unicodedata.normalize("NFKC", value)
    value = INVALID_FILENAME_CHARS.sub("_", value)
    value = WHITESPACE.sub(" ", value).strip(" .")
    return value[:180] or fallback


def clean_title(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(
        r"\s+[-|]\s+(YouTube Music|Spotify|Apple Music|Shazam)\s*$", "", value, flags=re.IGNORECASE
    )
    value = re.sub(r"\s+on\s+(Spotify|Apple Music|Shazam)\s*$", "", value, flags=re.IGNORECASE)
    return WHITESPACE.sub(" ", value).strip() or None
