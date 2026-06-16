from __future__ import annotations

import requests

from sync_me_maybe.resolver import LinkResolver, ResolveError
from sync_me_maybe.urls import LinkKind, ClassifiedLink


def test_youtube_resolves_to_same_download_url() -> None:
    resolved = LinkResolver().resolve(ClassifiedLink(LinkKind.YOUTUBE, "https://music.youtube.com/watch?v=abc"))
    assert resolved.download_url == "https://music.youtube.com/watch?v=abc"


def test_service_link_resolves_to_youtube_search(mocker) -> None:
    resolver = LinkResolver()
    mocker.patch.object(resolver, "_query_for", return_value=("Artist Song", "Song", "Artist", None))

    resolved = resolver.resolve(ClassifiedLink(LinkKind.APPLE_MUSIC, "https://music.apple.com/x"))

    assert resolved.download_url == "ytsearch1:Artist Song"
    assert resolved.title == "Song"
    assert resolved.artist == "Artist"


def test_apple_music_track_resolves_from_url_without_metadata(mocker) -> None:
    resolver = LinkResolver()
    page_metadata = mocker.patch.object(resolver, "_page_metadata")

    resolved = resolver.resolve(
        ClassifiedLink(LinkKind.APPLE_MUSIC, "https://music.apple.com/us/album/night-drive/1234567890?i=987654321")
    )

    assert resolved.download_url == "ytsearch1:night drive"
    assert resolved.search_query == "night drive"
    page_metadata.assert_not_called()


def test_shazam_track_resolves_from_url_without_metadata(mocker) -> None:
    resolver = LinkResolver()
    page_metadata = mocker.patch.object(resolver, "_page_metadata")
    get = mocker.patch("sync_me_maybe.resolver.requests.get")

    resolved = resolver.resolve(ClassifiedLink(LinkKind.SHAZAM, "https://www.shazam.com/track/123456789/night-drive"))

    assert resolved.download_url == "ytsearch1:night drive"
    assert resolved.search_query == "night drive"
    page_metadata.assert_not_called()
    get.assert_not_called()


def test_shazam_localized_track_resolves_from_url() -> None:
    resolved = LinkResolver().resolve(ClassifiedLink(LinkKind.SHAZAM, "https://www.shazam.com/de/track/123/night-drive"))

    assert resolved.search_query == "night drive"


def test_shazam_numeric_track_resolves_from_redirected_metadata(mocker) -> None:
    class Response:
        url = "https://www.shazam.com/song/1876297593/the-way-we-touch?referrer=share"
        text = """
        <html><head>
        <meta property="og:title" content="The Way We Touch - Charlotte Cardin: Song Lyrics, Music Videos & Concerts"/>
        </head></html>
        """

        def raise_for_status(self) -> None:
            return None

    get = mocker.patch("sync_me_maybe.resolver.requests.get", return_value=Response())

    resolved = LinkResolver().resolve(ClassifiedLink(LinkKind.SHAZAM, "https://www.shazam.com/track/861565079?referrer=share"))

    assert resolved.search_query == "Charlotte Cardin The Way We Touch"
    assert resolved.title == "The Way We Touch"
    assert resolved.artist == "Charlotte Cardin"
    get.assert_called_once()


def test_shazam_numeric_track_supports_unicode_metadata(mocker) -> None:
    class Response:
        url = "https://www.shazam.com/song/1864796243/del-tanha-%D8%AF%D9%84-%D8%AA%D9%86%D9%87%D8%A7"
        text = """
        <html><head>
        <meta name="twitter:title" content="Del tanha &quot;دل تنها&quot; - ONEDAM: Song Lyrics, Music Videos & Concerts"/>
        </head></html>
        """

        def raise_for_status(self) -> None:
            return None

    mocker.patch("sync_me_maybe.resolver.requests.get", return_value=Response())

    resolved = LinkResolver().resolve(ClassifiedLink(LinkKind.SHAZAM, "https://www.shazam.com/track/855119670?referrer=share"))

    assert resolved.search_query == 'ONEDAM Del tanha "دل تنها"'


def test_shazam_numeric_track_falls_back_to_redirect_slug(mocker) -> None:
    class Response:
        url = "https://www.shazam.com/song/1730969635/aria?referrer=share"
        text = "<html><head></head></html>"

        def raise_for_status(self) -> None:
            return None

    mocker.patch("sync_me_maybe.resolver.requests.get", return_value=Response())

    resolved = LinkResolver().resolve(ClassifiedLink(LinkKind.SHAZAM, "https://www.shazam.com/track/677829655?referrer=share"))

    assert resolved.search_query == "aria"


def test_shazam_numeric_track_network_failure_raises_clear_error(mocker) -> None:
    mocker.patch("sync_me_maybe.resolver.requests.get", side_effect=requests.RequestException("blocked"))

    try:
        LinkResolver().resolve(ClassifiedLink(LinkKind.SHAZAM, "https://www.shazam.com/track/123456789?referrer=share"))
    except ResolveError as exc:
        assert "search query" in str(exc)
    else:
        raise AssertionError("Expected ResolveError")


def test_spotify_falls_back_to_url_slug_when_metadata_fails(mocker) -> None:
    resolver = LinkResolver()
    mocker.patch.object(resolver, "_spotify_oembed", return_value=None)
    mocker.patch.object(resolver, "_page_metadata", side_effect=ResolveError("blocked"))

    resolved = resolver.resolve(ClassifiedLink(LinkKind.SPOTIFY, "https://open.spotify.com/track/night-drive"))

    assert resolved.search_query == "night drive"


def test_unparseable_service_url_raises() -> None:
    try:
        LinkResolver().resolve(ClassifiedLink(LinkKind.SHAZAM, "https://www.shazam.com/song/123456789"))
    except ResolveError as exc:
        assert "search query" in str(exc)
    else:
        raise AssertionError("Expected ResolveError")
