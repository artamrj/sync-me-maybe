from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
import requests

from sync_me_maybe.library.navidrome import NavidromeClient, NavidromeError
from sync_me_maybe.music.lyrics import LrclibClient, Lyrics, write_lyrics_sidecar


class FakeResponse:
    def __init__(self, data: Any, status_code: int = 200) -> None:
        self.data = data
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))

    def json(self) -> Any:
        return self.data


def test_lrclib_fetch_parses_lyrics_and_handles_missing() -> None:
    session = Mock()
    session.headers = {}
    session.get.return_value = FakeResponse(
        {"syncedLyrics": "[00:01.00] Hi", "plainLyrics": "Hi", "instrumental": False}
    )
    client = LrclibClient(session=session)

    lyrics = client.fetch("Song", "Artist", "Album", 200)

    assert lyrics == Lyrics(synced="[00:01.00] Hi", plain="Hi")
    assert session.get.call_args.kwargs["params"] == {
        "track_name": "Song",
        "artist_name": "Artist",
        "album_name": "Album",
        "duration": 200,
    }
    session.get.return_value = FakeResponse({}, status_code=404)
    assert client.fetch("Song", "Artist") is None
    session.get.return_value = FakeResponse({"syncedLyrics": None, "plainLyrics": " "})
    assert client.fetch("Song", "Artist") is None


def test_write_lyrics_sidecar_prefers_synced_and_never_overwrites(tmp_path: Path) -> None:
    audio = tmp_path / "01 - Song.m4a"
    assert write_lyrics_sidecar(audio, Lyrics(synced="[00:01.00] Hi")) == tmp_path / "01 - Song.lrc"
    assert (tmp_path / "01 - Song.lrc").read_text(encoding="utf-8") == "[00:01.00] Hi\n"
    assert write_lyrics_sidecar(audio, Lyrics(synced="[00:02.00] Other")) is None
    assert write_lyrics_sidecar(tmp_path / "Plain.m4a", Lyrics(plain="Words")) == (
        tmp_path / "Plain.txt"
    )
    assert write_lyrics_sidecar(tmp_path / "Inst.m4a", Lyrics(plain="x", instrumental=True)) is None
    assert write_lyrics_sidecar(audio, None) is None


def test_navidrome_call_uses_token_auth_and_raises_on_errors() -> None:
    session = Mock()
    session.get.return_value = FakeResponse({"subsonic-response": {"status": "ok"}})
    client = NavidromeClient("http://nd:4533/", "admin", "secret", session=session)

    client.start_scan()

    url = session.get.call_args.args[0]
    params = session.get.call_args.kwargs["params"]
    assert url == "http://nd:4533/rest/startScan"
    assert params["u"] == "admin" and params["f"] == "json"
    assert params["t"] == hashlib.md5(("secret" + params["s"]).encode()).hexdigest()
    assert "secret" not in params.values()

    session.get.return_value = FakeResponse(
        {"subsonic-response": {"status": "failed", "error": {"message": "Wrong password"}}}
    )
    with pytest.raises(NavidromeError, match="Wrong password"):
        client.start_scan()
    assert client.ping() is False


@pytest.mark.asyncio
async def test_navidrome_request_scan_is_debounced() -> None:
    client = NavidromeClient("http://nd", "u", "p", debounce_seconds=0.05)
    client.start_scan = Mock()  # type: ignore[method-assign]

    for _ in range(5):
        client.request_scan()
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.15)

    client.start_scan.assert_called_once()

    client.request_scan()
    await client.flush()
    assert client.start_scan.call_count == 2
