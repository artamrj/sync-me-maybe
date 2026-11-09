"""Write library tags and cover art into downloaded audio files.

Navidrome and other library scanners organize music by embedded tags, not by
filename, so every downloaded file gets title, artists, album, album artist,
track/disc numbers, date, ISRC, and cover art when they are known.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from pathlib import Path

from mutagen import File as MutagenFile
from mutagen import MutagenError
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCON,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TRCK,
    TSRC,
    TXXX,
)
from mutagen.mp3 import MP3
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class AudioTags:
    """Tag values to write; empty values are left out."""

    title: str | None = None
    artists: tuple[str, ...] = ()
    album: str | None = None
    album_artist: str | None = None
    track_number: int | None = None
    disc_number: int | None = None
    date: str | None = None
    isrc: str | None = None
    genres: tuple[str, ...] = ()
    source_url: str | None = None

    @property
    def artist(self) -> str | None:
        return ", ".join(self.artists) or None


@dataclass(frozen=True)
class CoverArt:
    data: bytes
    mime: str = "image/jpeg"


def cover_from_bytes(data: bytes | None) -> CoverArt | None:
    if not data:
        return None
    if data.startswith(b"\x89PNG"):
        return CoverArt(data, "image/png")
    if data.startswith(b"\xff\xd8"):
        return CoverArt(data, "image/jpeg")
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        # Few players render embedded WebP; skip rather than embed broken art.
        return None
    return CoverArt(data, "image/jpeg")


def write_tags(path: Path, tags: AudioTags, cover: CoverArt | None = None) -> bool:
    """Write tags in place. Returns False when the format is unsupported."""
    try:
        audio = MutagenFile(path)
    except MutagenError:
        LOGGER.debug("Unreadable audio file %s", path, exc_info=True)
        return False
    if audio is None:
        LOGGER.debug("No tag support for %s", path)
        return False
    if isinstance(audio, MP3):
        _write_id3(path, tags, cover)
    elif isinstance(audio, MP4):
        _write_mp4(audio, tags, cover)
        audio.save()
    elif isinstance(audio, FLAC):
        _write_vorbis(audio, tags)
        if cover:
            audio.clear_pictures()
            audio.add_picture(_flac_picture(cover))
        audio.save()
    elif audio.tags is not None or hasattr(audio, "add_tags"):
        # OggOpus / OggVorbis use Vorbis comments with base64 pictures.
        if audio.tags is None:
            audio.add_tags()
        _write_vorbis(audio, tags)
        if cover:
            picture = base64.b64encode(_flac_picture(cover).write()).decode("ascii")
            audio["METADATA_BLOCK_PICTURE"] = [picture]
        audio.save()
    else:
        return False
    return True


def _write_id3(path: Path, tags: AudioTags, cover: CoverArt | None) -> None:
    audio = MP3(path, ID3=ID3)
    if audio.tags is None:
        audio.add_tags()
    id3 = audio.tags
    assert id3 is not None
    frames = {
        "TIT2": TIT2(encoding=3, text=tags.title) if tags.title else None,
        "TPE1": TPE1(encoding=3, text=list(tags.artists)) if tags.artists else None,
        "TALB": TALB(encoding=3, text=tags.album) if tags.album else None,
        "TPE2": TPE2(encoding=3, text=tags.album_artist) if tags.album_artist else None,
        "TRCK": TRCK(encoding=3, text=str(tags.track_number)) if tags.track_number else None,
        "TPOS": TPOS(encoding=3, text=str(tags.disc_number)) if tags.disc_number else None,
        "TDRC": TDRC(encoding=3, text=tags.date) if tags.date else None,
        "TSRC": TSRC(encoding=3, text=tags.isrc) if tags.isrc else None,
        "TCON": TCON(encoding=3, text=list(tags.genres)) if tags.genres else None,
    }
    for frame_id, frame in frames.items():
        if frame is not None:
            id3.setall(frame_id, [frame])
    if tags.artists:
        id3.setall("TXXX:ARTISTS", [TXXX(encoding=3, desc="ARTISTS", text=list(tags.artists))])
    if tags.source_url:
        id3.setall("TXXX:SOURCE", [TXXX(encoding=3, desc="SOURCE", text=tags.source_url)])
    if cover:
        id3.setall(
            "APIC", [APIC(encoding=3, mime=cover.mime, type=3, desc="Cover", data=cover.data)]
        )
    audio.save(v2_version=4)


def _write_mp4(audio: MP4, tags: AudioTags, cover: CoverArt | None) -> None:
    if audio.tags is None:
        audio.add_tags()
    mp4 = audio.tags
    assert mp4 is not None
    if tags.title:
        mp4["\xa9nam"] = [tags.title]
    if tags.artist:
        mp4["\xa9ART"] = [tags.artist]
    if tags.album:
        mp4["\xa9alb"] = [tags.album]
    if tags.album_artist:
        mp4["aART"] = [tags.album_artist]
    if tags.track_number:
        mp4["trkn"] = [(tags.track_number, 0)]
    if tags.disc_number:
        mp4["disk"] = [(tags.disc_number, 0)]
    if tags.date:
        mp4["\xa9day"] = [tags.date]
    if tags.genres:
        mp4["\xa9gen"] = list(tags.genres)
    if tags.artists:
        mp4["----:com.apple.iTunes:ARTISTS"] = [
            MP4FreeForm(name.encode("utf-8")) for name in tags.artists
        ]
    if tags.isrc:
        mp4["----:com.apple.iTunes:ISRC"] = [MP4FreeForm(tags.isrc.encode("utf-8"))]
    if tags.source_url:
        mp4["----:com.apple.iTunes:SOURCE"] = [MP4FreeForm(tags.source_url.encode("utf-8"))]
    if cover:
        image_format = MP4Cover.FORMAT_PNG if cover.mime == "image/png" else MP4Cover.FORMAT_JPEG
        mp4["covr"] = [MP4Cover(cover.data, imageformat=image_format)]


def _write_vorbis(audio: object, tags: AudioTags) -> None:
    values: dict[str, list[str]] = {
        "TITLE": [tags.title] if tags.title else [],
        "ARTIST": [tags.artist] if tags.artist else [],
        "ARTISTS": list(tags.artists),
        "ALBUM": [tags.album] if tags.album else [],
        "ALBUMARTIST": [tags.album_artist] if tags.album_artist else [],
        "TRACKNUMBER": [str(tags.track_number)] if tags.track_number else [],
        "DISCNUMBER": [str(tags.disc_number)] if tags.disc_number else [],
        "DATE": [tags.date] if tags.date else [],
        "ISRC": [tags.isrc] if tags.isrc else [],
        "GENRE": list(tags.genres),
        "SOURCE": [tags.source_url] if tags.source_url else [],
    }
    for key, value in values.items():
        if value:
            audio[key] = value  # type: ignore[index]


def _flac_picture(cover: CoverArt) -> Picture:
    picture = Picture()
    picture.type = 3  # front cover
    picture.mime = cover.mime
    picture.desc = "Cover"
    picture.data = cover.data
    return picture
