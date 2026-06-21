from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sync_me_maybe.config import Settings
from sync_me_maybe.queueing.queue import JobKind, QueuedJob
from sync_me_maybe.telegram_bot.runtime import BotRuntime, RequestState


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        telegram_bot_token="123:token",
        allowed_telegram_user_ids={42},
        music_dir=tmp_path / "music",
        download_tmp_dir=tmp_path / "tmp",
        upload_batch_window_seconds=0,
    )


@pytest.fixture
def runtime(settings: Settings) -> BotRuntime:
    return BotRuntime(settings)


@pytest.fixture
def queued_job() -> QueuedJob:
    return QueuedJob(
        kind=JobKind.LINK,
        chat_id=1,
        original_message_id=2,
        status_message_id=3,
        user_id=42,
        source_label="source",
    )


@pytest.fixture
def request_state() -> RequestState:
    return RequestState(
        id="request-1",
        chat_id=1,
        status_message_id=10,
        title="Request",
        total=1,
    )


@pytest.fixture
def fake_status_message() -> SimpleNamespace:
    return SimpleNamespace(message_id=10, edit_text=AsyncMock())


@pytest.fixture
def fake_message(fake_status_message: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        chat_id=1,
        message_id=2,
        from_user=SimpleNamespace(id=42),
        text=None,
        caption=None,
        audio=None,
        document=None,
        reply_text=AsyncMock(return_value=fake_status_message),
    )


@pytest.fixture
def fake_update(fake_message: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        effective_message=fake_message,
        effective_user=SimpleNamespace(id=42),
        callback_query=None,
    )


@pytest.fixture
def fake_application(runtime: BotRuntime) -> SimpleNamespace:
    bot = SimpleNamespace(
        send_message=AsyncMock(),
        send_sticker=AsyncMock(),
        edit_message_text=AsyncMock(),
        send_chat_action=AsyncMock(),
        get_file=AsyncMock(),
        set_my_commands=AsyncMock(),
    )
    return SimpleNamespace(bot=bot, bot_data={"runtime": runtime})


@pytest.fixture
def fake_context(fake_application: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(application=fake_application)
