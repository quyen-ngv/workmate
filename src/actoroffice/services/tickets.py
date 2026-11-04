"""Tickets: single assignee, atomic checkout, comments, documents, blockers."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.core.events import EventBus
from actoroffice.domain.errors import Conflict, InvalidTransition, NotFound
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.run import WakeSource
from actoroffice.domain.work import (
    CHECKOUT_FROM,
    TRANSITIONS,
    Comment,
    Document,
    Priority,
    Ticket,
    TicketStatus,
)
from actoroffice.store import Database, Tx

from .interactions import cancel_closed_ticket_cards, interaction_from_row
from .wakes import WakeService

S = TicketStatus
CLOSED = (S.DONE.value, S.CANCELLED.value)


class TicketService:
    def __init__(self, db: Database, wakes: WakeService, bus: EventBus, key_prefix: str = "AO") -> None:
        self.db = db
        self.wakes = wakes
        self.bus = bus
        self.key_prefix = key_prefix

    # ---- tickets ----------------------------------------------------------------------------

    async def create(
        self,
        title: str,
        *,
        description: str = "",
        type: str = "task",
        project_id: str | None = None,
        priority: Priority = Priority.MEDIUM,
        assignee_actor_id: str | None = None,
        parent_id: int | None = None,
        created_by: str = "user",
        status: TicketStatus = S.TODO,
        blocked_by: list[int] | None = None,
        origin_conversation_id: str | None = None,
        due_at: str | None = None,
    ) -> Ticket:
        now = utcnow()
        async with self.db.tx() as tx:
            if parent_id is not None:
                parent = await tx.fetchone("SELECT origin_conversation_id, project_id FROM tickets WHERE id = ?", (parent_id,))
                if parent:
                    origin_conversation_id = origin_conversation_id or parent["origin_conversation_id"]
                    project_id = project_id or parent["project_id"]
            if project_id is None and origin_conversation_id:
                conv = await tx.fetchone("SELECT project_id FROM conversations WHERE id = ?", (origin_conversation_id,))
                project_id = conv["project_id"] if conv else None
            cur = await tx.execute(
                "INSERT INTO tickets(key, project_id, type, title, description, status, priority, "
                "assignee_actor_id, parent_id, created_by, origin_conversation_id, due_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    new_id("tmp"),
                    project_id,
                    type,
                    title,
                    description,
                    status.value,
                    priority.value,
                    assignee_actor_id,
                    parent_id,
                    created_by,
                    origin_conversation_id,
                    due_at,
                    now,
                    now,
                ),
            )
            ticket_id = int(cur.lastrowid or 0)
            await tx.execute("UPDATE tickets SET key = ? WHERE id = ?", (f"{self.key_prefix}-{ticket_id}", ticket_id))
            for blocker in blocked_by or []:
                await tx.execute("INSERT INTO ticket_blockers(ticket_id, blocker_id) VALUES (?, ?)", (ticket_id, blocker))
            if assignee_actor_id and status in (S.TODO, S.IN_REVIEW):
                await self.wakes.enqueue(assignee_actor_id, WakeSource.ASSIGNMENT, ticket_id=ticket_id, tx=tx)
            ticket = await self._get(tx, ticket_id)
        self.bus.publish("ticket.created", ticket=ticket.model_dump())
        return ticket

    async def get(self, ref: int | str) -> Ticket:
        if isinstance(ref, str) and not ref.isdigit():
            row = await self.db.fetchone("SELECT * FROM tickets WHERE key = ? COLLATE NOCASE", (ref,))
        else:
            row = await self.db.fetchone("SELECT * FROM tickets WHERE id = ?", (int(ref),))
        if not row:
            raise NotFound(f"Ticket {ref} not found")
        return Ticket.model_validate(row)

    async def list(
        self,
        *,
        status: list[TicketStatus] | None = None,
        assignee_actor_id: str | None = None,
        parent_id: int | None = None,
        origin_conversation_id: str | None = None,
        project_id: str | None = None,
        limit: int = 100,
    ) -> list[Ticket]:
        where, params = [], []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        if origin_conversation_id:
            where.append("origin_conversation_id = ?")
            params.append(origin_conversation_id)
        if status:
            where.append(f"status IN ({','.join('?' * len(status))})")
            params += [s.value for s in status]
        if assignee_actor_id:
            where.append("assignee_actor_id = ?")
            params.append(assignee_actor_id)
        if parent_id is not None:
            where.append("parent_id = ?")
            params.append(parent_id)
        sql = "SELECT * FROM tickets" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY id DESC LIMIT ?"
        rows = await self.db.fetchall(sql, (*params, limit))
        return [Ticket.model_validate(r) for r in rows]

    async def root_of(self, ref: int | str) -> Ticket:
        """The top of the ticket tree (the feature or request everything else hangs under)."""
        ticket = await self.get(ref)
        seen = {ticket.id}
        while ticket.parent_id is not None and ticket.parent_id not in seen:
            seen.add(ticket.parent_id)
            ticket = await self.get(ticket.parent_id)
        return ticket

    async def tree(self, ref: int | str) -> list[Ticket]:
        """Every ticket under (and including) the root of `ref`'s tree, top-down."""
        root = await self.root_of(ref)
        rows = await self.db.fetchall(
            "WITH RECURSIVE t(id) AS (SELECT ? UNION SELECT c.id FROM tickets c JOIN t ON c.parent_id = t.id) "
            "SELECT * FROM tickets WHERE id IN (SELECT id FROM t) ORDER BY id",
            (root.id,),
        )
        return [Ticket.model_validate(r) for r in rows]

    async def set_due(self, ref: int | str, due_at: str | None) -> Ticket:
        ticket = await self.get(ref)
        await self.db.execute("UPDATE tickets SET due_at = ?, updated_at = ? WHERE id = ?", (due_at, utcnow(), ticket.id))
        updated = await self.get(ticket.id)
        self.bus.publish("ticket.updated", ticket=updated.model_dump())
        return updated

    async def change_notice(
        self,
        source: int | str,
        summary: str,
        *,
        by: str,
        targets: list[int | str] | None = None,
        reopen_done: bool = False,
        skip: int | None = None,
    ) -> dict[str, list[str]]:
        """A requirement changed: record it on every affected ticket and wake whoever owns open ones.

        Default targets: the whole tree of `source`. Work in progress sees the change at its next tool call
        (OfficeGate), queued work sees it in its next prompt, done work is reopened only when asked."""
        origin = await self.get(source)
        tickets = [await self.get(t) for t in targets] if targets else await self.tree(origin.id)
        body = f"⚠️ Thay đổi yêu cầu (ghi bởi {by}, từ {origin.key}): {summary.strip()}"
        result: dict[str, list[str]] = {"notified": [], "reopened": [], "skipped": []}
        for ticket in tickets:
            if ticket.id == skip or ticket.status == S.CANCELLED:
                result["skipped"].append(ticket.key)
                continue
            async with self.db.tx() as tx:
                await self._insert_comment(tx, ticket.id, "change", by, body)
                reopen = ticket.status == S.DONE and reopen_done
                if reopen:
                    await tx.execute("UPDATE tickets SET status = 'todo', updated_at = ? WHERE id = ?", (utcnow(), ticket.id))
                    await self._system_comment(tx, ticket.id, f"done → todo by {by} (requirement change)")
                if ticket.assignee_actor_id and (ticket.status.value not in CLOSED or reopen):
                    await self.wakes.enqueue(
                        ticket.assignee_actor_id, WakeSource.COMMENT, ticket_id=ticket.id, payload={"change": True}, tx=tx
                    )
                updated = await self._get(tx, ticket.id)
            if ticket.status == S.DONE and not reopen_done:
                result["skipped"].append(f"{ticket.key} (done)")
            else:
                result["reopened" if ticket.status == S.DONE else "notified"].append(ticket.key)
            self.bus.publish("ticket.comment", ticket_id=ticket.id, comment={"author_kind": "change", "body": body})
            if updated.status != ticket.status:
                self.bus.publish("ticket.updated", ticket=updated.model_dump(), previous=ticket.status.value, by=by, comment=None)
        self.bus.publish("ticket.change", source=origin.key, summary=summary, by=by, result=result)
        return result

    async def last_comment_id(self, ticket_id: int, author_kind: str) -> int:
        row = await self.db.fetchone(
            "SELECT COALESCE(MAX(id), 0) AS id FROM comments WHERE ticket_id = ? AND author_kind = ?", (ticket_id, author_kind)
        )
        return int(row["id"]) if row else 0

    async def comments_after(self, ticket_id: int, author_kind: str, after_id: int) -> list[Comment]:
        rows = await self.db.fetchall(
            "SELECT * FROM comments WHERE ticket_id = ? AND author_kind = ? AND id > ? ORDER BY id", (ticket_id, author_kind, after_id)
        )
        return [Comment.model_validate(r) for r in rows]

    async def changes_after(self, ticket_id: int, after_id: int) -> list[Comment]:
        rows = await self.db.fetchall(
            "SELECT * FROM comments WHERE ticket_id = ? AND author_kind = 'change' AND id > ? ORDER BY id", (ticket_id, after_id)
        )
        return [Comment.model_validate(r) for r in rows]

    async def assign(self, ref: int | str, actor_id: str | None, *, by: str, user: bool = False) -> Ticket:
        ticket = await self.get(ref)
        async with self.db.tx() as tx:
            row = await tx.fetchone("SELECT checkout_run_id FROM tickets WHERE id = ?", (ticket.id,))
            if row and row["checkout_run_id"]:
                raise Conflict(f"{ticket.key} is being worked on; stop that run before reassigning")
            await tx.execute(
                "UPDATE tickets SET assignee_actor_id = ?, assignee_user = ?, updated_at = ? WHERE id = ?",
                (actor_id, 1 if user else 0, utcnow(), ticket.id),
            )
            await self._system_comment(tx, ticket.id, f"Assigned to {actor_id or ('user' if user else 'nobody')} by {by}")
            if actor_id and ticket.status.value not in CLOSED:
                await self.wakes.enqueue(actor_id, WakeSource.ASSIGNMENT, ticket_id=ticket.id, tx=tx)
            updated = await self._get(tx, ticket.id)
        self.bus.publish("ticket.updated", ticket=updated.model_dump())
        return updated

    async def transition(
        self,
        ref: int | str,
        to: TicketStatus,
        *,
        by: str,
        run_id: str | None = None,
        comment: str | None = None,
    ) -> Ticket:
        ticket = await self.get(ref)
        if to == ticket.status:
            return ticket
        if to not in TRANSITIONS[ticket.status]:
            raise InvalidTransition(f"{ticket.key}: {ticket.status.value} → {to.value} is not allowed")
        async with self.db.tx() as tx:
            row = await tx.fetchone("SELECT checkout_run_id FROM tickets WHERE id = ?", (ticket.id,))
            owner = row["checkout_run_id"] if row else None
            if owner and run_id and owner != run_id:
                raise Conflict(f"{ticket.key} is checked out by another run")
            await tx.execute(
                "UPDATE tickets SET status = ?, checkout_run_id = NULL, updated_at = ? WHERE id = ?",
                (to.value, utcnow(), ticket.id),
            )
            if comment:
                await self._insert_comment(tx, ticket.id, "actor" if run_id else "user", by, comment)
            await self._system_comment(tx, ticket.id, f"{ticket.status.value} → {to.value} by {by}")
            cancelled: list[str] = []
            if to == S.DONE or to == S.CANCELLED:
                await self._unblock_dependents(tx, ticket.id)
                # a held comment wake and an open gate card or question would outlive the ticket (SI-29)
                await self.wakes.drop_for_ticket(ticket.id, f"ticket {to.value}", tx)
                cancelled = await cancel_closed_ticket_cards(tx, "?", (ticket.id,))
            updated = await self._get(tx, ticket.id)
        self.bus.publish("ticket.updated", ticket=updated.model_dump(), previous=ticket.status.value, by=by, comment=comment)
        for interaction_id in cancelled:
            row = await self.db.fetchone("SELECT * FROM interactions WHERE id = ?", (interaction_id,))
            if row:
                self.bus.publish("interaction.resolved", interaction=interaction_from_row(row).model_dump())
        return updated

    async def checkout(self, ref: int | str, actor_id: str, run_id: str, *, ignore_blockers: bool = False) -> Ticket:
        """Atomically take ownership for one run. A 409 means someone else owns it: never retry.

        ignore_blockers: a requirement change must reach the owner of a blocked ticket too (a lead waiting
        for its developers re-plans); that run ends by setting the ticket blocked again."""
        ticket = await self.get(ref)
        async with self.db.tx() as tx:
            if ticket.assignee_actor_id != actor_id:
                raise Conflict(f"{ticket.key} is not assigned to this actor")
            open_blockers = await self._open_blockers(tx, ticket.id)
            if open_blockers and not ignore_blockers:
                raise Conflict(f"{ticket.key} is blocked by {', '.join(open_blockers)}")
            cur = await tx.execute(
                "UPDATE tickets SET status = 'in_progress', checkout_run_id = ?, updated_at = ? "
                "WHERE id = ? AND assignee_actor_id = ? AND ("
                f"  (checkout_run_id IS NULL AND status IN ({','.join('?' * len(CHECKOUT_FROM))}))"
                "   OR (status = 'in_progress' AND (checkout_run_id IS NULL OR checkout_run_id = ?)))",
                (run_id, utcnow(), ticket.id, actor_id, *[s.value for s in CHECKOUT_FROM], run_id),
            )
            if cur.rowcount != 1:
                raise Conflict(f"{ticket.key} cannot be checked out (status {ticket.status.value}, owned by another run)")
            updated = await self._get(tx, ticket.id)
        self.bus.publish("ticket.updated", ticket=updated.model_dump(), previous=ticket.status.value, by=actor_id)
        return updated

    async def release(self, ticket_id: int, run_id: str) -> None:
        await self.db.execute(
            "UPDATE tickets SET checkout_run_id = NULL, updated_at = ? WHERE id = ? AND checkout_run_id = ?",
            (utcnow(), ticket_id, run_id),
        )

    # ---- comments & documents ---------------------------------------------------------------

    async def add_comment(self, ref: int | str, body: str, *, author_kind: str, author_id: str | None) -> Comment:
        ticket = await self.get(ref)
        async with self.db.tx() as tx:
            comment = await self._insert_comment(tx, ticket.id, author_kind, author_id, body)
            # A human comment on an agent ticket wakes the assignee (like Paperclip's comment wake).
            if author_kind == "user" and ticket.assignee_actor_id and ticket.status.value not in CLOSED:
                await self.wakes.enqueue(
                    ticket.assignee_actor_id, WakeSource.COMMENT, ticket_id=ticket.id, payload={"comment_id": comment.id}, tx=tx
                )
        self.bus.publish("ticket.comment", ticket_id=ticket.id, comment=comment.model_dump())
        return comment

    async def comments(self, ticket_id: int, *, after_id: int = 0, limit: int = 50) -> list[Comment]:
        rows = await self.db.fetchall(
            "SELECT * FROM comments WHERE ticket_id = ? AND id > ? ORDER BY id LIMIT ?", (ticket_id, after_id, limit)
        )
        return [Comment.model_validate(r) for r in rows]

    async def put_document(
        self, ticket_id: int, key: str, body: str, *, author: str, base_revision: int | None
    ) -> Document:
        """Optimistic concurrency: pass the revision you read (None when creating)."""
        now = utcnow()
        async with self.db.tx() as tx:
            current = await tx.fetchone("SELECT revision FROM documents WHERE ticket_id = ? AND key = ?", (ticket_id, key))
            current_rev = current["revision"] if current else None
            if current_rev != base_revision:
                raise Conflict(f"Document '{key}' is at revision {current_rev}, not {base_revision}; re-read before writing")
            revision = (current_rev or 0) + 1
            await tx.execute(
                "INSERT INTO documents(ticket_id, key, revision, body, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(ticket_id, key) DO UPDATE SET revision = excluded.revision, body = excluded.body, "
                "updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (ticket_id, key, revision, body, author, now),
            )
            await tx.execute(
                "INSERT INTO document_revisions(ticket_id, key, revision, body, author, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ticket_id, key, revision, body, author, now),
            )
        doc = Document(ticket_id=ticket_id, key=key, revision=revision, body=body, updated_by=author, updated_at=now)
        self.bus.publish("ticket.document", ticket_id=ticket_id, key=key, revision=revision)
        return doc

    async def get_document(self, ticket_id: int, key: str) -> Document | None:
        row = await self.db.fetchone("SELECT * FROM documents WHERE ticket_id = ? AND key = ?", (ticket_id, key))
        return Document.model_validate(row) if row else None

    async def list_documents(self, ticket_id: int) -> list[Document]:
        rows = await self.db.fetchall("SELECT * FROM documents WHERE ticket_id = ? ORDER BY key", (ticket_id,))
        return [Document.model_validate(r) for r in rows]

    async def add_work_product(self, ticket_id: int, kind: str, ref: str, metadata: dict[str, Any] | None = None) -> None:
        await self.db.execute(
            "INSERT INTO work_products(ticket_id, kind, ref, metadata_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, kind, ref, json.dumps(metadata or {}, ensure_ascii=False), utcnow()),
        )

    async def work_products(self, ticket_id: int) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM work_products WHERE ticket_id = ? ORDER BY id", (ticket_id,))
        return [{**r, "metadata": json.loads(r.pop("metadata_json"))} for r in rows]

    # ---- blockers ---------------------------------------------------------------------------

    async def add_blocker(self, ticket_id: int, blocker_id: int) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO ticket_blockers(ticket_id, blocker_id) VALUES (?, ?)", (ticket_id, blocker_id)
        )

    async def open_blockers(self, ticket_id: int) -> list[str]:
        async with self.db.tx() as tx:
            return await self._open_blockers(tx, ticket_id)

    async def queue_blockers(self, ticket_id: int) -> list[str]:
        """The open tickets this one waits for only because its project runs one ticket at a time (SI-31)."""
        async with self.db.tx() as tx:
            return await self._open_blockers(tx, ticket_id, kind="queue")

    async def _open_blockers(self, tx: Tx, ticket_id: int, *, kind: str | None = None) -> list[str]:
        rows = await tx.fetchall(
            "SELECT t.key FROM ticket_blockers b JOIN tickets t ON t.id = b.blocker_id "
            f"WHERE b.ticket_id = ? AND t.status NOT IN ({','.join('?' * len(CLOSED))})" + (" AND b.kind = ?" if kind else "") + " ORDER BY t.id",
            (ticket_id, *CLOSED, *([kind] if kind else [])),
        )
        return [r["key"] for r in rows]

    async def queue_behind(self, tx: Tx, ticket_id: int, project_id: str) -> str | None:
        """Make a root ticket that starts its workflow wait for the project's ticket that started last (SI-31).

        A ticket "holds the project" when it is a root ticket on a workflow and still open, waiting ones included,
        so the waiting tickets form a chain. Returns the key waited for, or None when the project is free."""
        front = await tx.fetchone(
            "SELECT t.id, t.key FROM tickets t LEFT JOIN stage_runs r ON r.ticket_id = t.id "
            f"WHERE t.project_id = ? AND t.id != ? AND t.parent_id IS NULL AND t.stage IS NOT NULL AND t.status NOT IN ({','.join('?' * len(CLOSED))}) "
            "GROUP BY t.id ORDER BY COALESCE(MIN(r.id), 0) DESC, t.id DESC LIMIT 1",
            (project_id, ticket_id, *CLOSED),
        )
        if front is None:
            return None
        await tx.execute("INSERT OR IGNORE INTO ticket_blockers(ticket_id, blocker_id, kind) VALUES (?, ?, 'queue')", (ticket_id, front["id"]))
        await tx.execute(
            f"UPDATE tickets SET status = 'blocked', updated_at = ? WHERE id = ? AND status NOT IN ({','.join('?' * len(CLOSED))})",
            (utcnow(), ticket_id, *CLOSED),
        )
        await self._system_comment(tx, ticket_id, f"Chờ {front['key']} xong (dự án chạy từng ticket một)")
        return str(front["key"])

    async def release_queue(self, project_id: str) -> int:
        """The project stopped running one ticket at a time: drop its queue waits; real dependencies stay (SI-31).
        Returns how many tickets were released."""
        async with self.db.tx() as tx:
            rows = await tx.fetchall(
                "SELECT DISTINCT t.id, t.status, t.assignee_actor_id FROM ticket_blockers b JOIN tickets t ON t.id = b.ticket_id "
                "WHERE b.kind = 'queue' AND t.project_id = ?",
                (project_id,),
            )
            await tx.execute(
                "DELETE FROM ticket_blockers WHERE kind = 'queue' AND ticket_id IN (SELECT id FROM tickets WHERE project_id = ?)", (project_id,)
            )
            released = [row["id"] for row in rows if await self._release(tx, row)]
            updated = [await self._get(tx, ticket_id) for ticket_id in released]
        for ticket in updated:
            self.bus.publish("ticket.updated", ticket=ticket.model_dump(), previous=S.BLOCKED.value, by="office")
        return len(released)

    async def _unblock_dependents(self, tx: Tx, closed_id: int) -> None:
        # A waiting ticket closed in the middle of a project queue: the tickets behind it wait for its own front now.
        fronts = await tx.fetchall(
            "SELECT b.blocker_id FROM ticket_blockers b JOIN tickets t ON t.id = b.blocker_id "
            f"WHERE b.ticket_id = ? AND b.kind = 'queue' AND t.status NOT IN ({','.join('?' * len(CLOSED))})",
            (closed_id, *CLOSED),
        )
        dependents = await tx.fetchall(
            "SELECT t.id, t.status, t.assignee_actor_id, b.kind FROM ticket_blockers b JOIN tickets t ON t.id = b.ticket_id "
            "WHERE b.blocker_id = ?",
            (closed_id,),
        )
        for dep in dependents:
            if dep["kind"] == "queue" and fronts and dep["status"] not in CLOSED:
                for front in fronts:
                    await tx.execute(
                        "INSERT OR IGNORE INTO ticket_blockers(ticket_id, blocker_id, kind) VALUES (?, ?, 'queue')", (dep["id"], front["blocker_id"])
                    )
                keys = await self._open_blockers(tx, dep["id"], kind="queue")
                await self._system_comment(tx, dep["id"], f"Chờ {', '.join(keys)} xong (ticket phía trước đã đóng)")
            await self._release(tx, dep)

    async def _release(self, tx: Tx, dep: dict[str, Any]) -> bool:
        """A ticket whose blockers are all closed: blocked → todo and wake its assignee. True when it moved to todo."""
        if await self._open_blockers(tx, dep["id"]):
            return False
        moved = dep["status"] == S.BLOCKED.value
        if moved:
            await tx.execute("UPDATE tickets SET status = 'todo', updated_at = ? WHERE id = ?", (utcnow(), dep["id"]))
            await self._system_comment(tx, dep["id"], "All blockers resolved → todo")
        if dep["assignee_actor_id"] and dep["status"] not in CLOSED:
            await self.wakes.enqueue(dep["assignee_actor_id"], WakeSource.BLOCKERS_RESOLVED, ticket_id=dep["id"], tx=tx)
        return moved

    # ---- helpers ----------------------------------------------------------------------------

    async def _get(self, tx: Tx, ticket_id: int) -> Ticket:
        row = await tx.fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
        if not row:
            raise NotFound(f"Ticket {ticket_id} not found")
        return Ticket.model_validate(row)

    async def _insert_comment(self, tx: Tx, ticket_id: int, author_kind: str, author_id: str | None, body: str) -> Comment:
        now = utcnow()
        cur = await tx.execute(
            "INSERT INTO comments(ticket_id, author_kind, author_id, body, created_at) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, author_kind, author_id, body, now),
        )
        return Comment(id=int(cur.lastrowid or 0), ticket_id=ticket_id, author_kind=author_kind, author_id=author_id, body=body, created_at=now)

    async def _system_comment(self, tx: Tx, ticket_id: int, body: str) -> None:
        await self._insert_comment(tx, ticket_id, "system", None, body)
