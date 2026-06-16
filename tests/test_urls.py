from __future__ import annotations

from sync_me_maybe.urls import LinkKind, classify_url, extract_first_url


def test_extract_first_url_from_text() -> None:
    assert extract_first_url("please sync https://music.youtube.com/watch?v=abc.") == "https://music.youtube.com/watch?v=abc"


def test_classify_youtube_music_track() -> None:
    classified = classify_url("https://music.youtube.com/watch?v=abc&list=RDAMVMabc")
    assert classified.kind == LinkKind.YOUTUBE


def test_reject_youtube_playlist_without_track() -> None:
    classified = classify_url("https://music.youtube.com/playlist?list=PLabc")
    assert classified.kind == LinkKind.UNSUPPORTED
    assert "Playlists" in (classified.reason or "")


def test_classify_supported_service_links() -> None:
    assert classify_url("https://open.spotify.com/track/abc").kind == LinkKind.SPOTIFY
    assert classify_url("https://music.apple.com/us/album/song/123?i=456").kind == LinkKind.APPLE_MUSIC
    assert classify_url("https://www.shazam.com/track/123/song").kind == LinkKind.SHAZAM


def test_reject_unsupported_link() -> None:
    classified = classify_url("https://example.com/song")
    assert classified.kind == LinkKind.UNSUPPORTED
