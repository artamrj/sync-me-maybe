from __future__ import annotations

from pathlib import Path

from sync_me_maybe.filenames import sanitize_filename
from sync_me_maybe.storage import TrackInfo, store_completed_file, track_destination, upload_destination


def test_sanitize_filename_removes_unsafe_characters() -> None:
    assert sanitize_filename('AC/DC: "Song"?') == "AC_DC_ _Song__"


def test_track_destination_uses_artist_album_layout(tmp_path: Path) -> None:
    destination = track_destination(
        tmp_path,
        TrackInfo(title="Song", artist="Artist", album="Album", track_number=3),
    )
    assert destination == tmp_path / "Artist" / "Album" / "03 - Song.mp3"


def test_track_destination_skips_missing_album_folder(tmp_path: Path) -> None:
    destination = track_destination(
        tmp_path,
        TrackInfo(title="Song", artist="Artist", album=None, track_number=3),
    )
    assert destination == tmp_path / "Artist" / "03 - Song.mp3"


def test_track_destination_skips_unknown_album_folder(tmp_path: Path) -> None:
    destination = track_destination(
        tmp_path,
        TrackInfo(title="Song", artist="Artist", album=" unknown album "),
    )
    assert destination == tmp_path / "Artist" / "Song.mp3"


def test_upload_destination_keeps_original_name_safely(tmp_path: Path) -> None:
    assert upload_destination(tmp_path, "song?.flac") == tmp_path / "song_.flac"


def test_store_completed_file_moves_into_destination(tmp_path: Path) -> None:
    source = tmp_path / "tmp.mp3"
    source.write_text("audio", encoding="utf-8")
    destination = tmp_path / "music" / "Artist" / "Album" / "Song.mp3"

    result = store_completed_file(source, destination, tmp_path / "music")

    assert result.relative_path == "Artist/Album/Song.mp3"
    assert not result.skipped
    assert destination.read_text(encoding="utf-8") == "audio"
    assert not source.exists()


def test_store_completed_file_skips_existing_duplicate(tmp_path: Path) -> None:
    source = tmp_path / "tmp.mp3"
    source.write_text("new", encoding="utf-8")
    destination = tmp_path / "music" / "Song.mp3"
    destination.parent.mkdir(parents=True)
    destination.write_text("old", encoding="utf-8")

    result = store_completed_file(source, destination, tmp_path / "music")

    assert result.skipped
    assert result.relative_path == "Song.mp3"
    assert destination.read_text(encoding="utf-8") == "old"
    assert not source.exists()
