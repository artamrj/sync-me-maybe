from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from sync_me_maybe.music.downloader import DownloadError, YtDlpDownloader
from sync_me_maybe.music.resolver import ResolvedTrack


def resolved_track(**overrides: object) -> ResolvedTrack:
    values = {
        "source_url": "https://source",
        "download_url": "https://youtube",
        "search_query": None,
        "title": "Provider Title",
        "artist": "Provider Artist",
        "album": "Provider Album",
        "track_number": 5,
    }
    values.update(overrides)
    return ResolvedTrack(**values)  # type: ignore[arg-type]


def ydl_context(info: dict[str, object]) -> Mock:
    ydl = Mock()
    ydl.extract_info.return_value = info
    context = Mock()
    context.__enter__ = Mock(return_value=ydl)
    context.__exit__ = Mock(return_value=None)
    return context


def test_downloader_builds_options_merges_metadata_and_finds_mp3(tmp_path: Path) -> None:
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("cookies", encoding="utf-8")
    downloader = YtDlpDownloader(tmp_path, cookies_file=cookies, audio_format="mp3")

    context = ydl_context({"track": "Yt Title", "artist": "Yt Artist"})
    with patch(
        "sync_me_maybe.music.downloader.yt_dlp.YoutubeDL",
        return_value=context,
    ) as youtube_dl:
        result_path: Path | None = None

        def fake_extract(_: str, download: bool) -> dict[str, object]:
            nonlocal result_path
            assert download
            options = youtube_dl.call_args.args[0]
            run_id = Path(options["outtmpl"]).name.split(".")[0]
            result_path = tmp_path / f"{run_id}.mp3"
            result_path.write_text("audio", encoding="utf-8")
            return {
                "track": "Yt Title",
                "artist": "Yt Artist",
                "album": "Yt Album",
                "track_number": "9",
            }

        context.__enter__.return_value.extract_info.side_effect = fake_extract
        downloaded = downloader._download_sync(
            resolved_track(title=None, artist=None, album=None, track_number=None)
        )

    options = youtube_dl.call_args.args[0]
    assert options["cookiefile"] == str(cookies)
    assert options["postprocessors"][0]["preferredcodec"] == "mp3"
    assert downloaded.temp_file == result_path
    assert downloaded.info.title == "Yt Title"
    assert downloaded.info.artist == "Yt Artist"
    assert downloaded.info.album == "Yt Album"
    assert downloaded.info.track_number == 9


def test_downloader_uses_provider_metadata_and_strips_youtube_noise(tmp_path: Path) -> None:
    downloader = YtDlpDownloader(tmp_path)
    context = ydl_context({})

    with patch(
        "sync_me_maybe.music.downloader.yt_dlp.YoutubeDL",
        return_value=context,
    ) as youtube_dl:

        def fake_extract(_: str, download: bool) -> dict[str, object]:
            assert download
            run_id = Path(youtube_dl.call_args.args[0]["outtmpl"]).name.split(".")[0]
            (tmp_path / f"{run_id}.mp3").write_text("audio", encoding="utf-8")
            return {"title": "Song (Official Video)", "uploader": "Uploader"}

        context.__enter__.return_value.extract_info.side_effect = fake_extract
        downloaded = downloader._download_sync(resolved_track(title=None, artist=None, album=None))

    assert downloaded.info.title == "Song"
    assert downloaded.info.artist == "Uploader"


def test_downloader_handles_search_entries_missing_output_and_cleanup(tmp_path: Path) -> None:
    downloader = YtDlpDownloader(tmp_path)
    context = ydl_context({"entries": [{"title": "First"}]})

    with patch("sync_me_maybe.music.downloader.yt_dlp.YoutubeDL", return_value=context):
        with pytest.raises(DownloadError, match="no output file"):
            downloader._download_sync(resolved_track())

    context = ydl_context({"entries": []})
    with patch("sync_me_maybe.music.downloader.yt_dlp.YoutubeDL", return_value=context):
        with pytest.raises(DownloadError, match="No matching"):
            downloader._download_sync(resolved_track())

    partial = tmp_path / "partial.tmp"
    partial.write_text("partial", encoding="utf-8")
    failing = Mock()
    failing.__enter__ = Mock(side_effect=RuntimeError("yt-dlp failed"))
    failing.__exit__ = Mock(return_value=None)
    with patch("sync_me_maybe.music.downloader.yt_dlp.YoutubeDL", return_value=failing):
        with pytest.raises(DownloadError, match="Download failed"):
            downloader._download_sync(resolved_track())


def test_downloader_cancel_and_timeout_are_retry_classified(tmp_path: Path) -> None:
    downloader = YtDlpDownloader(tmp_path, max_seconds=0)
    with pytest.raises(DownloadError, match="Cancelled") as cancelled:
        downloader._download_sync(resolved_track(), cancel_check=lambda: True)
    assert not cancelled.value.retryable

    with patch("sync_me_maybe.music.downloader.time.monotonic", side_effect=[0, 1]):
        with pytest.raises(DownloadError, match="MAX_DOWNLOAD_SECONDS") as timed_out:
            downloader._download_sync(resolved_track(), cancel_check=lambda: False)
    assert timed_out.value.retryable


@pytest.mark.parametrize(
    ("audio_format", "codec", "extension"),
    [("m4a", "m4a", ".m4a"), ("opus", "opus", ".opus"), ("mp3", "mp3", ".mp3")],
)
def test_downloader_audio_format_controls_codec_and_output(
    tmp_path: Path, audio_format: str, codec: str, extension: str
) -> None:
    downloader = YtDlpDownloader(tmp_path, audio_format=audio_format)
    context = ydl_context({})

    with patch(
        "sync_me_maybe.music.downloader.yt_dlp.YoutubeDL", return_value=context
    ) as youtube_dl:

        def fake_extract(_: str, download: bool) -> dict[str, object]:
            run_id = Path(youtube_dl.call_args.args[0]["outtmpl"]).name.split(".")[0]
            (tmp_path / f"{run_id}{extension}").write_text("audio", encoding="utf-8")
            return {"title": "Song", "duration": 201.4}

        context.__enter__.return_value.extract_info.side_effect = fake_extract
        downloaded = downloader._download_sync(resolved_track())

    options = youtube_dl.call_args.args[0]
    assert options["postprocessors"][0]["preferredcodec"] == codec
    assert downloaded.temp_file.suffix == extension
    assert downloaded.duration_seconds == 201


def test_downloader_defaults_to_m4a_and_rejects_unknown_format(tmp_path: Path) -> None:
    assert YtDlpDownloader(tmp_path).audio_format == "m4a"
    with pytest.raises(ValueError, match="Unsupported audio format"):
        YtDlpDownloader(tmp_path, audio_format="wav")


def test_downloader_cleans_topic_channel_artist(tmp_path: Path) -> None:
    downloader = YtDlpDownloader(tmp_path)
    context = ydl_context({})
    with patch(
        "sync_me_maybe.music.downloader.yt_dlp.YoutubeDL", return_value=context
    ) as youtube_dl:

        def fake_extract(_: str, download: bool) -> dict[str, object]:
            run_id = Path(youtube_dl.call_args.args[0]["outtmpl"]).name.split(".")[0]
            (tmp_path / f"{run_id}.m4a").write_text("audio", encoding="utf-8")
            return {
                "title": "Some Band - Song (Official Video)",
                "uploader": "Some Band - Topic",
            }

        context.__enter__.return_value.extract_info.side_effect = fake_extract
        downloaded = downloader._download_sync(resolved_track(title=None, artist=None))

    assert downloaded.info.artist == "Some Band"
    assert downloaded.info.title == "Song"
