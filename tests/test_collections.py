from __future__ import annotations

from pathlib import Path

import pytest

from sync_me_maybe.collections import CollectionResolveError, CollectionResolver
from sync_me_maybe.config import Settings
from sync_me_maybe.urls import ClassifiedLink, LinkKind, LinkScope


def settings(tmp_path: Path, **kwargs) -> Settings:
    values = dict(
        telegram_bot_token="123:ABC",
        allowed_telegram_user_ids={1},
        music_dir=tmp_path / "music",
        download_tmp_dir=tmp_path / "tmp",
    )
    values.update(kwargs)
    return Settings(**values)


def test_youtube_flat_playlist_expansion(mocker, tmp_path: Path) -> None:
    class FakeYdl:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def extract_info(self, url, download):
            return {"entries": [{"title": "Song One", "uploader": "Artist", "url": "abc"}]}

    mocker.patch("sync_me_maybe.collections.yt_dlp.YoutubeDL", FakeYdl)

    tracks = CollectionResolver(settings(tmp_path))._expand_sync(
        ClassifiedLink(LinkKind.YOUTUBE, "https://music.youtube.com/playlist?list=abc", LinkScope.PLAYLIST)
    )

    assert len(tracks) == 1
    assert tracks[0].title == "Song One"
    assert tracks[0].artist == "Artist"


def test_spotify_album_without_credentials_expands_from_yt_dlp(mocker, tmp_path: Path) -> None:
    resolver = CollectionResolver(settings(tmp_path))
    mocker.patch.object(
        resolver,
        "_yt_dlp_public_entries",
        return_value=[
            mocker.Mock(title="Song", artist="Artist", album="Album", track_number=2, search_query="Artist Song")
        ],
    )

    tracks = resolver._expand_sync(ClassifiedLink(LinkKind.SPOTIFY, "https://open.spotify.com/album/abc", LinkScope.ALBUM))

    assert tracks[0].search_query == "Artist Song"


def test_spotify_public_html_embedded_json_expands_tracks(mocker, tmp_path: Path) -> None:
    class Response:
        text = """
        <script type="application/json">
        {"tracks":[{"name":"Song","artists":[{"name":"Artist"}],"albumName":"Album","trackNumber":2}]}
        </script>
        """

        def raise_for_status(self):
            return None

    resolver = CollectionResolver(settings(tmp_path))
    mocker.patch.object(resolver, "_yt_dlp_public_entries", return_value=[])
    mocker.patch("sync_me_maybe.collections.requests.get", return_value=Response())

    tracks = resolver._expand_sync(ClassifiedLink(LinkKind.SPOTIFY, "https://open.spotify.com/album/abc", LinkScope.ALBUM))

    assert tracks[0].search_query == "Artist Song"
    assert tracks[0].album == "Album"
    assert tracks[0].track_number == 2


def test_spotify_public_html_without_tracks_raises_clear_error(mocker, tmp_path: Path) -> None:
    class Response:
        text = "<html><script>{}</script></html>"

        def raise_for_status(self):
            return None

    resolver = CollectionResolver(settings(tmp_path))
    mocker.patch.object(resolver, "_yt_dlp_public_entries", return_value=[])
    mocker.patch("sync_me_maybe.collections.requests.get", return_value=Response())

    with pytest.raises(CollectionResolveError, match="Could not expand this Spotify collection"):
        resolver._expand_sync(
            ClassifiedLink(LinkKind.SPOTIFY, "https://open.spotify.com/album/abc", LinkScope.ALBUM)
        )


def test_apple_album_without_token_expands_from_yt_dlp(mocker, tmp_path: Path) -> None:
    resolver = CollectionResolver(settings(tmp_path))
    mocker.patch.object(
        resolver,
        "_yt_dlp_public_entries",
        return_value=[
            mocker.Mock(title="Song", artist="Artist", album="Album", track_number=1, search_query="Artist Song")
        ],
    )

    tracks = resolver._expand_sync(
        ClassifiedLink(LinkKind.APPLE_MUSIC, "https://music.apple.com/us/album/name/123", LinkScope.ALBUM)
    )

    assert tracks[0].search_query == "Artist Song"


def test_apple_public_html_json_ld_normalizes_tracks(mocker, tmp_path: Path) -> None:
    class Response:
        text = """
        <script type="application/ld+json">
        {"@type":"MusicRecording","name":"Song","byArtist":{"name":"Artist"},"album":"Album","trackNumber":1}
        </script>
        """

        def raise_for_status(self):
            return None

    resolver = CollectionResolver(settings(tmp_path))
    mocker.patch.object(resolver, "_yt_dlp_public_entries", return_value=[])
    mocker.patch("sync_me_maybe.collections.requests.get", return_value=Response())

    tracks = resolver._expand_sync(
        ClassifiedLink(LinkKind.APPLE_MUSIC, "https://music.apple.com/us/album/name/123", LinkScope.ALBUM)
    )

    assert tracks[0].search_query == "Artist Song"
    assert tracks[0].album == "Album"


def test_apple_public_html_without_tracks_raises_clear_error(mocker, tmp_path: Path) -> None:
    class Response:
        text = "<html><script>{}</script></html>"

        def raise_for_status(self):
            return None

    resolver = CollectionResolver(settings(tmp_path))
    mocker.patch.object(resolver, "_yt_dlp_public_entries", return_value=[])
    mocker.patch("sync_me_maybe.collections.requests.get", return_value=Response())

    with pytest.raises(CollectionResolveError, match="Could not expand this Apple Music collection"):
        resolver._expand_sync(
            ClassifiedLink(LinkKind.APPLE_MUSIC, "https://music.apple.com/us/album/name/123", LinkScope.ALBUM)
        )


def test_collection_size_limit(tmp_path: Path, mocker) -> None:
    resolver = CollectionResolver(settings(tmp_path, max_collection_tracks=1))
    mocker.patch.object(
        resolver,
        "_youtube_playlist",
        return_value=[
            mocker.Mock(title="One", search_query="One"),
            mocker.Mock(title="Two", search_query="Two"),
        ],
    )

    with pytest.raises(CollectionResolveError, match="above MAX_COLLECTION_TRACKS"):
        resolver._expand_sync(ClassifiedLink(LinkKind.YOUTUBE, "https://music.youtube.com/playlist?list=abc", LinkScope.PLAYLIST))
