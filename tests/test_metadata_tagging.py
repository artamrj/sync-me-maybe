from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import mutagen
import pytest

from sync_me_maybe.library.storage import TrackInfo
from sync_me_maybe.music.downloader import DownloadedTrack
from sync_me_maybe.music.enrichment import PreparedTrack, TrackEnricher, build_tags
from sync_me_maybe.music.matching import Candidate, MatchScore, MatchTarget, YouTubeMatch
from sync_me_maybe.music.metadata import CanonicalTrack, DeezerMetadata, canonical_from_deezer
from sync_me_maybe.music.resolver import ResolvedTrack
from sync_me_maybe.music.tagging import AudioTags, CoverArt, cover_from_bytes, write_tags

FIXTURES = Path(__file__).parent / "fixtures"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
TRACK_JSON: dict[str, Any] = {
    "id": 3135553,
    "title": "One More Time",
    "isrc": "GBDUW0000053",
    "duration": 320,
    "track_position": 1,
    "disk_number": 1,
    "release_date": "2001-03-12",
    "artist": {"name": "Daft Punk"},
    "contributors": [{"name": "Daft Punk"}, {"name": "Romanthony"}],
    "album": {"id": 302127, "title": "Discovery", "cover_xl": "https://cdn/track.jpg"},
}
ALBUM_JSON: dict[str, Any] = {
    "title": "Discovery",
    "artist": {"name": "Daft Punk"},
    "genres": {"data": [{"name": "Electro"}]},
    "cover_xl": "https://cdn/album.jpg",
}


class FakeResponse:
    def __init__(self, data: Any) -> None:
        self.data = data

    def raise_for_status(self) -> None:
        return None

    def json(self) -> Any:
        return self.data


class FakeSession:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any] | None]] = []

    def get(self, url: str, params: dict[str, Any] | None = None, timeout: int = 0) -> Any:
        self.calls.append((url, params))
        path = url.removeprefix("https://api.deezer.com")
        if path == "/search":
            query = (params or {}).get("q", "")
            return FakeResponse(self.routes.get(f"/search?{query}", {"data": []}))
        return FakeResponse(self.routes.get(path, {"error": {"code": 800}}))


def test_canonical_from_deezer_maps_track_and_album() -> None:
    canonical = canonical_from_deezer(TRACK_JSON, ALBUM_JSON)
    assert canonical.title == "One More Time"
    assert canonical.artists == ("Daft Punk", "Romanthony")
    assert canonical.album == "Discovery"
    assert canonical.album_artist == "Daft Punk"
    assert canonical.track_number == 1
    assert canonical.isrc == "GBDUW0000053"
    assert canonical.genres == ("Electro",)
    assert canonical.cover_url == "https://cdn/album.jpg"


def test_deezer_lookup_prefers_isrc_then_scored_search() -> None:
    routes = {
        "/track/isrc:GBDUW0000053": TRACK_JSON,
        "/track/3135553": TRACK_JSON,
        "/album/302127": ALBUM_JSON,
        "/search?Daft Punk One More Time": {
            "data": [
                {"id": 1, "title": "One More Time (Live)", "artist": {"name": "Daft Punk"}},
                {"id": 3135553, "title": "One More Time", "artist": {"name": "Daft Punk"}},
            ]
        },
    }
    session = FakeSession(routes)
    client = DeezerMetadata(session=session)  # type: ignore[arg-type]

    by_isrc = client.lookup(MatchTarget("Whatever", "Whoever"), isrc="GBDUW0000053")
    assert by_isrc is not None and by_isrc.isrc == "GBDUW0000053"

    by_search = client.lookup(MatchTarget("One More Time", "Daft Punk", 321))
    assert by_search is not None and by_search.album == "Discovery"
    searched = [params["q"] for _, params in session.calls if params]
    assert searched == ['artist:"Daft Punk" track:"One More Time"', "Daft Punk One More Time"]

    assert client.lookup(MatchTarget("Unknown Song", "Nobody")) is None


@pytest.mark.parametrize("extension", ["m4a", "mp3", "opus", "flac"])
def test_write_tags_round_trips_for_supported_formats(tmp_path: Path, extension: str) -> None:
    path = tmp_path / f"song.{extension}"
    shutil.copy(FIXTURES / f"silence.{extension}", path)
    tags = AudioTags(
        title="One More Time",
        artists=("Daft Punk", "Romanthony"),
        album="Discovery",
        album_artist="Daft Punk",
        track_number=1,
        disc_number=1,
        date="2001-03-12",
        isrc="GBDUW0000053",
        genres=("Electro",),
        source_url="https://open.spotify.com/track/x",
    )

    assert write_tags(path, tags, CoverArt(JPEG))

    easy = mutagen.File(path, easy=True)
    assert easy is not None
    assert easy["title"] == ["One More Time"]
    assert easy["album"] == ["Discovery"]
    assert easy["albumartist"] == ["Daft Punk"]
    assert easy["tracknumber"][0].split("/")[0] == "1"
    raw = mutagen.File(path)
    assert raw is not None
    if extension == "m4a":
        assert bytes(raw.tags["covr"][0]) == JPEG
    elif extension == "mp3":
        assert raw.tags.getall("APIC")[0].data == JPEG
        assert list(raw.tags.getall("TXXX:ARTISTS")[0].text) == ["Daft Punk", "Romanthony"]
    elif extension == "flac":
        assert raw.pictures[0].data == JPEG
    else:
        assert raw.tags["METADATA_BLOCK_PICTURE"]
        assert raw.tags["ARTISTS"] == ["Daft Punk", "Romanthony"]


def test_write_tags_skips_unknown_files_and_cover_detection(tmp_path: Path) -> None:
    text = tmp_path / "song.mp3"
    text.write_text("not audio", encoding="utf-8")
    assert write_tags(text, AudioTags(title="x")) is False
    assert cover_from_bytes(b"\x89PNG....") == CoverArt(b"\x89PNG....", "image/png")
    assert cover_from_bytes(b"RIFF\x00\x00\x00\x00WEBPVP8 ") is None
    assert cover_from_bytes(None) is None


def resolved(download_url: str = "ytsearch1:Daft Punk One More Time") -> ResolvedTrack:
    return ResolvedTrack(
        source_url="https://open.spotify.com/track/x",
        download_url=download_url,
        title="One More Time",
        artist="Daft Punk",
        duration_seconds=320,
    )


def youtube_match() -> YouTubeMatch:
    candidate = Candidate("One More Time", ("Daft Punk",), 320, "vid", True, "https://thumb")
    return YouTubeMatch(
        "https://www.youtube.com/watch?v=vid", candidate, MatchScore(99, 100, 100, 100, True)
    )


@pytest.mark.asyncio
async def test_enricher_prepare_swaps_search_for_match_and_falls_back() -> None:
    metadata = Mock()
    metadata.lookup.return_value = canonical_from_deezer(TRACK_JSON, ALBUM_JSON)
    matcher = Mock()
    matcher.find.return_value = youtube_match()
    enricher = TrackEnricher(metadata=metadata, matcher=matcher)

    prepared = await enricher.prepare(resolved())
    assert prepared.resolved.download_url == "https://www.youtube.com/watch?v=vid"
    assert prepared.canonical is not None and prepared.canonical.album == "Discovery"

    matcher.find.side_effect = RuntimeError("youtube music down")
    metadata.lookup.side_effect = RuntimeError("deezer down")
    fallback = await enricher.prepare(resolved())
    assert fallback.resolved.download_url.startswith("ytsearch1:")
    assert fallback.canonical is None

    direct = await enricher.prepare(resolved("https://youtu.be/abc"))
    assert direct.resolved.download_url == "https://youtu.be/abc"

    disabled = TrackEnricher(lookups_enabled=False, metadata=metadata, matcher=matcher)
    assert (await disabled.prepare(resolved())).resolved.download_url.startswith("ytsearch1:")


@pytest.mark.asyncio
async def test_enricher_finalize_writes_canonical_tags_and_cover(tmp_path: Path) -> None:
    path = tmp_path / "temp.m4a"
    shutil.copy(FIXTURES / "silence.m4a", path)
    downloaded = DownloadedTrack(path, TrackInfo("One More Time", "Daft Punk"), 320)
    canonical = canonical_from_deezer(TRACK_JSON, ALBUM_JSON)
    enricher = TrackEnricher(metadata=Mock(), matcher=Mock())
    enricher._cover = Mock(return_value=CoverArt(JPEG))  # type: ignore[method-assign]

    finalized = await enricher.finalize(downloaded, PreparedTrack(resolved(), canonical))
    assert finalized.tagged
    assert finalized.canonical == canonical

    enricher._cover.assert_called_once_with("https://cdn/album.jpg")
    tags = mutagen.File(path).tags
    assert tags["\xa9alb"] == ["Discovery"]
    assert tags["aART"] == ["Daft Punk"]
    assert tags["trkn"] == [(1, 0)]
    assert bytes(tags["covr"][0]) == JPEG


@pytest.mark.asyncio
async def test_enricher_finalize_offline_uses_known_info_and_never_raises(tmp_path: Path) -> None:
    path = tmp_path / "temp.mp3"
    shutil.copy(FIXTURES / "silence.mp3", path)
    metadata = Mock()
    enricher = TrackEnricher(lookups_enabled=False, metadata=metadata, matcher=Mock())
    downloaded = DownloadedTrack(path, TrackInfo("Song", "Band - Topic", album="Album"))

    finalized = await enricher.finalize(downloaded, PreparedTrack(resolved("https://youtu.be/x")))
    assert finalized.tagged
    metadata.lookup.assert_not_called()
    easy = mutagen.File(path, easy=True)
    assert easy["artist"] == ["Band"]
    assert easy["album"] == ["Album"]

    broken = DownloadedTrack(tmp_path / "missing.m4a", TrackInfo("Song"))
    assert (await enricher.finalize(broken, PreparedTrack(resolved()))).tagged is False


def test_build_tags_uses_canonical_only_when_present() -> None:
    downloaded = DownloadedTrack(Path("x.m4a"), TrackInfo("Song", "Artist", "Album", 7))
    plain = build_tags(downloaded, resolved(), None)
    assert plain.track_number is None  # playlist positions are not album track numbers
    assert plain.artists == ("Artist",)

    canonical = CanonicalTrack(
        "Real Title", ("A", "B"), album="LP", release_date="0000-00-00", track_number=3
    )
    tagged = build_tags(downloaded, resolved(), canonical)
    assert tagged.title == "Real Title"
    assert tagged.artists == ("A", "B")
    assert tagged.track_number == 3
    assert tagged.date is None


@pytest.mark.asyncio
async def test_enricher_skips_title_only_metadata_lookup() -> None:
    metadata = Mock()
    matcher = Mock()
    matcher.find.return_value = None
    enricher = TrackEnricher(metadata=metadata, matcher=matcher)
    shazam_slug = ResolvedTrack("https://shazam.com/x", "ytsearch1:get lucky", title="get lucky")

    prepared = await enricher.prepare(shazam_slug)

    metadata.lookup.assert_not_called()
    matcher.find.assert_called_once()
    assert prepared.canonical is None


@pytest.mark.asyncio
async def test_enricher_prefetches_cover_and_lyrics_during_download(tmp_path: Path) -> None:
    from sync_me_maybe.music.lyrics import Lyrics

    metadata = Mock()
    canonical = canonical_from_deezer(TRACK_JSON, ALBUM_JSON)
    metadata.lookup.return_value = canonical
    matcher = Mock()
    matcher.find.return_value = youtube_match()
    lyrics_client = Mock()
    lyrics_client.fetch.return_value = Lyrics(synced="[00:01.00] One more time")
    enricher = TrackEnricher(metadata=metadata, matcher=matcher, lyrics=lyrics_client)
    enricher._cover = Mock(return_value=CoverArt(JPEG))  # type: ignore[method-assign]

    # With a known duration, metadata lookup and matching run together.
    prepared = await enricher.prepare(resolved())
    task = enricher.prefetch(prepared)
    assert task is not None
    path = tmp_path / "temp.m4a"
    shutil.copy(FIXTURES / "silence.m4a", path)
    finalized = await enricher.finalize(
        DownloadedTrack(path, TrackInfo("One More Time", "Daft Punk"), 320), prepared, task
    )

    enricher._cover.assert_called_once_with("https://cdn/album.jpg")
    lyrics_client.fetch.assert_called_once_with("One More Time", "Daft Punk", "Discovery", 320)
    assert finalized.lyrics == Lyrics(synced="[00:01.00] One more time")
    assert finalized.tagged

    no_lyrics = TrackEnricher(
        metadata=metadata, matcher=matcher, lyrics=lyrics_client, lyrics_enabled=False
    )
    no_lyrics._cover = Mock(return_value=None)  # type: ignore[method-assign]
    extras = await no_lyrics.prefetch(prepared)  # type: ignore[misc]
    assert extras.lyrics is None and not extras.lyrics_tried
    assert TrackEnricher(lookups_enabled=False).prefetch(prepared) is None
