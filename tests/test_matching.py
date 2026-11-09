from __future__ import annotations

from typing import Any

from sync_me_maybe.music.matching import (
    Candidate,
    MatchTarget,
    YouTubeMusicMatcher,
    base_title,
    best_candidate,
    candidate_from_ytmusic,
    clean_channel_name,
    clean_video_title,
    large_thumbnail,
    primary_artist,
    score,
)


def test_text_helpers_normalize_titles_artists_and_channels() -> None:
    assert base_title("One More Time (Radio Edit)") == "one more time"
    assert base_title("Song - Remastered 2011") == "song"
    assert primary_artist("Daft Punk, Pharrell Williams") == "Daft Punk"
    assert primary_artist("Artist feat. Guest") == "Artist"
    assert clean_channel_name("Daft Punk - Topic") == "Daft Punk"
    assert clean_channel_name("DaftPunkVEVO") == "DaftPunk"
    assert clean_video_title("Daft Punk - One More Time (Official Video)", ["Daft Punk"]) == (
        "One More Time"
    )
    assert large_thumbnail("https://lh3.googleusercontent.com/x=w120-h120-l90-rj") == (
        "https://lh3.googleusercontent.com/x=w1200-h1200-l90-rj"
    )


def test_score_prefers_studio_version_over_live_and_covers() -> None:
    target = MatchTarget("Get Lucky", "Daft Punk", 248)
    studio = Candidate("Get Lucky", ("Daft Punk",), 249, "studio", official=True)
    live = Candidate("Get Lucky (Live)", ("Daft Punk",), 251, "live", official=True)
    cover = Candidate("Get Lucky", ("Some Cover Band",), 248, "cover", official=True)

    best = best_candidate(target, [live, cover, studio])

    assert best is not None
    assert best[0].video_id == "studio"
    assert score(target, studio).total > score(target, live).total
    assert not score(target, cover).accepted


def test_score_rejects_large_duration_mismatch_and_unrelated_titles() -> None:
    target = MatchTarget("Get Lucky", "Daft Punk", 248)
    extended = Candidate("Get Lucky", ("Daft Punk",), 369, "extended", official=True)
    unrelated = Candidate("Around the World", ("Daft Punk",), 248, "other", official=True)

    assert not score(target, extended).accepted
    assert not score(target, unrelated).accepted
    assert best_candidate(target, [extended, unrelated]) is None


def test_score_accepts_remix_when_remix_was_requested() -> None:
    target = MatchTarget("Song (Club Remix)", "Artist")
    candidate = Candidate("Song (Club Remix)", ("Artist",), None, "remix", official=True)
    assert score(target, candidate).accepted


def test_candidate_from_ytmusic_handles_songs_videos_and_bad_items() -> None:
    song = candidate_from_ytmusic(
        {
            "resultType": "song",
            "videoId": "abc",
            "title": "One More Time",
            "artists": [{"name": "Daft Punk"}],
            "duration_seconds": 320,
            "thumbnails": [{"url": "https://img/x=w60-h60"}, {"url": "https://img/x=w120-h120"}],
        }
    )
    video = candidate_from_ytmusic(
        {
            "resultType": "video",
            "videoId": "def",
            "title": "Daft Punk - One More Time (Official Video)",
            "artists": [{"name": "Daft Punk"}],
            "duration_seconds": 322,
        }
    )

    assert song is not None and song.official and song.thumbnail_url == "https://img/x=w1200-h1200"
    assert video is not None and not video.official and video.title == "One More Time"
    assert candidate_from_ytmusic({"title": "missing id"}) is None
    assert candidate_from_ytmusic("nope") is None


class FakeYTMusic:
    def __init__(self, results: dict[str, list[dict[str, Any]]]) -> None:
        self.results = results
        self.calls: list[tuple[str, str]] = []

    def search(self, query: str, filter: str, limit: int) -> list[dict[str, Any]]:
        self.calls.append((query, filter))
        return self.results.get(filter, [])


def test_matcher_uses_songs_first_then_strict_video_fallback() -> None:
    song_client = FakeYTMusic(
        {
            "songs": [
                {
                    "resultType": "song",
                    "videoId": "song-id",
                    "title": "Get Lucky",
                    "artists": [{"name": "Daft Punk"}],
                    "duration_seconds": 248,
                }
            ]
        }
    )
    match = YouTubeMusicMatcher(song_client).find(MatchTarget("Get Lucky", "Daft Punk", 248))
    assert match is not None
    assert match.url == "https://www.youtube.com/watch?v=song-id"
    assert song_client.calls == [("Daft Punk Get Lucky", "songs")]

    video_client = FakeYTMusic(
        {
            "videos": [
                {
                    "resultType": "video",
                    "videoId": "video-id",
                    "title": "Daft Punk - Get Lucky (Official Audio)",
                    "artists": [{"name": "DaftPunkVEVO"}],
                    "duration_seconds": 250,
                }
            ]
        }
    )
    match = YouTubeMusicMatcher(video_client).find(MatchTarget("Get Lucky", "Daft Punk", 248))
    assert match is not None and match.candidate.video_id == "video-id"
    assert [kind for _, kind in video_client.calls] == ["songs", "videos"]

    assert YouTubeMusicMatcher(FakeYTMusic({})).find(MatchTarget("Get Lucky", "Daft Punk")) is None
