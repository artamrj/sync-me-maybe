from __future__ import annotations

import shutil
from pathlib import Path

from sync_me_maybe.database import Database
from sync_me_maybe.library.index import (
    LibraryIndex,
    artist_key,
    read_index_tags,
    title_key,
    youtube_video_id,
)
from sync_me_maybe.library.playlists import (
    PlaylistEntry,
    PlaylistState,
    playlist_name,
    playlist_path,
    write_playlist,
)
from sync_me_maybe.library.storage import folder_artist_name, library_destination
from sync_me_maybe.music.tagging import AudioTags, write_tags

FIXTURES = Path(__file__).parent / "fixtures"


def test_keys_ignore_featured_artists_and_remasters_but_keep_versions() -> None:
    assert artist_key("Daft Punk, Pharrell Williams & Nile Rodgers") == "daft punk"
    assert title_key("Get Lucky (feat. Pharrell Williams)") == "get lucky"
    assert title_key("Get Lucky ft. Pharrell") == "get lucky"
    assert title_key("Song - Remastered 2011") == "song"
    assert title_key("Song (2011 Remaster)") == "song"
    assert title_key("Song (Live)") != title_key("Song")
    assert title_key(None) == ""


def test_youtube_video_id_parses_common_links() -> None:
    assert youtube_video_id("https://www.youtube.com/watch?v=abc123&list=x") == "abc123"
    assert youtube_video_id("https://youtu.be/abc123?t=4") == "abc123"
    assert youtube_video_id("https://music.youtube.com/watch?v=abc123") == "abc123"
    assert youtube_video_id("https://www.youtube.com/shorts/abc123") == "abc123"
    assert youtube_video_id("ytsearch1:artist song") is None
    assert youtube_video_id("https://example.com/watch?v=abc") is None
    assert youtube_video_id(None) is None


def test_index_lookup_prefers_ids_and_forgets_deleted_files(tmp_path: Path) -> None:
    music = tmp_path / "music"
    (music / "Artist").mkdir(parents=True)
    (music / "Artist" / "Song.m4a").write_text("audio", encoding="utf-8")
    index = LibraryIndex(Database(), music)

    index.add("Artist/Song.m4a", isrc="usabc1234567", video_id="vid", artist="Artist", title="Song")

    assert index.lookup(isrc="USABC1234567") == "Artist/Song.m4a"
    assert index.lookup(video_id="vid") == "Artist/Song.m4a"
    assert index.lookup(artist="Artist feat. Guest", title="Song (feat. Guest)") == (
        "Artist/Song.m4a"
    )
    assert index.lookup(artist="Artist", title="Song (Live)") is None
    assert index.count() == 1

    (music / "Artist" / "Song.m4a").unlink()
    assert index.lookup(video_id="vid") is None
    assert index.count() == 0


def test_scan_indexes_tagged_and_legacy_files_and_removes_missing(tmp_path: Path) -> None:
    music = tmp_path / "music"
    (music / "Band" / "Album").mkdir(parents=True)
    (music / ".hidden").mkdir()
    (music / "@eaDir").mkdir()
    tagged = music / "Band" / "Album" / "01 - Tagged.m4a"
    shutil.copy(FIXTURES / "silence.m4a", tagged)
    write_tags(tagged, AudioTags(title="Tagged Song", artists=("Band",), isrc="GBXXX0000001"))
    legacy = music / "Old Artist - Old Song.mp3"
    legacy.write_bytes(b"not really audio")
    numbered = music / "Band" / "Album" / "02 - Untagged.mp3"
    numbered.write_bytes(b"not really audio")
    (music / ".hidden" / "Secret - Song.mp3").write_bytes(b"x")
    (music / "@eaDir" / "Thumb - Song.mp3").write_bytes(b"x")
    (music / "cover.jpg").write_bytes(b"x")
    index = LibraryIndex(Database(), music)

    assert index.scan() == 3
    assert index.lookup(isrc="GBXXX0000001") == "Band/Album/01 - Tagged.m4a"
    assert index.lookup(artist="Old Artist", title="Old Song") == "Old Artist - Old Song.mp3"
    assert index.lookup(artist="Band", title="Untagged") == "Band/Album/02 - Untagged.mp3"
    assert index.scan() == 0  # unchanged files are skipped

    legacy.unlink()
    index.scan()
    assert index.count() == 2


def test_read_index_tags_falls_back_to_filename(tmp_path: Path) -> None:
    path = tmp_path / "Artist - Title.mp3"
    path.write_bytes(b"junk")
    tags = read_index_tags(path)
    assert (tags.artist, tags.title, tags.isrc) == ("Artist", "Title", None)


def test_library_destination_layout(tmp_path: Path) -> None:
    assert (
        library_destination(
            tmp_path,
            title="Get Lucky",
            artist="Daft Punk, Pharrell Williams",
            album="Random Access Memories",
            album_artist="Daft Punk",
            track_number=8,
            extension=".m4a",
        )
        == tmp_path / "Daft Punk" / "Random Access Memories" / "08 - Get Lucky.m4a"
    )
    assert (
        library_destination(
            tmp_path, title="Song", artist="A", album="LP", track_number=3, disc_number=2
        )
        == tmp_path / "A" / "LP" / "2-03 - Song.m4a"
    )
    assert library_destination(tmp_path, title="Single", artist="A & B", extension="opus") == (
        tmp_path / "A" / "Single.opus"
    )
    assert library_destination(tmp_path, title=None, artist=None) == (
        tmp_path / "Unknown Artist" / "Unknown Title.m4a"
    )
    assert folder_artist_name("Artist feat. Guest") == "Artist"
    assert folder_artist_name(None) is None


def test_write_playlist_uses_relative_paths_in_position_order(tmp_path: Path) -> None:
    assert playlist_name("Road Trip", "Arta") == "Arta - Road Trip"
    assert playlist_name("Road Trip", "open.spotify.com/user/x") == "Road Trip"
    assert playlist_name(None, None) == "Playlist"

    playlist = PlaylistState(name="Arta - Road Trip")
    assert write_playlist(tmp_path, playlist) is None
    playlist.entries[2] = PlaylistEntry("B/Second.m4a", "Second", "B", 200)
    playlist.entries[1] = PlaylistEntry("A/Album/01 - First.m4a", "First", "A")

    path = write_playlist(tmp_path, playlist)

    assert path == playlist_path(tmp_path, "Arta - Road Trip")
    assert path == tmp_path / "Playlists" / "Arta - Road Trip.m3u8"
    assert path.read_text(encoding="utf-8").splitlines() == [
        "#EXTM3U",
        "#PLAYLIST:Arta - Road Trip",
        "#EXTINF:-1,A - First",
        "../A/Album/01 - First.m4a",
        "#EXTINF:200,B - Second",
        "../B/Second.m4a",
    ]
