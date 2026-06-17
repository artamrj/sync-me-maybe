from __future__ import annotations

from sync_me_maybe.music.providers.base import ProviderError, ResolvedTrack
from sync_me_maybe.music.providers.registry import build_providers, provider_for
from sync_me_maybe.music.urls import ClassifiedLink


class ResolveError(RuntimeError):
    pass


class LinkResolver:
    def __init__(self, timeout_seconds: int = 20) -> None:
        self.timeout_seconds = timeout_seconds
        self.providers = build_providers()

    async def resolve(self, classified: ClassifiedLink) -> ResolvedTrack:
        provider = provider_for(classified.kind, self.providers)
        if not provider:
            raise ResolveError(classified.reason or "Unsupported link.")

        try:
            return await provider.resolve_track(classified)
        except ProviderError as exc:
            raise ResolveError(str(exc)) from exc
