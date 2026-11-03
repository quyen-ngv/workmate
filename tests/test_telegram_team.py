"""Team mode on Telegram: people are recognised by Telegram id, cards check roles, tickets get their own topic."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from actoroffice.app import Office
from actoroffice.channels.telegram import TelegramBridge
from actoroffice.config import Settings
from actoroffice.domain.work import InteractionKind

from .fakes import FakeRuntime
from .seed import seed_db
from .test_telegram import GROUP, FakeBot

LAN, MINH, STRANGER = 111, 222, 999


def msg(text: str, sender: int, thread: int | None = None, *, reply_to: dict[str, Any] | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {
        "message_id": 9000 + sender,
        "from": {"id": sender, "is_bot": False, "first_name": str(sender)},
        "chat": {"id": GROUP, "type": "supergroup", "is_forum": True},
        "text": text,
    }
    if thread:
        m.update(message_thread_id=thread, is_topic_message=True)
    if reply_to:
        m["reply_to_message"] = reply_to
    return {"update_id": 1, "message": m}


def press(data: str, sender: int, thread: int | None = None) -> dict[str, Any]:
    return {
        "update_id": 2,
        "callback_query": {"id": f"cb{sender}", "data": data, "from": {"id": sender}, "message": {"chat": {"id": GROUP}, "message_thread_id": thread}},
    }


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


@pytest.fixture
async def bot(office) -> AsyncIterator[FakeBot]:
    fake = FakeBot()
    office.telegram = TelegramBridge(office, fake)
    ready = asyncio.Event()
    task = asyncio.create_task(office.telegram.run(ready))
    await ready.wait()
    await office.telegram.handle_update(msg("/start", LAN))
    assert office.telegram.chat_id == GROUP
    yield fake
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def answers(bot: FakeBot) -> list[str]:
    return [p.get("text", "") for p in bot.sent("answerCallbackQuery")]


async def test_people_register_and_cards_check_roles(office: Office, bot: FakeBot) -> None:
    team = await office.teams.create_team("Billing")
    tg = office.telegram
    await tg.handle_update(msg("/adduser Lan ba", LAN))  # the first person registers themself and becomes admin
    lan = await office.teams.user_by_telegram(LAN)
    assert lan and set(lan.default_roles) == {"ba", "admin"}
    assert [m.user.name for m in await office.teams.members(team.id)] == ["Lan"]
    await tg.handle_update(msg("/adduser Minh dev", LAN, reply_to={"message_id": 5, "from": {"id": MINH, "is_bot": False}}))
    assert (await office.teams.user_by_telegram(MINH)).name == "Minh"

    # a stranger is told how to join and cannot press buttons
    await tg.handle_update(msg("xin chào", STRANGER, 5))
    assert any("chưa được đăng ký" in p["text"] for p in bot.sent())

    ticket = await office.tickets.create("Hoàn tiền", description="x")
    await office.db.execute("UPDATE tickets SET team_id = ?, stage = 'understand', stage_template = 'code' WHERE id = ?", (team.id, ticket.id))
    item = await office.interactions.create(
        InteractionKind.QUESTION, {"question": "VIP?", "options": ["Có", "Không"], "recommended": "Có", "roles": ["ba"], "roles_label": "BA", "asked_by": "Copilot"}, ticket_id=ticket.id
    )
    await tg.handle_update(press(f"i:{item.id}:o0", STRANGER))
    await tg.handle_update(press(f"i:{item.id}:o0", MINH))
    assert (await office.interactions.get(item.id)).status.value == "pending"
    assert any("BA" in a for a in answers(bot))
    await tg.handle_update(press(f"i:{item.id}:o0", LAN))
    resolved = await office.interactions.get(item.id)
    assert resolved.status.value == "answered" and resolved.response == {"answer": "Có", "answered_by": "Lan"}


async def test_task_command_opens_a_ticket_topic_and_gate_reply_sends_it_back(office: Office, bot: FakeBot) -> None:
    team = await office.teams.create_team("Billing")
    tg = office.telegram
    await tg.handle_update(msg("/adduser Lan ba", LAN))
    await office.teams.add_member(team.id, (await office.teams.user_by_telegram(LAN)).id, ["ba", "owner"])
    await tg.handle_update(msg("/task research So sánh 3 cổng thanh toán", LAN))
    ticket = (await office.tickets.list(limit=1))[0]
    assert ticket.stage == "understand" and ticket.stage_template == "research" and ticket.owner_user_id
    topics = [p["name"] for p in bot.sent("createForumTopic")]
    assert any(ticket.key in n for n in topics)
    thread = await tg.topic_for(ticket.origin_conversation_id)

    # a message in the ticket's topic is a comment on the ticket (the AI hears it), attributed to the person
    await tg.handle_update(msg("Ưu tiên phí thấp", LAN, thread))
    comments = await office.tickets.comments(ticket.id, limit=50)
    assert any(c.body == "Ưu tiên phí thấp" and c.author_id == "Lan" and c.author_kind == "user" for c in comments)

    # /status in the topic
    await tg.handle_update(msg("/status", LAN, thread))
    assert any("Giai đoạn" in p["text"] and ticket.key in p["text"] for p in bot.sent())

    # a gate card; a text reply to it sends the stage back with the note
    await office.tickets.put_document(ticket.id, "requirements", "AC-1: Given a When b Then c", author="Copilot", base_revision=None)
    await office.tickets.put_document(ticket.id, "dossier", "# Dossier", author="Copilot", base_revision=None)
    stage = await office.stages.stage_def(await office.tickets.get(ticket.id))
    gate = await office.stages._open_gate(await office.tickets.get(ticket.id), stage, "requirement", "xong", [], run_id=None)
    card = None
    for _ in range(100):  # the notifier posts the card asynchronously
        card = await office.db.fetchone("SELECT message_id FROM telegram_cards WHERE interaction_id = ?", (gate.id,))
        if card:
            break
        await asyncio.sleep(0.02)
    assert card, "the gate card was posted in the ticket topic"
    await tg.handle_update(msg("Thiếu tiêu chí về phí quốc tế", LAN, thread, reply_to={"message_id": card["message_id"]}))
    resolved = await office.interactions.get(gate.id)
    assert resolved.response["approved"] is False and "phí quốc tế" in resolved.response["note"]
    runs = await office.stages.runs(ticket.id)
    assert runs[-1].attempt == 2 and runs[-1].stage == "understand"
