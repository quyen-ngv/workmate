"""End to end: "always allow" is remembered per project; Kiro's names for our tools pass the gate; a staged ticket
that stops for a person waits instead of starting itself again (the AO-4 loop); pending approvals that a wider
permission now allows are approved by themselves (SI-2)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.ids import new_id
from actoroffice.domain.run import Run, RunKind, RunStatus, TurnResult, TurnStatus
from actoroffice.domain.work import (
    Interaction,
    InteractionKind,
    ProjectAccess,
    TicketStatus,
)
from actoroffice.engine.gate import OfficeGate
from actoroffice.runtimes.base import GateDecision
from actoroffice.security.policy import WAIT_LOOP_REASON
from actoroffice.services.approvals_recheck import AUTO_BY
from actoroffice.services.interactions import input_hash

from .fakes import FakeRuntime, TurnCtx, deferred
from .seed import seed_db

TOKEN = "t" * 40
HEADERS = {"X-Requested-With": "actoroffice"}


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=5), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    yield o
    await o.close()


async def project(office: Office, tmp_path, name: str):
    folder = tmp_path / name
    folder.mkdir()
    return await office.projects.create(name, path=str(folder), access=ProjectAccess.READ_ONLY)


async def approve_first(office: Office, scope: str = "prefix") -> str:
    for _ in range(500):
        if items := await office.interactions.pending():
            break
        await asyncio.sleep(0.01)
    rule = next(s["rule"] for s in items[0].payload["suggestions"] if s["scope"] == scope)
    await office.answer(items[0].id, {"approved": True, "remember": rule})
    return rule


async def test_always_allow_is_remembered_for_the_project_only(office, runtime, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    search = await project(office, tmp_path, "search")
    seen: list[GateDecision] = []

    async def first(ctx: TurnCtx) -> None:
        seen.append(await ctx.tool("Bash", {"command": "uvx ruff check src"}))
        seen.append(await ctx.tool("Bash", {"command": "uvx ruff check tests && git status"}))  # rule covers its part
        seen.append(await ctx.tool("@office/ticket_get", {}))  # Kiro's name for our tool: never asks
        await ctx.office("ticket_set_status", {"status": "done", "comment": "ok"})

    runtime.scripts.append(first)
    await office.tickets.create("lint", assignee_actor_id=ai.id, project_id=billing.id)
    task = asyncio.create_task(approve_first(office))
    await office.scheduler.wait_idle(timeout=15)
    assert await task == "Bash(uvx ruff*)"
    assert seen == [GateDecision.ALLOW, GateDecision.ALLOW, GateDecision.ALLOW]
    assert (await office.projects.get(billing.id)).allow_rules == ["Bash(uvx ruff*)"]
    assert (await office.org.get_actor(ai.id)).allow_rules == []
    card = (await office.interactions.for_run((await office.runs.for_ticket((await office.tickets.list())[0].id))[0].id))[0]
    assert card.payload["project"] == "billing"

    # the same command in another project asks again
    office.settings.approval_wait_s = 0.05
    asked: list[GateDecision] = []

    async def second(ctx: TurnCtx) -> TurnResult:
        asked.append(await ctx.tool("Bash", {"command": "uvx ruff check src"}))
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s")

    runtime.scripts.append(second)
    await office.tickets.create("lint 2", assignee_actor_id=ai.id, project_id=search.id)
    await office.scheduler.wait_idle(timeout=15)
    assert asked == [GateDecision.DEFER]


async def test_permission_mode_per_project_from_the_api(office, tmp_path):
    from actoroffice.domain.errors import OfficeError

    billing = await project(office, tmp_path, "billing")
    assert billing.permission_mode == "workspace"
    updated = await office.projects.set_permissions(billing.id, mode="read_only", allow_rules=["Bash(mvn*)", "Bash(mvn*)", " "])
    assert updated.permission_mode == "read_only" and updated.allow_rules == ["Bash(mvn*)"]
    with pytest.raises(OfficeError):
        await office.projects.set_permissions(billing.id, mode="yolo")


async def test_a_staged_ticket_that_stops_for_a_person_waits(office, runtime):
    ticket = await office.tickets.create("Mở PR", description="x")
    said: list[str] = []

    async def stop(ctx: TurnCtx) -> TurnResult:
        said.append(await ctx.office("ticket_set_status", {"status": "in_review", "comment": "Chờ dev mở PR rồi gửi URL"}))
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts.append(stop)
    await office.stages.start(ticket.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    current = await office.tickets.get(ticket.id)
    assert "waits for a person" in said[0] and current.status == TicketStatus.IN_REVIEW
    ai = await office.ensure_ai()
    assert not await office.wakes.pending_for(ai.id, ticket.id)  # nothing starts it again by itself
    status = await office.stages.status(current, viewer="user")
    assert status["status"] == "needs_you" and status["needs_viewer"]
    assert len(await office.runs.for_ticket(ticket.id)) == 1


async def test_one_click_switches_the_project_to_the_folder_mode(office, runtime, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    seen: list[GateDecision] = []

    async def work(ctx: TurnCtx) -> None:
        seen.append(await ctx.tool("Bash", {"command": "node build.js"}))
        seen.append(await ctx.tool("Bash", {"command": "uvx ruff check src && rm -r dist"}))  # no second click
        await ctx.office("ticket_set_status", {"status": "done", "comment": "ok"})

    runtime.scripts.append(work)
    await office.tickets.create("build", assignee_actor_id=ai.id, project_id=billing.id)
    task = asyncio.create_task(approve_first(office, scope="mode"))
    await office.scheduler.wait_idle(timeout=15)
    assert await task == "mode:folder"
    assert seen == [GateDecision.ALLOW, GateDecision.ALLOW]
    assert (await office.projects.get(billing.id)).permission_mode == "folder"


# --- SI-2: a wider permission approves the pending cards it now allows ---------------------------------------------


@pytest.fixture
async def api(office) -> AsyncIterator[httpx.AsyncClient]:
    from actoroffice.web.server import create_app

    transport = httpx.ASGITransport(app=create_app(office, TOKEN))
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:8765") as client:
        assert (await client.get(f"/login?t={TOKEN}", follow_redirects=False)).status_code == 303
        yield client


async def ticket_in(office: Office, project_id: str | None, actor_id: str, title: str = "build"):
    return await office.tickets.create(title, assignee_actor_id=actor_id, project_id=project_id)


async def ask_card(office: Office, ticket: Any, workspace: Path, args: dict[str, Any], tool: str = "Bash") -> Interaction:
    """A card made by the gate exactly as a ticket run makes it, after the run parked."""
    actor = await office.org.get_actor(ticket.assignee_actor_id)
    run_id = new_id("run")
    await office.runs.start(Run(id=run_id, actor_id=actor.id, kind=RunKind.TICKET, ticket_id=ticket.id))
    gate = OfficeGate(office, actor, await office.org.get_position(actor.position_id), run_id, workspace, ticket_id=ticket.id, ticket_key=ticket.key)
    wait, office.settings.approval_wait_s = office.settings.approval_wait_s, 0.01
    try:
        assert (await gate.before_tool(tool, args, None)).decision == GateDecision.DEFER
    finally:
        office.settings.approval_wait_s = wait
    await office.runs.finish(run_id, RunStatus.PARKED)
    return await office.interactions.get(gate.asked[-1])


async def raw_card(
    office: Office, ticket: Any, workspace: Path, args: dict[str, Any], *, tool: str = "Bash", shown: Any = None, context: bool = True
) -> Interaction:
    """A card written straight into the store: a call that asked once and that the new permission denies, or a card
    whose parameters were hidden (redacted, summarized) or made before cards kept their context."""
    payload: dict[str, Any] = {
        "tool": tool, "input": args if shown is None else shown, "input_hash": input_hash(tool, args), "target": tool, "suggestions": [],
    }
    if context:
        payload["context"] = {"workspace": str(workspace), "read_dirs": [], "tainted": False}
    return await office.interactions.create(InteractionKind.APPROVE_TOOL, payload, actor_id=ticket.assignee_actor_id, ticket_id=ticket.id)


async def status_of(office: Office, *cards: Interaction) -> list[str]:
    return [(await office.interactions.get(c.id)).status.value for c in cards]


async def grants(office: Office, card: Interaction) -> list[dict[str, Any]]:
    return await office.db.fetchall("SELECT * FROM approval_grants WHERE interaction_id = ?", (card.id,))


async def test_ac1_api_folder_mode_auto_approves_pending_card(office, api, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    card = await ask_card(office, ticket, Path(billing.path), {"command": "node build.js"})
    assert card.payload["context"] == {"workspace": billing.path, "read_dirs": [], "tainted": False}

    r = await api.post(f"/api/projects/{billing.id}/permissions", json={"mode": "folder"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    done = await office.interactions.get(card.id)
    assert done.status.value == "answered" and done.response["approved"] is True and done.resolved_by == AUTO_BY
    assert [g["decision"] for g in await grants(office, card)] == ["allow"]
    rows = await office.db.fetchall("SELECT * FROM audit_log WHERE action = 'tool:Bash' AND outcome = 'auto:allow'")
    assert len(rows) == 1 and json.loads(rows[0]["detail_json"])["by"] == AUTO_BY and rows[0]["ticket_id"] == ticket.id


async def test_ac2_live_run_continues_after_mode_change(office, runtime, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    seen: list[GateDecision] = []

    async def work(ctx: TurnCtx) -> None:
        seen.append(await ctx.tool("Bash", {"command": "node build.js"}))  # waits for a person (approval_wait_s = 5)
        await ctx.office("ticket_set_status", {"status": "done", "comment": "ok"})

    async def widen() -> None:
        for _ in range(500):
            if await office.interactions.pending():
                break
            await asyncio.sleep(0.01)
        await office.projects.set_permissions(billing.id, mode="folder")
        assert await office.permissions_recheck(project_id=billing.id) == 1

    runtime.scripts.append(work)
    ticket = await office.tickets.create("build", assignee_actor_id=ai.id, project_id=billing.id)
    task = asyncio.create_task(widen())
    await office.scheduler.wait_idle(timeout=15)
    await task
    assert seen == [GateDecision.ALLOW]
    [run] = await office.runs.for_ticket(ticket.id)
    cards = await office.interactions.for_run(run.id)
    assert len(cards) == 1 and cards[0].resolved_by == AUTO_BY
    outcomes = [r["outcome"] for r in await office.db.fetchall("SELECT outcome FROM audit_log WHERE action = 'tool:Bash' ORDER BY id")]
    assert outcomes == ["auto:allow", "human:allow"]


async def test_ac3_parked_run_is_woken_and_allowed(office, runtime, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    cmd = {"command": "node build.js"}
    seen: list[GateDecision] = []

    async def first(ctx: TurnCtx) -> TurnResult:
        seen.append(await ctx.tool("Bash", cmd))
        return deferred(ctx.spec, "Bash", cmd)

    async def second(ctx: TurnCtx) -> None:
        seen.append(await ctx.tool("Bash", cmd))  # the CLI re-issues the deferred call
        await ctx.office("ticket_set_status", {"status": "done", "comment": "built"})

    runtime.scripts += [first, second]
    office.settings.approval_wait_s = 0.05
    ticket = await office.tickets.create("build", assignee_actor_id=ai.id, project_id=billing.id)
    await office.scheduler.wait_idle(timeout=15)
    [card] = await office.interactions.pending()
    assert card.payload["parked"] is True

    await office.projects.set_permissions(billing.id, mode="folder")
    assert await office.permissions_recheck(project_id=billing.id) == 1
    assert await office.wakes.pending_for(ai.id, ticket.id)
    await office.scheduler.wait_idle(timeout=15)
    assert seen == [GateDecision.DEFER, GateDecision.ALLOW]
    assert len(await office.db.fetchall("SELECT id FROM interactions WHERE ticket_id = ?", (ticket.id,))) == 1


async def test_ac4_always_allow_rule_approves_matching_pending_cards(office, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    ws = Path(billing.path)
    ruff = await ask_card(office, ticket, ws, {"command": "uvx ruff check src"})
    curl = await ask_card(office, ticket, ws, {"command": "curl https://example.com"})
    other = await ask_card(office, ticket, ws, {"command": "uvx ruff format src"})
    rule = next(s["rule"] for s in other.payload["suggestions"] if s["scope"] == "prefix")
    assert rule == "Bash(uvx ruff*)"

    await office.answer(other.id, {"approved": True, "remember": rule})
    assert await status_of(office, ruff, curl, other) == ["answered", "pending", "answered"]
    assert (await office.interactions.get(ruff.id)).resolved_by == AUTO_BY
    assert (await office.interactions.get(other.id)).resolved_by == "user"


async def test_ac4_folder_mode_chosen_on_a_card_approves_the_others(office, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    ws = Path(billing.path)
    build = await ask_card(office, ticket, ws, {"command": "node build.js"})
    clicked = await ask_card(office, ticket, ws, {"command": "node test.js"})
    await office.answer(clicked.id, {"approved": True, "remember": "mode:folder"})
    assert (await office.interactions.get(build.id)).resolved_by == AUTO_BY


async def test_ac5_network_command_still_asks_in_folder_mode(office, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    curl = await ask_card(office, ticket, Path(billing.path), {"command": "curl https://example.com"})
    await office.projects.set_permissions(billing.id, mode="folder")
    assert await office.permissions_recheck(project_id=billing.id) == 0
    assert await status_of(office, curl) == ["pending"]


@pytest.mark.parametrize("kind", ["hard_deny", "protected", "team_policy"])
async def test_ac6_denied_calls_are_never_auto_approved(office, tmp_path, kind):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    ws = Path(billing.path)
    if kind == "hard_deny":
        tool, args = "Bash", {"command": "git push --force"}
    elif kind == "protected":
        tool, args = "Write", {"file_path": str(office.settings.config_path), "content": "x"}
    else:
        rule = await office.rules.propose(
            title="Không gọi example", body="x", kind="policy", scope="global", spec={"deny_patterns": [r"example\.com"]}, by="user", activate=True
        )
        assert rule.status.value == "active"
        await office.db.execute("UPDATE tickets SET stage = 'build' WHERE id = ?", (ticket.id,))  # team rules apply to staged work
        tool, args = "Bash", {"command": "curl https://example.com"}
    card = await raw_card(office, ticket, ws, args, tool=tool)
    await office.projects.set_permissions(billing.id, mode="full")
    assert await office.permissions_recheck(project_id=billing.id) == 0
    assert await status_of(office, card) == ["pending"]
    assert await grants(office, card) == []


async def test_ac7_other_projects_cards_untouched(office, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    search = await project(office, tmp_path, "search")
    mine = await ask_card(office, await ticket_in(office, billing.id, ai.id), Path(billing.path), {"command": "curl https://example.com"})
    theirs = await ask_card(office, await ticket_in(office, search.id, ai.id, "s"), Path(search.path), {"command": "curl https://example.com"})
    await office.projects.set_permissions(billing.id, mode="full")
    assert await office.permissions_recheck(project_id=billing.id) == 1
    assert await status_of(office, mine, theirs) == ["answered", "pending"]


@pytest.mark.parametrize("hidden", ["summarized", "redacted", "no_context"])
async def test_ac8_card_without_exact_input_is_kept(office, tmp_path, hidden):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    args = {"command": "curl -H 'Authorization: Bearer abc' https://example.com"}
    if hidden == "summarized":
        card = await raw_card(office, ticket, Path(billing.path), args, shown={"summary": "curl -H …"})
    elif hidden == "redacted":
        card = await raw_card(office, ticket, Path(billing.path), args, shown={"command": "curl -H 'Authorization: Bearer ***' https://example.com"})
    else:
        card = await raw_card(office, ticket, Path(billing.path), args, context=False)
    await office.projects.set_permissions(billing.id, mode="full")
    assert await office.permissions_recheck(project_id=billing.id) == 0
    assert await status_of(office, card) == ["pending"]


async def test_ac9_actor_rule_approves_only_that_actors_cards(office, tmp_path):
    ai = await office.ensure_ai()
    other = await office.org.hire((await office.org.find_position("dev")).id, "Minh")
    folder = tmp_path / "ws"
    folder.mkdir()
    mine = await ask_card(office, await ticket_in(office, None, ai.id), folder, {"command": "uvx ruff check src"})
    theirs = await ask_card(office, await ticket_in(office, None, other.id, "b"), folder, {"command": "uvx ruff check src"})
    clicked = await ask_card(office, await ticket_in(office, None, ai.id, "c"), folder, {"command": "uvx ruff format src"})
    await office.answer(clicked.id, {"approved": True, "remember": "Bash(uvx ruff*)"})
    assert (await office.org.get_actor(ai.id)).allow_rules == ["Bash(uvx ruff*)"]
    assert await status_of(office, mine, theirs) == ["answered", "pending"]


async def test_ac10_narrowing_changes_nothing(office, api, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    await office.projects.set_permissions(billing.id, mode="folder", allow_rules=["Bash(npm*)"])
    ticket = await ticket_in(office, billing.id, ai.id)
    curl = await ask_card(office, ticket, Path(billing.path), {"command": "curl https://example.com"})
    old = await raw_card(office, ticket, Path(billing.path), {"command": "node build.js"}, context=False)
    for body in ({"allow_rules": []}, {"mode": "workspace"}):
        r = await api.post(f"/api/projects/{billing.id}/permissions", json=body, headers=HEADERS)
        assert r.status_code == 200 and r.json()["auto_approved"] == 0
    assert await status_of(office, curl, old) == ["pending", "pending"]


async def test_ac11_human_answer_first_wins(office, api, tmp_path, monkeypatch):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    first = await ask_card(office, ticket, Path(billing.path), {"command": "node build.js"})
    second = await ask_card(office, ticket, Path(billing.path), {"command": "node test.js"})
    resolve = office.interactions.resolve

    async def person_clicks_first(interaction_id: str, response: dict[str, Any], *, by: str) -> Interaction:
        if interaction_id == first.id and by == AUTO_BY:
            await resolve(interaction_id, {"approved": False}, by="user")  # between the recheck's query and its resolve
        return await resolve(interaction_id, response, by=by)

    monkeypatch.setattr(office.interactions, "resolve", person_clicks_first)
    r = await api.post(f"/api/projects/{billing.id}/permissions", json={"mode": "folder"}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["auto_approved"] == 1
    kept = await office.interactions.get(first.id)
    assert kept.resolved_by == "user" and kept.response == {"approved": False}
    assert (await office.interactions.get(second.id)).resolved_by == AUTO_BY


async def test_ac11_a_failing_card_does_not_break_the_change(office, api, tmp_path, monkeypatch):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    broken = await ask_card(office, ticket, Path(billing.path), {"command": "node build.js"})
    fine = await ask_card(office, ticket, Path(billing.path), {"command": "node test.js"})
    decide = office.policy.decide

    def flaky(tool: str, args: dict[str, Any], ctx: Any, **kw: Any) -> Any:
        if args == {"command": "node build.js"}:
            raise RuntimeError("boom")
        return decide(tool, args, ctx, **kw)

    monkeypatch.setattr(office.policy, "decide", flaky)
    r = await api.post(f"/api/projects/{billing.id}/permissions", json={"mode": "folder"}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["auto_approved"] == 1
    assert (await office.projects.get(billing.id)).permission_mode == "folder"
    assert await status_of(office, broken, fine) == ["pending", "answered"]


async def test_ac11_a_failing_recheck_still_saves_the_permission(office, api, tmp_path, monkeypatch):
    billing = await project(office, tmp_path, "billing")

    async def down(**_kw: Any) -> int:
        raise RuntimeError("store is busy")

    monkeypatch.setattr(office.approvals, "run", down)
    r = await api.post(f"/api/projects/{billing.id}/permissions", json={"mode": "full"}, headers=HEADERS)
    assert r.status_code == 200 and r.json()["auto_approved"] == 0
    assert (await office.projects.get(billing.id)).permission_mode == "full"


async def test_ac12_api_reports_auto_approved_count(office, api, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    for command in ("node build.js", "node test.js"):
        await ask_card(office, ticket, Path(billing.path), {"command": command})
    r = await api.post(f"/api/projects/{billing.id}/permissions", json={"mode": "folder"}, headers=HEADERS)
    body = r.json()
    assert r.status_code == 200 and body["auto_approved"] == 2
    assert body["id"] == billing.id and body["name"] == "billing" and body["permission_mode"] == "folder" and "allow_rules" in body


# --- SI-9: a one-time 'allow' used while the permission allows the call does not outlive it ------------------------


async def gate_for(office: Office, ticket: Any, workspace: Path) -> OfficeGate:
    actor = await office.org.get_actor(ticket.assignee_actor_id)
    run_id = new_id("run")
    await office.runs.start(Run(id=run_id, actor_id=actor.id, kind=RunKind.TICKET, ticket_id=ticket.id))
    return OfficeGate(office, actor, await office.org.get_position(actor.position_id), run_id, workspace, ticket_id=ticket.id, ticket_key=ticket.key)


async def test_ac1_ac2_auto_approved_grant_is_used_and_narrowing_asks_again(office, runtime, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    cmd = {"command": "node build.js"}
    seen: list[GateDecision] = []

    async def first(ctx: TurnCtx) -> TurnResult:
        seen.append(await ctx.tool("Bash", cmd))
        return deferred(ctx.spec, "Bash", cmd)

    async def second(ctx: TurnCtx) -> None:
        seen.append(await ctx.tool("Bash", cmd))
        await ctx.office("ticket_set_status", {"status": "done", "comment": "built"})

    runtime.scripts += [first, second]
    office.settings.approval_wait_s = 0.05
    ticket = await office.tickets.create("build", assignee_actor_id=ai.id, project_id=billing.id)
    await office.scheduler.wait_idle(timeout=15)
    [card] = await office.interactions.pending()
    await office.projects.set_permissions(billing.id, mode="folder")
    assert await office.permissions_recheck(project_id=billing.id) == 1
    await office.scheduler.wait_idle(timeout=15)
    assert seen == [GateDecision.DEFER, GateDecision.ALLOW]
    assert [(g["decision"], g["used"]) for g in await grants(office, card)] == [("allow", 1)]  # AC-1

    await office.projects.set_permissions(billing.id, mode="workspace")
    gate = await gate_for(office, ticket, Path(billing.path))
    assert (await gate.before_tool("Bash", cmd, None)).decision == GateDecision.DEFER  # AC-2
    [again] = await office.interactions.pending()
    assert again.id != card.id and again.payload["input_hash"] == input_hash("Bash", cmd)


async def test_ac3_clicked_allow_once_is_used_when_a_wider_permission_allows(office, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    cmd = {"command": "node build.js"}
    card = await ask_card(office, ticket, Path(billing.path), cmd)
    await office.interactions.resolve(card.id, {"approved": True}, by="user")
    await office.projects.set_permissions(billing.id, mode="folder", allow_rules=["Bash(node*)"])  # recheck not run: card is answered

    gate = await gate_for(office, ticket, Path(billing.path))
    assert (await gate.before_tool("Bash", cmd, None)).decision == GateDecision.ALLOW
    assert [g["used"] for g in await grants(office, card)] == [1]

    await office.projects.set_permissions(billing.id, mode="workspace", allow_rules=[])
    office.settings.approval_wait_s = 0.01
    assert (await gate.before_tool("Bash", cmd, None)).decision == GateDecision.DEFER
    assert len(await office.interactions.pending()) == 1


async def test_ac3_allow_on_an_allowed_call_keeps_a_deny_grant(office, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    ticket = await ticket_in(office, billing.id, ai.id)
    cmd = {"command": "node build.js"}
    card = await ask_card(office, ticket, Path(billing.path), cmd)
    await office.interactions.resolve(card.id, {"approved": False}, by="user")
    await office.projects.set_permissions(billing.id, mode="folder")

    gate = await gate_for(office, ticket, Path(billing.path))
    assert (await gate.before_tool("Bash", cmd, None)).decision == GateDecision.ALLOW
    assert [g["used"] for g in await grants(office, card)] == [0]  # only 'allow' grants are used up on ALLOW


async def test_ac10_gate_denies_wait_loop_without_a_card(office, tmp_path):
    ai = await office.ensure_ai()
    billing = await project(office, tmp_path, "billing")
    await office.projects.set_permissions(billing.id, mode="full")
    ticket = await ticket_in(office, billing.id, ai.id)
    run_id = new_id("run")
    await office.runs.start(Run(id=run_id, actor_id=ai.id, kind=RunKind.TICKET, ticket_id=ticket.id))
    gate = OfficeGate(office, ai, await office.org.get_position(ai.position_id), run_id, Path(billing.path), ticket_id=ticket.id, ticket_key=ticket.key)

    result = await gate.before_tool("Bash", {"command": "while true; do sleep 5; done"}, None)
    assert result.decision == GateDecision.DENY and result.reason == WAIT_LOOP_REASON
    rows = await office.db.fetchall("SELECT outcome, detail_json FROM audit_log WHERE action = 'tool:Bash'")
    assert [r["outcome"] for r in rows] == ["denied"] and WAIT_LOOP_REASON[:40] in rows[0]["detail_json"]
    assert await office.interactions.pending() == [] and gate.asked == []
