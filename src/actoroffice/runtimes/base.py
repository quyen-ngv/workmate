"""The contract every agent runtime (Claude SDK, ACP/Kiro, …) implements."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from actoroffice.domain.run import TurnResult
from actoroffice.tools.registry import BoundTool


class GateDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    DEFER = "defer"  # park the run; a human decides later


@dataclass(frozen=True)
class GateResult:
    decision: GateDecision
    reason: str = ""


class ToolGate(Protocol):
    """Called for every tool call the model makes, before and after it runs."""

    async def before_tool(self, tool: str, args: dict[str, Any], tool_use_id: str | None) -> GateResult: ...

    async def after_tool(self, tool: str, args: dict[str, Any], result: Any) -> None: ...


EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


@dataclass
class TurnSpec:
    run_id: str
    prompt: str
    cwd: Path
    system_prompt: str
    model: str
    tools: list[str]
    coding_preset: bool = False
    resume_session_id: str | None = None
    internal_tools: list[BoundTool] = field(default_factory=list)
    add_dirs: list[Path] = field(default_factory=list)
    max_turns: int = 40
    hook_timeout_s: float = 180.0
    env: dict[str, str] = field(default_factory=dict)  # extra environment for the agent process and its tools
    show_thinking: bool = True  # stream the model's (summarized) thinking as "thinking" events


@dataclass(frozen=True)
class RuntimeCaps:
    resume: bool
    defer: bool  # can park a tool call and re-issue it on resume
    custom_system_prompt: bool


class AgentRuntime(Protocol):
    name: str
    caps: RuntimeCaps

    async def run_turn(self, spec: TurnSpec, gate: ToolGate, sink: EventSink) -> TurnResult: ...

    async def cancel(self, run_id: str) -> None: ...
