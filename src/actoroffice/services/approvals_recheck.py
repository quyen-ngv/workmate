"""A wider permission approves the pending tool approvals it now allows (SI-2).

After a project's mode or "always allow" rules change, or an actor gets a new rule, each pending approve_tool card in
scope is decided again with the gate's own logic. Only ALLOW approves a card, once, like "Allow once"; ASK and DENY
leave it for a person. A card is re-checked only when the exact call is known: its context was kept and the shown
parameters still hash to the real ones (not redacted, not summarized).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import Conflict
from actoroffice.domain.work import Interaction
from actoroffice.security.policy import Decision, target_of

from .interactions import input_hash

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

AUTO_BY = "auto: permission change"


class ApprovalRecheck:
    def __init__(self, office: Office) -> None:
        self.office = office

    async def run(self, *, project_id: str | None = None, actor_id: str | None = None) -> int:
        """Re-check the pending cards of a project's tickets, or of one actor's tickets. Returns how many were approved."""
        sql = (
            "SELECT i.id FROM interactions i JOIN tickets t ON t.id = i.ticket_id "
            "WHERE i.kind = 'approve_tool' AND i.status = 'pending'"
        )
        params: list[Any] = []
        if project_id is not None:
            sql += " AND t.project_id = ?"
            params.append(project_id)
        if actor_id is not None:
            sql += " AND i.actor_id = ?"
            params.append(actor_id)
        rows = await self.office.db.fetchall(sql + " ORDER BY i.created_at", params)
        approved = 0
        for row in rows:
            try:
                approved += await self._recheck(await self.office.interactions.get(row["id"]))
            except Conflict:
                continue  # a person answered it meanwhile: their answer stands
            except Exception:  # one card must not stop the others, nor the permission change
                log.exception("could not re-check approval %s", row["id"])
        return approved

    async def _recheck(self, item: Interaction) -> bool:
        office = self.office
        payload = item.payload
        context, tool, args = payload.get("context"), payload.get("tool"), payload.get("input")
        if not isinstance(context, dict) or not item.actor_id or not isinstance(args, dict) or not tool:
            return False
        if input_hash(tool, args) != payload.get("input_hash"):
            return False  # the card shows hidden or shortened parameters: the exact call is unknown
        actor = await office.org.get_actor(item.actor_id)
        position = await office.org.get_position(actor.position_id)
        assert item.ticket_id is not None
        ticket = await office.tickets.get(item.ticket_id)
        project = await office.projects.get(ticket.project_id) if ticket.project_id else None
        policies = (await office.rules.compile(ticket=ticket, project=project, stage=ticket.stage)).policies if ticket.stage else []
        from actoroffice.engine.gate import evaluate_call

        check = await evaluate_call(
            office, actor.id, position, project,
            Path(context["workspace"]) if context.get("workspace") else None,
            [Path(d) for d in context.get("read_dirs") or []],
            bool(context.get("tainted")),
            policies, tool, args, on_ticket=True,
        )
        if check.verdict.decision != Decision.ALLOW:
            return False
        await office.interactions.resolve(item.id, {"approved": True, "auto": True}, by=AUTO_BY)
        await office.audit.record(
            f"tool:{tool}",
            "auto:allow",
            actor_id=item.actor_id,
            run_id=item.run_id,
            ticket_id=item.ticket_id,
            target=office.redact(target_of(tool, args) or tool)[:300],
            detail={"by": AUTO_BY, "interaction": item.id, "reason": check.verdict.reason},
        )
        return True
