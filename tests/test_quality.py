"""Quality layer: watchdog, diff checks on a real git worktree, the independent reviewer, per-ticket ports,
live changes and evidence artifacts."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import ProjectAccess
from actoroffice.engine.workspace import changes, ticket_env
from actoroffice.runtimes.base import GateDecision
from actoroffice.services.checks import parse_coverage

from .fakes import FakeRuntime, TurnCtx

REQUIREMENTS = "- AC-1: Given a When b Then c\n- AC-2: Given d When e Then f\n"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def ok(ctx: TurnCtx, text: str = "") -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", text=text)


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


async def repo_project(office: Office, tmp_path: Path, profile: dict | None = None):
    repo = tmp_path / "svc"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("def total(x):\n    return x\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    project = await office.projects.create("svc", path=str(repo), access=ProjectAccess.WORKTREE)
    if profile:
        project = await office.projects.configure(project.id, profile=profile)
    return project


async def build_ticket(office: Office, project) -> int:
    ticket = await office.tickets.create("Tổng tiền", description="x", project_id=project.id)
    await office.db.execute("UPDATE tickets SET stage = 'build', stage_template = 'code' WHERE id = ?", (ticket.id,))
    await office.tickets.put_document(ticket.id, "requirements", REQUIREMENTS, author="Copilot", base_revision=None)
    await office.tickets.put_document(ticket.id, "handoff", "pytest", author="Copilot", base_revision=None)
    return ticket.id


async def test_watchdog_stops_a_loop_and_asks_a_developer(office: Office, runtime: FakeRuntime) -> None:
    ticket = await office.tickets.create("Lặp", description="x")
    seen: list[str] = []

    async def loop(ctx: TurnCtx) -> TurnResult:
        for i in range(3):
            await ctx.sink("tool_use", {"tool": "Bash", "input": {"command": "pytest -q tests/test_a.py"}, "id": f"c{i}"})
            await ctx.sink("tool_result", {"id": f"c{i}", "error": True, "text": "1 failed"})
        decision = await ctx.gate.before_tool("Edit", {"file_path": "a.py", "old_string": "a", "new_string": "b"}, None)
        seen.append(decision.decision.value)
        seen.append(decision.reason)
        ask = await ctx.gate.before_tool("mcp__office__ask_human", {"roles": ["dev"], "question": "?"}, None)
        seen.append(ask.decision.value)
        return ok(ctx)

    runtime.scripts.append(loop)
    await office.stages.start(ticket.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert seen[0] == "deny" and "ask_human" in seen[1] and seen[2] == "allow"
    run = (await office.runs.for_ticket(ticket.id))[0]
    health = [a for a in await office.runs.activities(run.id) if a["kind"] == "health"]
    assert health and "lặp" in health[0]["title"]


async def test_diff_checks_on_a_worktree(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    project = await repo_project(office, tmp_path, profile={"coverage": {"kind": "lcov", "path": "coverage.lcov", "min": 80}})
    ticket_id = await build_ticket(office, project)
    results: list[str] = []

    async def work(ctx: TurnCtx) -> TurnResult:
        ws = ctx.spec.cwd
        (ws / "src" / "app.py").write_text(
            "def total(x, vip=False):\n    print('debug')\n    return x * 2 if vip else x\n", encoding="utf-8"
        )
        (ws / "tests").mkdir(exist_ok=True)
        (ws / "tests" / "test_app.py").write_text("def test_ac1_total():\n    assert True\n", encoding="utf-8")
        (ws / "src" / "conf.py").write_text('API_KEY = "sk-ant-abcdefghijklmnopqrstuvwx"\n', encoding="utf-8")
        await ctx.office("plan_steps", {"steps": ["code"]})
        await ctx.office("step_done", {"index": 1})
        results.append(await ctx.office("stage_complete", {"summary": "x"}))
        # fix: AC-2 test, no print, no secret, coverage report for the changed lines
        (ws / "src" / "app.py").write_text("def total(x, vip=False):\n    return x * 2 if vip else x\n", encoding="utf-8")
        (ws / "tests" / "test_app.py").write_text("def test_ac1_total():\n    assert True\n\n\ndef test_ac2_vip():\n    assert True\n", encoding="utf-8")
        (ws / "src" / "conf.py").write_text("import os\nAPI_KEY = os.environ.get('API_KEY')\n", encoding="utf-8")
        (ws / "coverage.lcov").write_text("SF:src/app.py\nDA:1,1\nDA:2,1\nend_of_record\nSF:src/conf.py\nDA:1,1\nDA:2,1\nend_of_record\n", encoding="utf-8")
        results.append(await ctx.office("stage_complete", {"summary": "đã sửa"}))
        return ok(ctx)

    runtime.scripts.append(work)
    await office.tickets.assign(ticket_id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=30)
    first, second = results
    for check in ("ac_coverage", "no_secrets", "log_lint"):
        assert check in first, check
    assert "AC-2" in first and "API key" in first and "print" in first
    assert "Next stage: pr" in second, second
    srun = [r for r in await office.stages.runs(ticket_id) if r.stage == "build"][-1]
    by_id = {c["id"]: c for c in srun.checks}
    assert by_id["coverage_changed_lines"]["status"] == "passed" and "100%" in by_id["coverage_changed_lines"]["summary"]
    assert by_id["review_clean"]["status"] == "skipped" or by_id["review_clean"]["status"] == "passed"


async def test_reviewer_findings_fail_the_check_for_two_rounds_then_go_to_people(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    project = await repo_project(office, tmp_path)
    ticket_id = await build_ticket(office, project)
    await office.db.execute("UPDATE tickets SET stage = 'build' WHERE id = ?", (ticket_id,))
    outcomes: list[str] = []
    high = '{"findings": [{"severity": "high", "file": "src/app.py", "line": 2, "claim": "VIP chưa có test", "evidence": "AC-2"}]}'

    def reviewer(text: str):
        async def script(ctx: TurnCtx) -> TurnResult:
            assert "strict, independent code reviewer" in ctx.spec.system_prompt and "def total" in ctx.spec.prompt
            denied = await ctx.gate.before_tool("Bash", {"command": "rm -rf ."}, None)
            assert denied.decision == GateDecision.DENY
            return ok(ctx, text)

        return script

    async def work(ctx: TurnCtx) -> TurnResult:
        (ctx.spec.cwd / "src" / "app.py").write_text("def total(x, vip=False):\n    return x * 2 if vip else x\n", encoding="utf-8")
        await ctx.office("plan_steps", {"steps": ["code"]})
        await ctx.office("step_done", {"index": 1})
        for _ in range(3):
            outcomes.append(await ctx.office("stage_complete", {"summary": "x"}))
        return ok(ctx)

    # build stage template without the other diff checks for this test: only review_clean matters
    from actoroffice.domain.stage import StageDef, StageTemplate
    from actoroffice.domain.team import TeamSettings

    team = await office.teams.create_team("T", settings=TeamSettings(stage_templates={"code": StageTemplate(name="code", stages=[StageDef(id="build", checks=["review_clean"]), StageDef(id="close")])}))
    await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (team.id, ticket_id))
    runtime.scripts += [work, reviewer(high), reviewer(high)]
    await office.tickets.assign(ticket_id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=30)
    assert "VIP chưa có test" in outcomes[0] and "review_clean" in outcomes[0]
    assert "review_clean" in outcomes[1]  # second round still has a high finding
    assert "Next stage: close" in outcomes[2]  # after two rounds it goes to people instead of looping
    reviewer_runs = await office.db.fetchall("SELECT status FROM runs WHERE purpose = 'reviewer'")
    assert [r["status"] for r in reviewer_runs] == ["succeeded", "succeeded"]


async def test_ports_changes_and_artifacts(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    project = await repo_project(office, tmp_path, profile={"ports": ["APP_PORT", "DB_PORT"]})
    ticket = await office.tickets.create("UI", description="x", project_id=project.id)
    env = ticket_env(ticket, project)
    assert env["COMPOSE_PROJECT_NAME"] == f"ao-{ticket.key.lower()}" and int(env["DB_PORT"]) == int(env["APP_PORT"]) + 1
    other = await office.tickets.create("UI 2", description="x", project_id=project.id)
    assert ticket_env(other, project)["APP_PORT"] != env["APP_PORT"]
    await office.db.execute("UPDATE tickets SET stage = 'build', stage_template = 'code' WHERE id = ?", (ticket.id,))
    events: list[dict] = []

    async def work(ctx: TurnCtx) -> TurnResult:
        assert ctx.spec.env["APP_PORT"] == env["APP_PORT"]
        (ctx.spec.cwd / "shot.png").write_bytes(b"\x89PNG fake")
        (ctx.spec.cwd / "src" / "app.py").write_text("def total(x):\n    return x + 0\n", encoding="utf-8")
        out = await ctx.office("artifact_add", {"path": "shot.png", "kind": "screenshot", "caption": "AC-1 màn hình tổng tiền", "ac": "AC-1"})
        assert "Saved screenshot" in out
        return ok(ctx)

    async with office.bus.subscribe() as queue:
        runtime.scripts.append(work)
        await office.tickets.assign(ticket.id, (await office.ensure_ai()).id, by="user")
        await office.scheduler.wait_idle(timeout=30)
        while not queue.empty():
            event = queue.get_nowait()
            if event.kind == "ticket.artifact":
                events.append(event.data)
    assert events and events[0]["kind"] == "screenshot" and Path(events[0]["path"]).is_file()
    root = await office.tickets.root_of(ticket.id)
    diff = await changes(office.settings.workspaces_dir / root.key, "main")
    paths = {f["path"] for f in diff["files"]}
    assert "src/app.py" in paths and diff["branch"].startswith("t/")


def test_coverage_report_parsers(tmp_path: Path) -> None:
    jacoco = '<report><package name="com/acme"><sourcefile name="Refund.java"><line nr="3" ci="2"/><line nr="4" ci="0"/></sourcefile></package></report>'
    assert parse_coverage("jacoco", jacoco, tmp_path) == {"com/acme/Refund.java": {3: True, 4: False}}
    cobertura = '<coverage><packages><package><classes><class filename="src/a.py"><lines><line number="1" hits="1"/><line number="2" hits="0"/></lines></class></classes></package></packages></coverage>'
    assert parse_coverage("cobertura", cobertura, tmp_path) == {"src/a.py": {1: True, 2: False}}


async def test_artifacts_reach_the_ticket_topic(office: Office, tmp_path: Path) -> None:
    from actoroffice.channels.telegram import TelegramBridge

    from .test_telegram import FakeBot, tg_message

    bot = FakeBot()
    office.telegram = TelegramBridge(office, bot)
    ready = asyncio.Event()
    task = asyncio.create_task(office.telegram.run(ready))
    await ready.wait()
    try:
        await office.telegram.handle_update(tg_message("/start"))
        ticket = await office.tickets.create("UI", description="x")
        ticket = await office.stages.start(ticket.id, template="code", by="user")
        folder = office.settings.data_dir / "artifacts" / ticket.key
        folder.mkdir(parents=True)
        (folder / "a.png").write_bytes(b"png")
        office.bus.publish("ticket.artifact", ticket_id=ticket.id, path=str(folder / "a.png"), kind="screenshot", caption="AC-1")
        outside = tmp_path / "secret.txt"
        outside.write_text("x", encoding="utf-8")
        office.bus.publish("ticket.artifact", ticket_id=ticket.id, path=str(outside), kind="file", caption="")
        for _ in range(100):
            if bot.documents:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)
        assert [name for _thread, name in bot.documents] == ["a.png"]  # files outside the artifacts folder are never sent
    finally:
        task.cancel()


async def test_watchdog_lets_many_different_edits_through_but_stops_repeats_and_undos() -> None:
    from types import SimpleNamespace

    from actoroffice.engine.activity import describe_call
    from actoroffice.engine.watchdog import Watchdog

    async def event(*_args, **_kwargs) -> None:
        return None

    def edit(old: str, new: str) -> dict:
        activity = describe_call("Edit", {"file_path": "src/a.py", "old_string": old, "new_string": new})
        return {"kind": "edit", "status": "running", "fields": activity.fields}

    office = SimpleNamespace(runs=SimpleNamespace(event=event))
    dog = Watchdog(office, "run_x", stall_after_s=600, slow_command_s=600)
    for i in range(5):  # one change written in several pieces (SI-2, SI-3 were stopped at the third)
        await dog.observe(edit(f"old {i}", f"new {i}"))
    assert dog.loop is None

    await dog.observe(edit("old 1", "new 1"))
    assert dog.loop and "lặp lại" in dog.loop

    undo = Watchdog(office, "run_y", stall_after_s=600, slow_command_s=600)
    await undo.observe(edit("a = 1", "a = 2"))
    await undo.observe(edit("a = 2", "a = 1"))
    assert undo.loop and "hoàn tác" in undo.loop

    reads = Watchdog(office, "run_z", stall_after_s=600, slow_command_s=600)
    for offset in (1, 200, 400, 600, 800):  # reading a big file part by part is not a loop
        activity = describe_call("Read", {"file_path": "src/big.py", "offset": offset, "limit": 200})
        await reads.observe({"kind": "read", "status": "running", "fields": activity.fields})
    assert reads.loop is None
