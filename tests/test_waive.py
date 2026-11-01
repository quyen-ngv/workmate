"""Waive a failing check and force a ticket status: who may, why it is audited, how long it lasts."""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.cli_team import cmd_stage
from actoroffice.config import Settings
from actoroffice.domain.errors import PolicyDenied
from actoroffice.domain.stage import StageOutcome
from actoroffice.domain.work import InteractionKind, InteractionStatus, TicketStatus
from actoroffice.services.stages import StageError
from actoroffice.tools.stage_tools import STAGE_TOOLS

from .fakes import FakeRuntime
from .test_web import HEADERS, client, login  # noqa: F401  (fixture re-export)

S = TicketStatus
REASON = "code đã ở main, không có PR"


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


@pytest.fixture
async def people(office: Office):
    team = await office.teams.create_team("Billing", jira_project_key="bil")
    lan = await office.teams.create_user("Lan", telegram_id=111)
    minh = await office.teams.create_user("Minh", telegram_id=222)
    hoa = await office.teams.create_user("Hoa", telegram_id=333)
    await office.teams.add_member(team.id, lan.id, ["ba"])
    await office.teams.add_member(team.id, minh.id, ["dev"])
    await office.teams.add_member(team.id, hoa.id, ["qe"])
    return team, lan, minh, hoa


@pytest.fixture
async def pr_ticket(office: Office, people):
    """A code ticket parked at stage `pr` with no PR linked (the AO-4 situation)."""
    team = people[0]
    ticket = await office.tickets.create("Kẹt ở pr", description="")
    await office.stages.start(ticket.key, template="code", by="user", team_id=team.id)
    await office.scheduler.wait_idle(timeout=20)
    await office.db.execute("UPDATE tickets SET stage = 'pr', status = 'in_progress' WHERE id = ?", (ticket.id,))
    ticket = await office.tickets.get(ticket.id)
    await office.stages.ensure_run(ticket)
    return ticket


async def cards(office: Office, ticket_id: int, status: str = "pending"):
    rows = await office.db.fetchall("SELECT id FROM interactions WHERE ticket_id = ? AND kind = 'waive_check' AND status = ?", (ticket_id, status))
    return [await office.interactions.get(r["id"]) for r in rows]


async def complete(office: Office, ticket, **kw):
    return await office.stages.complete(ticket.id, run_id=None, summary="xong", workspace=None, **kw)


def check(result: dict, check_id: str) -> dict:
    return next(c for c in result["checks"] if c["id"] == check_id)


# ---- US-1: the AI asks, a person decides --------------------------------------------------------------------


async def test_ac1_complete_with_waive_request_opens_card(office: Office, people, pr_ticket) -> None:
    _, _, minh, _ = people
    result = await complete(office, pr_ticket, waive=[{"check": "pr_linked", "reason": REASON}])
    assert result["ok"] is False and [c["id"] for c in result["failed"]] == ["pr_linked"]
    (card,) = await cards(office, pr_ticket.id)
    assert card.kind == InteractionKind.WAIVE_CHECK
    assert card.payload["check"] == "pr_linked" and card.payload["reason"] == REASON
    assert minh.id in card.payload["approver_ids"]
    assert [c["id"] for c in card.payload["failed"]] == ["pr_linked"]
    # asking twice does not stack cards
    await complete(office, pr_ticket, waive=[{"check": "pr_linked", "reason": REASON}])
    assert len(await cards(office, pr_ticket.id)) == 1


async def test_ac2_approve_card_then_complete_passes(office: Office, people, pr_ticket) -> None:
    _, _, minh, _ = people
    await complete(office, pr_ticket, waive=[{"check": "pr_linked", "reason": REASON}])
    (card,) = await cards(office, pr_ticket.id)
    resolved = await office.answer(card.id, {"approved": True}, by=minh.id)
    assert resolved.status == InteractionStatus.ANSWERED
    result = await complete(office, pr_ticket)
    assert result["ok"] is True and result["gate"] == "merge" and result["external"]
    waived = check(result, "pr_linked")
    assert waived["status"] == "passed" and waived["summary"].startswith("⚠️ đã bỏ qua")
    assert waived["data"]["waived"]["by"] == minh.id and waived["data"]["waived"]["reason"] == REASON
    assert waived["data"]["waived"]["at"]
    ticket = await office.tickets.get(pr_ticket.id)
    assert ticket.stage == "pr" and ticket.status == S.IN_REVIEW  # the gate still waits (BR-7)


async def test_ac3_reject_card_keeps_check_failed_and_wakes_ai(office: Office, people, pr_ticket) -> None:
    _, _, minh, _ = people
    await complete(office, pr_ticket, waive=[{"check": "pr_linked", "reason": REASON}])
    (card,) = await cards(office, pr_ticket.id)
    await office.answer(card.id, {"approved": False, "note": "hãy mở PR đã"}, by=minh.id)
    result = await complete(office, pr_ticket)
    assert result["ok"] is False
    comments = [c.body for c in await office.tickets.comments(pr_ticket.id)]
    assert any("hãy mở PR đã" in b for b in comments)
    rows = await office.db.fetchall("SELECT payload_json FROM wake_requests WHERE ticket_id = ?", (pr_ticket.id,))
    assert any('"waive_rejected": "pr_linked"' in r["payload_json"] and "hãy mở PR đã" in r["payload_json"] for r in rows)


# ---- US-2: a person waives directly --------------------------------------------------------------------------


async def test_ac4_waive_direct_then_complete(office: Office, people, pr_ticket) -> None:
    _, _, minh, _ = people
    run = await office.stages.waive(pr_ticket.key, "pr_linked", REASON, by=minh.id)
    assert run.waivers[0]["check"] == "pr_linked" and run.waivers[0]["via"] == "direct" and run.waivers[0]["by_name"] == "Minh"
    result = await complete(office, pr_ticket)
    assert result["ok"] is True and check(result, "pr_linked")["data"]["waived"]["via"] == "direct"
    rows = await office.db.fetchall("SELECT * FROM audit_log WHERE action = 'stage:waive'")
    assert len(rows) == 1


async def test_ac5_waive_requires_reason(office: Office, people, pr_ticket) -> None:
    with pytest.raises(StageError, match="lý do"):
        await office.stages.waive(pr_ticket.key, "pr_linked", "   ", by="user")
    assert (await office.stages.current_run(pr_ticket)).waivers == []
    assert StageError.code == 422


async def test_ac6_waiver_scoped_to_run(office: Office, people, pr_ticket) -> None:
    await office.stages.waive(pr_ticket.key, "pr_linked", REASON, by="user")
    assert (await complete(office, pr_ticket))["ok"] is True
    await office.stages.retry(pr_ticket.key, by="user")  # a new attempt of the same stage
    ticket = await office.tickets.get(pr_ticket.id)
    assert (await office.stages.current_run(ticket)).waivers == []
    assert (await complete(office, ticket))["ok"] is False


# ---- US-3: permission ----------------------------------------------------------------------------------------


async def test_ac7_may_bypass_matrix(office: Office, people, pr_ticket) -> None:
    _, lan, minh, hoa = people
    ticket = await office.tickets.get(pr_ticket.id)
    assert await office.stages.may_bypass("user", ticket)
    assert await office.stages.may_bypass(minh.id, ticket)
    assert not await office.stages.may_bypass(lan.id, ticket)
    assert not await office.stages.may_bypass(hoa.id, ticket)
    lead = await office.teams.create_user("Sơn")
    await office.teams.add_member(ticket.team_id, lead.id, ["lead"])
    assert await office.stages.may_bypass(lead.id, ticket)
    await office.db.execute("UPDATE tickets SET owner_user_id = ? WHERE id = ?", (hoa.id, ticket.id))
    assert await office.stages.may_bypass(hoa.id, await office.tickets.get(ticket.id))  # the ticket's owner


async def test_ac7_ba_cannot_waive_or_approve_card(office: Office, people, pr_ticket) -> None:
    _, lan, _, hoa = people
    with pytest.raises(PolicyDenied):
        await office.stages.waive(pr_ticket.key, "pr_linked", REASON, by=lan.id)
    await complete(office, pr_ticket, waive=[{"check": "pr_linked", "reason": REASON}])
    (card,) = await cards(office, pr_ticket.id)
    with pytest.raises(PolicyDenied):
        await office.answer(card.id, {"approved": True}, by=hoa.id)
    assert (await office.stages.current_run(pr_ticket)).waivers == []


def test_ac8_ai_has_no_waive_tool() -> None:
    names = {t.name for t in STAGE_TOOLS}
    assert "stage_complete" in names and not any("waive" in n for n in names)


async def test_ac8_waive_request_alone_keeps_check_failed(office: Office, people, pr_ticket) -> None:
    result = await complete(office, pr_ticket, waive=[{"check": "pr_linked", "reason": REASON}])
    assert result["ok"] is False
    assert (await office.stages.current_run(pr_ticket)).waivers == []


async def test_ac9_no_secrets_cannot_be_waived(office: Office, people, pr_ticket) -> None:
    with pytest.raises(StageError, match="no_secrets"):
        await office.stages.waive(pr_ticket.key, "no_secrets", REASON, by="user")
    result = await complete(office, pr_ticket, waive=[{"check": "no_secrets", "reason": REASON}])
    assert result["ok"] is False and "no_secrets" in result["waive_refused"][0]
    assert await cards(office, pr_ticket.id) == []


# ---- US-4: force status --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("start", "to"),
    [(S.BLOCKED, S.DONE), (S.BACKLOG, S.IN_REVIEW), (S.DONE, S.BACKLOG), (S.CANCELLED, S.DONE), (S.BACKLOG, S.DONE)],
)
async def test_ac10_force_status_any_to_any(office: Office, people, start, to) -> None:
    team, _, minh, _ = people
    ticket = await office.tickets.create("Tự định nghĩa done", description="")
    await office.db.execute("UPDATE tickets SET status = ?, team_id = ? WHERE id = ?", (start.value, team.id, ticket.id))
    updated = await office.stages.force_status(ticket.key, to, "chốt thủ công", by=minh.id)
    assert updated.status == to
    assert any("chốt thủ công" in c.body for c in await office.tickets.comments(ticket.id))
    assert await office.db.fetchall("SELECT * FROM audit_log WHERE action = 'stage:force_status'")


async def test_ac11_force_done_mid_stage_skips_the_rest(office: Office, people, pr_ticket) -> None:
    await complete(office, pr_ticket, waive=[{"check": "pr_linked", "reason": REASON}])
    assert await cards(office, pr_ticket.id)
    done = await office.stages.force_status(pr_ticket.key, S.DONE, "chấp nhận code trên main", by="user")
    assert done.status == S.DONE
    runs = {r.stage: r for r in await office.stages.runs(pr_ticket.id) if r.stage in ("pr", "close")}
    assert runs["pr"].outcome == StageOutcome.SKIPPED.value and runs["pr"].finished_at
    assert runs["close"].outcome == StageOutcome.SKIPPED.value
    assert await cards(office, pr_ticket.id) == []  # the waiting card was cancelled
    assert (await office.stages.status(done))["status"] == "done"


async def test_ac12_force_status_denied_or_empty_reason(office: Office, people, pr_ticket) -> None:
    _, lan, _, _ = people
    with pytest.raises(PolicyDenied):
        await office.stages.force_status(pr_ticket.key, S.DONE, "muốn xong", by=lan.id)
    with pytest.raises(StageError, match="lý do"):
        await office.stages.force_status(pr_ticket.key, S.DONE, " ", by="user")
    assert (await office.tickets.get(pr_ticket.id)).status == S.IN_PROGRESS


# ---- US-5: visible -------------------------------------------------------------------------------------------


async def test_ac13_status_and_comments_show_waivers(office: Office, people, pr_ticket) -> None:
    await office.stages.waive(pr_ticket.key, "pr_linked", REASON, by="user")
    status = await office.stages.status(pr_ticket.key)
    assert status["waivers"][0]["check"] == "pr_linked" and status["waivers"][0]["reason"] == REASON
    assert any("pr_linked" in c.body and REASON in c.body for c in await office.tickets.comments(pr_ticket.id))


async def test_ac13_cli_stage_waive_and_force(office: Office, people, pr_ticket, capsys) -> None:
    ns = argparse.Namespace(key=pr_ticket.key, action="waive", text=["pr_linked", *REASON.split()])
    await cmd_stage(office, ns)
    assert "pr_linked" in capsys.readouterr().out
    assert (await complete(office, pr_ticket))["ok"] is True
    await cmd_stage(office, argparse.Namespace(key=pr_ticket.key, action="force", text=["done", "chốt", "tay"]))
    assert (await office.tickets.get(pr_ticket.id)).status == S.DONE


def test_ac13_web_waive_force_and_stage_payload(client) -> None:  # noqa: F811
    login(client)
    task = client.post("/api/tasks", json={"title": "Kẹt", "template": "code"}, headers=HEADERS).json()
    base = f"/api/tickets/{task['key']}"
    r = client.post(f"{base}/control", json={"cmd": "waive", "check": "outputs", "text": ""}, headers=HEADERS)
    assert r.status_code == 422
    r = client.post(f"{base}/control", json={"cmd": "waive", "check": "no_secrets", "text": "x"}, headers=HEADERS)
    assert r.status_code == 422
    r = client.post(f"{base}/control", json={"cmd": "waive", "check": "outputs", "text": REASON}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"]["waivers"][0]["check"] == "outputs"
    stage = client.get(f"{base}/stage").json()
    assert stage["stage_runs"][0]["waivers"][0]["reason"] == REASON
    assert client.post(f"{base}/force-status", json={"status": "done", "reason": ""}, headers=HEADERS).status_code == 422
    r = client.post(f"{base}/force-status", json={"status": "done", "reason": "chốt tay"}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "done"
