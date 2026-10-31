"""Runs, their events, and resumable CLI sessions."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.core.events import EventBus
from actoroffice.core.schedule import from_iso
from actoroffice.core.stats import merge
from actoroffice.domain.ids import utcnow
from actoroffice.domain.run import Run, RunKind, RunStatus, Usage
from actoroffice.engine.activity import ActivityTracker
from actoroffice.security.redact import Redactor
from actoroffice.store import Database

EVENT_TEXT_LIMIT = 8000


class RunService:
    def __init__(self, db: Database, bus: EventBus, redact: Redactor) -> None:
        self.db = db
        self.bus = bus
        self.redact = redact
        self.activity = ActivityTracker()

    async def start(self, run: Run) -> Run:
        run.status = RunStatus.RUNNING
        run.started_at = utcnow()
        await self.db.execute(
            "INSERT INTO runs(id, actor_id, kind, ticket_id, conversation_id, status, session_before, started_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (run.id, run.actor_id, run.kind.value, run.ticket_id, run.conversation_id, run.status.value, run.session_before, run.started_at),
        )
        self.bus.publish("run.started", run=run.model_dump())
        return run

    async def finish(
        self,
        run_id: str,
        status: RunStatus,
        *,
        session_after: str | None = None,
        usage: Usage | None = None,
        cost_usd: float = 0.0,
        tainted: bool = False,
        error: str | None = None,
        stats: dict[str, Any] | None = None,
    ) -> None:
        if stats is not None:
            await self.db.execute("UPDATE runs SET stats_json = ? WHERE id = ?", (json.dumps(stats, ensure_ascii=False), run_id))
        await self.db.execute(
            "UPDATE runs SET status = ?, session_after = COALESCE(?, session_after), usage_json = ?, cost_usd = ?, "
            "tainted = ?, error = ?, finished_at = ? WHERE id = ?",
            (
                status.value,
                session_after,
                (usage or Usage()).model_dump_json(),
                cost_usd,
                1 if tainted else 0,
                self.redact(error)[:2000] if error else None,
                utcnow(),
                run_id,
            ),
        )
        self.activity.forget(run_id)
        run = await self.get(run_id)
        self.bus.publish("run.finished", run_id=run_id, status=status.value, error=error, run=run.model_dump() if run else None)

    async def work_summary(self, ticket_id: int) -> dict[str, Any]:
        """Everything spent and done on a ticket and its sub-tickets: tokens, cost, time, files, lines, commands."""
        rows = await self.db.fetchall(
            "WITH RECURSIVE t(id) AS (SELECT ? UNION SELECT c.id FROM tickets c JOIN t ON c.parent_id = t.id) "
            "SELECT usage_json, cost_usd, stats_json, started_at, finished_at, actor_id FROM runs "
            "WHERE ticket_id IN (SELECT id FROM t) AND status <> 'running'",
            (ticket_id,),
        )
        total: dict[str, Any] = {"runs": len(rows), "cost_usd": 0.0, "duration_s": 0, "actors": set()}
        for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            total[key] = 0
        for row in rows:
            usage = json.loads(row["usage_json"] or "{}")
            for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                total[key] += int(usage.get(key) or 0)
            total["cost_usd"] += float(row["cost_usd"] or 0)
            total["actors"].add(row["actor_id"])
            if row["started_at"] and row["finished_at"]:
                total["duration_s"] += int((from_iso(row["finished_at"]) - from_iso(row["started_at"])).total_seconds())
            merge(total, json.loads(row["stats_json"] or "{}"))
        total["actors"] = len(total["actors"])
        total.pop("files", None)
        return total

    async def usage_summary(
        self,
        *,
        conversation_id: str | None = None,
        ticket_ids: list[int] | None = None,
        actor_id: str | None = None,
        since: str | None = None,
    ) -> dict[str, Any]:
        """Totals for a chat (its chat turns + the tickets it spawned), a set of tickets, or an actor."""
        where, params = [], []
        scope = []
        if conversation_id:
            scope.append("conversation_id = ?")
            params.append(conversation_id)
        if ticket_ids:
            scope.append(f"ticket_id IN ({','.join('?' * len(ticket_ids))})")
            params += ticket_ids
        if scope:
            where.append("(" + " OR ".join(scope) + ")")
        elif conversation_id is not None or ticket_ids is not None:
            return _empty_usage()
        if actor_id:
            where.append("actor_id = ?")
            params.append(actor_id)
        if since:
            where.append("started_at >= ?")
            params.append(since)
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS runs, "
            "COALESCE(SUM(json_extract(usage_json, '$.input_tokens')), 0) AS input_tokens, "
            "COALESCE(SUM(json_extract(usage_json, '$.output_tokens')), 0) AS output_tokens, "
            "COALESCE(SUM(json_extract(usage_json, '$.cache_read_tokens')), 0) AS cache_read_tokens, "
            "COALESCE(SUM(json_extract(usage_json, '$.cache_write_tokens')), 0) AS cache_write_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd FROM runs" + (f" WHERE {' AND '.join(where)}" if where else ""),
            params,
        )
        return dict(row) if row else _empty_usage()

    async def usage_by_actor(self, *, since: str | None = None) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT actor_id, COUNT(*) AS runs, "
            "COALESCE(SUM(json_extract(usage_json, '$.input_tokens')), 0) AS input_tokens, "
            "COALESCE(SUM(json_extract(usage_json, '$.output_tokens')), 0) AS output_tokens, "
            "COALESCE(SUM(json_extract(usage_json, '$.cache_read_tokens')), 0) AS cache_read_tokens, "
            "COALESCE(SUM(json_extract(usage_json, '$.cache_write_tokens')), 0) AS cache_write_tokens, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd FROM runs" + (" WHERE started_at >= ?" if since else "") + " GROUP BY actor_id",
            (since,) if since else (),
        )
        return [dict(r) for r in rows]

    async def by_actor(self, actor_id: str, *, limit: int = 20) -> list[Run]:
        rows = await self.db.fetchall(
            "SELECT * FROM runs WHERE actor_id = ? ORDER BY started_at DESC LIMIT ?", (actor_id, limit)
        )
        return [_run(r) for r in rows]

    async def recent(self, *, limit: int = 40, actor_id: str | None = None) -> list[dict[str, Any]]:
        """Latest runs with what they were about (ticket key/title or chat title), for the activity view."""
        rows = await self.db.fetchall(
            "SELECT r.*, t.key AS ticket_key, t.title AS ticket_title, c.title AS conversation_title FROM runs r "
            "LEFT JOIN tickets t ON t.id = r.ticket_id LEFT JOIN conversations c ON c.id = r.conversation_id "
            + ("WHERE r.actor_id = ? " if actor_id else "") + "ORDER BY r.started_at DESC LIMIT ?",
            (actor_id, limit) if actor_id else (limit,),
        )
        return [
            {**_run(r).model_dump(), "ticket_key": r["ticket_key"], "ticket_title": r["ticket_title"], "conversation_title": r["conversation_title"]}
            for r in rows
        ]

    async def chat_runs(self, conversation_id: str) -> list[Run]:
        rows = await self.db.fetchall(
            "SELECT * FROM runs WHERE conversation_id = ? ORDER BY started_at DESC LIMIT 500", (conversation_id,)
        )
        return [_run(r) for r in rows]

    async def get(self, run_id: str) -> Run | None:
        row = await self.db.fetchone("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _run(row) if row else None

    async def by_status(self, *statuses: RunStatus) -> list[Run]:
        rows = await self.db.fetchall(
            f"SELECT * FROM runs WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY started_at",
            [s.value for s in statuses],
        )
        return [_run(r) for r in rows]

    async def for_ticket(self, ticket_id: int, limit: int = 30) -> list[Run]:
        rows = await self.db.fetchall(
            "SELECT * FROM runs WHERE ticket_id = ? ORDER BY started_at DESC LIMIT ?", (ticket_id, limit)
        )
        return [_run(r) for r in rows]

    async def for_tree_running(self, root_id: int) -> list[Run]:
        rows = await self.db.fetchall(
            "WITH RECURSIVE t(id) AS (SELECT ? UNION SELECT c.id FROM tickets c JOIN t ON c.parent_id = t.id) "
            "SELECT * FROM runs WHERE status = 'running' AND ticket_id IN (SELECT id FROM t)",
            (root_id,),
        )
        return [_run(r) for r in rows]

    async def previous_start(self, actor_id: str, ticket_id: int, before_run: str) -> str | None:
        row = await self.db.fetchone(
            "SELECT started_at FROM runs WHERE actor_id = ? AND ticket_id = ? AND id <> ? AND started_at IS NOT NULL "
            "ORDER BY started_at DESC LIMIT 1",
            (actor_id, ticket_id, before_run),
        )
        return row["started_at"] if row else None

    async def event(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        """Store one run event with its per-run sequence number and its normalized activity line (if any)."""
        text = self.redact(json.dumps(payload, ensure_ascii=False, default=str))
        if len(text) > EVENT_TEXT_LIMIT:
            text = json.dumps({"truncated": text[:EVENT_TEXT_LIMIT]}, ensure_ascii=False)
        try:
            activity = self.activity.classify(run_id, kind, payload)
        except Exception:  # an odd payload must never break the run
            activity = None
        act = self.redact(activity.model_dump_json()) if activity else None
        now = utcnow()
        async with self.db.tx() as tx:
            row = await tx.fetchone("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM run_events WHERE run_id = ?", (run_id,))
            seq = int(row["seq"]) if row else 1
            await tx.execute(
                "INSERT INTO run_events(run_id, kind, payload_json, seq, activity_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, kind, text, seq, act, now),
            )
        self.bus.publish(
            "run.event", run_id=run_id, seq=seq, kind=kind, payload=json.loads(text), activity=json.loads(act) if act else None, at=now
        )

    async def events(self, run_id: str, *, since_seq: int = 0) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT seq, kind, payload_json, activity_json, created_at FROM run_events WHERE run_id = ? AND COALESCE(seq, id) > ? "
            "ORDER BY COALESCE(seq, id)",
            (run_id, since_seq),
        )
        return [
            {
                "seq": r["seq"],
                "kind": r["kind"],
                "payload": json.loads(r["payload_json"]),
                "activity": json.loads(r["activity_json"]) if r["activity_json"] else None,
                "at": r["created_at"],
            }
            for r in rows
        ]

    async def activities(self, run_id: str, *, since_seq: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        """Only the normalized activity lines of a run (what people see), oldest first."""
        rows = await self.db.fetchall(
            "SELECT seq, activity_json, created_at FROM run_events WHERE run_id = ? AND activity_json IS NOT NULL AND seq > ? "
            "ORDER BY seq LIMIT ?",
            (run_id, since_seq, limit),
        )
        return [{"seq": r["seq"], "at": r["created_at"], **json.loads(r["activity_json"])} for r in rows]

    async def last_activity(self, run_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone(
            "SELECT seq, activity_json, created_at FROM run_events WHERE run_id = ? AND activity_json IS NOT NULL "
            "AND json_extract(activity_json, '$.kind') NOT IN ('thinking', 'usage') ORDER BY seq DESC LIMIT 1",
            (run_id,),
        )
        return {"seq": row["seq"], "at": row["created_at"], **json.loads(row["activity_json"])} if row else None

    # ---- sessions: one per (actor, ticket) or (actor, conversation) ------------------------

    async def get_session(self, key: str, cwd: str) -> str | None:
        row = await self.db.fetchone("SELECT session_id, cwd FROM sessions WHERE key = ?", (key,))
        # Claude stores sessions per working directory: a different cwd cannot resume.
        return row["session_id"] if row and row["cwd"] == cwd else None

    async def save_session(self, key: str, actor_id: str, session_id: str, cwd: str) -> None:
        await self.db.execute(
            "INSERT INTO sessions(key, actor_id, session_id, cwd, updated_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET session_id = excluded.session_id, cwd = excluded.cwd, updated_at = excluded.updated_at",
            (key, actor_id, session_id, cwd, utcnow()),
        )

    async def clear_sessions(self, *, actor_id: str, key_prefix: str | None = None) -> int:
        sql, params = "DELETE FROM sessions WHERE actor_id = ?", [actor_id]
        if key_prefix:
            sql += " AND key LIKE ?"
            params.append(f"{key_prefix}%")
        cur = await self.db.execute(sql, params)
        return cur.rowcount


def _empty_usage() -> dict[str, Any]:
    return {"runs": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0}


def ticket_session_key(actor_id: str, ticket_id: int) -> str:
    return f"{actor_id}:t{ticket_id}"


def _run(row: dict[str, Any]) -> Run:
    return Run(
        id=row["id"],
        actor_id=row["actor_id"],
        kind=RunKind(row["kind"]),
        ticket_id=row["ticket_id"],
        conversation_id=row["conversation_id"],
        status=RunStatus(row["status"]),
        session_before=row["session_before"],
        session_after=row["session_after"],
        usage=Usage.model_validate_json(row["usage_json"] or "{}"),
        cost_usd=row["cost_usd"],
        tainted=bool(row["tainted"]),
        error=row["error"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        stats=json.loads(row.get("stats_json") or "{}"),
    )
