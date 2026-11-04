"""SI-38: a run copes with a command that runs too long, a stalled turn and a lost API connection by itself, and
says what kind of failure stopped it when it must stop."""

from __future__ import annotations

import ast
import asyncio
import shutil
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from actoroffice import proctree
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import RateLimit, RunStatus, TurnResult, TurnStatus, Usage
from actoroffice.engine import executor as executor_module
from actoroffice.engine.clock import AwakeClock
from actoroffice.engine.command_guard import CommandGuard, command_limit
from actoroffice.engine.failures import FailureKind, classify
from actoroffice.engine.watchdog import Watchdog
from actoroffice.runtimes.base import GateDecision, GateResult, TurnSpec
from actoroffice.runtimes.claude_sdk import ClaudeSdkRuntime, _error_text
from actoroffice.runtimes.kiro_acp import _Turn

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db

CERT = "API Error: Unable to connect to API: Self-signed certificate detected"
INIT_TIMEOUT = "Exception: Control request timeout: initialize"
SRC = Path(__file__).resolve().parents[1] / "src" / "actoroffice"
SLEEPER = [sys.executable, "-c", "import time; time.sleep(60)"]


# ---- helpers ----------------------------------------------------------------------------------------------


def git_bash() -> str | None:
    """Git Bash on Windows (never WSL's bash.exe), bash elsewhere."""
    if sys.platform == "win32":
        git = shutil.which("git")
        candidates = [Path(git).parents[1] / "bin" / "bash.exe", Path(git).parents[2] / "bin" / "bash.exe"] if git else []
        found = next((str(p) for p in candidates if p.is_file()), None) or shutil.which("bash")
        return found if found and "system32" not in found.lower() else None
    return shutil.which("bash")


BASH = git_bash()
needs_bash = pytest.mark.skipif(BASH is None, reason="bash not installed")


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def advance(clock: FakeClock, guard: CommandGuard, seconds: float, step: float = 30) -> None:
    """Awake time passing: small steps, each seen by the guard (as its 1 s loop does)."""
    end = clock.t + seconds
    while clock.t < end:
        clock.t = min(end, clock.t + step)
        await guard.poll()


class AllowGate:
    def __init__(self) -> None:
        self.after: list[dict[str, Any]] = []

    async def before_tool(self, tool: str, args: dict[str, Any], tool_use_id: str | None) -> GateResult:
        return GateResult(GateDecision.ALLOW)

    async def after_tool(self, tool: str, args: dict[str, Any], result: Any) -> None:
        self.after.append(args)


def claude_hooks(guard: CommandGuard, gate: AllowGate) -> tuple[Any, Any, Any, dict[str, str]]:
    spec = TurnSpec(run_id="run_c", prompt="x", cwd=Path.cwd(), system_prompt="s", model="sonnet", tools=["Bash"], command_guard=guard)
    options = ClaudeSdkRuntime()._options(spec, gate)
    hooks = options.hooks
    return hooks["PreToolUse"][0].hooks[0], hooks["PostToolUse"][0].hooks[0], hooks["PostToolUseFailure"][0].hooks[0], options.env


async def wrapped(pre: Any, call_id: str, command: str, **extra: Any) -> str:
    out = await pre({"tool_name": "Bash", "tool_input": {"command": command, **extra}}, call_id, None)
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "allow"
    return spec["updatedInput"]["command"]


async def until(check: Any, timeout: float = 30) -> None:  # a cold Git Bash start can be slow under a loaded suite
    end = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < end, "timed out"
        await asyncio.sleep(0.05)


def tree(pid: int) -> list[psutil.Process]:
    parent = psutil.Process(pid)
    return [parent, *parent.children(recursive=True)]


def gone(procs: list[psutil.Process]) -> bool:
    return all(not p.is_running() or p.status() == psutil.STATUS_ZOMBIE for p in procs)


def sleeps(*seconds: str) -> list[psutil.Process]:
    """`sleep N` processes found by their command line: under Git Bash they are not in the shell's Windows tree."""
    found = []
    for p in psutil.process_iter(["name", "cmdline"]):
        cmdline = p.info["cmdline"] or []
        if (p.info["name"] or "").startswith("sleep") and len(cmdline) == 2 and cmdline[1] in seconds:
            found.append(p)
    return found


def written(pid_file: Path) -> bool:
    """The command's shell wrote its PID file completely (the redirect creates it first, empty)."""
    return pid_file.exists() and pid_file.read_text().split()[-1:] == ["."]


# ---- office fixtures ----------------------------------------------------------------------------------------


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
def settings_extra() -> dict[str, Any]:
    return {}


@pytest.fixture
async def office(tmp_path, runtime, settings_extra) -> AsyncIterator[Office]:
    values = {"approval_wait_s": 0.05, "corrective_turns": 0, "transient_retry_delays_s": [0, 0, 0], **settings_extra}
    o = Office(Settings(data_dir=tmp_path / "home", **values), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


async def run_ticket(office: Office, *, model: str | None = None) -> tuple[Any, list]:
    team = await office.teams.create_team("Billing", jira_project_key="bil")
    if model:
        team.settings.model = model
        await office.teams.update_team(team)
    ticket = await office.tickets.create("Export CSV", description="x")
    await office.stages.start(ticket.key, template="quick", by="user", team_id=team.id)
    await office.scheduler.wait_idle(timeout=60)
    return ticket, await office.runs.for_ticket(ticket.id)


async def events(office: Office, run_id: str, kind: str) -> list[dict]:
    return [e for e in await office.runs.events(run_id) if e["kind"] == kind]


async def comments(office: Office, ticket_id: int) -> str:
    return "\n".join(c.body for c in await office.tickets.comments(ticket_id, limit=50))


def error(text: str, **kw: Any):
    async def script(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.ERROR, error=text, **kw)

    return script


async def ok(ctx: TurnCtx) -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", usage=Usage(input_tokens=10, output_tokens=5))


# ---- AC-1..AC-3: hard limit of the AI's commands ----------------------------------------------------------


@needs_bash
async def test_ac1_claude_bash_over_limit_killed_tree(tmp_path) -> None:
    clock = FakeClock()
    guard = CommandGuard("run_c", command_limit(None, 900), tmp_path / "cmd", AwakeClock(raw=clock))
    gate = AllowGate()
    pre, post, _failure, env = claude_hooks(guard, gate)
    assert env["BASH_MAX_TIMEOUT_MS"] == str(960_000)  # the CLI waits past the office limit
    command = "sleep 6101 & sleep 6102"
    proc = subprocess.Popen([BASH, "-c", await wrapped(pre, "tu1", command)])
    try:
        await until(lambda: written(guard.commands["tu1"].pid_file) and len(sleeps("6101", "6102")) == 2)
        procs = tree(proc.pid) + sleeps("6101", "6102")  # the shell, Git Bash's launcher on Windows, both sleeps
        await advance(clock, guard, 899)
        assert gone(procs) is False
        await advance(clock, guard, 3)
        psutil.wait_procs(procs, timeout=10)
        assert gone(procs) and sleeps("6101", "6102") == []
        out = await post({"tool_name": "Bash", "tool_input": {"command": await wrapped(pre, "tu-other", "true")}}, "tu1", None)
        note = out["hookSpecificOutput"]["additionalContext"]
        assert "sleep 6101 & sleep 6102" in note and "after 90" in note and "limit 900s" in note and "narrower" in note
        assert gate.after[-1]["command"] == "true"  # the office records the command as the AI wrote it
    finally:
        proc.kill()
        await guard.close()


@needs_bash
async def test_ac2_profile_timeout_5s_claude(tmp_path) -> None:
    limit = command_limit({"command_timeout_s": 5}, 900)
    guard = CommandGuard("run_c", limit, tmp_path / "cmd")
    guard.start()
    pre, _post, failure, _env = claude_hooks(guard, AllowGate())
    started = time.monotonic()
    proc = subprocess.Popen([BASH, "-c", await wrapped(pre, "tu1", "sleep 30")])
    try:
        await until(lambda: guard.commands.get("tu1") is not None and guard.commands["tu1"].pid_file.exists())
        await until(lambda: proc.poll() is not None, timeout=20)
        assert 4 <= time.monotonic() - started < 15
        out = await failure({"tool_name": "Bash"}, "tu1", None)
        assert "limit 5s" in out["hookSpecificOutput"]["additionalContext"]
    finally:
        proc.kill()
        await guard.close()


def test_ac2_g3_wrong_command_timeout_uses_the_default() -> None:
    for wrong in (0, -3, "abc", True, None):
        assert command_limit({"command_timeout_s": wrong}, 900) == 900
    assert command_limit({"command_timeout_s": "12"}, 900) == 12


async def test_ac2_profile_timeout_5s_kiro(tmp_path) -> None:
    guard = CommandGuard("run_k", 5, tmp_path / "cmd")
    guard.start()
    spec = TurnSpec(run_id="run_k", prompt="x", cwd=tmp_path, system_prompt="s", model="kiro", tools=[], command_guard=guard)

    async def sink(_kind: str, _payload: dict[str, Any]) -> None:
        return None

    turn = _Turn(["kiro-cli"], spec, AllowGate(), sink)
    code = "import subprocess, sys, time; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); time.sleep(30)"
    params = {"command": sys.executable, "args": ["-c", code]}
    turn.approved_commands.add(" ".join([sys.executable, "-c", code]))
    started = time.monotonic()
    try:
        terminal_id = (await turn.on_terminal_create(params))["terminalId"]
        proc = turn.terminals[terminal_id].proc
        await until(lambda: len(tree(proc.pid)) >= 2)
        procs = tree(proc.pid)
        await asyncio.wait_for(turn.on_terminal_wait({"terminalId": terminal_id}), 20)
        assert 4 <= time.monotonic() - started < 15
        psutil.wait_procs(procs, timeout=10)
        assert gone(procs)
        output = (await turn.on_terminal_output({"terminalId": terminal_id}))["output"]
        assert "Office stopped this command" in output and "limit 5s" in output
    finally:
        await guard.close()
        await turn._shutdown()


@needs_bash
async def test_ac3_background_command_limited(tmp_path) -> None:
    clock = FakeClock()
    guard = CommandGuard("run_c", 900, tmp_path / "cmd", AwakeClock(raw=clock))
    gate = AllowGate()
    pre, post, _failure, _env = claude_hooks(guard, gate)
    proc = subprocess.Popen([BASH, "-c", await wrapped(pre, "bg1", "sleep 6103", run_in_background=True)])
    try:
        pid_file = guard.commands["bg1"].pid_file
        await until(lambda: written(pid_file) and len(sleeps("6103")) == 1)
        # run_in_background: the tool call returns at once, the command goes on
        assert await post({"tool_name": "Bash", "tool_input": {"command": "x"}}, "bg1", None) == {}
        procs = tree(proc.pid) + sleeps("6103")
        await advance(clock, guard, 901)
        psutil.wait_procs(procs, timeout=10)
        assert gone(procs) and sleeps("6103") == []
        # told with the next tool result, whatever the tool
        out = await post({"tool_name": "Read", "tool_input": {"file_path": "a.py"}}, "tu2", None)
        assert "sleep 6103" in out["hookSpecificOutput"]["additionalContext"]
    finally:
        proc.kill()
        await guard.close()


@pytest.mark.parametrize("settings_extra", [{"command_timeout_s": 1}])
async def test_ac3_leftover_background_killed_at_run_end(office: Office, runtime: FakeRuntime, settings_extra) -> None:
    procs: list[subprocess.Popen] = []

    async def works_with_commands(ctx: TurnCtx) -> TurnResult:
        guard = ctx.spec.command_guard
        over = subprocess.Popen(SLEEPER)
        guard.watch("c1", "uv run pytest", over.pid)
        await ctx.sink("tool_use", {"tool": "Bash", "input": {"command": "uv run pytest"}, "id": "c1"})
        await until(lambda: over.poll() is not None)  # stopped at its 1 s limit, the run goes on
        guard.limit_s = 3600
        left = subprocess.Popen(SLEEPER)
        procs.append(left)
        guard.watch("c2", "python -m http.server", left.pid)  # still running in the background when the turn ends
        return await ok(ctx)

    runtime.scripts += [works_with_commands]
    _ticket, [run] = await run_ticket(office)
    assert run.status == RunStatus.SUCCEEDED and run.error_kind is None  # AC-1: a stopped command does not fail the run
    assert procs[0].wait(timeout=10) is not None
    titles = [e["payload"]["title"] for e in await events(office, run.id, "health")]
    assert any(t.startswith("Đã dừng lệnh `uv run pytest` sau") for t in titles)


# ---- AC-4: one way to kill a process tree ------------------------------------------------------------------


def test_ac4_single_kill_tree() -> None:
    defined = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and ("kill_tree" in node.name or "kill_and_wait" in node.name):
                defined.append(f"{path.relative_to(SRC).as_posix()}:{node.name}")
    assert defined == ["proctree.py:kill_tree"]
    from actoroffice.engine import command_guard
    from actoroffice.runtimes import kiro_acp
    from actoroffice.services import automations, checks, merges, ui_preview

    for module in (automations, checks, merges, ui_preview, kiro_acp, command_guard):
        assert module.kill_tree is proctree.kill_tree


def test_ac4_kill_tree_waits_until_gone() -> None:
    proc = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; subprocess.Popen(sys.argv[1:]); time.sleep(60)", *SLEEPER])
    try:
        deadline = time.monotonic() + 15
        while len(tree(proc.pid)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        procs = tree(proc.pid)
        assert proctree.kill_tree(proc.pid, wait_s=10) is True
        assert gone(procs) and proctree.alive(proc.pid) is False
        assert proctree.kill_tree(proc.pid) is False and proctree.alive(None) is None
    finally:
        proc.kill()


# ---- AC-5, AC-8: stall threshold per stage, time asleep not counted ----------------------------------------


def watchdog(stall_after_s: float, clock: FakeClock) -> tuple[Watchdog, list[dict]]:
    from types import SimpleNamespace

    seen: list[dict] = []

    async def event(_run_id: str, kind: str, payload: dict) -> None:
        if kind == "health":
            seen.append(payload)

    office = SimpleNamespace(runs=SimpleNamespace(event=event))
    return Watchdog(office, "run_w", stall_after_s, 300, command_limit_s=900, clock=AwakeClock(raw=clock)), seen


async def tick(dog: Watchdog, clock: FakeClock, seconds: float, step: float = 15) -> None:
    end = clock.t + seconds
    while clock.t < end:
        clock.t = min(end, clock.t + step)
        await dog.check()


def states(seen: list[dict]) -> list[str]:
    return [e["state"] for e in seen]


async def test_ac5_stall_threshold_by_stage() -> None:
    assert Settings(data_dir=Path("x")).stall_after_s_by_stage == {"understand": 600, "build": 900}
    clock = FakeClock()
    dog, seen = watchdog(600, clock)
    stalls: list[bool] = []
    dog.on_stall = lambda: stalls.append(True)
    await tick(dog, clock, 590)
    assert "stalled" not in states(seen)
    await tick(dog, clock, 20)
    assert states(seen).count("stalled") == 1 and stalls == [True] and dog.stalled


async def test_ac5_running_command_not_stall() -> None:
    clock = FakeClock()
    dog, seen = watchdog(600, clock)
    await dog.observe({"kind": "command", "status": "running", "call_id": "t1", "fields": {"command": "uv run pytest"}})
    await tick(dog, clock, 700)
    assert "stalled" not in states(seen) and "slow" in states(seen)
    # just past the command limit (900 s): the guard is stopping it and the CLI answers within a minute (review F1)
    await tick(dog, clock, 300)
    assert "stalled" not in states(seen) and not dog.stalled
    await tick(dog, clock, 490)  # 890 s: no answer yet, still within the stage threshold after the limit
    assert "stalled" not in states(seen)
    await tick(dog, clock, 30)  # the limit plus the whole stage threshold with no answer: stalled
    assert states(seen).count("stalled") == 1 and dog.stalled


async def test_ac5_executor_uses_the_stage_threshold(office: Office, runtime: FakeRuntime) -> None:
    seen: list[float] = []

    async def look(ctx: TurnCtx) -> TurnResult:
        seen.append(office.gates[ctx.spec.run_id].watchdog.stall_after_s)
        return await ok(ctx)

    office.settings.stall_after_s_by_stage = {"scope": 123}
    runtime.scripts += [look]
    await run_ticket(office)
    assert seen == [123]


async def test_ac8_sleep_gap_not_counted(tmp_path) -> None:
    clock = FakeClock()
    dog, seen = watchdog(600, clock)
    guard = CommandGuard("run_w", 900, tmp_path / "cmd", dog.clock)
    proc = subprocess.Popen(SLEEPER)
    try:
        guard.watch("c1", "uv run pytest", proc.pid)
        await tick(dog, clock, 500)
        clock.t += 4000  # the machine slept
        await dog.check()
        await guard.poll()
        assert "stalled" not in states(seen) and proc.poll() is None
        assert any(e["state"] == "sleep" and "4000s" in e["title"] for e in seen)
        await tick(dog, clock, 90)  # 590 s awake since the last event: not stalled yet
        assert "stalled" not in states(seen)
        await advance(clock, guard, 300)  # 500 + 90 + 300 = 890 s awake: the command is still within its limit
        assert proc.poll() is None
        await advance(clock, guard, 20)
        assert proc.wait(timeout=10) is not None
    finally:
        proc.kill()
        await guard.close()


# ---- AC-6, AC-7: a stalled turn is stopped and retried as a network failure --------------------------------


class StallingRuntime(FakeRuntime):
    """A runtime whose CLI process is a real process: alive (AC-6) or already gone (AC-7)."""

    def __init__(self, alive: bool) -> None:
        super().__init__(scripts=[])
        self.proc = subprocess.Popen(SLEEPER if alive else [sys.executable, "-c", "pass"])
        if not alive:
            self.proc.wait()
        self.aborted: list[str] = []
        self.released = asyncio.Event()

    def pid(self, run_id: str) -> int:
        return self.proc.pid

    async def abort(self, run_id: str) -> None:
        self.aborted.append(run_id)
        proctree.kill_tree(self.proc.pid)
        self.released.set()


@pytest.fixture
def stall_settings(monkeypatch) -> dict[str, Any]:
    monkeypatch.setattr(executor_module, "ABORT_GRACE_S", 0.5)
    return {"stall_after_s": 1, "stall_after_s_by_stage": {}}


@pytest.mark.parametrize("alive", [True, False])
async def test_ac6_ac7_stalled_runtime_retried_as_network(tmp_path, stall_settings, alive: bool) -> None:
    runtime = StallingRuntime(alive)

    async def hangs(ctx: TurnCtx) -> TurnResult:
        await ctx.sink("text", {"text": "Chạy test."})
        await runtime.released.wait()  # never set when the process is already gone: the office cancels the turn
        return TurnResult(status=TurnStatus.ERROR, error="Exception: CLI process exited", session_id="sess-1")

    runtime.scripts += [hangs, ok]
    settings = Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, corrective_turns=0, transient_retry_delays_s=[0, 0, 0], **stall_settings)
    office = Office(settings, runtime=runtime)
    seed_db(office.settings.db_path)
    await office.open()
    try:
        _ticket, [run] = await run_ticket(office)
        assert len(runtime.specs) == 2 and run.status == RunStatus.SUCCEEDED
        assert runtime.aborted == ([run.id] if alive else [])
        [retry] = await events(office, run.id, "retry")
        assert retry["payload"]["kind"] == "network" and "stalled" in retry["payload"]["error"]
        assert "mạng/API" in retry["activity"]["title"]
        assert any(e["payload"]["state"] == "stalled" for e in await events(office, run.id, "health"))
    finally:
        runtime.proc.kill()
        await office.close()


# ---- AC-9..AC-14, AC-17: retries and failure kinds ------------------------------------------------------------


@pytest.mark.parametrize("settings_extra", [{"transient_retry_delays_s": [30, 120, 300]}])
async def test_ac9_network_error_retries_30_120_300(office: Office, runtime: FakeRuntime, monkeypatch, settings_extra) -> None:
    waits: list[float] = []

    async def wait(_run_id: str, _ticket_id: int | None, delay: float) -> bool:
        waits.append(delay)
        return True

    monkeypatch.setattr(office.executor, "_wait_retry", wait)
    runtime.scripts += [error(CERT)] * 5
    ticket, [run] = await run_ticket(office)
    assert len(runtime.specs) == 4 and waits == [30, 120, 300]
    assert run.status == RunStatus.FAILED and run.error_kind == "network"
    text = await comments(office, ticket.id)
    assert "Lượt chạy lỗi: Claude · mạng/API: đã thử lại 3 lần; kiểm tra kết nối" in text and CERT in text
    titles = [e["activity"]["title"] for e in await events(office, run.id, "retry")]
    assert titles == [
        "Claude · mạng/API: tự thử lại lần 1/3 sau 30s, không cần làm gì",
        "Claude · mạng/API: tự thử lại lần 2/3 sau 2m, không cần làm gì",
        "Claude · mạng/API: tự thử lại lần 3/3 sau 5m, không cần làm gì",
    ]


def test_ac9_default_delays() -> None:
    settings = Settings(data_dir=Path("x"))
    assert settings.transient_retry_delays_s == [30, 120, 300] and settings.command_timeout_s == 900


@pytest.mark.parametrize("text", [CERT, "API Error: 529 Overloaded", "API Error: 503 Service Unavailable", "fetch failed: ECONNRESET"])
def test_ac9_network_errors_classified(text: str) -> None:
    assert classify(text, progressed=True) == FailureKind.NETWORK
    assert classify("wrote 500 lines", progressed=True) == FailureKind.AI
    assert classify("FileNotFoundError: no such file", progressed=True) == FailureKind.AI


async def test_ac10_mixed_transient_errors_share_budget(office: Office, runtime: FakeRuntime) -> None:
    runtime.scripts += [error(INIT_TIMEOUT), error(CERT), error(CERT), error(CERT), ok]
    _ticket, [run] = await run_ticket(office)
    assert len(runtime.specs) == 4 and run.status == RunStatus.FAILED
    kinds = [e["payload"]["kind"] for e in await events(office, run.id, "retry")]
    assert kinds == ["runtime_start", "network", "network"]


async def test_ac11_resume_after_progress_names_command(office: Office, runtime: FakeRuntime) -> None:
    seen: list[tuple[str | None, str]] = []

    async def cut_off(ctx: TurnCtx) -> TurnResult:
        await ctx.sink("tool_use", {"tool": "Read", "input": {"file_path": "plan.md"}, "id": "t0"})
        await ctx.sink("tool_result", {"id": "t0", "error": False, "text": "ok"})
        await ctx.sink("tool_use", {"tool": "Bash", "input": {"command": "uv run pytest tests/test_x.py"}, "id": "t1"})
        return TurnResult(status=TurnStatus.ERROR, error=CERT, session_id="sess-A", usage=Usage(input_tokens=90_000))

    async def resumed(ctx: TurnCtx) -> TurnResult:
        seen.append((ctx.spec.resume_session_id, ctx.spec.prompt))
        return await ok(ctx)

    runtime.scripts += [cut_off, resumed]
    _ticket, [run] = await run_ticket(office)
    assert run.status == RunStatus.SUCCEEDED
    [(session, prompt)] = seen
    assert session == "sess-A" and "mất kết nối API" in prompt and "`uv run pytest tests/test_x.py`" in prompt
    assert "check the result of your last command" in prompt


async def test_ac12_rate_limit_not_retried(office: Office, runtime: FakeRuntime) -> None:
    resets = int(time.time()) + 3600

    async def limited(ctx: TurnCtx) -> TurnResult:
        office.scheduler.observe_rate_limit({"status": "rejected", "resets_at": resets})
        return TurnResult(status=TurnStatus.RATE_LIMITED, error="rate limited", rate_limit=RateLimit(status="rejected", resets_at=resets))

    runtime.scripts += [limited]
    ticket, [run] = await run_ticket(office)
    assert len(runtime.specs) == 1
    assert await events(office, run.id, "retry") == []
    assert run.error == "rate limited" and run.error_kind is None
    assert "hết hạn mức Claude" in await comments(office, ticket.id)
    wake = await office.wakes.pending_for(run.actor_id, ticket.id)
    assert wake is not None and any(r.get("retry") == "rate_limit" for r in wake.payload.get("reasons", []))


@pytest.mark.parametrize(("model", "name", "other"), [("kiro", "Kiro", "Claude"), (None, "Claude", "Kiro")])
async def test_ac13_retry_activity_names_runtime(office: Office, runtime: FakeRuntime, model, name, other) -> None:
    runtime.scripts += [error(INIT_TIMEOUT), ok]
    _ticket, [run] = await run_ticket(office, model=model)
    [retry] = await events(office, run.id, "retry")
    title = retry["activity"]["title"]
    assert title.startswith(f"{name} · lỗi khởi động: tự thử lại lần 1/3") and other not in title


async def command_timeout(ctx: TurnCtx) -> TurnResult:
    proc = subprocess.Popen(SLEEPER)
    ctx.spec.command_guard.watch("c1", "uv run pytest", proc.pid)
    await ctx.sink("tool_use", {"tool": "Bash", "input": {"command": "uv run pytest"}, "id": "c1"})
    await until(lambda: proc.poll() is not None)
    return TurnResult(status=TurnStatus.ERROR, error="Exception: CLI process exited")


async def ai_error(ctx: TurnCtx) -> TurnResult:
    await ctx.sink("text", {"text": "Sửa executor."})
    return TurnResult(status=TurnStatus.ERROR, error="Exception: CLI process exited")


@pytest.mark.parametrize("settings_extra", [{"command_timeout_s": 1}])
@pytest.mark.parametrize(
    ("scripts", "kind", "hint"),
    [
        ([error(CERT)] * 4, "network", "đã thử lại 3 lần; kiểm tra kết nối mạng/API rồi bấm Thử lại"),
        ([error(INIT_TIMEOUT)] * 4, "runtime_start", "đã thử lại 3 lần; kiểm tra Claude trên máy"),
        ([command_timeout], "command_timeout", "cần bạn xem: lệnh của AI chạy quá giới hạn"),
        ([ai_error], "ai", "cần bạn xem tiến độ"),
    ],
)
async def test_ac14_error_kind_each_class(office: Office, runtime: FakeRuntime, settings_extra, scripts, kind, hint) -> None:
    runtime.scripts += scripts
    ticket, [run] = await run_ticket(office)
    assert run.status == RunStatus.FAILED and run.error_kind == kind
    assert hint in await comments(office, ticket.id) and hint in (run.error or "")
    # what the web team pane receives (GET /api/tickets/{key}: detail.runs[])
    from actoroffice.web.server import create_app

    app = create_app(office, "t" * 40)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8765") as client:
        await client.get(f"/login?t={'t' * 40}")
        detail = (await client.get(f"/api/tickets/{ticket.key}")).json()
    assert detail["runs"][0]["error_kind"] == kind


def test_ac14_command_line_too_long_is_a_start_failure() -> None:
    try:
        try:
            raise OSError(206, "The filename or extension is too long", None, 206)
        except OSError as cause:
            raise FileNotFoundError("Claude Code not found at: claude.exe") from cause
    except FileNotFoundError as exc:
        text = _error_text(exc)
    assert "Claude Code not found" in text and "WinError 206" in text
    from actoroffice.engine.failures import hint

    assert classify(text, progressed=False) == FailureKind.RUNTIME_START
    assert "quá dài (WinError 206), không phải thiếu Claude" in hint("runtime_start", "Claude", 3, text)


async def test_ac17_ai_error_not_retried(office: Office, runtime: FakeRuntime) -> None:
    runtime.scripts += [ai_error, ok]
    ticket, [run] = await run_ticket(office)
    assert len(runtime.specs) == 1 and await events(office, run.id, "retry") == []
    assert run.error_kind == "ai" and "Claude · lỗi của AI: cần bạn xem" in await comments(office, ticket.id)
