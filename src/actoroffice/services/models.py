"""Which LLM a ticket's run uses: per task, per stage, and a switch that waits for a run or stage boundary.

Priority, high to low: stage model > task model > team model > workspace `ai_model` > actor > position > default.
A switch never interrupts a run: it is stored on the ticket and applied when the next run starts (scope "turn")
or when the next stage (attempt) starts (scope "stage")."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.ids import utcnow
from actoroffice.domain.work import Ticket
from actoroffice.runtimes.router import is_kiro

if TYPE_CHECKING:
    from actoroffice.app import Office

CLAUDE_MODELS = ("haiku", "sonnet", "opus")
SCOPES = {"turn": "từ lượt chạy kế tiếp", "stage": "từ giai đoạn kế tiếp"}


class ModelError(OfficeError):
    code = 422


def validate_model(value: str | None) -> str:
    """haiku | sonnet | opus | kiro | kiro:<id>, trimmed. Anything else is refused (Vietnamese message)."""
    model = (value or "").strip()
    if model in (*CLAUDE_MODELS, "kiro") or (model.startswith("kiro:") and model[5:].strip() == model[5:] and len(model) > 5):
        return model
    if not model:
        raise ModelError("Model không được để trống (haiku, sonnet, opus, kiro hoặc kiro:<id>).")
    raise ModelError(f"Model '{model}' không hợp lệ: chọn haiku, sonnet, opus, kiro hoặc kiro:<id>.")


def provider(model: str) -> str:
    return "kiro" if is_kiro(model) else "claude"


def resolve_model(
    *,
    stage_model: str | None = None,
    task_model: str | None = None,
    team_model: str | None = None,
    ai_model: str | None = None,
    actor_model: str | None = None,
    position_model: str | None = None,
    default_model: str,
) -> str:
    return stage_model or task_model or team_model or ai_model or actor_model or position_model or default_model


class ModelService:
    def __init__(self, office: Office) -> None:
        self.office = office

    # ---- resolution -----------------------------------------------------------------------------------

    async def effective(self, ticket: Ticket, actor: Any, position: Any) -> str:
        """The model the next run of this ticket would use. Tickets outside the stage flow keep the old
        behaviour (no team/workspace model) but still honour the task model."""
        office = self.office
        staged = bool(ticket.stage)
        team_model = None
        if staged and ticket.team_id:
            team_model = (await office.teams.get_team(ticket.team_id)).settings.model
        return resolve_model(
            stage_model=ticket.stage_models.get(ticket.stage or "") if staged else None,
            task_model=ticket.model,
            team_model=team_model,
            ai_model=office.settings.ai_model if staged else None,
            actor_model=getattr(actor, "model", None),
            position_model=getattr(position, "model", None),
            default_model=office.settings.default_model,
        )

    async def _effective_for(self, ticket: Ticket) -> str:
        actor = position = None
        if ticket.assignee_actor_id:
            try:
                actor = await self.office.org.get_actor(ticket.assignee_actor_id)
                position = await self.office.org.get_position(actor.position_id)
            except OfficeError:
                actor = position = None
        return await self.effective(ticket, actor, position)

    # ---- setting ----------------------------------------------------------------------------------------

    async def _check_stage(self, ticket: Ticket, stage: str) -> str:
        stage = stage.strip()
        if ticket.stage_template:
            ids = [s.id for s in (await self.office.stages.template_of(ticket)).stages]
            if stage not in ids:
                raise ModelError(f"Không có giai đoạn '{stage}' (có: {', '.join(ids)}).")
        return stage

    async def set_task_model(self, ref: int | str, model: str | None, *, by: str) -> Ticket:
        """Set (or clear, with None) the task model. Takes effect from the next run; a running run is never touched."""
        ticket = await self.office.tickets.get(ref)
        value = validate_model(model) if model is not None else None
        old = await self._effective_for(ticket)
        await self.office.db.execute("UPDATE tickets SET model = ?, updated_at = ? WHERE id = ?", (value, utcnow(), ticket.id))
        updated = await self.office.tickets.get(ticket.id)
        await self._audit(updated, f"Model của task: {ticket.model or '(không đặt)'} → {value or '(không đặt)'}", old, await self.office.stages._name(by), "đặt trực tiếp")
        return updated

    async def set_stage_model(self, ref: int | str, stage: str, model: str | None, *, by: str) -> Ticket:
        """Set (or clear) the model of one stage. For a finished stage it only matters if the stage runs again."""
        office = self.office
        ticket = await office.tickets.get(ref)
        stage = await self._check_stage(ticket, stage)
        value = validate_model(model) if model is not None else None
        old = await self._effective_for(ticket)
        models = dict(ticket.stage_models)
        before = models.get(stage)
        if value is None:
            models.pop(stage, None)
        else:
            models[stage] = value
        await office.db.execute(
            "UPDATE tickets SET stage_models = ?, updated_at = ? WHERE id = ?", (json.dumps(models) if models else None, utcnow(), ticket.id)
        )
        updated = await office.tickets.get(ticket.id)
        await self._audit(updated, f"Model của giai đoạn {stage}: {before or '(không đặt)'} → {value or '(không đặt)'}", old, await office.stages._name(by), "đặt trực tiếp")
        return updated

    # ---- switching while running ------------------------------------------------------------------------

    async def request_switch(self, ref: int | str, model: str, *, scope: str, by: str, stage: str | None = None) -> Ticket:
        """Ask for a model change that waits for a boundary. One request at a time; a new one replaces the old."""
        office = self.office
        if scope not in SCOPES:
            raise ModelError("Chọn mốc áp dụng: 'turn' (từ lượt chạy kế tiếp) hoặc 'stage' (từ giai đoạn kế tiếp).")
        ticket = await office.tickets.get(ref)
        value = validate_model(model)
        if stage:
            stage = await self._check_stage(ticket, stage)
        current = await office.stages.current_run(ticket) if ticket.stage else None
        pending = {
            "model": value,
            "scope": scope,
            "stage": stage or None,
            "by": by,
            "by_name": await office.stages._name(by),
            "at": utcnow(),
            "stage_run_id": current.id if current else None,
        }
        await office.db.execute(
            "UPDATE tickets SET pending_model = ?, updated_at = ? WHERE id = ?", (json.dumps(pending, ensure_ascii=False), utcnow(), ticket.id)
        )
        updated = await office.tickets.get(ticket.id)
        await office.tickets.add_comment(
            ticket.id,
            f"🔀 {pending['by_name']} yêu cầu đổi model sang {value} ({SCOPES[scope]}); việc đang chạy không bị ngắt.",
            author_kind="system",
            author_id=None,
        )
        office.bus.publish("ticket.updated", ticket=updated.model_dump())
        return updated

    async def cancel_switch(self, ref: int | str, *, by: str) -> Ticket:
        office = self.office
        ticket = await office.tickets.get(ref)
        if not ticket.pending_model:
            raise ModelError("Không có yêu cầu đổi model nào đang chờ.")
        await office.db.execute("UPDATE tickets SET pending_model = NULL, updated_at = ? WHERE id = ?", (utcnow(), ticket.id))
        await office.tickets.add_comment(
            ticket.id,
            f"↩️ {await office.stages._name(by)} huỷ yêu cầu đổi model sang {ticket.pending_model['model']}; giữ model hiện tại.",
            author_kind="system",
            author_id=None,
        )
        updated = await office.tickets.get(ticket.id)
        office.bus.publish("ticket.updated", ticket=updated.model_dump())
        return updated

    async def apply_due(self, ticket: Ticket, stage_run: Any, actor: Any, position: Any) -> Ticket:
        """Called when a run starts: apply the waiting switch if its boundary has passed. Idempotent."""
        pend = ticket.pending_model
        if not pend:
            return ticket
        if pend["scope"] == "stage" and stage_run is not None and pend.get("stage_run_id") == stage_run.id:
            return ticket  # still inside the stage that was running when the switch was asked for
        office = self.office
        old = await self.effective(ticket, actor, position)
        async with office.db.tx() as tx:
            row = await tx.fetchone("SELECT stage_models FROM tickets WHERE id = ? AND pending_model IS NOT NULL", (ticket.id,))
            if row is None:
                return await office.tickets.get(ticket.id)  # another run applied it first
            if pend.get("stage"):
                models = dict(json.loads(row["stage_models"]) if row["stage_models"] else {})
                models[pend["stage"]] = pend["model"]
                await tx.execute(
                    "UPDATE tickets SET stage_models = ?, pending_model = NULL, updated_at = ? WHERE id = ?",
                    (json.dumps(models), utcnow(), ticket.id),
                )
            else:
                await tx.execute(
                    "UPDATE tickets SET model = ?, pending_model = NULL, updated_at = ? WHERE id = ?", (pend["model"], utcnow(), ticket.id)
                )
        updated = await office.tickets.get(ticket.id)
        target = f"giai đoạn {pend['stage']}" if pend.get("stage") else "task"
        await self._audit(
            updated,
            f"Model của {target} → {pend['model']} (yêu cầu bởi {pend.get('by_name') or pend.get('by')} lúc {pend.get('at')}, {SCOPES[pend['scope']]})",
            old,
            pend.get("by_name") or pend.get("by") or "",
            SCOPES[pend["scope"]],
            actor=actor,
            position=position,
        )
        return updated

    # ---- reading ----------------------------------------------------------------------------------------

    async def info(self, ticket: Ticket) -> dict[str, Any]:
        pend = ticket.pending_model
        return {
            "current": await self._effective_for(ticket),
            "task": ticket.model,
            "stages": ticket.stage_models,
            "pending": (
                {**pend, "scope_label": SCOPES.get(pend["scope"], pend["scope"])} if pend else None
            ),
        }

    async def _audit(
        self, ticket: Ticket, what: str, old: str, by: str, when: str, *, actor: Any = None, position: Any = None
    ) -> None:
        new = await self.effective(ticket, actor, position) if actor is not None else await self._effective_for(ticket)
        await self.office.tickets.add_comment(
            ticket.id,
            f"🔀 {what}. Model dùng cho lượt chạy kế tiếp: {old} → {new}. Người đổi: {by}; mốc: {when}; lúc {utcnow()}.",
            author_kind="system",
            author_id=None,
        )
        self.office.bus.publish("ticket.updated", ticket=ticket.model_dump())
