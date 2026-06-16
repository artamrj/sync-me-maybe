from __future__ import annotations

from sync_me_maybe.urls import LinkKind, LinkScope, classify_url, extract_first_url, extract_urls


def test_extract_first_url_from_text() -> None:
    assert extract_first_url("please sync https://music.youtube.com/watch?v=abc.") == "https://music.youtube.com/watch?v=abc"


def test_extract_urls_returns_all_links_in_order() -> None:
    assert extract_urls(
        "one https://music.youtube.com/watch?v=abc two https://open.spotify.com/track/def"
    ) == [
        "https://music.youtube.com/watch?v=abc",
        "https://open.spotify.com/track/def",
    ]


def test_extract_urls_strips_trailing_punctuation_and_deduplicates() -> None:
    assert extract_urls(
        "https://music.youtube.com/watch?v=abc, https://music.youtube.com/watch?v=abc. https://www.shazam.com/track/1/song]"
    ) == [
        "https://music.youtube.com/watch?v=abc",
        "https://www.shazam.com/track/1/song",
    ]


def test_classify_youtube_music_track() -> None:
    classified = classify_url("https://music.youtube.com/watch?v=abc&list=RDAMVMabc")
    assert classified.kind == LinkKind.YOUTUBE


def test_classify_youtube_playlist_without_track() -> None:
    classified = classify_url("https://music.youtube.com/playlist?list=PLabc")
    assert classified.kind == LinkKind.YOUTUBE
    assert classified.scope == LinkScope.PLAYLIST


def test_classify_spotify_collections() -> None:
    assert classify_url("https://open.spotify.com/playlist/abc").scope == LinkScope.PLAYLIST
    assert classify_url("https://open.spotify.com/album/abc").scope == LinkScope.ALBUM


def test_classify_apple_album_without_track_id() -> None:
    classified = classify_url("https://music.apple.com/us/album/name/123")
    assert classified.kind == LinkKind.APPLE_MUSIC
    assert classified.scope == LinkScope.ALBUM


def test_classify_supported_service_links() -> None:
    assert classify_url("https://open.spotify.com/track/abc").kind == LinkKind.SPOTIFY
    assert classify_url("https://music.apple.com/us/album/song/123?i=456").kind == LinkKind.APPLE_MUSIC
    assert classify_url("https://www.shazam.com/track/123/song").kind == LinkKind.SHAZAM


def test_reject_unsupported_link() -> None:
    classified = classify_url("https://example.com/song")
    assert classified.kind == LinkKind.UNSUPPORTED
