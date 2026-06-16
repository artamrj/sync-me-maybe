from __future__ import annotations

import pytest

from sync_me_maybe.downloader import DownloadError, YtDlpDownloader, _strip_youtube_noise, _track_info
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


def test_downloader_cancellation_cleans_partial_files(tmp_path, monkeypatch) -> None:
    class FakeYoutubeDL:
        def __init__(self, options):
            self.options = options

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download):
            partial = tmp_path / self.options["outtmpl"].split("/")[-1].replace("%(ext)s", "part")
            partial.write_text("partial", encoding="utf-8")
            for hook in self.options["progress_hooks"]:
                hook({"status": "downloading"})
            return {}

    monkeypatch.setattr("sync_me_maybe.downloader.yt_dlp.YoutubeDL", FakeYoutubeDL)
    downloader = YtDlpDownloader(tmp_path)

    with pytest.raises(DownloadError, match="Cancelled by user"):
        downloader._download_sync(
            ResolvedTrack(source_url="x", download_url="ytsearch1:test"),
            cancel_check=lambda: True,
        )

    assert not list(tmp_path.glob("*"))
