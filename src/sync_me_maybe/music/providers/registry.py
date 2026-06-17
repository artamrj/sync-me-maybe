"""Provider factory and lookup helpers."""

from __future__ import annotations

from sync_me_maybe.config import Settings
from sync_me_maybe.music.providers.apple import AppleMusicProvider
from sync_me_maybe.music.providers.base import Provider
from sync_me_maybe.music.providers.shazam import ShazamProvider
from sync_me_maybe.music.providers.spotify import SpotifyProvider
from sync_me_maybe.music.providers.youtube import YouTubeProvider
from sync_me_maybe.music.urls import LinkKind


def build_providers(settings: Settings | None = None) -> list[Provider]:
    """Create provider adapters in classification priority order."""
    return [
        YouTubeProvider(settings),
        SpotifyProvider(),
        AppleMusicProvider(),
        ShazamProvider(),
    ]


def provider_for(kind: LinkKind, providers: list[Provider] | None = None) -> Provider | None:
    """Return the provider that handles a classified link kind."""
    for provider in providers or build_providers():
        if provider.kind == kind:
            return provider
    return None
