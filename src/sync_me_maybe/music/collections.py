"""Expand playlist and album links into individual track search items."""

from __future__ import annotations

from sync_me_maybe.config import Settings
from sync_me_maybe.music.providers.base import ProviderError, TrackSearchItem
from sync_me_maybe.music.providers.registry import build_providers, provider_for
from sync_me_maybe.music.urls import ClassifiedLink, LinkScope


class CollectionResolveError(RuntimeError):
    """User-facing collection expansion failure with retry metadata."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class CollectionResolver:
    """Facade that routes playlist/album expansion to provider implementations."""

    def __init__(self, settings: Settings, timeout_seconds: int = 20) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.providers = build_providers(settings)

    async def expand(self, classified: ClassifiedLink) -> list[TrackSearchItem]:
        """Return individual tracks from a supported playlist or album link."""
        if classified.scope == LinkScope.TRACK:
            raise CollectionResolveError("This link is not a playlist or album.", retryable=False)

        provider = provider_for(classified.kind, self.providers)
        if not provider:
            raise CollectionResolveError(
                "This provider does not support playlist or album expansion.", retryable=False
            )

        try:
            tracks = await provider.expand_collection(classified)
        except ProviderError as exc:
            raise CollectionResolveError(str(exc), retryable=exc.retryable) from exc

        # Collection expansion can produce many queue jobs, so enforce the
        # configured cap before handlers create user-visible work items.
        if not tracks:
            raise CollectionResolveError("No tracks found in this collection.", retryable=False)
        if len(tracks) > self.settings.max_collection_tracks:
            raise CollectionResolveError(
                f"Collection has {len(tracks)} tracks, "
                f"above MAX_COLLECTION_TRACKS={self.settings.max_collection_tracks}.",
                retryable=False,
            )
        return tracks
