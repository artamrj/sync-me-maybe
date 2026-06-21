from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import pytest

from sync_me_maybe.config import Settings
from sync_me_maybe.music.collections import CollectionResolveError, CollectionResolver
from sync_me_maybe.music.providers.apple import AppleMusicProvider
from sync_me_maybe.music.providers.base import ExpandedCollection, ProviderError, TrackSearchItem
from sync_me_maybe.music.providers.common import (
    clean_slug,
    int_or_none,
    slug_query,
    usable_slug_query,
)
from sync_me_maybe.music.providers.public_scrape import (
    PublicCollectionScraper,
    balanced_object,
    collection_metadata,
    collection_metadata_from_page_title,
    dedupe_tracks,
    extract_balanced_json_objects,
    track_from_dict,
    tracks_from_entries,
)
from sync_me_maybe.music.providers.shazam import ShazamProvider
from sync_me_maybe.music.providers.spotify import SpotifyProvider
from sync_me_maybe.music.providers.youtube import YouTubeProvider
from sync_me_maybe.music.resolver import LinkResolver, ResolveError
from sync_me_maybe.music.urls import (
    LinkKind,
    LinkScope,
    classify_url,
    extract_first_url,
    extract_urls,
)


@pytest.mark.parametrize(
    ("url", "kind", "scope"),
    [
        ("https://music.youtube.com/playlist?list=abc", LinkKind.YOUTUBE, LinkScope.PLAYLIST),
        ("https://youtu.be/video-id", LinkKind.YOUTUBE, LinkScope.TRACK),
        ("https://open.spotify.com/playlist/abc", LinkKind.SPOTIFY, LinkScope.PLAYLIST),
        ("https://open.spotify.com/album/abc", LinkKind.SPOTIFY, LinkScope.ALBUM),
        ("https://music.apple.com/us/album/name/123", LinkKind.APPLE_MUSIC, LinkScope.ALBUM),
        ("https://music.apple.com/us/album/name/123?i=456", LinkKind.APPLE_MUSIC, LinkScope.TRACK),
        ("https://www.shazam.com/track/123/name", LinkKind.SHAZAM, LinkScope.TRACK),
    ],
)
def test_classifies_supported_provider_scopes(url: str, kind: LinkKind, scope: LinkScope) -> None:
    classified = classify_url(url)
    assert classified.kind == kind
    assert classified.scope == scope


def test_url_extraction_dedupes_and_reports_unsupported() -> None:
    text = "Try https://a.test/song, then https://a.test/song and https://b.test/x]"
    assert extract_urls(text) == ["https://a.test/song", "https://b.test/x"]
    assert extract_first_url(text) == "https://a.test/song"
    classified = classify_url("https://example.com/song")
    assert classified.kind == LinkKind.UNSUPPORTED
    assert "Unsupported link" in (classified.reason or "")


def test_common_slug_helpers() -> None:
    assert clean_slug("Song-Name-12345") == "Song Name"
    assert slug_query("https://example.com/artist/track/song-name") == "song name"
    assert not usable_slug_query("album")
    assert not usable_slug_query("123")
    assert int_or_none("4") == 4
    assert int_or_none("bad") is None


@pytest.mark.asyncio
async def test_link_resolver_maps_provider_errors() -> None:
    provider = Mock()
    provider.kind = LinkKind.SPOTIFY
    provider.resolve_track = AsyncMock(side_effect=ProviderError("temporary", retryable=True))
    resolver = LinkResolver()
    resolver.providers = [provider]

    with pytest.raises(ResolveError) as exc:
        await resolver.resolve(classify_url("https://open.spotify.com/track/abc"))

    assert exc.value.retryable
    with pytest.raises(ResolveError, match="Unsupported"):
        await resolver.resolve(classify_url("https://example.com/nope"))


@pytest.mark.asyncio
async def test_spotify_resolution_prefers_oembed_then_metadata_then_slug() -> None:
    provider = SpotifyProvider()
    link = classify_url("https://open.spotify.com/track/artist-song")

    with (
        patch.object(
            provider,
            "_spotify_oembed",
            return_value=("Artist Song", "Artist Song", None, None),
        ) as oembed,
        patch.object(provider, "_page_metadata") as page_metadata,
    ):
        resolved = await provider.resolve_track(link)
    oembed.assert_called_once()
    page_metadata.assert_not_called()
    assert resolved.download_url == "ytsearch1:Artist Song"

    with (
        patch.object(provider, "_spotify_oembed", return_value=None),
        patch.object(provider, "_page_metadata", return_value=("Song", "Artist", "Album")),
    ):
        metadata = await provider.resolve_track(link)
    assert metadata.search_query == "Artist Song"
    assert metadata.album == "Album"

    with (
        patch.object(provider, "_spotify_oembed", return_value=None),
        patch.object(provider, "_page_metadata", side_effect=ProviderError("fetch failed")),
    ):
        fallback = await provider.resolve_track(link)
    assert fallback.search_query == "artist song"


@pytest.mark.asyncio
async def test_apple_and_shazam_slug_resolution_and_numeric_fetch() -> None:
    apple_link = classify_url("https://music.apple.com/us/album/song-name/123?i=456")
    shazam_link = classify_url("https://www.shazam.com/track/123456/song-name-artist-name")
    apple = await AppleMusicProvider().resolve_track(apple_link)
    shazam = await ShazamProvider().resolve_track(shazam_link)
    assert apple.download_url == "ytsearch1:song name"
    assert shazam.download_url == "ytsearch1:song name artist name"

    response = Mock()
    response.text = '<meta property="og:title" content="Song - Artist | Shazam">'
    response.url = "https://www.shazam.com/track/123"
    response.raise_for_status.return_value = None
    with patch("sync_me_maybe.music.providers.shazam.requests.get", return_value=response):
        numeric = await ShazamProvider().resolve_track(
            classify_url("https://www.shazam.com/track/123")
        )
    assert numeric.search_query == "Artist Song"


@pytest.mark.asyncio
async def test_collection_providers_expand_or_raise() -> None:
    spotify = SpotifyProvider()
    apple = AppleMusicProvider()
    collection = ExpandedCollection([TrackSearchItem("Song")], owner="Owner", title="Playlist")
    with patch.object(spotify.public_scraper, "collection", return_value=collection):
        assert (
            await spotify.expand_collection(classify_url("https://open.spotify.com/playlist/abc"))
            == collection
        )
    with (
        patch.object(apple, "_catalog_collection", return_value=ExpandedCollection([])),
        patch.object(apple.public_scraper, "collection", return_value=ExpandedCollection([])),
    ):
        with pytest.raises(ProviderError, match="Could not expand"):
            await apple.expand_collection(
                classify_url("https://music.apple.com/us/playlist/name/pl.1")
            )
    with pytest.raises(ProviderError, match="does not support"):
        await ShazamProvider().expand_collection(
            classify_url("https://www.shazam.com/track/1/name")
        )


def test_spotify_collection_falls_back_to_embed_page() -> None:
    provider = SpotifyProvider()
    link = classify_url("https://open.spotify.com/playlist/abc?si=token")
    original = ExpandedCollection([], owner="Owner", title="Playlist")
    embedded = ExpandedCollection([TrackSearchItem("Song", "Artist")])

    with patch.object(
        provider.public_scraper, "collection", side_effect=[original, embedded]
    ) as collection:
        assert provider._collection_sync(link) == ExpandedCollection(
            [TrackSearchItem("Song", "Artist")], owner="Owner", title="Playlist"
        )

    assert collection.call_args_list[0].args == ("https://open.spotify.com/playlist/abc?si=token",)
    assert collection.call_args_list[1].args == ("https://open.spotify.com/embed/playlist/abc",)


def test_apple_playlist_catalog_expansion_follows_pagination() -> None:
    provider = AppleMusicProvider()

    responses = [
        apple_response(text='<script src="/assets/index~abc123.js"></script>'),
        apple_response(text='const go="1";qc="token";'),
        apple_response(
            json_data={
                "data": [
                    {
                        "type": "playlists",
                        "relationships": {
                            "tracks": {
                                "data": [
                                    apple_song("1", "First", "Artist", "Album", 1),
                                    apple_song("2", "Second", "Artist", "Album", 2),
                                ],
                                "next": "/v1/catalog/de/playlists/pl.test/tracks?l=en-DE&offset=2",
                            }
                        },
                    }
                ]
            }
        ),
        apple_response(
            json_data={
                "data": [
                    apple_song("3", "Third", "Other", "Album", 3),
                    apple_song("4", "Fourth", "Other", "Album", 4),
                ]
            }
        ),
    ]

    with patch("sync_me_maybe.music.providers.apple.requests.get", side_effect=responses) as get:
        tracks = provider._catalog_collection(
            "https://music.apple.com/de/playlist/all/pl.test?l=en"
        )

    assert tracks.tracks == [
        TrackSearchItem("First", "Artist", "Album", 1, "https://music.apple.com/song/1"),
        TrackSearchItem("Second", "Artist", "Album", 2, "https://music.apple.com/song/2"),
        TrackSearchItem("Third", "Other", "Album", 3, "https://music.apple.com/song/3"),
        TrackSearchItem("Fourth", "Other", "Album", 4, "https://music.apple.com/song/4"),
    ]
    assert tracks.owner is None
    assert tracks.title is None
    assert get.call_args_list[2].args[0] == (
        "https://amp-api.music.apple.com/v1/catalog/de/playlists/pl.test?l=en-DE&include=tracks"
    )
    assert get.call_args_list[3].args[0] == (
        "https://amp-api.music.apple.com/v1/catalog/de/playlists/pl.test/tracks?l=en-DE&offset=2"
    )


def test_apple_catalog_expansion_preserves_duplicate_songs() -> None:
    provider = AppleMusicProvider()
    responses = [
        apple_response(text='<script src="/assets/index~abc123.js"></script>'),
        apple_response(text='qc="token";'),
        apple_response(
            json_data={
                "data": [
                    {
                        "type": "playlists",
                        "relationships": {
                            "tracks": {
                                "data": [
                                    apple_song("1", "Same", "Artist", "Album", 1),
                                    apple_song("2", "Same", "Artist", "Album", 2),
                                ]
                            }
                        },
                    }
                ]
            }
        ),
    ]

    with patch("sync_me_maybe.music.providers.apple.requests.get", side_effect=responses):
        tracks = provider._catalog_collection("https://music.apple.com/us/playlist/all/pl.test")

    assert tracks.tracks == [
        TrackSearchItem("Same", "Artist", "Album", 1, "https://music.apple.com/song/1"),
        TrackSearchItem("Same", "Artist", "Album", 2, "https://music.apple.com/song/2"),
    ]


def apple_song(
    track_id: str, title: str, artist: str, album: str, track_number: int
) -> dict[str, object]:
    return {
        "id": track_id,
        "type": "songs",
        "attributes": {
            "name": title,
            "artistName": artist,
            "albumName": album,
            "trackNumber": track_number,
            "url": f"https://music.apple.com/song/{track_id}",
        },
    }


def apple_response(*, text: str = "", json_data: dict[str, object] | None = None) -> Mock:
    response = Mock()
    response.text = text
    response.raise_for_status.return_value = None
    response.json.return_value = json_data or {}
    return response


@pytest.mark.asyncio
async def test_youtube_playlist_entry_conversion() -> None:
    provider = YouTubeProvider()
    ydl = Mock()
    ydl.extract_info.return_value = {
        "entries": [
            {"title": "First Song", "uploader": "Artist", "webpage_url": "https://youtu.be/1"},
            {"title": "", "uploader": "Ignored"},
        ]
    }
    ydl_context = Mock()
    ydl_context.__enter__ = Mock(return_value=ydl)
    ydl_context.__exit__ = Mock(return_value=None)

    with patch("sync_me_maybe.music.providers.youtube.yt_dlp.YoutubeDL", return_value=ydl_context):
        tracks = await provider.expand_collection(
            classify_url("https://youtube.com/playlist?list=abc")
        )

    assert tracks.tracks == [TrackSearchItem("First Song", "Artist", None, 1, "https://youtu.be/1")]


def test_public_scraper_reads_json_scripts_balanced_objects_and_dedupes() -> None:
    html = """
    <script type="application/json">
    {"tracks": [
      {"title": "Song", "artistName": "Artist", "albumName": "Album", "trackNumber": 2},
      {"title": "Song", "artistName": "Artist", "albumName": "Album", "trackNumber": 2}
    ]}
    </script>
    """
    response = Mock()
    response.text = html
    response.raise_for_status.return_value = None

    scraper = PublicCollectionScraper()
    with (
        patch.object(scraper, "yt_dlp_collection", return_value=ExpandedCollection([])),
        patch("sync_me_maybe.music.providers.public_scrape.requests.get", return_value=response),
    ):
        tracks = scraper.collection("https://open.spotify.com/playlist/abc")
    assert tracks.tracks == [TrackSearchItem("Song", "Artist", "Album", 2, None)]

    assert balanced_object('x {"tracks":[{"title":"A"}]} y', 2) == '{"tracks":[{"title":"A"}]}'
    assert extract_balanced_json_objects('x {"trackName":"B","artistName":"A"}')
    assert dedupe_tracks([TrackSearchItem("S", "A"), TrackSearchItem("S", "A")]) == [
        TrackSearchItem("S", "A")
    ]


def test_public_scraper_track_conversion_filters_non_tracks() -> None:
    assert tracks_from_entries([{"title": "Song", "uploader": "Artist"}])[0].track_number == 1
    assert track_from_dict({"name": "Page Title"}) is None
    assert track_from_dict(
        {
            "entityType": "track",
            "title": "Je suis fan",
            "subtitle": "Alice et Moi",
            "uri": "spotify:track:0ek3SCgTcQBeRE897H2IDp",
        }
    ) == TrackSearchItem(
        title="Je suis fan",
        artist="Alice et Moi",
        album=None,
        track_number=None,
        source_url="spotify:track:0ek3SCgTcQBeRE897H2IDp",
    )
    assert track_from_dict(
        {"@type": "MusicRecording", "name": "Song", "byArtist": {"name": "Artist"}}
    ) == TrackSearchItem(
        title="Song",
        artist="Artist",
        album=None,
        track_number=None,
        source_url=None,
    )


def test_public_scraper_reads_spotify_embed_collection_metadata() -> None:
    assert collection_metadata_from_page_title("feels - playlist by Romy Brunner | Spotify") == (
        "Romy Brunner",
        "feels",
    )
    assert collection_metadata(
        [
            {
                "props": {
                    "pageProps": {
                        "state": {
                            "data": {
                                "entity": {
                                    "type": "playlist",
                                    "title": "femme",
                                    "subtitle": "Valeria G!",
                                    "trackList": [{"title": "Song"}],
                                }
                            }
                        }
                    }
                }
            }
        ]
    ) == ("Valeria G!", "femme")


@pytest.mark.asyncio
async def test_collection_resolver_enforces_scope_empty_and_limit() -> None:
    settings = Settings(
        telegram_bot_token="token",
        allowed_telegram_user_ids={1},
        music_dir=Path("/music"),
        download_tmp_dir=Path("/tmp"),
        max_collection_tracks=1,
    )
    resolver = CollectionResolver(settings)
    provider = Mock()
    provider.kind = LinkKind.SPOTIFY
    provider.expand_collection = AsyncMock()
    resolver.providers = [provider]
    link = classify_url("https://open.spotify.com/playlist/abc")

    with pytest.raises(CollectionResolveError, match="not a playlist"):
        await resolver.expand(classify_url("https://open.spotify.com/track/abc"))

    provider.expand_collection.return_value = ExpandedCollection([])
    with pytest.raises(CollectionResolveError, match="No tracks found"):
        await resolver.expand(link)

    provider.expand_collection.return_value = ExpandedCollection(
        [TrackSearchItem("One"), TrackSearchItem("Two")]
    )
    with pytest.raises(CollectionResolveError, match="MAX_COLLECTION_TRACKS=1"):
        await resolver.expand(link)
