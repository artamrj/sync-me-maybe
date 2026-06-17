from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from sync_me_maybe.config import Settings
from sync_me_maybe.music.collections import CollectionResolveError, CollectionResolver
from sync_me_maybe.music.providers.apple import AppleMusicProvider
from sync_me_maybe.music.providers.base import ProviderError, TrackSearchItem
from sync_me_maybe.music.providers.public_scrape import PublicCollectionScraper
from sync_me_maybe.music.providers.shazam import ShazamProvider
from sync_me_maybe.music.providers.spotify import SpotifyProvider
from sync_me_maybe.music.providers.youtube import YouTubeProvider
from sync_me_maybe.music.urls import LinkKind, LinkScope, classify_url


class ProviderClassificationTests(unittest.TestCase):
    def test_classifies_supported_provider_scopes(self) -> None:
        cases = [
            (
                "https://music.youtube.com/playlist?list=abc",
                LinkKind.YOUTUBE,
                LinkScope.PLAYLIST,
            ),
            ("https://youtu.be/video-id", LinkKind.YOUTUBE, LinkScope.TRACK),
            ("https://open.spotify.com/playlist/abc", LinkKind.SPOTIFY, LinkScope.PLAYLIST),
            ("https://open.spotify.com/album/abc", LinkKind.SPOTIFY, LinkScope.ALBUM),
            ("https://music.apple.com/us/album/name/123", LinkKind.APPLE_MUSIC, LinkScope.ALBUM),
            (
                "https://music.apple.com/us/album/name/123?i=456",
                LinkKind.APPLE_MUSIC,
                LinkScope.TRACK,
            ),
            ("https://www.shazam.com/track/123/name", LinkKind.SHAZAM, LinkScope.TRACK),
        ]

        for url, kind, scope in cases:
            with self.subTest(url=url):
                classified = classify_url(url)
                self.assertEqual(classified.kind, kind)
                self.assertEqual(classified.scope, scope)

    def test_classifies_unsupported_url(self) -> None:
        classified = classify_url("https://example.com/song")

        self.assertEqual(classified.kind, LinkKind.UNSUPPORTED)
        self.assertIn("Unsupported link", classified.reason or "")


class TrackResolutionTests(unittest.TestCase):
    def test_spotify_uses_oembed_before_page_metadata(self) -> None:
        provider = SpotifyProvider()
        link = classify_url("https://open.spotify.com/track/abc")

        with (
            patch.object(
                provider,
                "_spotify_oembed",
                return_value=("Artist Song", "Artist Song", None, None),
            ) as oembed,
            patch.object(provider, "_page_metadata") as page_metadata,
        ):
            resolved = asyncio.run(provider.resolve_track(link))

        oembed.assert_called_once()
        page_metadata.assert_not_called()
        self.assertEqual(resolved.download_url, "ytsearch1:Artist Song")
        self.assertEqual(resolved.search_query, "Artist Song")

    def test_spotify_falls_back_to_page_metadata_then_slug(self) -> None:
        provider = SpotifyProvider()
        link = classify_url("https://open.spotify.com/track/artist-song")

        with (
            patch.object(provider, "_spotify_oembed", return_value=None),
            patch.object(provider, "_page_metadata", return_value=("Song", "Artist", "Album")),
        ):
            resolved = asyncio.run(provider.resolve_track(link))

        self.assertEqual(resolved.search_query, "Artist Song")
        self.assertEqual(resolved.title, "Song")
        self.assertEqual(resolved.artist, "Artist")
        self.assertEqual(resolved.album, "Album")

        with (
            patch.object(provider, "_spotify_oembed", return_value=None),
            patch.object(provider, "_page_metadata", side_effect=ProviderError("fetch failed")),
        ):
            fallback = asyncio.run(provider.resolve_track(link))

        self.assertEqual(fallback.search_query, "artist song")

    def test_apple_and_shazam_slug_resolution(self) -> None:
        apple_link = classify_url("https://music.apple.com/us/album/song-name/123?i=456")
        shazam_link = classify_url("https://www.shazam.com/track/123456/song-name-artist-name")

        apple = asyncio.run(AppleMusicProvider().resolve_track(apple_link))
        shazam = asyncio.run(ShazamProvider().resolve_track(shazam_link))

        self.assertEqual(apple.download_url, "ytsearch1:song name")
        self.assertEqual(shazam.download_url, "ytsearch1:song name artist name")


class CollectionExpansionTests(unittest.TestCase):
    def test_youtube_playlist_entry_conversion(self) -> None:
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

        with patch(
            "sync_me_maybe.music.providers.youtube.yt_dlp.YoutubeDL",
            return_value=ydl_context,
        ):
            tracks = asyncio.run(
                provider.expand_collection(classify_url("https://youtube.com/playlist?list=abc"))
            )

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0].title, "First Song")
        self.assertEqual(tracks[0].artist, "Artist")
        self.assertEqual(tracks[0].track_number, 1)

    def test_public_scraper_reads_json_scripts_and_dedupes(self) -> None:
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
            patch.object(scraper, "yt_dlp_entries", return_value=[]),
            patch(
                "sync_me_maybe.music.providers.public_scrape.requests.get",
                return_value=response,
            ),
        ):
            tracks = scraper.collection("https://open.spotify.com/playlist/abc")

        self.assertEqual(tracks, [TrackSearchItem("Song", "Artist", "Album", 2, None)])

    def test_collection_resolver_enforces_empty_and_limit(self) -> None:
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

        provider.expand_collection.return_value = []
        with self.assertRaisesRegex(CollectionResolveError, "No tracks found"):
            asyncio.run(resolver.expand(link))

        provider.expand_collection.return_value = [
            TrackSearchItem("One"),
            TrackSearchItem("Two"),
        ]
        with self.assertRaisesRegex(CollectionResolveError, "MAX_COLLECTION_TRACKS=1"):
            asyncio.run(resolver.expand(link))


if __name__ == "__main__":
    unittest.main()
