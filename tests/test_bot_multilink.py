from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace

from sync_me_maybe.bot import handle_message
from sync_me_maybe.config import Settings
from sync_me_maybe.bot import BotRuntime


@dataclass
class FakeStatusMessage:
    message_id: int
    edits: list[str]

    async def edit_text(self, text: str, **kwargs) -> None:
        self.edits.append(text)


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.caption = None
        self.audio = None
        self.document = None
        self.chat_id = 123
        self.message_id = 456
        self.from_user = SimpleNamespace(id=1)
        self.replies: list[tuple[str, dict]] = []
        self._next_message_id = 1000

    async def reply_text(self, text: str, **kwargs) -> FakeStatusMessage:
        self.replies.append((text, kwargs))
        self._next_message_id += 1
        return FakeStatusMessage(self._next_message_id, [])


def make_context(runtime: BotRuntime):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"runtime": runtime}))


def make_runtime(tmp_path) -> BotRuntime:
    return BotRuntime(
        Settings(
            telegram_bot_token="123:ABC",
            allowed_telegram_user_ids={1},
            music_dir=tmp_path / "music",
            download_tmp_dir=tmp_path / "tmp",
        )
    )


def test_message_with_two_supported_links_enqueues_two_jobs(tmp_path) -> None:
    async def run() -> None:
        runtime = make_runtime(tmp_path)
        message = FakeMessage(
            "https://music.youtube.com/watch?v=abc https://open.spotify.com/track/def"
        )
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1))

        await handle_message(update, make_context(runtime))

        snapshot = await runtime.queue.snapshot()
        assert len(snapshot.pending) == 2
        assert snapshot.pending[0].source_label == "youtube link 1/2"
        assert snapshot.pending[1].source_label == "spotify link 2/2"
        assert all(reply[1]["reply_to_message_id"] == 456 for reply in message.replies)
        assert "Link 1 of 2" in message.replies[0][0]

    asyncio.run(run())


def test_mixed_supported_and_unsupported_links_enqueues_supported_only(tmp_path) -> None:
    async def run() -> None:
        runtime = make_runtime(tmp_path)
        message = FakeMessage(
            "https://example.com/nope https://www.shazam.com/track/123/song"
        )
        update = SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1))

        await handle_message(update, make_context(runtime))

        snapshot = await runtime.queue.snapshot()
        assert len(snapshot.pending) == 1
        assert snapshot.pending[0].source_label == "shazam link 2/2"
        assert "Link 1 of 2" in message.replies[0][0]
        assert "Unsupported link" in message.replies[0][0]

    asyncio.run(run())
