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
    monkeypatch.setenv("LOG_LEVEL", "debug")
    settings = Settings.from_env()
    assert settings.telegram_bot_token == "token"
    assert settings.allowed_telegram_user_ids == {42}
    assert settings.max_download_seconds == 15
    assert settings.max_collection_tracks == 7
    assert settings.upload_batch_window_seconds == 0.5
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
    assert progress_bar(0, 0, width=3) == "░░░ 0%"
    assert "Queue: #2" in render_status(StatusStage.QUEUED, "spotify", "detail", position=2)
    assert "Path: Artist/Song.mp3" in render_success("Artist/Song.mp3")
    assert "Failed" in render_error("broken")
    assert "Tracks: 2" in render_collection_progress("playlist", total=2, queued=2)

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
    assert "2 stored/skipped path" in request_text
    assert "a.mp3" in request_text

    keyboard = status_keyboard(
        source_url="https://example.com",
        relative_path="a.mp3",
        path_callback_data="path:1",
        issue_callback_data="issues:1",
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
        "🩺 Health",
    ]
