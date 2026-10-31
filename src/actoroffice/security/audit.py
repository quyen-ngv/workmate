"""Append-only audit log of mutations and tool decisions."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.domain.ids import utcnow
from actoroffice.store import Database

from .redact import Redactor


class AuditLog:
    def __init__(self, db: Database, redact: Redactor) -> None:
        self.db = db
        self.redact = redact

    async def record(
        self,
        action: str,
        outcome: str,
        *,
        actor_id: str | None = None,
        run_id: str | None = None,
        ticket_id: int | None = None,
        target: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        payload = self.redact(json.dumps(detail or {}, ensure_ascii=False, default=str))[:4000]
        await self.db.execute(
            "INSERT INTO audit_log(ts, actor_id, run_id, ticket_id, action, target, detail_json, outcome) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (utcnow(), actor_id, run_id, ticket_id, action, self.redact(target or "")[:500], payload, outcome),
        )
