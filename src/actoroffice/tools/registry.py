"""Runtime-agnostic tool definitions.

A tool is written once as an async function `(args, ctx) -> str`. Each runtime adapts
the bound tools to its own transport (in-process MCP for Claude, HTTP MCP later).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from actoroffice.app import Office
    from actoroffice.domain.org import Actor, Position


class ToolError(Exception):
    """Raised by a tool to return an error result to the model (not a crash)."""


@dataclass(frozen=True)
class ToolCtx:
    office: Office
    actor: Actor
    position: Position
    run_id: str
    ticket_id: int | None = None
    conversation_id: str | None = None
    workspace: Path | None = None  # the run's working directory


Handler = Callable[[dict[str, Any], ToolCtx], Awaitable[str]]


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema object
    handler: Handler
    read_only: bool = False


@dataclass(frozen=True)
class BoundTool:
    definition: ToolDef
    ctx: ToolCtx

    @property
    def name(self) -> str:
        return self.definition.name

    async def call(self, args: dict[str, Any]) -> str:
        return await self.definition.handler(args, self.ctx)


def schema(properties: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or list(properties)}
