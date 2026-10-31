"""Long-term memory: lessons, facts, preferences and decisions, searchable with FTS5.

Scopes: an actor sees its own memories, its position's, and the shared ones. Deletes are soft
(`deleted_at`) so "undo" and "clear" are reversible from the database.
"""

from __future__ import annotations

import re
from typing import Any

from actoroffice.core.events import EventBus
from actoroffice.domain.errors import NotFound
from actoroffice.domain.ids import utcnow
from actoroffice.domain.knowledge import Memory, MemoryKind, MemoryScope
from actoroffice.domain.org import Actor
from actoroffice.security.redact import Redactor
from actoroffice.store import Database

MAX_CONTENT = 1000
VISIBLE = (
    "deleted_at IS NULL AND ((scope = 'actor' AND actor_id = ?) OR (scope = 'position' AND position_id = ?) "
    "OR scope = 'shared')"
)


class MemoryService:
    def __init__(self, db: Database, bus: EventBus, redact: Redactor) -> None:
        self.db = db
        self.bus = bus
        self.redact = redact

    async def add(
        self,
        actor: Actor,
        content: str,
        *,
        kind: MemoryKind = MemoryKind.LESSON,
        scope: MemoryScope = MemoryScope.ACTOR,
        created_by: str,
        source_ticket_id: int | None = None,
        pinned: bool = False,
        conversation_id: str | None = None,  # where it was learned, so the 📌 notice shows up there
    ) -> Memory:
        # Never keep secrets in memory, even if an actor tries to.
        text = self.redact(content.strip())[:MAX_CONTENT]
        if not text:
            raise ValueError("empty memory")
        existing = await self.db.fetchone(
            f"SELECT id FROM memories WHERE {VISIBLE} AND lower(content) = lower(?)", (actor.id, actor.position_id, text)
        )
        if existing:
            return await self.get(int(existing["id"]))
        now = utcnow()
        async with self.db.tx() as tx:
            cur = await tx.execute(
                "INSERT INTO memories(actor_id, position_id, scope, kind, content, pinned, created_by, source_ticket_id, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    actor.id if scope == MemoryScope.ACTOR else None,
                    actor.position_id if scope == MemoryScope.POSITION else None,
                    scope.value,
                    kind.value,
                    text,
                    1 if pinned else 0,
                    created_by,
                    source_ticket_id,
                    now,
                    now,
                ),
            )
            memory_id = int(cur.lastrowid or 0)
            await tx.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (memory_id, text))
        memory = await self.get(memory_id)
        self.bus.publish("memory.saved", memory=memory.model_dump(), actor_id=actor.id, conversation_id=conversation_id)
        return memory

    async def get(self, memory_id: int) -> Memory:
        row = await self.db.fetchone("SELECT * FROM memories WHERE id = ?", (memory_id,))
        if not row:
            raise NotFound(f"Memory {memory_id} not found")
        return _memory(row)

    async def visible(self, actor: Actor, *, limit: int = 200) -> list[Memory]:
        rows = await self.db.fetchall(
            f"SELECT * FROM memories WHERE {VISIBLE} ORDER BY pinned DESC, updated_at DESC LIMIT ?",
            (actor.id, actor.position_id, limit),
        )
        return [_memory(r) for r in rows]

    async def search(self, actor: Actor, query: str, *, limit: int = 8) -> list[Memory]:
        match = _match(query)
        if not match:
            return []
        rows = await self.db.fetchall(
            f"SELECT m.* FROM memories_fts f JOIN memories m ON m.id = f.rowid WHERE memories_fts MATCH ? AND {VISIBLE} "
            "ORDER BY bm25(memories_fts) LIMIT ?",
            (match, actor.id, actor.position_id, limit),
        )
        return [_memory(r) for r in rows]

    async def context_for(self, actor: Actor, query: str, *, budget_chars: int = 2500) -> list[Memory]:
        """Pinned first, then what matches the task, then the most recent — within a size budget."""
        picked: dict[int, Memory] = {}
        pinned = [m for m in await self.visible(actor, limit=50) if m.pinned]
        for group in (pinned, await self.search(actor, query, limit=10), await self.visible(actor, limit=10)):
            for m in group:
                picked.setdefault(m.id, m)
        out, used = [], 0
        for m in picked.values():
            if used + len(m.content) > budget_chars:
                break
            out.append(m)
            used += len(m.content)
        if out:
            await self.db.execute(
                f"UPDATE memories SET use_count = use_count + 1 WHERE id IN ({','.join('?' * len(out))})", [m.id for m in out]
            )
        return out

    async def history(self, actor: Actor, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        """Search past work: tickets, comments, documents (office-wide) and the chats this actor takes part in."""
        match = _match(query)
        if not match:
            return []
        rows = await self.db.fetchall(
            "SELECT h.kind, h.ref, h.at, snippet(history_fts, 0, '«', '»', '…', 28) AS snip, "
            "t.key AS ticket_key, t.title AS ticket_title, c.title AS conv_title "
            "FROM history_fts h "
            "LEFT JOIN tickets t ON h.kind IN ('ticket', 'comment', 'doc') "
            "AND t.id = CAST(CASE WHEN h.kind = 'doc' THEN substr(h.ref, 1, instr(h.ref, ':') - 1) ELSE h.ref END AS INTEGER) "
            "LEFT JOIN conversations c ON c.id = h.conv "
            "WHERE history_fts MATCH ? AND (h.conv IS NULL OR h.conv IN "
            "(SELECT conversation_id FROM participants WHERE member_kind = 'actor' AND member_id = ?)) "
            "ORDER BY bm25(history_fts) LIMIT ?",
            (match, actor.id, limit),
        )
        return [dict(r, snip=self.redact(r["snip"] or "")) for r in rows]

    async def update(self, memory_id: int, *, content: str | None = None, pinned: bool | None = None) -> Memory:
        memory = await self.get(memory_id)
        text = self.redact(content.strip())[:MAX_CONTENT] if content is not None else memory.content
        async with self.db.tx() as tx:
            await tx.execute(
                "UPDATE memories SET content = ?, pinned = ?, updated_at = ? WHERE id = ?",
                (text, int(memory.pinned if pinned is None else pinned), utcnow(), memory_id),
            )
            await tx.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))
            await tx.execute("INSERT INTO memories_fts(rowid, content) VALUES (?, ?)", (memory_id, text))
        return await self.get(memory_id)

    async def delete(self, memory_id: int) -> None:
        async with self.db.tx() as tx:
            await tx.execute("UPDATE memories SET deleted_at = ? WHERE id = ?", (utcnow(), memory_id))
            await tx.execute("DELETE FROM memories_fts WHERE rowid = ?", (memory_id,))
        self.bus.publish("memory.deleted", memory_id=memory_id)

    async def clear(self, actor: Actor) -> int:
        """Forget everything this actor learned on its own (position/shared memories stay)."""
        rows = await self.db.fetchall("SELECT id FROM memories WHERE actor_id = ? AND deleted_at IS NULL", (actor.id,))
        for row in rows:
            await self.delete(int(row["id"]))
        return len(rows)


def _match(query: str) -> str:
    terms = [t for t in re.findall(r"\w+", query.lower()) if len(t) > 1][:12]
    return " OR ".join(f'"{t}"' for t in terms)


def _memory(row: dict[str, Any]) -> Memory:
    return Memory(
        id=row["id"],
        scope=MemoryScope(row["scope"]),
        kind=MemoryKind(row["kind"]),
        content=row["content"],
        actor_id=row["actor_id"],
        position_id=row["position_id"],
        pinned=bool(row["pinned"]),
        created_by=row["created_by"],
        source_ticket_id=row["source_ticket_id"],
        use_count=row["use_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
