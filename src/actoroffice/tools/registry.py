"""Runtime-agnostic tool definitions.

A tool is written once as an async function `(args, ctx) -> str`. Each runtime adapts
the bound tools to its own transport (in-process MCP for Claude, HTTP MCP later).
"""

from __future__ import annotations

import importlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.ports.connection import Capability

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


EFFECTS = ("read", "write", "external")
ACTIVITIES = ("read", "search", "source", "edit", "shell", "office", "quiet")


@dataclass(frozen=True)
class ToolDef:
    """One office tool and what the rest of the office needs to know about it, so nothing has to name tools.

    - `effect`: "read", "write" (changes something inside the office) or "external" (acts outside the machine: a Jira
      comment, a pull request…; the gate applies the position's "external" rule). Left empty it follows `read_only`.
    - `activity`: how a call shows in the run's activity: "read", "search", "source" (data of a running system),
      "office" (the default), or "quiet" (not shown: its effect shows through another event). "edit" and "shell" are
      what the runtime's own tools (Edit, Bash…) are; no office tool uses them today.
    - `reads_source`: for a tool that reads a document a ticket can name as a source, the reference a call reads, as
      "<kind>:<ref>" with kind `ticket` (an office ticket key), `jira` (an issue key) or `page` (a wiki page id); None
      when the call reads no such document. It is given the call's arguments (or the fields kept of them in the
      activity) and must not do I/O. `services/sources.py` marks a source read from it.
    - `needs`: capabilities of outside systems the tool cannot work without. A run offers the tool only when ALL of
      them are available in the ticket's scope (project → team → office): a tool that needs two systems is useless
      with one. Empty: always offered.
    - `untrusted`: what it returns is content from outside the office, so a call taints the run (acting outside
      afterwards asks a person first).
    """

    name: str
    description: str
    input_schema: dict[str, Any]  # JSON Schema object
    handler: Handler
    read_only: bool = False
    effect: str = ""
    activity: str = "office"
    reads_source: Callable[[dict[str, Any]], str | None] | None = None
    needs: frozenset[Capability] = frozenset()
    untrusted: bool = False

    def __post_init__(self) -> None:
        if not self.effect:
            object.__setattr__(self, "effect", "read" if self.read_only else "write")
        if self.effect not in EFFECTS or self.activity not in ACTIVITIES:
            raise ValueError(f"tool {self.name}: unknown effect '{self.effect}' or activity '{self.activity}'")
        object.__setattr__(self, "needs", frozenset(self.needs))


def source_ref(kind: str, *keys: str) -> Callable[[dict[str, Any]], str | None]:
    """A `reads_source`: "<kind>:<value>" from the first of `keys` the call has a value for."""

    def read(args: dict[str, Any]) -> str | None:
        value = next((str(args[k]).strip() for k in keys if args.get(k)), "")
        return f"{kind}:{value}" if value else None

    return read


# Every list a run's tools are taken from (engine/executor.py, services/crosscheck.py). One place: the lookup below is
# what the gate, the activity classifier, the policy and the "read" mark of sources ask instead of naming tools.
TOOL_LISTS = (
    ("actoroffice.tools.office_tools", "OFFICE_TOOLS"),
    ("actoroffice.tools.knowledge_tools", "KNOWLEDGE_TOOLS"),
    ("actoroffice.tools.work_tools", "WORK_TOOLS"),
    ("actoroffice.tools.work_tools", "CHAT_WORK_TOOLS"),
    ("actoroffice.tools.code_tools", "CODE_TOOLS"),
    ("actoroffice.tools.source_tools", "SOURCE_TOOLS"),
    ("actoroffice.tools.proposal_tools", "PROPOSE_TICKETS"),
    ("actoroffice.tools.stage_tools", "STAGE_TOOLS"),
    ("actoroffice.tools.atlassian_tools", "ATLASSIAN_TOOLS"),
    ("actoroffice.tools.infra_tools", "INFRA_TOOLS"),
    ("actoroffice.tools.chat_tools", "CHAT_TOOLS"),
)


@cache
def catalog() -> dict[str, ToolDef]:
    """Every office tool by its bare name. A name defined in two lists (ticket_get for tickets and for chat) has the
    same metadata in both (tests/test_tool_meta.py), so the first one stands for it."""
    found: dict[str, ToolDef] = {}
    for module, attr in TOOL_LISTS:
        value = getattr(importlib.import_module(module), attr)
        for definition in [value] if isinstance(value, ToolDef) else value:
            found.setdefault(definition.name, definition)
    return found


def tool_def(name: str | None) -> ToolDef | None:
    """The definition of an office tool by its bare name (`jira_issue`, not `mcp__office__jira_issue`); None for the
    runtime's own tools and anything unknown."""
    return catalog().get(name) if name else None


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
