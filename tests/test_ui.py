from __future__ import annotations

from sync_me_maybe.ui import RequestView, StatusStage, progress_bar, render_error, render_request, render_status, render_success, render_welcome, status_keyboard


def test_render_welcome_shows_authorization() -> None:
    text = render_welcome(True)
    assert "sync-me-maybe" in text
    assert "Authorized" in text
    assert "/health" in text


def test_render_status_contains_stage_source_and_detail() -> None:
    text = render_status(StatusStage.DOWNLOADING, "spotify", "Artist Song", position=2)
    assert "Downloading" in text
    assert "Source: spotify" in text
    assert "Queue: #2" in text
    assert "Artist Song" in text


def test_render_success_distinguishes_duplicate() -> None:
    assert "✅ Done" in render_success("Artist/Song.mp3")
    assert "⏭️ Skipped" in render_success("Artist/Song.mp3", skipped=True)


def test_render_error_is_short_and_actionable() -> None:
    assert render_error("No match found") == "❌ Failed\n\nNo match found"


def test_render_request_uses_progress_bar_and_counters() -> None:
    text = render_request(
        RequestView(
            title="spotify playlist",
            stage=StatusStage.DOWNLOADING,
            total=10,
            completed=4,
            skipped=1,
            failed=1,
            current="Track 7/10",
            queue_position=0,
        )
    )
    assert "⬇️ Downloading" in text
    assert "██████░░░░ 60%\n\n✅ 4" in text
    assert "✅ 4 stored  ⏭️ 1 skipped  ❌ 1 failed  ⏳ 4 queued" in text
    assert "Queue: active" in text
    assert "Now: Track 7/10" in text


def test_progress_bar_handles_empty_total() -> None:
    assert progress_bar(0, 0) == "░░░░░░░░░░ 0%"


def test_status_keyboard_builds_expected_buttons() -> None:
    keyboard = status_keyboard(
        source_url="https://music.youtube.com/watch?v=abc",
        relative_path="Artist/Song.mp3",
        path_callback_data="path:abc",
        cancel_callback_data="cancel:abc",
    )
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].text == "🔗 Open source"
    assert keyboard.inline_keyboard[1][0].text == "⛔ Stop"
    assert keyboard.inline_keyboard[1][1].callback_data == "path:abc"


def test_render_request_cancelled_stage() -> None:
    text = render_request(RequestView(title="spotify playlist", stage=StatusStage.CANCELLED, total=10, completed=2, failed=3))
    assert "⛔ Cancelled" in text


def test_status_keyboard_omits_stop_when_not_requested() -> None:
    keyboard = status_keyboard(refresh_callback_data="refresh:abc", results_callback_data="results:abc")
    assert keyboard is not None
    labels = [button.text for row in keyboard.inline_keyboard for button in row]
    assert "⛔ Stop" not in labels
    assert "🔄 Refresh" in labels
    assert "📂 Show results" in labels
