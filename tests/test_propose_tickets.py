"""SI-24: the AI proposes a list of tickets on one card; an owner or lead approves it once."""

from __future__ import annotations

from typing import Any

import pytest

from actoroffice.app import Office
from actoroffice.channels import tg_format as fmt
from actoroffice.domain.errors import PolicyDenied
from actoroffice.domain.ids import new_id
from actoroffice.domain.run import Run, RunKind
from actoroffice.domain.stage import StageRun, default_templates
from actoroffice.domain.work import Interaction, InteractionKind, InteractionStatus, TicketStatus
from actoroffice.engine import prompt_pack
from actoroffice.engine.executor import TICKET_TOOLS
from actoroffice.services.proposals import parse_drop
from actoroffice.tools.chat_tools import CHAT_TOOLS
from actoroffice.tools.proposal_tools import propose_tickets
from actoroffice.tools.registry import ToolCtx, ToolError

from .test_telegram import GROUP, bind, bot, office, tg_message, until  # noqa: F401  (fixtures)

S = TicketStatus

ITEMS: list[dict[str, Any]] = [
    {"title": "Hàng đợi theo dự án", "why": "17 ticket viết tay (loop log)", "description": "AC-1: chạy tuần tự", "workflow": "code"},
    {"title": "Thẻ đề xuất trên web", "why": "team.js:303 chưa có thẻ", "description": "AC-1: thẻ hiện", "workflow": "quick", "depends_on": [1]},
    {"title": "Tài liệu hướng dẫn", "why": "README thiếu", "description": "AC-1: có mục mới"},
]


async def ctx_for(office: Office, *, ticket_id: int | None = None, conversation: str | None = None) -> ToolCtx:
    ai = await office.ensure_ai()
    position = await office.org.get_position(ai.position_id)
    kind = RunKind.TICKET if ticket_id else RunKind.CHAT
    run = await office.runs.start(Run(id=new_id("run"), actor_id=ai.id, kind=kind, ticket_id=ticket_id, conversation_id=conversation))
    return ToolCtx(office, ai, position, run_id=run.id, ticket_id=ticket_id, conversation_id=conversation)


async def research(office: Office, team_id: str | None = None):
    ticket = await office.tickets.create("Nghiên cứu backlog", description="Brainstorm")
    return await office.stages.start(ticket.id, template="research", by="user", team_id=team_id)


async def cards(office: Office) -> list[Interaction]:
    return [i for i in await office.interactions.pending() if i.kind == InteractionKind.PROPOSE_TICKETS]


async def count_tickets(office: Office) -> int:
    row = await office.db.fetchone("SELECT COUNT(*) AS n FROM tickets")
    return int(row["n"])


async def propose_from_chat(office: Office, items: list[dict[str, Any]] | None = None) -> tuple[Interaction, str]:
    ai = await office.ensure_ai()
    dm = await office.chat.dm(ai.id)
    await propose_tickets({"items": items or ITEMS}, await ctx_for(office, conversation=dm.id))
    [card] = await cards(office)
    return card, dm.id


async def propose_from_ticket(office: Office, items: list[dict[str, Any]] | None = None, team_id: str | None = None):
    source = await research(office, team_id)
    await propose_tickets({"items": items or ITEMS}, await ctx_for(office, ticket_id=source.id))
    [card] = await cards(office)
    return card, source


async def created(office: Office, card: Interaction):
    resolved = await office.interactions.get(card.id)
    return [await office.tickets.get(k) for k in resolved.response["created"]]


# ---- AC-1, AC-2: the tool -----------------------------------------------------------------------------


async def test_ac1_propose_creates_pending_card_no_tickets(office: Office) -> None:
    before = await count_tickets(office)
    card, dm = await propose_from_chat(office)
    assert card.status == InteractionStatus.PENDING and card.ticket_id is None
    assert card.payload["conversation_id"] == dm
    assert [i["title"] for i in card.payload["items"]] == [i["title"] for i in ITEMS]
    assert [i["run_now"] for i in card.payload["items"]] == [True, False, False]
    assert card.payload["items"][1]["depends_on"] == [1] and card.payload["items"][2]["workflow"] == "code"
    assert await count_tickets(office) == before
    blockers = await office.db.fetchone("SELECT COUNT(*) AS n FROM ticket_blockers")
    assert blockers["n"] == 0


async def test_ac1_propose_from_ticket_session_and_replace_previous(office: Office) -> None:
    source = await research(office)
    before = await count_tickets(office)
    ctx = await ctx_for(office, ticket_id=source.id)
    await propose_tickets({"items": ITEMS}, ctx)
    [first] = await cards(office)
    assert first.ticket_id == source.id and first.payload["source_ticket"] == source.key
    out = await propose_tickets({"items": ITEMS[:1]}, ctx)  # A-4: a new list replaces the pending one
    [second] = await cards(office)
    assert second.id != first.id and len(second.payload["items"]) == 1 and "replaces" in out
    assert (await office.interactions.get(first.id)).status == InteractionStatus.CANCELLED
    assert await count_tickets(office) == before


@pytest.mark.parametrize(
    ("items", "message"),
    [
        ([], "1..20"),
        ([ITEMS[2]] * 21, "1..20"),
        ([{"why": "x", "description": "y"}], "mục 1: thiếu trường title"),
        ([ITEMS[0], {**ITEMS[2], "workflow": "research"}], "mục 2: trường workflow"),
        ([{**ITEMS[0], "project": "Không có"}], "mục 1: trường project"),
        ([{**ITEMS[0], "depends_on": [2]}, ITEMS[2]], "mục 1: trường depends_on"),
        ([{**ITEMS[0], "depends_on": ["AO-999"]}], "mục 1: trường depends_on: không có ticket AO-999"),
    ],
)
async def test_ac2_invalid_proposals_rejected(office: Office, items: list[dict[str, Any]], message: str) -> None:
    ctx = await ctx_for(office, conversation=(await office.chat.dm((await office.ensure_ai()).id)).id)
    with pytest.raises(ToolError) as err:
        await propose_tickets({"items": items}, ctx)
    assert message in str(err.value)
    assert await cards(office) == []


# ---- AC-3..AC-7: approving -----------------------------------------------------------------------------


async def test_ac3_approve_defaults_creates_in_order(office: Office) -> None:
    card, _dm = await propose_from_chat(office)
    ai = await office.ensure_ai()
    await office.answer(card.id, {"approved": True}, by="user")
    t1, t2, t3 = await created(office, card)
    assert t1.id < t2.id < t3.id
    assert [t.title for t in (t1, t2, t3)] == [i["title"] for i in ITEMS]
    assert t1.stage == "understand" and t1.stage_template == "code" and t1.assignee_actor_id == ai.id
    assert (t2.status, t2.stage, t2.stage_template) == (S.BACKLOG, None, "quick")
    assert (t3.status, t3.stage, t3.stage_template) == (S.BACKLOG, None, "code")
    assert await office.tickets.open_blockers(t2.id) == [t1.key]
    resolved = await office.interactions.get(card.id)
    assert resolved.status == InteractionStatus.ANSWERED and resolved.response["created"] == [t1.key, t2.key, t3.key]
    assert "Đề xuất từ chat" in t1.description and "**Vì sao:** 17 ticket" in t1.description


async def test_ac4_run_now_dependent_blocked_then_unblocked(office: Office) -> None:
    card, _dm = await propose_from_chat(office)
    ai = await office.ensure_ai()
    await office.answer(card.id, {"approved": True, "items": [{"n": 2, "run_now": True}]}, by="user")
    t1, t2, _t3 = await created(office, card)
    assert t2.status == S.BLOCKED and t2.stage is not None and t2.assignee_actor_id == ai.id
    await office.db.execute("DELETE FROM wake_requests WHERE ticket_id = ?", (t2.id,))
    run_id = new_id("run")
    await office.tickets.checkout(t1.id, ai.id, run_id)
    await office.tickets.transition(t1.id, S.DONE, by="Copilot", run_id=run_id)
    assert (await office.tickets.get(t2.id)).status == S.TODO
    assert await office.wakes.pending_for(ai.id, t2.id) is not None


async def test_ac5_drop_and_edit_items(office: Office) -> None:
    card, _dm = await propose_from_chat(office)
    edits = [
        {"n": 1, "selected": True, "title": "Hàng đợi (sửa)", "description": "Mô tả mới", "workflow": "quick"},
        {"n": 3, "selected": False},
    ]
    await office.answer(card.id, {"approved": True, "items": edits}, by="user")
    tickets = await created(office, card)
    assert len(tickets) == 2
    t1 = tickets[0]
    assert t1.title == "Hàng đợi (sửa)" and t1.description.startswith("Mô tả mới") and t1.stage_template == "quick"
    assert (await office.interactions.get(card.id)).response["dropped"] == [3]


async def test_ac6_dropped_dependency_removed_and_noted(office: Office) -> None:
    card, _dm = await propose_from_chat(office)
    await office.answer(card.id, {"approved": True, "items": [{"n": 1, "selected": False}]}, by="user")
    t2, _t3 = await created(office, card)
    assert await office.tickets.open_blockers(t2.id) == []
    assert 'Bỏ phụ thuộc vào "Hàng đợi theo dự án" (không được chọn)' in t2.description


async def test_ac7_drop_all_creates_nothing(office: Office) -> None:
    card, dm = await propose_from_chat(office)
    before = await count_tickets(office)
    resolved = await office.answer(card.id, {"approved": False}, by="user")
    assert resolved.response == {"approved": False} and resolved.status == InteractionStatus.ANSWERED
    assert await count_tickets(office) == before
    assert any("Đã bỏ đề xuất 3 ticket" in m.body for m in await office.chat.messages(dm))


async def test_ac7_drop_all_from_ticket_comments_on_source(office: Office) -> None:
    card, source = await propose_from_ticket(office)
    await office.answer(card.id, {"approved": False}, by="user")
    assert any("Đã bỏ đề xuất 3 ticket" in c.body for c in await office.tickets.comments(source.id))


async def test_ac3_failed_start_leaves_ticket_in_backlog(office: Office, monkeypatch) -> None:
    from actoroffice.services.stages import StageError

    async def broken(*_a: Any, **_k: Any) -> Any:
        raise StageError("không có mẫu")

    card, _dm = await propose_from_chat(office)
    monkeypatch.setattr(office.stages, "start", broken)
    resolved = await office.answer(card.id, {"approved": True}, by="user")
    t1 = await office.tickets.get(resolved.response["created"][0])
    assert (t1.status, t1.stage) == (S.BACKLOG, None)  # still reachable: "Bắt đầu" in the Backlog list
    assert t1.key in [b["ticket"] for b in (await office.proposals.backlog())[1]]
    assert any(t1.key in e for e in resolved.response["errors"])
    with pytest.raises(StageError):
        await office.proposals.start_backlog(t1.key, by="user")
    assert (await office.tickets.get(t1.id)).status == S.BACKLOG


async def test_ac13_start_backlog_twice_starts_once(office: Office) -> None:
    import asyncio

    card, _dm = await propose_from_chat(office)
    await office.answer(card.id, {"approved": True}, by="user")
    key = (await office.interactions.get(card.id)).response["created"][2]
    results = await asyncio.gather(*(office.proposals.start_backlog(key, by="user") for _ in range(2)), return_exceptions=True)
    assert sorted(type(r).__name__ for r in results) == ["Conflict", "Ticket"]


async def test_ac9_failed_item_still_records_created_and_audit(office: Office, monkeypatch) -> None:
    card, dm = await propose_from_chat(office)
    real = office.tickets.create
    calls = {"n": 0}

    async def flaky(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("ổ đĩa đầy")
        return await real(*a, **k)

    monkeypatch.setattr(office.tickets, "create", flaky)
    resolved = await office.answer(card.id, {"approved": True}, by="user")
    assert len(resolved.response["created"]) == 2
    assert any("mục 2: không tạo được" in e for e in resolved.response["errors"])
    assert (await audit(office))[-1]["created"] == resolved.response["created"]
    assert any("Đã tạo 2 ticket" in m.body for m in await office.chat.messages(dm))


# ---- AC-8, AC-9: who approves, audit ---------------------------------------------------------------------


async def test_ac8_dev_cannot_approve(office: Office) -> None:
    team = await office.teams.create_team("Billing")
    dev = await office.teams.create_user("Minh", telegram_id=222)
    lead = await office.teams.create_user("Lan", telegram_id=111)
    await office.teams.add_member(team.id, dev.id, ["dev"])
    await office.teams.add_member(team.id, lead.id, ["lead"])
    card, _source = await propose_from_ticket(office, team_id=team.id)
    before = await count_tickets(office)
    with pytest.raises(PolicyDenied) as err:
        await office.answer(card.id, {"approved": True}, by=dev.id)
    assert err.value.code == 403
    with pytest.raises(PolicyDenied):
        await office.answer(card.id, {"approved": False}, by=dev.id)
    assert (await office.interactions.get(card.id)).status == InteractionStatus.PENDING
    assert await count_tickets(office) == before
    await office.answer(card.id, {"approved": True}, by=lead.id)
    assert await count_tickets(office) == before + 3


async def test_ac8_chat_card_needs_owner_or_lead_in_some_team(office: Office) -> None:
    team = await office.teams.create_team("Billing")
    dev = await office.teams.create_user("Minh")
    lead = await office.teams.create_user("Lan")
    await office.teams.add_member(team.id, dev.id, ["dev"])
    await office.teams.add_member(team.id, lead.id, ["lead"])
    card, _dm = await propose_from_chat(office)
    with pytest.raises(PolicyDenied):
        await office.answer(card.id, {"approved": True}, by=dev.id)
    assert (await office.interactions.get(card.id)).status == InteractionStatus.PENDING
    await office.answer(card.id, {"approved": False}, by=lead.id)  # A-1: a lead of any team decides a chat card


async def audit(office: Office) -> list[dict[str, Any]]:
    import json

    rows = await office.db.fetchall("SELECT outcome, detail_json, target FROM audit_log WHERE action = 'tickets:propose' ORDER BY id")
    return [{"outcome": r["outcome"], "target": r["target"], **json.loads(r["detail_json"])} for r in rows]


async def test_ac9_audit_propose_approve_drop(office: Office) -> None:
    card, source = await propose_from_ticket(office)
    await office.answer(card.id, {"approved": True, "drop": [3]}, by="user")
    keys = (await office.interactions.get(card.id)).response["created"]
    proposed, approved = await audit(office)
    assert proposed == {"outcome": "proposed", "target": card.id, "by": "Copilot", "source": source.key, "count": 3}
    assert approved == {"outcome": "approved", "target": card.id, "by": "user", "created": keys, "dropped": [3]}
    card2, _dm = await propose_from_chat(office)
    await office.answer(card2.id, {"approved": False}, by="user")
    last = (await audit(office))[-1]
    assert last == {"outcome": "dropped", "target": card2.id, "by": "user"}


# ---- AC-12: Telegram -----------------------------------------------------------------------------------


def test_ac12_tg_card_fits_and_buttons() -> None:
    items = [
        {"n": n, "title": f"Việc số {n} " + "x" * 200, "why": "lý do " * 60, "workflow": "code", "run_now": n == 1, "depends_on": [n - 1] if n > 1 else []}
        for n in range(1, 21)
    ]
    item = {"id": "ix_1", "kind": "propose_tickets", "payload": {"items": items, "proposed_by": "Copilot", "source_ticket": "SI-9"}}
    text, rows = fmt.card(item)
    assert len(text) < fmt.LIMIT
    assert "1. <b>Việc số 1" in text and "chạy ngay" in text and "backlog" in text and "sau 1" in text
    assert "mục khác (xem trên web)" in text
    assert [b["text"] for b in rows[0]] == ["✅ Tạo 20 ticket", "🗑 Bỏ hết"]
    assert fmt.response_for(item, "y") == {"approved": True} and fmt.response_for(item, "n") == {"approved": False}
    assert fmt.verdict({**item, "status": "answered", "response": {"approved": True, "created": ["SI-1"]}}) == "✅ Đã tạo SI-1"
    assert fmt.verdict({**item, "status": "answered", "response": {"approved": False}}) == "🗑 Đã bỏ hết"


def test_ac12_parse_drop() -> None:
    assert parse_drop("bỏ 2, 5") == [2, 5]
    assert parse_drop("Bỏ 5 2 2") == [2, 5]
    assert parse_drop("ok tạo hết") is None
    assert parse_drop("bỏ") is None


async def test_ac12_tg_reply_drop_numbers(office: Office, bot) -> None:
    await bind(office, bot)
    items = ITEMS + [{"title": f"Việc {n}", "why": "w", "description": "d"} for n in (4, 5)]
    card, dm = await propose_from_chat(office, items)
    await until(lambda: any("đề xuất 5 ticket" in p["text"] for p in bot.sent()))
    row = await office.db.fetchone("SELECT message_id FROM telegram_cards WHERE interaction_id = ?", (card.id,))
    thread = await office.telegram.topic_for(dm)
    await office.telegram.handle_update(tg_message("bỏ 2, 5", thread, reply_to=row["message_id"]))
    resolved = await office.interactions.get(card.id)
    assert resolved.response["dropped"] == [2, 5]
    assert [(await office.tickets.get(k)).title for k in resolved.response["created"]] == ["Hàng đợi theo dự án", "Tài liệu hướng dẫn", "Việc 4"]
    await until(lambda: any("Đã tạo" in p["text"] for p in bot.sent("editMessageText")))


# ---- AC-14: prompt and tool sets ---------------------------------------------------------------------------


def _produce_prompt(template_name: str) -> str:
    from actoroffice.domain.org import Actor
    from actoroffice.domain.run import WakeRequest, WakeSource
    from actoroffice.domain.work import Ticket

    template = default_templates()[template_name]
    stage = template.stage("produce")
    assert stage is not None
    return prompt_pack.build(
        actor=Actor(id="copilot", position_id="ai-engineer", name="Copilot"),
        ticket=Ticket(id=1, key="SI-24", title="t"),
        template=template,
        stage=stage,
        stage_run=StageRun(id=1, ticket_id=1, stage="produce", attempt=1, started_at=""),
        language="Vietnamese",
        team_name=None,
        people=[],
        project_line="",
        dossier="",
        guidelines=[],
        memories=[],
        skills=[],
        wake=WakeRequest(id=1, actor_id="copilot", source=WakeSource.ASSIGNMENT),
        resumed=False,
        answered=[],
        new_comments=[],
        changes=[],
        office_runs_tests=False,
    ).system


def test_ac14_produce_prompt_and_toolsets() -> None:
    assert "calling propose_tickets" in _produce_prompt("research")
    assert "propose_tickets" not in _produce_prompt("doc")  # the doc workflow's produce stage is unchanged
    assert "propose_tickets" in {t.name for t in CHAT_TOOLS}
    assert "propose_tickets" in {t.name for t in TICKET_TOOLS}
