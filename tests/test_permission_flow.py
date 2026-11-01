"""End to end: "always allow" is remembered per project; Kiro's names for our tools pass the gate; a staged ticket
that stops for a person waits instead of starting itself again (the AO-4 loop)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import ProjectAccess, TicketStatus
from actoroffice.runtimes.base import GateDecision

from .fakes import FakeRuntime, TurnCtx


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=5), runtime=runtime)
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
