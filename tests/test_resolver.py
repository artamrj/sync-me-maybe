from __future__ import annotations

import pytest

from sync_me_maybe.resolver import LinkResolver, ResolveError
from sync_me_maybe.urls import LinkKind, ClassifiedLink


def test_youtube_resolves_to_same_download_url() -> None:
    resolved = LinkResolver().resolve(ClassifiedLink(LinkKind.YOUTUBE, "https://music.youtube.com/watch?v=abc"))
    assert resolved.download_url == "https://music.youtube.com/watch?v=abc"


def test_service_link_resolves_to_youtube_search(mocker) -> None:
    resolver = LinkResolver()
    mocker.patch.object(resolver, "_metadata_query", return_value=("Artist Song", "Song", "Artist", None))

    resolved = resolver.resolve(ClassifiedLink(LinkKind.APPLE_MUSIC, "https://music.apple.com/x"))

    assert resolved.download_url == "ytsearch1:Artist Song"
    assert resolved.title == "Song"
    assert resolved.artist == "Artist"


def test_service_link_without_metadata_raises(mocker) -> None:
    resolver = LinkResolver()
    mocker.patch.object(resolver, "_metadata_query", return_value=(None, None, None, None))

    with pytest.raises(ResolveError):
        resolver.resolve(ClassifiedLink(LinkKind.SHAZAM, "https://www.shazam.com/track/x"))
