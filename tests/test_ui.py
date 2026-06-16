from __future__ import annotations

from sync_me_maybe.ui import StatusStage, render_error, render_status, render_success, render_welcome, status_keyboard


def test_render_welcome_shows_authorization() -> None:
    text = render_welcome(True)
    assert "sync-me-maybe" in text
    assert "Authorized" in text
    assert "/health" in text


def test_render_status_contains_stage_source_and_detail() -> None:
    text = render_status(StatusStage.DOWNLOADING, "spotify", "Artist Song")
    assert "Downloading" in text
    assert "Source: spotify" in text
    assert "Artist Song" in text


def test_render_success_distinguishes_duplicate() -> None:
    assert "Stored" in render_success("Artist/Song.mp3")
    assert "Skipped duplicate" in render_success("Artist/Song.mp3", skipped=True)


def test_render_error_is_short_and_actionable() -> None:
    assert render_error("No match found") == "Failed\n\nNo match found"


def test_status_keyboard_builds_expected_buttons() -> None:
    keyboard = status_keyboard(
        source_url="https://music.youtube.com/watch?v=abc",
        relative_path="Artist/Song.mp3",
        path_callback_data="path:abc",
    )
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].text == "Open source"
    assert keyboard.inline_keyboard[1][0].callback_data == "path:abc"
