from __future__ import annotations

from sync_me_maybe.config import Settings
from sync_me_maybe.music.providers.base import ProviderError, TrackSearchItem
from sync_me_maybe.music.providers.registry import build_providers, provider_for
from sync_me_maybe.music.urls import ClassifiedLink, LinkScope


class CollectionResolveError(RuntimeError):
    pass


class CollectionResolver:
    def __init__(self, settings: Settings, timeout_seconds: int = 20) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        self.providers = build_providers(settings)

    async def expand(self, classified: ClassifiedLink) -> list[TrackSearchItem]:
        if classified.scope == LinkScope.TRACK:
            raise CollectionResolveError("This link is not a playlist or album.")

        provider = provider_for(classified.kind, self.providers)
        if not provider:
            raise CollectionResolveError(
                "This provider does not support playlist or album expansion."
            )

        try:
            tracks = await provider.expand_collection(classified)
        except ProviderError as exc:
            raise CollectionResolveError(str(exc)) from exc

        if not tracks:
            raise CollectionResolveError("No tracks found in this collection.")
        if len(tracks) > self.settings.max_collection_tracks:
            raise CollectionResolveError(
                f"Collection has {len(tracks)} tracks, "
                f"above MAX_COLLECTION_TRACKS={self.settings.max_collection_tracks}."
            )
        return tracks
