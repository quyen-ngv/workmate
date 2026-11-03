"""Human in the loop: approvals and questions.

Two waiting modes:
- short wait: a live run awaits `wait()` for up to N seconds (the user is around);
- long wait: the run parks; when the user answers, a one-time grant is stored and the
  assignee is woken. On resume the CLI re-issues the deferred tool call and the policy
  hook consumes the grant (allow or deny) instead of asking again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from actoroffice.core.events import EventBus
from actoroffice.domain.errors import Conflict, NotFound
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.run import WakeSource
from actoroffice.domain.work import Interaction, InteractionKind, InteractionStatus
from actoroffice.store import Database, Tx

from .wakes import WakeService


def input_hash(tool: str, args: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([tool, args], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class InteractionService:
    def __init__(self, db: Database, wakes: WakeService, bus: EventBus) -> None:
        self.db = db
        self.wakes = wakes
        self.bus = bus
        self._waiters: dict[str, asyncio.Event] = {}

    async def create(
        self,
        kind: InteractionKind,
        payload: dict[str, Any],
        *,
        actor_id: str | None = None,
        ticket_id: int | None = None,
        run_id: str | None = None,
    ) -> Interaction:
        item = Interaction(
            id=new_id("ix"), kind=kind, actor_id=actor_id, ticket_id=ticket_id, run_id=run_id, payload=payload, created_at=utcnow()
        )
        await self.db.execute(
            "INSERT INTO interactions(id, kind, actor_id, ticket_id, run_id, payload_json, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (item.id, kind.value, actor_id, ticket_id, run_id, json.dumps(payload, ensure_ascii=False), item.created_at),
        )
        self._waiters[item.id] = asyncio.Event()
        self.bus.publish("interaction.created", interaction=item.model_dump())
        return item

    async def get(self, interaction_id: str) -> Interaction:
        row = await self.db.fetchone("SELECT * FROM interactions WHERE id = ?", (interaction_id,))
        if not row:
            raise NotFound(f"Interaction {interaction_id} not found")
        return _interaction(row)

    async def pending(self, *, ticket_id: int | None = None, run_id: str | None = None) -> list[Interaction]:
        sql, params = "SELECT * FROM interactions WHERE status = 'pending'", []
        if ticket_id is not None:
            sql += " AND ticket_id = ?"
            params.append(ticket_id)
        if run_id is not None:
            sql += " AND run_id = ?"
            params.append(run_id)
        rows = await self.db.fetchall(sql + " ORDER BY created_at", params)
        return [_interaction(r) for r in rows]

    async def colleague_questions(self, conversation_id: str) -> list[Interaction]:
        rows = await self.db.fetchall(
            "SELECT * FROM interactions WHERE kind = 'ask_colleague' AND status = 'pending' "
            "AND json_extract(payload_json, '$.conversation_id') = ? ORDER BY created_at",
            (conversation_id,),
        )
        return [_interaction(r) for r in rows]

    async def for_run(self, run_id: str) -> list[Interaction]:
        rows = await self.db.fetchall("SELECT * FROM interactions WHERE run_id = ? ORDER BY created_at", (run_id,))
        return [_interaction(r) for r in rows]

    async def wait(self, interaction_id: str, timeout: float) -> Interaction | None:
        """Wait for an answer while the run is live. None on timeout."""
        event = self._waiters.setdefault(interaction_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except TimeoutError:
            return None
        return await self.get(interaction_id)

    async def resolve(self, interaction_id: str, response: dict[str, Any], *, by: str) -> Interaction:
        item = await self.get(interaction_id)
        now = utcnow()
        async with self.db.tx() as tx:
            cur = await tx.execute(
                "UPDATE interactions SET status = 'answered', response_json = ?, resolved_by = ?, resolved_at = ? "
                "WHERE id = ? AND status = 'pending'",
                (json.dumps(response, ensure_ascii=False), by, now, interaction_id),
            )
            if cur.rowcount != 1:
                raise Conflict(f"Interaction {interaction_id} is already {item.status.value}")
            if item.kind == InteractionKind.APPROVE_TOOL and item.actor_id:
                decision = "allow" if response.get("approved") else "deny"
                await tx.execute(
                    "INSERT INTO approval_grants(actor_id, ticket_id, tool, input_hash, decision, interaction_id, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (item.actor_id, item.ticket_id, item.payload["tool"], item.payload["input_hash"], decision, item.id, now),
                )
            run = await tx.fetchone("SELECT status FROM runs WHERE id = ?", (item.run_id,)) if item.run_id else None
            # The run that asked is gone (parked or finished): wake the actor to continue.
            # A confirmed ticket proposal wakes its assignee through the new ticket instead (the result of a list of
            # proposals is a comment or chat message).
            quiet = (InteractionKind.CONFIRM_TICKET, InteractionKind.PROPOSE_TICKETS)
            if item.actor_id and item.kind not in quiet and (run is None or run["status"] != "running"):
                await self.wakes.enqueue(
                    item.actor_id,
                    WakeSource.INTERACTION,
                    ticket_id=item.ticket_id,
                    payload={"interaction_id": item.id},
                    tx=tx,
                )
        resolved = await self.get(interaction_id)
        if waiter := self._waiters.pop(interaction_id, None):
            waiter.set()
        self.bus.publish("interaction.resolved", interaction=resolved.model_dump())
        return resolved

    async def set_response(self, interaction_id: str, response: dict[str, Any]) -> Interaction:
        """Add details to an already resolved interaction (e.g. the key of the ticket it created)."""
        await self.db.execute(
            "UPDATE interactions SET response_json = ? WHERE id = ?", (json.dumps(response, ensure_ascii=False), interaction_id)
        )
        item = await self.get(interaction_id)
        self.bus.publish("interaction.resolved", interaction=item.model_dump())
        return item

    async def consume_grant(
        self, actor_id: str, ticket_id: int | None, tool: str, args: dict[str, Any], *, decision: str | None = None
    ) -> str | None:
        """Return 'allow' / 'deny' if a one-time human decision exists for exactly this call, and use it up.

        `decision` limits it to grants of that kind (the gate uses up an 'allow' grant on a call the policy already
        allows, so it cannot run the call unasked after the permission is narrowed again)."""
        digest = input_hash(tool, args)
        sql = "SELECT id, decision FROM approval_grants WHERE actor_id = ? AND ticket_id IS ? AND tool = ? AND input_hash = ? AND used = 0"
        params: tuple[Any, ...] = (actor_id, ticket_id, tool, digest)
        if decision is not None:
            sql += " AND decision = ?"
            params += (decision,)
        # read first, outside a write transaction: most calls have no grant and must not cost a write
        row = await self.db.fetchone(sql + " ORDER BY id LIMIT 1", params)
        if not row:
            return None
        cur = await self.db.execute("UPDATE approval_grants SET used = 1 WHERE id = ? AND used = 0", (row["id"],))
        if cur.rowcount != 1:  # another run used it in between
            return None
        return str(row["decision"])

    async def mark_parked(self, interaction_id: str) -> None:
        """The asking run stopped waiting; the answer will arrive through a wake instead."""
        await self.db.execute(
            "UPDATE interactions SET payload_json = json_set(payload_json, '$.parked', json('true')) WHERE id = ?",
            (interaction_id,),
        )

    async def cancel(self, interaction_id: str) -> Interaction | None:
        """Withdraw a pending card (e.g. a ticket proposal replaced by a newer one). None when it was not pending."""
        cur = await self.db.execute(
            "UPDATE interactions SET status = 'cancelled', resolved_at = ? WHERE id = ? AND status = 'pending'", (utcnow(), interaction_id)
        )
        if cur.rowcount != 1:
            return None
        item = await self.get(interaction_id)
        self.bus.publish("interaction.resolved", interaction=item.model_dump())
        return item

    async def cancel_for_closed_tickets(self) -> int:
        """Withdraw the gate cards and questions left pending on tickets closed earlier (before SI-29, closing kept them)."""
        async with self.db.tx() as tx:
            ids = await cancel_closed_ticket_cards(tx, "SELECT id FROM tickets WHERE status IN ('done', 'cancelled')", ())
        for interaction_id in ids:
            self._waiters.pop(interaction_id, None)
            self.bus.publish("interaction.resolved", interaction=(await self.get(interaction_id)).model_dump())
        return len(ids)

    async def cancel_for_run(self, run_id: str) -> None:
        await self.db.execute(
            "UPDATE interactions SET status = 'cancelled', resolved_at = ? WHERE run_id = ? AND status = 'pending' "
            "AND kind = 'approve_tool' AND json_extract(payload_json, '$.parked') IS NULL",
            (utcnow(), run_id),
        )


CLOSED_TICKET_KINDS = (InteractionKind.APPROVE_STAGE.value, InteractionKind.QUESTION.value)


async def cancel_closed_ticket_cards(tx: Tx, tickets_sql: str, params: tuple[Any, ...]) -> list[str]:
    """Cancel the pending gate cards and questions of the tickets `tickets_sql` selects; the ids of the cancelled cards.

    A closed ticket has no stage left to approve nor a run to answer; other kinds (tool approvals, proposals) stay."""
    where = f"status = 'pending' AND kind IN ({','.join('?' * len(CLOSED_TICKET_KINDS))}) AND ticket_id IN ({tickets_sql})"
    args = (*CLOSED_TICKET_KINDS, *params)
    rows = await tx.fetchall(f"SELECT id FROM interactions WHERE {where}", args)
    await tx.execute(f"UPDATE interactions SET status = 'cancelled', resolved_at = ? WHERE {where}", (utcnow(), *args))
    return [r["id"] for r in rows]


def interaction_from_row(row: dict[str, Any]) -> Interaction:
    return _interaction(row)


def _interaction(row: dict[str, Any]) -> Interaction:
    return Interaction(
        id=row["id"],
        kind=InteractionKind(row["kind"]),
        actor_id=row["actor_id"],
        ticket_id=row["ticket_id"],
        run_id=row["run_id"],
        payload=json.loads(row["payload_json"]),
        status=InteractionStatus(row["status"]),
        response=json.loads(row["response_json"]) if row["response_json"] else None,
        resolved_by=row["resolved_by"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )
