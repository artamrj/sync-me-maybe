from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sync_me_maybe.music.urls import ClassifiedLink, LinkKind


@dataclass(frozen=True)
class ResolvedTrack:
    source_url: str
    download_url: str
    search_query: str | None = None
    title: str | None = None
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None


@dataclass(frozen=True)
class TrackSearchItem:
    title: str
    artist: str | None = None
    album: str | None = None
    track_number: int | None = None
    source_url: str | None = None

    @property
    def search_query(self) -> str:
        return " ".join(part for part in [self.artist, self.title] if part)


class ProviderError(RuntimeError):
    pass


class UnsupportedProviderCapability(ProviderError):
    pass


class Provider(Protocol):
    kind: LinkKind

    def classify(self, url: str) -> ClassifiedLink | None:
        raise NotImplementedError

    async def resolve_track(self, link: ClassifiedLink) -> ResolvedTrack:
        raise NotImplementedError

    async def expand_collection(self, link: ClassifiedLink) -> list[TrackSearchItem]:
        raise NotImplementedError


def unsupported_collection() -> UnsupportedProviderCapability:
    return UnsupportedProviderCapability(
        "This provider does not support playlist or album expansion."
    )
