"""Shared provider contracts and data models.

Providers hide the differences between YouTube, Spotify, Apple Music, and
Shazam so the resolver and queue can work with one common shape.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sync_me_maybe.music.urls import ClassifiedLink, LinkKind


@dataclass(frozen=True)
class ResolvedTrack:
    """A single track resolved to a URL or search target yt-dlp can handle."""

    source_url: str
    download_url: str
    search_query: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None


@dataclass(frozen=True)
class TrackSearchItem:
    """One track discovered inside a playlist or album."""

    title: str
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    source_url: str | None = None

    @property
    def search_query(self) -> str:
        """Build a provider-neutral YouTube Music search query."""
        return " ".join(part for part in [self.artist, self.title] if part)


class ProviderError(RuntimeError):
    """Provider failure with retry metadata for queue retry decisions."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class UnsupportedProviderCapability(ProviderError):
    """Raised when a provider cannot support a requested operation."""

    def __init__(self, message: str) -> None:
        super().__init__(message, retryable=False)


class Provider(Protocol):
    """Interface every music provider adapter implements."""

    kind: LinkKind

    def classify(self, url: str) -> ClassifiedLink | None:
        raise NotImplementedError

    async def resolve_track(self, link: ClassifiedLink) -> ResolvedTrack:
        raise NotImplementedError

    async def expand_collection(self, link: ClassifiedLink) -> list[TrackSearchItem]:
        raise NotImplementedError


def unsupported_collection() -> UnsupportedProviderCapability:
    """Create the standard error for providers without collection support."""
    return UnsupportedProviderCapability(
        "This provider does not support playlist or album expansion."
    )
