from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.chat import ConversationKind, MessageKind
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import InteractionKind, TicketStatus
from actoroffice.services.chat import handle

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, max_unattended_agent_messages=3), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    yield o
    await o.close()


async def hire(office: Office, position: str, name: str):
    pos = await office.org.find_position(position)
    return await office.org.hire(pos.id, name)


def reply(text: str):
    async def script(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", text=text)

    return script


def test_handles_ignore_accents_and_spaces():
    assert handle("Hùng") == "hung"
    assert handle("Minh Anh") == "minhanh"
    assert handle("Đức") == "duc"


async def test_dm_message_wakes_actor_and_reply_is_posted(office, runtime):
    lan = await hire(office, "writer", "Lan")
    runtime.scripts.append(reply("Chào bạn, mình là Lan."))
    conv = await office.chat.dm(lan.id)
    assert (await office.chat.dm(lan.id)).id == conv.id  # get-or-create

    await office.chat.post_user(conv.id, "Chào Lan")
    await office.scheduler.wait_idle(timeout=10)

    msgs = await office.chat.messages(conv.id)
    assert [(m.author_kind, m.body) for m in msgs] == [("user", "Chào Lan"), ("actor", "Chào bạn, mình là Lan.")]
    spec = runtime.specs[0]
    assert "[User] Chào Lan" in spec.prompt
    assert spec.tools == ["Read", "Glob", "Grep"] and not spec.coding_preset
    assert {t.name for t in spec.internal_tools} >= {"propose_ticket", "chat_create_group", "tickets_list"}


async def test_group_only_mentioned_actors_reply(office, runtime):
    mai, minh, hung = [await hire(office, p, n) for p, n in (("ba", "Mai"), ("dev", "Minh"), ("qa", "Hùng"))]
    conv = await office.chat.create_group("Login", [mai.id, minh.id, hung.id])

    await office.chat.post_user(conv.id, "Mọi người xem giúp")  # no mention → nobody is woken
    assert await office.wakes.pending() == []

    runtime.scripts.append(reply("Ok"))
    await office.chat.post_user(conv.id, "@Hung kiểm tra giúp")  # accent-insensitive mention
    [wake] = await office.wakes.pending()
    assert wake.actor_id == hung.id and wake.conversation_id == conv.id


async def test_mentioning_outsider_adds_them_to_group(office):
    mai, minh = await hire(office, "ba", "Mai"), await hire(office, "dev", "Minh")
    conv = await office.chat.create_group("Spec", [mai.id])
    await office.chat.post_user(conv.id, "@Minh vào xem với")
    assert minh.id in (await office.chat.get(conv.id)).actor_ids


async def test_actor_chatter_pauses_after_limit(office):
    a, b = await hire(office, "ba", "Mai"), await hire(office, "dev", "Minh")
    conv = await office.chat.create_group("Ping", [a.id, b.id])
    await office.chat.post_user(conv.id, "bắt đầu")
    for i in range(5):
        speaker, other = (a, "Minh") if i % 2 == 0 else (b, "Mai")
        await office.chat.post_actor(conv.id, speaker, f"@{other} lượt {i}")
    msgs = await office.chat.messages(conv.id)
    assert any("Tạm dừng" in m.body for m in msgs if m.author_kind == "system")


async def test_relay_mention_does_not_wake_an_engaged_actor(office):
    from actoroffice.domain.ids import new_id
    from actoroffice.domain.run import Run, RunKind

    pm, lan = await hire(office, "pm", "Phuong"), await hire(office, "writer", "Lan")
    conv = await office.chat.create_group("README", [pm.id, lan.id])
    await office.chat.post_user(conv.id, "@Lan viết 2 tiêu đề, @Phuong chọn 1")
    # Lan is already replying to the user when Phuong relays the request to her.
    await office.runs.start(Run(id=new_id("run"), actor_id=lan.id, kind=RunKind.CHAT, conversation_id=conv.id))
    await office.wakes.finish("none")
    for w in await office.wakes.pending():
        if w.actor_id == lan.id:
            await office.wakes.drop(w.id, "taken by the running turn")

    await office.chat.post_actor(conv.id, pm, "@Lan bạn đề xuất 2 tiêu đề nhé")
    assert [w for w in await office.wakes.pending() if w.actor_id == lan.id] == []


async def test_propose_ticket_then_confirm_creates_and_assigns(office, runtime):
    pm, lan = await hire(office, "pm", "Phuong"), await hire(office, "writer", "Lan")
    conv = await office.chat.dm(pm.id)

    async def propose(ctx: TurnCtx) -> TurnResult:
        await ctx.office("propose_ticket", {"title": "Viết bài giới thiệu", "description": "500 chữ", "assignee": "Lan"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s1", text="Mình đề xuất ticket cho Lan, bạn xác nhận nhé.")

    async def lan_works(ctx: TurnCtx) -> None:
        await ctx.office("ticket_set_status", {"status": "done", "comment": "Đã viết xong bài 500 chữ"})

    runtime.scripts += [propose, lan_works]
    await office.chat.post_user(conv.id, "Cần một bài giới thiệu sản phẩm")
    await office.scheduler.wait_idle(timeout=10)

    [proposal] = await office.interactions.pending()
    assert proposal.kind == InteractionKind.CONFIRM_TICKET
    msgs = await office.chat.messages(conv.id)
    assert any(m.kind == MessageKind.INTERACTION and m.meta["interaction_id"] == proposal.id for m in msgs)

    resolved = await office.answer(proposal.id, {"confirmed": True, "title": "Viết bài giới thiệu sản phẩm"})
    ticket = await office.tickets.get(resolved.response["ticket_key"])
    assert ticket.assignee_actor_id == lan.id and ticket.origin_conversation_id == conv.id
    assert ticket.title == "Viết bài giới thiệu sản phẩm"

    await office.scheduler.wait_idle(timeout=10)
    assert (await office.tickets.get(ticket.id)).status == TicketStatus.DONE
    msgs = await office.chat.messages(conv.id)
    events = [(m.meta.get("event"), m.meta.get("status")) for m in msgs if m.kind == MessageKind.TICKET]
    assert ("created", "todo") in events
    # Finished work is reported by the assignee, in the conversation where it was requested.
    [report] = [m for m in msgs if m.meta.get("report")]
    assert report.author_kind == "actor" and report.author_id == lan.id
    assert "Đã viết xong bài 500 chữ" in report.body and ticket.key in report.body


async def test_question_from_ticket_lands_in_actor_dm(office, runtime):
    ba = await hire(office, "ba", "Mai")

    async def ask(ctx: TurnCtx) -> None:
        await ctx.office("ask_user", {"question": "Đối tượng người dùng?", "options": ["B2B", "B2C"]})
        await ctx.office("ticket_set_status", {"status": "in_review", "comment": "chờ trả lời"})

    runtime.scripts.append(ask)
    await office.tickets.create("Spec", assignee_actor_id=ba.id)
    await office.scheduler.wait_idle(timeout=10)

    dm = await office.chat.dm(ba.id)
    assert dm.kind == ConversationKind.DM
    kinds = [m.kind for m in await office.chat.messages(dm.id)]
    assert MessageKind.INTERACTION in kinds


async def test_actor_can_open_group_from_dm(office, runtime):
    mai, minh = await hire(office, "ba", "Mai"), await hire(office, "dev", "Minh")
    dm = await office.chat.dm(mai.id)

    async def open_group(ctx: TurnCtx) -> TurnResult:
        await ctx.office("chat_create_group", {"title": "Làm rõ API", "members": ["Minh"], "message": "@Minh API trả lỗi gì khi hết hạn?"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", text="Mình đã lập nhóm với Minh.")

    runtime.scripts += [open_group, reply("Trả 401.")]
    await office.chat.post_user(dm.id, "Hỏi Minh giúp mình về API")
    await office.scheduler.wait_idle(timeout=10)

    groups = [c for c in await office.chat.list() if c["kind"] == "group"]
    assert len(groups) == 1 and {groups[0]["members"][i]["id"] for i in range(3)} >= {mai.id, minh.id, "user"}
    bodies = [m.body for m in await office.chat.messages(groups[0]["id"])]
    assert "Trả 401." in bodies
