"""Wake requests: the durable queue that says "actor X should look at Y"."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.domain.ids import utcnow
from actoroffice.domain.run import WakeRequest, WakeSource
from actoroffice.store import Database, Tx


class WakeService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def enqueue(
        self,
        actor_id: str,
        source: WakeSource,
        *,
        ticket_id: int | None = None,
        conversation_id: str | None = None,
        payload: dict[str, Any] | None = None,
        not_before: str | None = None,
        tx: Tx | None = None,
    ) -> int:
        args = (actor_id, source, ticket_id, conversation_id, payload or {}, not_before)
        if tx is None:
            async with self.db.tx() as own:
                return await self._enqueue(own, *args)
        return await self._enqueue(tx, *args)

    async def _enqueue(
        self,
        tx: Tx,
        actor_id: str,
        source: WakeSource,
        ticket_id: int | None,
        conversation_id: str | None,
        payload: dict[str, Any],
        not_before: str | None,
    ) -> int:
        # Coalesce: one pending wake per (actor, ticket, conversation); keep every reason.
        existing = await tx.fetchone(
            "SELECT id, payload_json FROM wake_requests WHERE status = 'pending' AND actor_id = ? "
            "AND ticket_id IS ? AND conversation_id IS ?",
            (actor_id, ticket_id, conversation_id),
        )
        if existing:
            merged = json.loads(existing["payload_json"])
            merged.setdefault("reasons", []).append({"source": source.value, **payload})
            await tx.execute(
                "UPDATE wake_requests SET coalesced = coalesced + 1, payload_json = ? WHERE id = ?",
                (json.dumps(merged, ensure_ascii=False), existing["id"]),
            )
            return int(existing["id"])
        body = {"reasons": [{"source": source.value, **payload}]}
        cur = await tx.execute(
            "INSERT INTO wake_requests(actor_id, ticket_id, conversation_id, source, payload_json, status, not_before, created_at) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (actor_id, ticket_id, conversation_id, source.value, json.dumps(body, ensure_ascii=False), not_before, utcnow()),
        )
        return int(cur.lastrowid or 0)

    async def pending(self, limit: int = 100, *, due_only: bool = False) -> list[WakeRequest]:
        sql = "SELECT * FROM wake_requests WHERE status = 'pending'"
        params: list[Any] = []
        if due_only:
            sql += " AND (not_before IS NULL OR not_before <= ?)"
            params.append(utcnow())
        rows = await self.db.fetchall(sql + " ORDER BY id LIMIT ?", (*params, limit))
        return [_wake(r) for r in rows]

    async def pending_for(self, actor_id: str, ticket_id: int) -> WakeRequest | None:
        row = await self.db.fetchone(
            "SELECT * FROM wake_requests WHERE status = 'pending' AND actor_id = ? AND ticket_id = ?", (actor_id, ticket_id)
        )
        return _wake(row) if row else None

    async def claim(self, wake_id: int, run_id: str, tx: Tx) -> bool:
        cur = await tx.execute(
            "UPDATE wake_requests SET status = 'claimed', run_id = ? WHERE id = ? AND status = 'pending'",
            (run_id, wake_id),
        )
        return cur.rowcount == 1

    async def finish(self, run_id: str) -> None:
        await self.db.execute("UPDATE wake_requests SET status = 'done' WHERE run_id = ?", (run_id,))

    async def drop(self, wake_id: int, reason: str) -> None:
        await self.db.execute(
            "UPDATE wake_requests SET status = 'dropped', payload_json = json_set(payload_json, '$.dropped', ?) "
            "WHERE id = ?",
            (reason, wake_id),
        )

    async def drop_unchanged(self, wake: WakeRequest, reason: str) -> bool:
        """Drop `wake` only if it is still pending with the reasons it was read with (nothing coalesced or claimed since)."""
        cur = await self.db.execute(
            "UPDATE wake_requests SET status = 'dropped', payload_json = json_set(payload_json, '$.dropped', ?) "
            "WHERE id = ? AND status = 'pending' AND coalesced = ?",
            (reason, wake.id, wake.coalesced),
        )
        return cur.rowcount == 1


def only_comments(reasons: list[dict[str, Any]]) -> bool:
    """The wake only carries comments or directions from people (no change, answer, stage move or continuation)."""
    return bool(reasons) and all(r.get("steer") or r.get("comment_id") for r in reasons) and not any(r.get("change") for r in reasons)


def _wake(row: dict[str, Any]) -> WakeRequest:
    return WakeRequest(
        id=row["id"],
        actor_id=row["actor_id"],
        ticket_id=row["ticket_id"],
        conversation_id=row["conversation_id"],
        source=WakeSource(row["source"]),
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        coalesced=row["coalesced"],
        created_at=row["created_at"],
    )
