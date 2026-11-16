"""A scripted runtime: each turn runs a coroutine that drives the gate and office tools
exactly like the Claude CLI would, without spawning anything."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from actoroffice.domain.run import TurnResult, TurnStatus, Usage
from actoroffice.runtimes.base import EventSink, GateDecision, RuntimeCaps, ToolGate, TurnSpec


@dataclass
class TurnCtx:
    spec: TurnSpec
    gate: ToolGate
    sink: EventSink

    async def tool(self, name: str, args: dict[str, Any]) -> GateDecision:
        return (await self.gate.before_tool(name, args, None)).decision

    async def office(self, name: str, args: dict[str, Any]) -> str:
        decision = await self.tool(f"mcp__office__{name}", args)
        assert decision == GateDecision.ALLOW
        bound = next(t for t in self.spec.internal_tools if t.name == name)
        return await bound.call(args)

    async def call(self, name: str, args: dict[str, Any]) -> str:
        """An office tool call the way a real runtime reports it: the call and its result are events of the run, so
        what the office reads from the activity (which documents were opened, whether the code graph was asked) sees it."""
        self._calls = getattr(self, "_calls", 0) + 1
        call_id = f"call-{self._calls}"
        await self.sink("tool_use", {"tool": f"mcp__office__{name}", "input": args, "id": call_id})
        try:
            out = await self.office(name, args)
        except Exception as exc:
            await self.sink("tool_result", {"id": call_id, "text": str(exc), "error": True})
            raise
        await self.sink("tool_result", {"id": call_id, "text": out, "error": False})
        return out


async def study(ctx: TurnCtx, epic_key: str, *, docs: tuple[str, ...] = ("analysis",), targets: tuple[str, ...] = ("src/shop/limits.py:3",)) -> None:
    """What a run of a pack stage does before it writes, and the office checks from the activity (`sources_read`,
    `impact_covered`): it opens the epic's documents its own document is written from, and asks the code graph what
    its change reaches."""
    for key in docs:
        await ctx.call("doc_get", {"ticket": epic_key, "key": key})
    if targets:
        await ctx.call("code_impact", {"targets": list(targets)})


Script = Callable[[TurnCtx], Awaitable[TurnResult | None]]


@dataclass
class FakeRuntime:
    scripts: list[Script]
    name: str = "fake"
    caps: RuntimeCaps = RuntimeCaps(resume=True, defer=True, custom_system_prompt=True)
    specs: list[TurnSpec] = field(default_factory=list)
    hold: asyncio.Event | None = None  # when set, turns block until released (concurrency tests)
    active: int = 0
    max_active: int = 0

    async def run_turn(self, spec: TurnSpec, gate: ToolGate, sink: EventSink) -> TurnResult:
        self.specs.append(spec)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.hold is not None:
                await self.hold.wait()
            script = self.scripts.pop(0) if self.scripts else _noop
            result = await script(TurnCtx(spec, gate, sink))
            return result or TurnResult(
                status=TurnStatus.COMPLETED, session_id=f"sess-{spec.run_id}", usage=Usage(input_tokens=10, output_tokens=5)
            )
        finally:
            self.active -= 1

    async def cancel(self, run_id: str) -> None:
        return None


async def _noop(_ctx: TurnCtx) -> None:
    return None


def deferred(spec: TurnSpec, tool: str, args: dict[str, Any]) -> TurnResult:
    from actoroffice.domain.run import DeferredTool

    return TurnResult(
        status=TurnStatus.DEFERRED, session_id=f"sess-{spec.run_id}", deferred=DeferredTool(tool_use_id="tu1", name=tool, input=args)
    )
