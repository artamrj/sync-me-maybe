from __future__ import annotations

from sync_me_maybe.downloader import _strip_youtube_noise, _track_info
from sync_me_maybe.resolver import ResolvedTrack


def test_track_info_prefers_resolved_minimal_metadata() -> None:
    info = _track_info(
        {"title": "Noisy Official Video", "artist": "YouTube Artist", "album": "YT Album"},
        ResolvedTrack(
            source_url="https://open.spotify.com/track/x",
            download_url="ytsearch1:Artist Song",
            title="Song",
            artist="Artist",
            album=None,
        ),
    )

    assert info.title == "Song"
    assert info.artist == "Artist"
    assert info.album == "YT Album"


def test_strip_youtube_noise_removes_common_suffixes() -> None:
    assert _strip_youtube_noise("Song (Official Video)") == "Song"
