"""Switching to another Telegram bot: stale update offsets and group bindings of the old bot must not carry over."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from actoroffice.app import Office
from actoroffice.channels.telegram import TelegramBridge, TelegramError
from actoroffice.config import Settings

from .fakes import FakeRuntime
from .seed import seed_db

GROUP = -100123


class Bot:
    def __init__(self, bot_id: str, *, in_group: bool) -> None:
        self.bot_id = bot_id
        self.in_group = in_group
        self.calls: list[str] = []

    async def call(self, method: str, **params: Any) -> Any:
        self.calls.append(method)
        if method == "getMe":
            return {"username": f"bot{self.bot_id}"}
        if method == "getChat" and not self.in_group:
            raise TelegramError("Bad Request: chat not found", 400)
        if method == "getUpdates":
            await asyncio.sleep(0.05)
            return []
        return {"message_id": 1, "message_thread_id": 1}

    async def close(self) -> None:
        pass


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home"), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    yield o
    await o.close()


async def state(office: Office) -> dict[str, str]:
    return {r["key"]: r["value"] for r in await office.db.fetchall("SELECT key, value FROM telegram_state")}


async def seed(office: Office, bot_id: str) -> None:
    for key, value in (("bot_id", bot_id), ("offset", "940059168"), ("chat_id", str(GROUP))):
        await office.db.execute("INSERT OR REPLACE INTO telegram_state(key, value) VALUES (?, ?)", (key, value))


async def test_same_bot_keeps_its_offset_and_group(office: Office) -> None:
    await seed(office, "111")
    bridge = TelegramBridge(office, Bot("111", in_group=True))
    await bridge._check_bot()
    assert await state(office) == {"bot_id": "111", "offset": "940059168", "chat_id": str(GROUP)}
    assert bridge.bot_name == "bot111"


async def test_new_bot_in_the_old_group_keeps_the_group_but_not_the_offset(office: Office) -> None:
    await seed(office, "111")
    await TelegramBridge(office, Bot("222", in_group=True))._check_bot()
    assert await state(office) == {"bot_id": "222", "chat_id": str(GROUP)}


async def test_new_bot_elsewhere_waits_for_start_in_its_group(office: Office) -> None:
    await seed(office, "111")
    await TelegramBridge(office, Bot("222", in_group=False))._check_bot()
    assert await state(office) == {"bot_id": "222"}


async def test_offices_from_before_this_check_reset_only_the_offset(office: Office) -> None:
    await seed(office, "111")
    await office.db.execute("DELETE FROM telegram_state WHERE key = 'bot_id'")
    await TelegramBridge(office, Bot("111", in_group=True))._check_bot()
    assert await state(office) == {"bot_id": "111", "chat_id": str(GROUP)}


async def test_stop_telegram_stops_the_bridge_and_keeps_the_office(office: Office) -> None:
    bot = Bot("111", in_group=True)
    office.telegram = TelegramBridge(office, bot)
    ready = asyncio.Event()
    office._tasks.append(asyncio.create_task(office.telegram.run(ready), name="telegram"))
    await ready.wait()
    await office.stop_telegram()
    assert office.telegram is None and not [t for t in office._tasks if t.get_name() == "telegram"]
    await office.stop_telegram()  # nothing running: a no-op
