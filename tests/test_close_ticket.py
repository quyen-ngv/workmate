"""SI-29: closing a ticket (done / cancelled) drops its held wakes and withdraws its gate cards and questions;
cards left behind by tickets closed earlier are withdrawn when the office starts."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import InteractionKind, InteractionStatus, TicketStatus

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db
from .test_stages import REQUIREMENTS


def ok(ctx: TurnCtx) -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


async def waiting_for_card(office: Office, runtime: FakeRuntime) -> int:
    """A ticket whose understand stage finished and waits for its gate card, with a comment wake held back."""
    team = await office.teams.create_team("Billing", jira_project_key="bil")
    lan = await office.teams.create_user("Lan", telegram_id=111)
    await office.teams.add_member(team.id, lan.id, ["ba"])
    ticket = await office.tickets.create("Export CSV", description="x")

    async def understand(ctx: TurnCtx) -> TurnResult:
        await ctx.office("doc_put", {"key": "requirements", "body": REQUIREMENTS, "base_revision": None})
        await ctx.office("dossier_update", {"section": "context", "body": "Khách cần CSV", "sources": ["msg:1"]})
        await ctx.office("stage_complete", {"summary": "2 AC"})
        return ok(ctx)

    runtime.scripts.append(understand)
    await office.stages.start(ticket.key, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    await office.tickets.add_comment(ticket.id, "Thêm cột ngày nhé", author_kind="user", author_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    await office.db.execute("UPDATE tickets SET status = 'in_review' WHERE id = ?", (ticket.id,))
    return ticket.id


async def wake_statuses(office: Office, ticket_id: int) -> list[str]:
    rows = await office.db.fetchall("SELECT status FROM wake_requests WHERE ticket_id = ? ORDER BY id", (ticket_id,))
    return [r["status"] for r in rows]


@pytest.mark.parametrize("to", [TicketStatus.DONE, TicketStatus.CANCELLED])
async def test_ac3_closing_drops_the_held_comment_wake(office: Office, runtime: FakeRuntime, to: TicketStatus) -> None:
    ticket_id = await waiting_for_card(office, runtime)
    assert "pending" in await wake_statuses(office, ticket_id)  # held while the gate card waits

    await office.tickets.transition(ticket_id, to, by="user")
    statuses = await wake_statuses(office, ticket_id)
    assert statuses and "pending" not in statuses and "dropped" in statuses


@pytest.mark.parametrize("to", [TicketStatus.DONE, TicketStatus.CANCELLED])
async def test_ac6_closing_cancels_gate_cards_and_questions(office: Office, runtime: FakeRuntime, to: TicketStatus) -> None:
    ticket_id = await waiting_for_card(office, runtime)
    [card] = [i for i in await office.interactions.pending(ticket_id=ticket_id) if i.kind == InteractionKind.APPROVE_STAGE]
    question = await office.interactions.create(InteractionKind.QUESTION, {"question": "?"}, ticket_id=ticket_id)
    tool = await office.interactions.create(InteractionKind.APPROVE_TOOL, {"tool": "Bash", "input_hash": "h"}, ticket_id=ticket_id)
    other = await office.tickets.create("Khác", description="x")
    elsewhere = await office.interactions.create(InteractionKind.QUESTION, {"question": "?"}, ticket_id=other.id)
    async with office.bus.subscribe() as queue:
        await office.tickets.transition(ticket_id, to, by="user")
        events = [queue.get_nowait() for _ in range(queue.qsize())]
    resolved = [e.data["interaction"]["id"] for e in events if e.kind == "interaction.resolved"]
    assert (await office.interactions.get(card.id)).status == InteractionStatus.CANCELLED
    assert (await office.interactions.get(question.id)).status == InteractionStatus.CANCELLED
    assert sorted(resolved) == sorted([card.id, question.id])
    assert (await office.interactions.get(tool.id)).status == InteractionStatus.PENDING  # other kinds untouched
    assert (await office.interactions.get(elsewhere.id)).status == InteractionStatus.PENDING


async def test_ac7_start_cancels_cards_of_tickets_closed_before(tmp_path, runtime: FakeRuntime) -> None:
    settings = Settings(data_dir=tmp_path / "home", approval_wait_s=0.05)
    o = Office(settings, runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    cards: dict[str, str] = {}
    for status in ("done", "cancelled", "in_review"):
        ticket = await o.tickets.create(f"T {status}", description="x")
        await o.db.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket.id))
        cards[f"{status}:stage"] = (await o.interactions.create(InteractionKind.APPROVE_STAGE, {"stage": "build"}, ticket_id=ticket.id)).id
        cards[f"{status}:question"] = (await o.interactions.create(InteractionKind.QUESTION, {"question": "?"}, ticket_id=ticket.id)).id
        cards[f"{status}:tool"] = (await o.interactions.create(InteractionKind.APPROVE_TOOL, {"tool": "Bash"}, ticket_id=ticket.id)).id
    await o.close()

    restarted = Office(settings, runtime=runtime)
    await restarted.start(schedule=False)
    try:
        status = {k: (await restarted.interactions.get(v)).status.value for k, v in cards.items()}
    finally:
        await restarted.close()
    assert status == {
        "done:stage": "cancelled",
        "done:question": "cancelled",
        "done:tool": "pending",
        "cancelled:stage": "cancelled",
        "cancelled:question": "cancelled",
        "cancelled:tool": "pending",
        "in_review:stage": "pending",
        "in_review:question": "pending",
        "in_review:tool": "pending",
    }
