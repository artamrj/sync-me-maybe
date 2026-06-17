"""Resolve one classified music link into a downloadable track."""

from __future__ import annotations

from sync_me_maybe.music.providers.base import ProviderError, ResolvedTrack
from sync_me_maybe.music.providers.registry import build_providers, provider_for
from sync_me_maybe.music.urls import ClassifiedLink


class ResolveError(RuntimeError):
    """User-facing track resolution failure with retry metadata."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class LinkResolver:
    """Facade that routes single-track links to the correct provider."""

    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds
        self.providers = build_providers()

    async def resolve(self, classified: ClassifiedLink) -> ResolvedTrack:
        """Resolve provider metadata into the YouTube/search target to download."""
        provider = provider_for(classified.kind, self.providers)
        if not provider:
            raise ResolveError(classified.reason or "Unsupported link.", retryable=False)

        try:
            return await provider.resolve_track(classified)
        except ProviderError as exc:
            raise ResolveError(str(exc), retryable=exc.retryable) from exc
