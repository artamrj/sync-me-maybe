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


@dataclass
class FakeAudio:
    file_id: str
    file_unique_id: str
    file_name: str
    title: str | None = None


class FakeBot:
    def __init__(self) -> None:
        self.edits: list[tuple[int, str, dict]] = []
        self.sent: list[tuple[int, str, dict]] = []
        self._next_message_id = 2000

    async def edit_message_text(self, chat_id: int, message_id: int, text: str, **kwargs):
        self.edits.append((message_id, text, kwargs))
        return True

    async def send_message(self, chat_id: int, text: str, **kwargs):
        self.sent.append((chat_id, text, kwargs))
        self._next_message_id += 1
        return FakeStatusMessage(self._next_message_id, [])


class FakeMessage:
    def __init__(self, text: str = "", *, audio: FakeAudio | None = None, chat_id: int = 123, user_id: int = 1, message_id: int = 456) -> None:
        self.text = text
        self.caption = None
        self.audio = audio
        self.document = None
        self.chat_id = chat_id
        self.message_id = message_id
        self.from_user = SimpleNamespace(id=user_id)
        self.replies: list[tuple[str, dict]] = []
        self._next_message_id = 1000

    async def reply_text(self, text: str, **kwargs) -> FakeStatusMessage:
        self.replies.append((text, kwargs))
        self._next_message_id += 1
        return FakeStatusMessage(self._next_message_id, [])


def make_context(runtime: BotRuntime, bot: FakeBot | None = None):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"runtime": runtime}, bot=bot or FakeBot()))


def make_runtime(tmp_path, upload_batch_window_seconds: float = 2) -> BotRuntime:
    return BotRuntime(
        Settings(
            telegram_bot_token="123:ABC",
            allowed_telegram_user_ids={1},
            music_dir=tmp_path / "music",
            download_tmp_dir=tmp_path / "tmp",
            upload_batch_window_seconds=upload_batch_window_seconds,
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
        assert len(message.replies) == 1
        assert all(reply[1]["reply_to_message_id"] == 456 for reply in message.replies)
        assert "2 music link(s)" in message.replies[0][0]

    asyncio.run(run())


def test_two_audio_messages_from_same_user_share_one_upload_request(tmp_path) -> None:
    async def run() -> None:
        runtime = make_runtime(tmp_path, upload_batch_window_seconds=0.01)
        bot = FakeBot()
        context = make_context(runtime, bot)
        first = FakeMessage(audio=FakeAudio("file-1", "unique-1", "one.mp3"), message_id=10)
        second = FakeMessage(audio=FakeAudio("file-2", "unique-2", "two.mp3"), message_id=11)

        await handle_message(SimpleNamespace(effective_message=first, effective_user=SimpleNamespace(id=1)), context)
        await handle_message(SimpleNamespace(effective_message=second, effective_user=SimpleNamespace(id=1)), context)
        await asyncio.sleep(0.03)

        snapshot = await runtime.queue.snapshot()
        assert len(snapshot.pending) == 2
        assert len(first.replies) == 1
        assert len(second.replies) == 0
        assert len({job.request_id for job in snapshot.pending}) == 1
        assert "Telegram uploads" in bot.edits[-1][1]

    asyncio.run(run())


def test_audio_messages_from_different_users_create_separate_batches(tmp_path) -> None:
    async def run() -> None:
        runtime = make_runtime(tmp_path, upload_batch_window_seconds=0.01)
        runtime.settings.allowed_telegram_user_ids.add(2)
        context = make_context(runtime)
        first = FakeMessage(audio=FakeAudio("file-1", "unique-1", "one.mp3"), user_id=1, message_id=10)
        second = FakeMessage(audio=FakeAudio("file-2", "unique-2", "two.mp3"), user_id=2, message_id=11)

        await handle_message(SimpleNamespace(effective_message=first, effective_user=SimpleNamespace(id=1)), context)
        await handle_message(SimpleNamespace(effective_message=second, effective_user=SimpleNamespace(id=2)), context)
        await asyncio.sleep(0.03)

        snapshot = await runtime.queue.snapshot()
        assert len(snapshot.pending) == 2
        assert len(first.replies) == 1
        assert len(second.replies) == 1
        assert len({job.request_id for job in snapshot.pending}) == 2

    asyncio.run(run())


def test_single_audio_upload_creates_one_request(tmp_path) -> None:
    async def run() -> None:
        runtime = make_runtime(tmp_path, upload_batch_window_seconds=0.01)
        message = FakeMessage(audio=FakeAudio("file-1", "unique-1", "one.mp3"), message_id=10)

        await handle_message(SimpleNamespace(effective_message=message, effective_user=SimpleNamespace(id=1)), make_context(runtime))
        await asyncio.sleep(0.03)

        snapshot = await runtime.queue.snapshot()
        assert len(snapshot.pending) == 1
        assert len(message.replies) == 1
        assert snapshot.pending[0].source_label == "one.mp3"

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
        assert len(message.replies) == 1
        assert "Unsupported link" in message.replies[0][0]
        assert "❌ 1" in message.replies[0][0]

    asyncio.run(run())
