"""Memory compaction: merge duplicate, overlapping or outdated memories of an actor into a shorter list.

One small LLM call (the meta model, no tools). Old entries are soft-deleted, so a compaction can be undone from
the database; pinned memories are never touched.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.ids import new_id
from actoroffice.domain.knowledge import MemoryKind, MemoryScope
from actoroffice.domain.org import Actor
from actoroffice.domain.run import Run, RunKind, RunStatus, TurnStatus
from actoroffice.runtimes.base import GateDecision, GateResult, TurnSpec

if TYPE_CHECKING:
    from actoroffice.app import Office

MIN_TO_COMPACT = 6

PROMPT = """You maintain the long-term memory of {name}, an employee ({title}). Below are their memories as JSON.

Rewrite them as a shorter list:
- merge duplicates and near-duplicates into one precise sentence;
- when two memories conflict, keep the newer one (higher id) and drop the older;
- drop items that are vague, temporary (about one finished task only) or no longer useful;
- keep every concrete fact that is still useful (names, versions, commands, preferences, decisions);
- keep the language of the originals; one sentence per memory, at most 200 characters.

Answer with JSON only, no prose: {{"memories": [{{"kind": "lesson|fact|preference|decision", "content": "...", "from": [ids]}}]}}

Memories:
{memories}
"""


@dataclass
class NoTools:
    async def before_tool(self, tool: str, args: dict[str, Any], tool_use_id: str | None) -> GateResult:
        return GateResult(GateDecision.DENY, "No tools in memory compaction; answer with JSON only.")

    async def after_tool(self, tool: str, args: dict[str, Any], result: Any) -> None:
        return None


_locks: dict[str, asyncio.Lock] = {}


async def compact_memories(office: Office, actor: Actor) -> dict[str, int]:
    lock = _locks.setdefault(actor.id, asyncio.Lock())
    if lock.locked():
        raise OfficeError("A compaction of this employee's memory is already running")
    async with lock:
        return await _compact(office, actor)


async def _compact(office: Office, actor: Actor) -> dict[str, int]:
    memories = [m for m in await office.memory.visible(actor) if m.scope == MemoryScope.ACTOR and not m.pinned]
    if len(memories) < MIN_TO_COMPACT:
        return {"before": len(memories), "after": len(memories), "changed": 0}
    position = await office.org.get_position(actor.position_id)
    payload = json.dumps([{"id": m.id, "kind": m.kind.value, "content": m.content} for m in memories], ensure_ascii=False)
    run_id = new_id("run")
    await office.runs.start(Run(id=run_id, actor_id=actor.id, kind=RunKind.SYSTEM))
    cwd = office.settings.workspaces_dir / "_system"
    cwd.mkdir(parents=True, exist_ok=True)
    spec = TurnSpec(
        run_id=run_id,
        prompt=PROMPT.format(name=actor.name, title=position.title, memories=payload),
        cwd=cwd,
        system_prompt="You rewrite memory lists. Output JSON only.",
        model=office.settings.meta_model,
        tools=[],
        max_turns=2,
        show_thinking=False,  # a JSON rewrite on the meta model needs no visible reasoning
    )

    async def sink(kind: str, data: dict[str, Any]) -> None:
        await office.runs.event(run_id, kind, data)

    status, error = RunStatus.FAILED, None
    result = None
    try:
        result = await office.runtime.run_turn(spec, NoTools(), sink)
        if result.status != TurnStatus.COMPLETED:
            raise OfficeError(f"Compaction failed: {result.error or result.status.value}")
        items = _parse(result.text)
        known = {m.id for m in memories}
        for item in items:
            sources = item.get("from")
            if not isinstance(sources, list) or not all(str(i).isdigit() and int(i) in known for i in sources) or not sources:
                raise OfficeError("Compaction answer refers to unknown memories; nothing was changed")
            if not office.redact(str(item["content"])).strip():
                raise OfficeError("Compaction answer has an empty memory; nothing was changed")
        for memory in memories:
            await office.memory.delete(memory.id)
        for item in items:
            kind = item.get("kind") if item.get("kind") in {k.value for k in MemoryKind} else MemoryKind.FACT.value
            await office.memory.add(actor, str(item["content"]), kind=MemoryKind(kind), created_by="compaction")
        status = RunStatus.SUCCEEDED
        return {"before": len(memories), "after": len(items), "changed": len(memories)}
    except OfficeError as exc:
        error = str(exc)
        raise
    finally:
        await office.runs.finish(
            run_id, status, usage=result.usage if result else None, cost_usd=result.cost_usd if result else 0.0, error=error
        )


def _parse(text: str) -> list[dict[str, Any]]:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise OfficeError("Compaction answer was not JSON; nothing was changed")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise OfficeError("Compaction answer was not valid JSON; nothing was changed") from exc
    items = data.get("memories")
    if not isinstance(items, list) or not all(isinstance(i, dict) and str(i.get("content", "")).strip() for i in items):
        raise OfficeError("Compaction answer had no memories; nothing was changed")
    return items
