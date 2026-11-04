"""SI-38 AC-16: a run out of automatic retries is reported in its ticket's Telegram topic."""

from __future__ import annotations

import asyncio

from actoroffice.domain.run import Run, RunKind, RunStatus

from .test_telegram import bind, bot, hire, office, until  # noqa: F401  (fixtures)

ERROR = "Claude · mạng/API: đã thử lại 3 lần; kiểm tra kết nối mạng/API rồi bấm Thử lại. Chi tiết: Unable to connect to API"


async def failed_run(office, actor, ticket, run_id: str, kind: str) -> None:
    await office.runs.start(Run(id=run_id, actor_id=actor.id, kind=RunKind.TICKET, ticket_id=ticket.id))
    await office.runs.finish(run_id, RunStatus.FAILED, error=ERROR if kind == "network" else "Claude · lỗi của AI: boom", error_kind=kind)


async def test_ac16_telegram_notice_on_exhausted_retries(office, bot) -> None:
    minh = await hire(office, "dev", "Minh")
    await bind(office, bot)
    dm = await office.chat.dm(minh.id)
    ticket = await office.tickets.create("Xuất báo cáo", assignee_actor_id=minh.id, origin_conversation_id=dm.id)
    office.telegram.progress.enabled = False  # the notice does not depend on the progress cards

    await failed_run(office, minh, ticket, "run_net", "network")
    await until(lambda: any("hết lượt tự thử lại" in p["text"] for p in bot.sent()))
    [notice] = [p for p in bot.sent() if "hết lượt tự thử lại" in p["text"]]
    assert ticket.key in notice["text"] and "mạng/API" in notice["text"] and "Claude" in notice["text"]
    assert notice["message_thread_id"] == await office.telegram.topic_for(dm.id)

    await failed_run(office, minh, ticket, "run_ai", "ai")  # not a passing failure: comment and web card only
    await asyncio.sleep(0.3)
    assert len([p for p in bot.sent() if "hết lượt tự thử lại" in p["text"]]) == 1
