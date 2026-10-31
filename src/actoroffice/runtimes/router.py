"""Each actor's model picks its runtime: "kiro" / "kiro:<model>" → Kiro CLI (ACP), anything else → Claude."""

from __future__ import annotations

from actoroffice.domain.run import TurnResult

from .base import AgentRuntime, EventSink, ToolGate, TurnSpec


def is_kiro(model: str | None) -> bool:
    return bool(model) and (model == "kiro" or str(model).startswith("kiro:"))


class RuntimeRouter:
    name = "router"

    def __init__(self, claude: AgentRuntime, kiro: AgentRuntime) -> None:
        self.claude = claude
        self.kiro = kiro
        self.caps = claude.caps

    def pick(self, model: str | None) -> AgentRuntime:
        return self.kiro if is_kiro(model) else self.claude

    async def run_turn(self, spec: TurnSpec, gate: ToolGate, sink: EventSink) -> TurnResult:
        return await self.pick(spec.model).run_turn(spec, gate, sink)

    async def cancel(self, run_id: str) -> None:
        await self.claude.cancel(run_id)
        await self.kiro.cancel(run_id)
