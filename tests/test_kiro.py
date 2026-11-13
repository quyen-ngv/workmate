"""The Kiro CLI runtime (ACP), driven against a scripted ACP agent (tests/fake_acp_agent.py)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from actoroffice.domain.run import TurnStatus
from actoroffice.runtimes import kiro_acp
from actoroffice.runtimes.base import GateDecision, GateResult, TurnSpec
from actoroffice.runtimes.kiro_acp import KiroAcpRuntime
from actoroffice.runtimes.router import RuntimeRouter, is_kiro
from actoroffice.tools.registry import BoundTool, ToolDef, schema

AGENT = [sys.executable, str(Path(__file__).parent / "fake_acp_agent.py")]


class Gate:
    """Allows work inside the workspace, refuses writes elsewhere, defers `rm -rf` to the user."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def before_tool(self, tool: str, args: dict[str, Any], tool_use_id: str | None) -> GateResult:
        self.calls.append((tool, args))
        if "rm -rf" in str(args.get("command", "")):
            return GateResult(GateDecision.DEFER, "Waiting for the user's approval")
        path = args.get("file_path")
        if tool == "Write" and path and not Path(path).resolve().is_relative_to(self.workspace):
            return GateResult(GateDecision.DENY, f"{path} is outside this ticket's workspace")
        return GateResult(GateDecision.ALLOW)

    async def after_tool(self, tool: str, args: dict[str, Any], result: Any) -> None:
        return None


def office_tool(comments: list[str]) -> BoundTool:
    async def ticket_comment(args: dict[str, Any], _ctx: Any) -> str:
        comments.append(args["body"])
        return "Comment added."

    return BoundTool(ToolDef("ticket_comment", "Add a comment", schema({"body": {"type": "string"}}), ticket_comment), ctx=None)  # type: ignore[arg-type]


def spec_for(workspace: Path, prompt: str, *, comments: list[str], resume: str | None = None, model: str = "kiro:claude-sonnet") -> TurnSpec:
    return TurnSpec(
        run_id="run_k1", prompt=prompt, cwd=workspace, system_prompt="Bạn là Minh, Developer.", model=model, tools=[],
        resume_session_id=resume, internal_tools=[office_tool(comments)],
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws" / "AO-1"
    ws.mkdir(parents=True)
    (ws / "notes.md").write_text("ghi chú", encoding="utf-8")
    return ws


async def test_a_turn_goes_through_the_office_gate_and_tools(workspace: Path):
    gate, comments, events = Gate(workspace), [], []

    async def sink(kind: str, data: dict[str, Any]) -> None:
        events.append((kind, data))

    result = await KiroAcpRuntime(command=AGENT).run_turn(spec_for(workspace, "Làm AO-1", comments=comments), gate, sink)

    assert result.status == TurnStatus.COMPLETED and result.session_id == "sess-1", result.error
    assert result.text.endswith("Xong: xin chao") and result.cost_usd == pytest.approx(0.0125)
    assert (workspace / "out.md").read_text(encoding="utf-8") == "ghi chú | xin chao"
    log = json.loads((workspace / "agent_log.json").read_text(encoding="utf-8"))
    assert log["first_prompt"].startswith("<office_instructions>\nBạn là Minh")  # no system prompt in ACP
    assert log["outside_error"] and not (workspace.parent / "outside.txt").exists()
    assert log["tools"] == ["ticket_comment"] and log["called"]["isError"] is False and comments == ["đã xong"]
    tools = [name for name, _ in gate.calls]
    assert tools[:2] == ["Read", "Bash"] and tools.count("Bash") == 1  # the approved command is not asked twice
    assert "Write" in tools and "mcp__office__ticket_comment" in tools
    kinds = [k for k, _ in events]
    assert kinds.index("thinking") < kinds.index("tool_use")
    narration = next(d["text"] for k, d in events if k == "text")
    assert narration == "Kế hoạch: đọc, chạy, ghi."  # chunks are joined into one line, before the tools
    assert any(k == "tool_use" and d["tool"] == "TodoWrite" for k, d in events)
    assert any(k == "tool_result" and d["error"] for k, d in events)


async def test_a_resumed_session_is_loaded_without_replaying_old_messages(workspace: Path):
    comments: list[str] = []
    result = await KiroAcpRuntime(command=AGENT).run_turn(
        spec_for(workspace, "Tiếp tục", comments=comments, resume="sess-9"), Gate(workspace), _ignore
    )
    assert result.session_id == "sess-9" and "OLD" not in result.text
    assert json.loads((workspace / "agent_log.json").read_text(encoding="utf-8"))["first_prompt"] == "Tiếp tục"


async def test_a_call_that_needs_the_user_parks_the_turn(workspace: Path):
    result = await KiroAcpRuntime(command=AGENT).run_turn(spec_for(workspace, "DEFER", comments=[]), Gate(workspace), _ignore)
    assert result.status == TurnStatus.DEFERRED
    assert result.deferred is not None and result.deferred.name == "Bash" and "rm -rf" in result.deferred.input["command"]


async def test_missing_kiro_cli_is_explained(workspace: Path, monkeypatch):
    monkeypatch.setattr(kiro_acp, "find_kiro_cli", lambda: None)
    result = await KiroAcpRuntime().run_turn(spec_for(workspace, "x", comments=[]), Gate(workspace), _ignore)
    assert result.status == TurnStatus.ERROR and "kiro-cli" in (result.error or "")


async def test_the_actors_model_picks_the_runtime():
    class Probe:
        caps = KiroAcpRuntime.caps

        def __init__(self, name: str) -> None:
            self.name, self.seen = name, []

        async def run_turn(self, spec, gate, sink):
            self.seen.append(spec.model)

        async def cancel(self, run_id):
            self.seen.append(f"cancel {run_id}")

    claude, kiro = Probe("claude"), Probe("kiro")
    router = RuntimeRouter(claude, kiro)  # type: ignore[arg-type]
    for model in ("sonnet", "kiro", "kiro:claude-sonnet-4.5"):
        await router.run_turn(TurnSpec(run_id="r", prompt="", cwd=Path("."), system_prompt="", model=model, tools=[]), None, None)  # type: ignore[arg-type]
    await router.cancel("r")
    assert claude.seen == ["sonnet", "cancel r"] and kiro.seen == ["kiro", "kiro:claude-sonnet-4.5", "cancel r"]
    assert is_kiro("kiro:auto") and not is_kiro("sonnet") and not is_kiro(None)


async def _ignore(_kind: str, _data: dict[str, Any]) -> None:
    return None


async def test_a_tool_that_breaks_answers_with_an_error_and_keeps_the_server(workspace: Path):
    from actoroffice.runtimes.office_mcp import ToolServer

    async def broken(args: dict[str, Any], _ctx: Any) -> str:
        return args["missing"]

    server = ToolServer([BoundTool(ToolDef("broken", "Breaks", schema({}, required=[]), broken), ctx=None), office_tool([])], Gate(workspace))  # type: ignore[arg-type]
    reply = await server.call("broken", {})
    assert reply["isError"] and "KeyError" in reply["text"]
    assert (await server.call("ticket_comment", {"body": "vẫn chạy"}))["text"] == "Comment added."


async def test_the_first_prompt_waits_until_the_agent_listed_the_office_tools(workspace: Path):
    from actoroffice.runtimes.office_mcp import ToolServer

    server = ToolServer([office_tool([])], Gate(workspace))
    assert await server.wait_listed(0.05) is False
    server.listed.set()  # what the `list` request of the agent's MCP shim does
    assert await server.wait_listed(0.05) is True
    assert KiroAcpRuntime(command=AGENT).mcp_ready_s == 0 and KiroAcpRuntime().mcp_ready_s > 0
