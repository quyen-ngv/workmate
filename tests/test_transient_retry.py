"""SI-17: a turn that fails before doing anything (Claude CLI start-up timeout) is re-run by the office."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import RunStatus, TurnResult, TurnStatus, Usage

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db

INIT_TIMEOUT = "Exception: Control request timeout: initialize"


async def startup_error(ctx: TurnCtx) -> TurnResult:
    return TurnResult(status=TurnStatus.ERROR, error=INIT_TIMEOUT)


async def ok(ctx: TurnCtx) -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", usage=Usage(input_tokens=10, output_tokens=5))


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
def delays() -> list[float]:
    return [0, 0]


@pytest.fixture
async def office(tmp_path, runtime, delays) -> AsyncIterator[Office]:
    # no disposition reminder: every runtime call counted below is a try of the same turn
    settings = Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, corrective_turns=0, transient_retry_delays_s=delays)
    o = Office(settings, runtime=runtime)
    seed_db(o.settings.db_path)
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

    [first] = runs
    assert [s.run_id for s in runtime.specs] == [first.id, first.id]
    assert first.status == RunStatus.SUCCEEDED
    retries = await events(office, first.id, "retry")
    assert [e["payload"]["attempt"] for e in retries] == [1]
    assert retries[0]["activity"]["title"] == "Claude gặp lỗi, thử lại lần 1"


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


async def test_ac3_error_after_a_tool_call_is_not_retried(office: Office, runtime: FakeRuntime) -> None:
    async def called_a_tool(ctx: TurnCtx) -> TurnResult:
        await ctx.gate.before_tool("Read", {"file_path": "a.txt"}, None)  # no event, no token: the CLI still worked
        return TurnResult(status=TurnStatus.ERROR, error="boom")

    runtime.scripts += [called_a_tool]
    _, runs = await run_ticket(office)
    assert len(runtime.specs) == 1 and await events(office, runs[0].id, "retry") == []


@pytest.mark.parametrize("delays", [[30, 30]])
async def test_ac2_stop_while_waiting_to_retry_ends_the_wait(office: Office, runtime: FakeRuntime, delays) -> None:
    runtime.scripts += [startup_error] * 3
    team = await office.teams.create_team("Billing", jira_project_key="bil")
    ticket = await office.tickets.create("Export CSV", description="x")
    await office.stages.start(ticket.key, template="quick", by="user", team_id=team.id)
    idle = asyncio.create_task(office.scheduler.wait_idle(timeout=20))
    while not runtime.specs or not await events(office, runtime.specs[0].run_id, "retry"):
        await asyncio.sleep(0.05)
    started = time.monotonic()
    await office.scheduler.stop_run(runtime.specs[0].run_id)
    await idle

    assert time.monotonic() - started < 5  # not the 30 s wait
    [run] = await office.runs.for_ticket(ticket.id)
    assert len(runtime.specs) == 1 and run.status == RunStatus.CANCELLED


async def test_si29_ac4_kiro_failure_names_kiro(office: Office, runtime: FakeRuntime) -> None:
    runtime.scripts += [startup_error] * 3
    team = await office.teams.create_team("Billing", jira_project_key="bil")
    team.settings.model = "kiro"
    await office.teams.update_team(team)
    ticket = await office.tickets.create("Export CSV", description="x")
    await office.stages.start(ticket.key, template="quick", by="user", team_id=team.id)
    await office.scheduler.wait_idle(timeout=20)

    [run] = await office.runs.for_ticket(ticket.id)
    assert runtime.specs[0].model == "kiro" and run.status == RunStatus.FAILED
    assert (run.error or "").startswith("Kiro lỗi trước khi làm được gì 3 lần liền: ")
    assert "Claude" not in (run.error or "") and "khởi động" not in (run.error or "")
    titles = [e["activity"]["title"] for e in await events(office, run.id, "retry")]
    assert titles == ["Kiro gặp lỗi, thử lại lần 1", "Kiro gặp lỗi, thử lại lần 2"]


async def test_si29_ac5_chat_startup_error_once_then_replies(office: Office, runtime: FakeRuntime) -> None:
    async def answer(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", text="Chào bạn.")

    await office.install_default_positions(skills=False)
    pos = await office.org.find_position("writer")
    lan = await office.org.hire(pos.id, "Lan")
    runtime.scripts += [startup_error, answer]
    conv = await office.chat.dm(lan.id)
    await office.chat.post_user(conv.id, "Chào Lan")
    await office.scheduler.wait_idle(timeout=20)

    assert len(runtime.specs) == 2 and runtime.specs[0].run_id == runtime.specs[1].run_id
    run = await office.runs.get(runtime.specs[0].run_id)
    assert run.status == RunStatus.SUCCEEDED
    assert [e["payload"]["attempt"] for e in await events(office, run.id, "retry")] == [1]
    msgs = await office.chat.messages(conv.id)
    assert [(m.author_kind, m.body) for m in msgs if m.author_kind != "system"] == [("user", "Chào Lan"), ("actor", "Chào bạn.")]


async def test_ac2_ticket_paused_while_waiting_is_not_retried(office: Office, runtime: FakeRuntime) -> None:
    async def paused_meanwhile(ctx: TurnCtx) -> TurnResult:
        await office.db.execute("UPDATE tickets SET paused = 1")  # paused by a person without reaching stop_run
        return await startup_error(ctx)

    runtime.scripts += [paused_meanwhile, startup_error, startup_error]
    _, runs = await run_ticket(office)
    assert len(runtime.specs) == 1 and runs[0].status == RunStatus.CANCELLED
