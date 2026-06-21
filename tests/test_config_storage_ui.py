from __future__ import annotations

from pathlib import Path

import pytest

from sync_me_maybe.auth import is_allowed
from sync_me_maybe.config import ConfigError, Settings, parse_user_ids
from sync_me_maybe.library.storage import (
    StoreResult,
    TrackInfo,
    store_completed_file,
    track_destination,
    upload_destination,
)
from sync_me_maybe.music.filenames import clean_title, sanitize_filename
from sync_me_maybe.ui.messages import (
    RequestView,
    StatusStage,
    progress_bar,
    render_collection_progress,
    render_error,
    render_request,
    render_status,
    render_success,
    status_keyboard,
)


def test_parse_user_ids_accepts_commas_semicolons_and_blanks() -> None:
    assert parse_user_ids("1, 2; ;3") == {1, 2, 3}
    assert parse_user_ids(None) == set()


def test_parse_user_ids_rejects_non_integer() -> None:
    with pytest.raises(ConfigError, match="Invalid Telegram user ID"):
        parse_user_ids("1,nope")


def test_settings_from_env_validates_required_and_numeric_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_IDS", "42")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Settings.from_env()

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", " token ")
    monkeypatch.setenv("MAX_DOWNLOAD_SECONDS", "bad")
    with pytest.raises(ConfigError, match="MAX_DOWNLOAD_SECONDS"):
        Settings.from_env()

    monkeypatch.setenv("MAX_DOWNLOAD_SECONDS", "15")
    monkeypatch.setenv("MAX_COLLECTION_TRACKS", "7")
    monkeypatch.setenv("UPLOAD_BATCH_WINDOW_SECONDS", "0.5")
    monkeypatch.setenv("RECEIVED_STICKER_ID", "sticker-id")
    monkeypatch.setenv("LOG_LEVEL", "debug")
    settings = Settings.from_env()
    assert settings.telegram_bot_token == "token"
    assert settings.allowed_telegram_user_ids == {42}
    assert settings.max_download_seconds == 15
    assert settings.max_collection_tracks == 7
    assert settings.upload_batch_window_seconds == 0.5
    assert settings.received_sticker_id == "sticker-id"
    assert settings.log_level == "DEBUG"

    monkeypatch.delenv("MAX_COLLECTION_TRACKS")
    assert Settings.from_env().max_collection_tracks == 1000


def test_is_allowed_requires_user_id_in_allowlist() -> None:
    assert is_allowed(42, {42})
    assert not is_allowed(None, {42})
    assert not is_allowed(7, {42})


def test_filename_helpers_clean_provider_noise_and_invalid_characters() -> None:
    assert sanitize_filename(" bad/name:*? .mp3 ", "fallback") == "bad_name___ .mp3"
    assert sanitize_filename("", "fallback") == "fallback"
    assert clean_title("Song - YouTube Music") == "Song"
    assert clean_title("Song on Spotify") == "Song"


def test_track_and_upload_destinations_use_safe_library_layout(tmp_path: Path) -> None:
    info = TrackInfo(title="Song", artist="Artist", album="Album", track_number=3)
    assert track_destination(tmp_path, info) == tmp_path / "Artist - Song.mp3"
    missing_artist = TrackInfo(title="Song", artist=None, album="Album", track_number=3)
    assert track_destination(tmp_path, missing_artist) == tmp_path / "Song.mp3"
    collection = TrackInfo(
        title="Song",
        artist="Artist",
        collection_owner="Owner",
        collection_title="Playlist",
    )
    assert (
        track_destination(tmp_path, collection) == tmp_path / "Owner - Playlist/Artist - Song.mp3"
    )
    missing_owner = TrackInfo(title="Song", artist="Artist", collection_title="Playlist")
    assert track_destination(tmp_path, missing_owner) == tmp_path / "Playlist/Artist - Song.mp3"
    missing_owner_with_url = TrackInfo(
        title="Song",
        artist="Artist",
        collection_title="Playlist",
        collection_url="https://open.spotify.com/playlist/abc",
    )
    assert track_destination(tmp_path, missing_owner_with_url) == (
        tmp_path / "Playlist(https___open.spotify.com_playlist_abc)/Artist - Song.mp3"
    )
    url_like_owner = TrackInfo(
        title="Song",
        artist="Artist",
        collection_owner="https://open.spotify.com/user/roullin",
        collection_title="Feels",
        collection_url="https://open.spotify.com/playlist/abc",
    )
    assert track_destination(tmp_path, url_like_owner) == (
        tmp_path / "Feels(https___open.spotify.com_playlist_abc)/Artist - Song.mp3"
    )
    unsafe_collection = TrackInfo(
        title="Song",
        artist="Artist",
        collection_owner="Own/er",
        collection_title="Play:list?",
    )
    assert track_destination(tmp_path, unsafe_collection) == (
        tmp_path / "Own_er - Play_list_/Artist - Song.mp3"
    )
    assert upload_destination(tmp_path, "folder/bad?.flac") == tmp_path / "folder_bad_.flac"


def test_store_completed_file_moves_or_skips_duplicate(tmp_path: Path) -> None:
    music_dir = tmp_path / "music"
    source = tmp_path / "source.mp3"
    destination = music_dir / "Artist - Song.mp3"
    source.write_text("new", encoding="utf-8")

    result = store_completed_file(source, destination, music_dir)
    assert result == StoreResult(destination, "Artist - Song.mp3", False)
    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()

    duplicate = tmp_path / "duplicate.mp3"
    duplicate.write_text("duplicate", encoding="utf-8")
    skipped = store_completed_file(duplicate, destination, music_dir)
    assert skipped.skipped
    assert skipped.relative_path == "Artist - Song.mp3"
    assert not duplicate.exists()
    assert destination.read_text(encoding="utf-8") == "new"


def test_ui_renderers_and_keyboards_show_expected_status() -> None:
    assert progress_bar(1, 4, width=4) == "█░░░ 25%"
    assert progress_bar(13, 20, width=10, show_count=True) == "██████░░░░ 65%  • 13/20"
    assert progress_bar(0, 0, width=3) == "░░░ 0%"
    assert "Queue: #2" in render_status(StatusStage.QUEUED, "spotify", "detail", position=2)
    assert "Path: Artist/Song.mp3" in render_success("Artist/Song.mp3")
    assert "Failed" in render_error("broken")
    assert "📥 0 saved • ⏭️ 0 skipped • ❌ 0 failed" in render_collection_progress(
        "playlist", total=2, queued=2
    )

    received = render_request(
        RequestView(
            title="Spotify playlist",
            stage=StatusStage.RECEIVED,
            source_label="Spotify playlist",
        )
    )
    assert received == "📥 Received\n🎵 Spotify playlist"

    request_text = render_request(
        RequestView(
            title="Batch",
            stage=StatusStage.DONE,
            total=2,
            completed=1,
            skipped=1,
            paths=["a.mp3", "b.mp3"],
        )
    )
    assert "📂 Results" not in request_text
    assert "stored/skipped path" not in request_text
    assert "a.mp3" not in request_text
    assert "Now:" not in request_text
    assert "Track:" not in request_text
    assert "Queue:" not in request_text

    active_playlist = render_request(
        RequestView(
            title="Spotify playlist",
            stage=StatusStage.DOWNLOADING,
            total=4,
            completed=1,
            current="Mumford & Sons - White Blank Page",
            collection_title="femme",
            collection_owner="Valeria Gershannik",
            source_label="Spotify playlist",
            elapsed_seconds=60,
        )
    )
    assert "⬇️ Downloading ·" not in active_playlist
    assert "🔵 Status     Downloading" in active_playlist
    assert "🎵 Spotify playlist “femme” by Valeria Gershannik" in active_playlist
    assert "🎧 Spotify playlist" not in active_playlist
    assert "Source:" not in active_playlist
    assert "Playlist name:" not in active_playlist
    assert "Album name:" not in active_playlist
    assert "Playlist: femme" not in active_playlist
    assert "By:" not in active_playlist
    assert "██░░░░░░░░ 25%  • 1/4" in active_playlist
    assert "⏳ ~3m 0s remaining" in active_playlist
    assert "📥 1 saved • ⏭️ 0 skipped • ❌ 0 failed" in active_playlist
    assert "Queue: active" not in active_playlist
    assert "Track: Mumford & Sons - White Blank Page" in active_playlist

    queued_playlist = render_request(
        RequestView(
            title="Spotify playlist",
            stage=StatusStage.QUEUED,
            total=50,
            queue_position=2,
            collection_title="feels",
            collection_owner="Romy Brunner",
            source_label="Spotify playlist",
        )
    )
    assert "🟡 Status     Queued" in queued_playlist
    assert "🎵 Spotify playlist “feels” by Romy Brunner" in queued_playlist
    assert "⏳ Waiting in queue · position #2" in queued_playlist
    assert "📦 50 tracks detected" in queued_playlist

    active_album = render_request(
        RequestView(
            title="Apple Music album",
            stage=StatusStage.THINKING,
            collection_title="Album",
            source_label="Apple Music album",
        )
    )
    assert "🍎 Apple Music album “Album”" in active_album
    assert "Source:" not in active_album
    assert "Album name:" not in active_album

    missing_title_playlist = render_request(
        RequestView(
            title="Music link",
            stage=StatusStage.DONE,
            source_label="Spotify playlist",
            queue_position=0,
        )
    )
    assert "🎵 Spotify playlist" in missing_title_playlist
    assert "Source:" not in missing_title_playlist
    assert "Track:" not in missing_title_playlist
    assert "Queue:" not in missing_title_playlist

    title_only_playlist = render_request(
        RequestView(
            title="Spotify playlist",
            stage=StatusStage.EXPANDING,
            collection_title="femme",
            source_label="Spotify playlist",
        )
    )
    assert "🟣 Status     Preparing" in title_only_playlist
    assert "🎵 Spotify playlist “femme”" in title_only_playlist
    assert "🔍 Reading playlist..." in title_only_playlist
    assert "Playlist name:" not in title_only_playlist
    assert "By:" not in title_only_playlist

    upload_request = render_request(
        RequestView(title="Telegram upload", stage=StatusStage.DOWNLOADING, current="song.mp3")
    )
    assert "📁 File “song.mp3”" in upload_request
    assert "Item: song.mp3" in upload_request

    keyboard = status_keyboard(
        source_url="https://example.com",
        relative_path="a.mp3",
        path_callback_data="path:1",
        issue_callback_data="issues:1",
        rerun_failed_callback_data="rerun_failed:1",
        refresh_callback_data="refresh:1",
        cancel_callback_data="cancel:1",
        include_health=True,
    )
    assert keyboard is not None
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert labels == [
        "🔗 Open source",
        "⛔ Stop",
        "🔄 Refresh",
        "📍 Show path",
        "🧾 Skipped/failed details",
        "🔁 Rerun failed",
        "🩺 Health",
    ]
