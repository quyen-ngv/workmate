"""SI-17: a turn that fails before doing anything (Claude CLI start-up timeout) is re-run by the office."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import RunStatus, TurnResult, TurnStatus, Usage

from .fakes import FakeRuntime, TurnCtx

INIT_TIMEOUT = "Exception: Control request timeout: initialize"


async def startup_error(ctx: TurnCtx) -> TurnResult:
    return TurnResult(status=TurnStatus.ERROR, error=INIT_TIMEOUT)


async def ok(ctx: TurnCtx) -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", usage=Usage(input_tokens=10, output_tokens=5))


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, transient_retry_delays_s=[0, 0]), runtime=runtime)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


async def run_ticket(office: Office) -> tuple[int, list]:
    team = await office.teams.create_team("Billing", jira_project_key="bil")
    lan = await office.teams.create_user("Lan", telegram_id=111)
    await office.teams.add_member(team.id, lan.id, ["ba"])
    ticket = await office.tickets.create("Export CSV", description="x")
    await office.stages.start(ticket.key, template="quick", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    return ticket.id, await office.runs.for_ticket(ticket.id)


async def events(office: Office, run_id: str, kind: str) -> list[dict]:
    return [e for e in await office.runs.events(run_id) if e["kind"] == kind]


async def test_ac1_startup_error_once_then_runs(office: Office, runtime: FakeRuntime) -> None:
    runtime.scripts += [startup_error, ok]
    _, runs = await run_ticket(office)

    first = runs[-1]
    assert len(runtime.specs) >= 2 and {s.run_id for s in runtime.specs[:2]} == {first.id}
    assert first.status != RunStatus.FAILED
    retries = await events(office, first.id, "retry")
    assert [e["payload"]["attempt"] for e in retries] == [1]
    assert retries[0]["activity"]["title"] == "Claude khởi động lỗi, thử lại lần 1"


async def test_ac2_startup_error_every_time_fails_after_three_calls(office: Office, runtime: FakeRuntime) -> None:
    runtime.scripts += [startup_error] * 5
    ticket_id, runs = await run_ticket(office)

    assert len(runtime.specs) == 3 and len(runs) == 1
    assert runs[0].status == RunStatus.FAILED
    assert "3 lần" in (runs[0].error or "") and INIT_TIMEOUT in (runs[0].error or "")
    assert [e["payload"]["attempt"] for e in await events(office, runs[0].id, "retry")] == [1, 2]
    assert (await office.stages.status(ticket_id))["status"] == "failed"


@pytest.mark.parametrize(
    "work",
    [
        lambda ctx: ctx.sink("text", {"text": "Đọc plan trước."}),
        lambda ctx: ctx.sink("tool_use", {"tool": "Read", "input": {"file_path": "a.py"}, "id": "t1"}),
        None,  # tokens used, no event
    ],
)
async def test_ac3_error_after_progress_is_not_retried(office: Office, runtime: FakeRuntime, work) -> None:
    async def fails_midway(ctx: TurnCtx) -> TurnResult:
        if work is not None:
            await work(ctx)
            return TurnResult(status=TurnStatus.ERROR, error="Exception: CLI process exited")
        return TurnResult(status=TurnStatus.ERROR, error="Exception: CLI process exited", usage=Usage(input_tokens=1200))

    runtime.scripts += [fails_midway]
    _, runs = await run_ticket(office)

    assert len(runtime.specs) == 1 and runs[0].status == RunStatus.FAILED
    assert await events(office, runs[0].id, "retry") == []
    assert "lần liền" not in (runs[0].error or "")
