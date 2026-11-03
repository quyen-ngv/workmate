"""The stage state machine of a ticket: start → (work → checks → gate)* → done.

- A stage is worked in its own session (stage_runs row; attempt increases on retry, reject or reopen).
- `complete()` runs the stage's checks (machine-verified, never self-reported); when they pass it opens the
  stage's gate (a card for the people holding the gate's roles) or moves straight on.
- Gate answers come back through `answer_gate()`: approve → next stage; reject → the same stage again with
  the reviewer's note. External gates (PR merged, automation approved) move on through `external_passed()`.
- The status people see (`status()`) is derived from runs, wakes and pending interactions, never stored.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import Conflict, InvalidTransition, NotFound, OfficeError, PolicyDenied
from actoroffice.domain.ids import utcnow
from actoroffice.domain.run import Usage, WakeSource
from actoroffice.domain.stage import (
    STAGE_STATUS_VI,
    STAGE_TITLES,
    StageDef,
    StageOutcome,
    StageRun,
    StageStatus,
    StageTemplate,
    Step,
)
from actoroffice.domain.team import LOCAL_OWNER_ID, ROLE_VI
from actoroffice.domain.work import Interaction, InteractionKind, Ticket, TicketStatus

from .checks import CheckResult, CheckRunner, review_acs, ui_evidence
from .review import gate_items

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)
S = TicketStatus
WAITING_KINDS = (
    InteractionKind.QUESTION.value,
    InteractionKind.APPROVE_STAGE.value,
    InteractionKind.APPROVE_TOOL.value,
    InteractionKind.APPROVE_ACCESS.value,
    InteractionKind.APPROVE_AUTOMATION.value,
    InteractionKind.ASK_COLLEAGUE.value,
    InteractionKind.ENV_ACCESS.value,
)


class StageError(OfficeError):
    code = 422


def stage_title(stage_id: str | None) -> str:
    return STAGE_TITLES.get(stage_id or "", stage_id or "-")


class StageService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self.checks = CheckRunner(office)
        self.finished_runs: set[str] = set()  # runs that finished (or left) their stage: no auto-continuation

    # ---- start --------------------------------------------------------------------------------------

    async def start(
        self,
        ref: int | str,
        *,
        template: str,
        by: str,
        team_id: str | None = None,
        owner_user_id: str | None = None,
        mode: str | None = None,
    ) -> Ticket:
        office = self.office
        ticket = await office.tickets.get(ref)
        if ticket.stage:
            raise Conflict(f"{ticket.key} đã ở giai đoạn {ticket.stage}")
        team_id = team_id or ticket.team_id
        probe = ticket.model_copy(update={"stage_template": template, "team_id": team_id})
        tpl = await office.teams.template(probe)
        if tpl is None or not tpl.stages:
            raise StageError(f"Không có mẫu giai đoạn '{template}'")
        first = tpl.stages[0]
        ai = await office.ensure_ai()
        await office.db.execute(
            "UPDATE tickets SET stage = ?, stage_template = ?, team_id = ?, owner_user_id = COALESCE(?, owner_user_id), "
            "mode = COALESCE(?, mode), paused = 0, updated_at = ? WHERE id = ?",
            (first.id, tpl.name, team_id, owner_user_id, mode, utcnow(), ticket.id),
        )
        await self._open_run(ticket.id, first.id)
        await self.conversation_of(await office.tickets.get(ticket.id))
        await office.tickets.add_comment(ticket.id, f"Bắt đầu quy trình '{tpl.name}': {self._route(tpl)}", author_kind="system", author_id=None)
        if ticket.assignee_actor_id != ai.id:
            ticket = await office.tickets.assign(ticket.id, ai.id, by=by)
        else:
            await office.wakes.enqueue(ai.id, WakeSource.ASSIGNMENT, ticket_id=ticket.id, payload={"stage": first.id})
        await office.audit.record("stage:start", "started", ticket_id=ticket.id, target=tpl.name, detail={"by": by})
        return await self._publish(ticket.id)

    async def conversation_of(self, ticket: Ticket) -> str:
        """The ticket's own room (group chat linked to the ticket = one Telegram topic). Created on first need and
        used as the ticket's origin conversation, so every notice, question and gate card lands there."""
        office = self.office
        ai = await office.ensure_ai()
        room = await office.chat.ticket_room(ticket, [ai.id], by="office", create=True, title=f"{ticket.key} · {ticket.title}")
        assert room is not None
        if not ticket.origin_conversation_id:
            await office.db.execute("UPDATE tickets SET origin_conversation_id = ? WHERE id = ?", (room.id, ticket.id))
        return room.id

    async def known(self, ticket: Ticket, actor: Any) -> dict[str, Any]:
        """What the team already knows that bears on this ticket: memories and similar earlier tickets."""
        office = self.office
        query = f"{ticket.title} {ticket.description[:400]}"
        memories = await office.memory.context_for(actor, query)
        kb = await office.knowledge.for_ticket(ticket)
        similar: list[dict[str, Any]] = []
        seen: set[str] = {ticket.key}
        for hit in await office.memory.history(actor, query, limit=20):
            key = hit.get("ticket_key")
            if not key or key in seen:
                continue
            seen.add(key)
            with contextlib.suppress(NotFound):
                other = await office.tickets.get(key)
                similar.append({"key": other.key, "title": other.title, "status": other.status.value, "snip": hit.get("snip", "")})
            if len(similar) >= 3:
                break
        return {"memories": memories, "similar": similar, "kb": kb}

    async def post_known(self, ticket: Ticket, known: dict[str, Any]) -> None:
        """Show people what the AI starts from, so a wrong assumption is corrected before any question is asked."""
        memories, similar, kb = known["memories"], known["similar"], known.get("kb") or []
        if not memories and not similar and not kb:
            return
        lines = ["📚 Mình đã biết (sai thì sửa giúp mình ngay ở đây):"]
        lines += [f"- [{i.kind}{'' if i.status == 'verified' else ', ' + i.status}] {i.title}: {i.content[:200]}" for i in kb[:8]]
        lines += [f"- {m.content}" for m in memories[:8]]
        if similar:
            lines.append("Việc tương tự trước đây:")
            lines += [f"- {s['key']} · {s['title']} ({s['status']})" for s in similar]
        conv = ticket.origin_conversation_id or await self.conversation_of(ticket)
        await self.office.chat.post_system(conv, "\n".join(lines))

    def _route(self, tpl: StageTemplate) -> str:
        return " → ".join(stage_title(s.id) + (" ✋" if s.gate else "") for s in tpl.stages)

    # ---- stage runs -----------------------------------------------------------------------------------

    async def _open_run(self, ticket_id: int, stage: str) -> StageRun:
        # A new stage run (next stage, redo, retry, reopen, switch, new attempt) makes every open gate card stale.
        for item in await self.office.interactions.pending(ticket_id=ticket_id):
            if item.kind == InteractionKind.APPROVE_STAGE:
                await self.office.interactions.cancel(item.id)
        row = await self.office.db.fetchone(
            "SELECT COALESCE(MAX(attempt), 0) AS n FROM stage_runs WHERE ticket_id = ? AND stage = ?", (ticket_id, stage)
        )
        attempt = int(row["n"] if row else 0) + 1
        cur = await self.office.db.execute(
            "INSERT INTO stage_runs(ticket_id, stage, attempt, started_at) VALUES (?, ?, ?, ?)", (ticket_id, stage, attempt, utcnow())
        )
        return await self.get_run(int(cur.lastrowid or 0))

    async def get_run(self, stage_run_id: int) -> StageRun:
        row = await self.office.db.fetchone("SELECT * FROM stage_runs WHERE id = ?", (stage_run_id,))
        if not row:
            raise NotFound(f"stage run {stage_run_id} not found")
        return _stage_run(row)

    async def current_run(self, ticket: Ticket) -> StageRun | None:
        if not ticket.stage:
            return None
        row = await self.office.db.fetchone(
            "SELECT * FROM stage_runs WHERE ticket_id = ? AND stage = ? ORDER BY id DESC LIMIT 1", (ticket.id, ticket.stage)
        )
        return _stage_run(row) if row else None

    async def ensure_run(self, ticket: Ticket) -> StageRun:
        """The open stage run for the ticket's current stage (a new attempt when the last one finished)."""
        current = await self.current_run(ticket)
        if current is not None and current.finished_at is None:
            return current
        if current is not None and current.outcome == StageOutcome.COMPLETED.value and await self._waits_outside(ticket):
            # Done and waiting for an outside gate (PR review/merge, automation approval): follow-up work such as
            # review fixes continues in the same attempt and session.
            return current
        assert ticket.stage
        return await self._open_run(ticket.id, ticket.stage)

    async def _waits_outside(self, ticket: Ticket) -> bool:
        try:
            stage = await self.stage_def(ticket)
        except OfficeError:
            return False
        if not stage.gate:
            return False
        return (await self.office.teams.gate_policy(ticket, stage.gate)).external

    async def waits_for_card(self, ticket: Ticket) -> bool:
        """Done and waiting for a gate card (the stage's gate or gate rules): comments wait for the answer."""
        current = await self.current_run(ticket)
        if current is None or current.outcome != StageOutcome.COMPLETED.value or await self._waits_outside(ticket):
            return False
        return any(
            i.kind == InteractionKind.APPROVE_STAGE and i.payload.get("stage") == ticket.stage
            for i in await self.office.interactions.pending(ticket_id=ticket.id)
        )

    async def runs(self, ticket_id: int) -> list[StageRun]:
        rows = await self.office.db.fetchall("SELECT * FROM stage_runs WHERE ticket_id = ? ORDER BY id", (ticket_id,))
        return [_stage_run(r) for r in rows]

    async def add_usage(self, stage_run_id: int, usage: Usage, cost: float, session_id: str | None) -> None:
        run = await self.get_run(stage_run_id)
        total: dict[str, Any] = {k: int(run.usage.get(k) or 0) + int(getattr(usage, k)) for k in Usage.model_fields if k != "model"}
        total["model"] = usage.model or run.usage.get("model") or ""
        await self.office.db.execute(
            "UPDATE stage_runs SET usage_json = ?, cost_usd = cost_usd + ?, session_id = COALESCE(?, session_id) WHERE id = ?",
            (json.dumps(total), cost, session_id, stage_run_id),
        )

    async def set_rules(self, stage_run_id: int, rule_refs: list[str]) -> None:
        await self.office.db.execute("UPDATE stage_runs SET rules_json = ? WHERE id = ?", (json.dumps(rule_refs), stage_run_id))

    # ---- steps ---------------------------------------------------------------------------------------

    async def set_steps(self, stage_run_id: int, texts: list[str]) -> list[Step]:
        run = await self.get_run(stage_run_id)
        done = {s.text: s for s in run.steps if s.done}
        steps = [done.get(t.strip(), Step(text=t.strip())) for t in texts if t.strip()]
        if not steps:
            raise StageError("Cần ít nhất một bước")
        await self._save_steps(stage_run_id, steps, run.ticket_id)
        return steps

    async def step_done(self, stage_run_id: int, index: int, note: str = "") -> list[Step]:
        run = await self.get_run(stage_run_id)
        if not 1 <= index <= len(run.steps):
            raise StageError(f"Không có bước {index} (có {len(run.steps)} bước)")
        run.steps[index - 1] = Step(text=run.steps[index - 1].text, done=True, note=note.strip())
        await self._save_steps(stage_run_id, run.steps, run.ticket_id)
        return run.steps

    async def _save_steps(self, stage_run_id: int, steps: list[Step], ticket_id: int) -> None:
        await self.office.db.execute(
            "UPDATE stage_runs SET steps_json = ? WHERE id = ?", (json.dumps([s.model_dump() for s in steps], ensure_ascii=False), stage_run_id)
        )
        self.office.bus.publish("ticket.steps", ticket_id=ticket_id, stage_run_id=stage_run_id, steps=[s.model_dump() for s in steps])

    async def last_review(self, ticket_id: int) -> list[dict[str, Any]]:
        """Medium/high findings of the last finished stage run that was reviewed (the build stage, read at pr)."""
        for run in reversed(await self.runs(ticket_id)):
            check = next((c for c in run.checks if c.get("id") == "review_clean"), None)
            if run.stage == "build" and run.finished_at and run.outcome == StageOutcome.COMPLETED.value and check is not None:
                return gate_items(check.get("data"))
        return []

    async def review_acs(self, ticket_id: int) -> list[dict[str, str]]:
        """AC [review] items from the latest requirements and handoff, for the gate card and the pr pane."""
        requirements = await self.office.tickets.get_document(ticket_id, "requirements")
        if requirements is None:
            return []
        handoff = await self.office.tickets.get_document(ticket_id, "handoff")
        return review_acs(requirements.body, handoff.body if handoff else "")

    async def ui_evidence(self, ticket_id: int) -> list[dict[str, Any]]:
        """AC [ui] items with their screenshots and the reasons none could be taken (SI-23): for the check, the pr
        pane, the gate card and the PR description. ACs come from the requirements, or the plan (quick workflow)."""
        office = self.office
        doc = await office.tickets.get_document(ticket_id, "requirements") or await office.tickets.get_document(ticket_id, "plan")
        if doc is None:
            return []
        handoff = await office.tickets.get_document(ticket_id, "handoff")
        return ui_evidence(doc.body, handoff.body if handoff else "", await office.tickets.work_products(ticket_id))

    async def save_review(self, stage_run_id: int, review: dict[str, Any]) -> None:
        await self.office.db.execute("UPDATE stage_runs SET review_json = ? WHERE id = ?", (json.dumps(review, ensure_ascii=False), stage_run_id))

    # ---- complete → checks → gate ----------------------------------------------------------------------

    async def template_of(self, ticket: Ticket) -> StageTemplate:
        tpl = await self.office.teams.template(ticket)
        if tpl is None:
            raise StageError(f"{ticket.key} không chạy theo quy trình giai đoạn")
        return tpl

    async def stage_def(self, ticket: Ticket) -> StageDef:
        tpl = await self.template_of(ticket)
        stage = tpl.stage(ticket.stage)
        if stage is None:
            raise StageError(f"{ticket.key}: giai đoạn '{ticket.stage}' không có trong mẫu {tpl.name}")
        return stage

    async def complete(self, ticket_id: int, *, run_id: str, summary: str, workspace: Path | None) -> dict[str, Any]:
        """Called by the AI (stage_complete). Returns what happened: failed checks, a gate opened, or the next stage."""
        office = self.office
        ticket = await office.tickets.get(ticket_id)
        stage = await self.stage_def(ticket)
        srun = await self.ensure_run(ticket)
        project = await office.projects.get(ticket.project_id) if ticket.project_id else None
        compiled = await office.rules.compile(ticket=ticket, project=project, stage=stage.id)
        check_ids = list(stage.checks) + [f"rule:{r.id}" for r in compiled.checks]
        results = await self.checks.run(ticket, stage, srun, workspace=workspace, run_id=run_id, only=check_ids)
        await office.db.execute(
            "UPDATE stage_runs SET checks_json = ? WHERE id = ?", (json.dumps([r.model_dump() for r in results], ensure_ascii=False), srun.id)
        )
        failed = [r for r in results if r.failed]
        if failed:
            return {"ok": False, "failed": [r.model_dump() for r in failed], "checks": [r.model_dump() for r in results]}
        await office.db.execute(
            "UPDATE stage_runs SET finished_at = ?, outcome = ?, summary = ? WHERE id = ?",
            (utcnow(), StageOutcome.COMPLETED.value, summary.strip()[:4000], srun.id),
        )
        self.finished_runs.add(run_id)
        await office.tickets.add_comment(
            ticket.id, f"✅ Xong giai đoạn {stage_title(stage.id)}: {summary.strip()[:1500]}", author_kind="actor", author_id=office.settings.ai_name
        )
        extra = await self._gate_rules(compiled.gates, workspace, project)
        if extra and not stage.gate:
            item = await self._open_gate(ticket, stage, "rules", summary, results, run_id=run_id, extra=extra)
            await self._set_status(ticket.id, S.IN_REVIEW, run_id=run_id, comment=None)
            await self._publish(ticket.id)
            return {"ok": True, "gate": "rules", "interaction_id": item.id, "approvers": item.payload.get("approver_names"), "checks": [r.model_dump() for r in results]}
        if stage.gate:
            policy = await office.teams.gate_policy(ticket, stage.gate)
            if policy.external:
                await self._set_status(ticket.id, S.IN_REVIEW, run_id=run_id, comment=self._external_note(stage.gate))
                await self._publish(ticket.id)
                return {"ok": True, "gate": stage.gate, "external": True, "checks": [r.model_dump() for r in results]}
            item = await self._open_gate(ticket, stage, stage.gate, summary, results, run_id=run_id, extra=extra)
            await self._set_status(ticket.id, S.IN_REVIEW, run_id=run_id, comment=None)
            await self._publish(ticket.id)
            return {"ok": True, "gate": stage.gate, "interaction_id": item.id, "approvers": item.payload.get("approver_names"), "checks": [r.model_dump() for r in results]}
        nxt = await self.advance(ticket.id, by=office.settings.ai_name, run_id=run_id)
        return {"ok": True, "advanced_to": nxt.stage, "done": nxt.status == S.DONE, "checks": [r.model_dump() for r in results]}

    def _external_note(self, gate: str) -> str:
        return {
            "merge": "Chờ review và merge PR. Khi PR được merge, quy trình tự sang bước tiếp theo.",
            "script": "Chờ người duyệt automation (thẻ duyệt script). Khi được duyệt, quy trình tự sang bước tiếp theo.",
        }.get(gate, f"Chờ cổng '{gate}' bên ngoài.")

    async def _gate_rules(self, gates: list[Any], workspace: Path | None, project: Any) -> list[Any]:
        """Gate rules whose paths were changed in this ticket's workspace."""
        if not gates or workspace is None or not (workspace / ".git").exists():
            return []
        from .checks import changed_files
        from .rules import gate_rules_hit

        return gate_rules_hit(gates, await changed_files(workspace, project.main_branch if project else "main"))

    async def _open_gate(
        self,
        ticket: Ticket,
        stage: StageDef,
        gate: str,
        summary: str,
        results: list[CheckResult],
        *,
        run_id: str | None,
        extra: list[Any] | None = None,
    ) -> Interaction:
        office = self.office
        from actoroffice.domain.stage import GatePolicy

        policy = await office.teams.gate_policy(ticket, gate) if gate != "rules" else GatePolicy(roles=[], fallback="owner")
        for rule in extra or []:  # e.g. migrations changed → a lead must approve too
            roles = [r for r in rule.spec.get("roles") or [] if r not in policy.roles]
            policy = policy.model_copy(update={"roles": policy.roles + roles, "min": max(policy.min, int(rule.spec.get("min") or 1))})
        approvers = await office.teams.approvers(ticket, policy)
        docs = []
        for key in [o for out in stage.outputs for o in out.split("|")]:
            doc = await office.tickets.get_document(ticket.id, key)
            if doc is not None:
                docs.append({"key": doc.key, "revision": doc.revision})
        payload = {
            "ticket": ticket.key,
            "title": ticket.title,
            "stage": stage.id,
            "stage_title": stage_title(stage.id),
            "gate": gate,
            "roles": policy.roles,
            "roles_label": " / ".join(ROLE_VI.get(r, r) for r in policy.roles),  # type: ignore[call-overload]
            "min": policy.min,
            "approver_ids": [u.id for u in approvers],
            "approver_names": [u.name for u in approvers],
            "summary": summary.strip()[:3000],
            "docs": docs,
            "checks": [{"id": r.id, "status": r.status, "summary": r.summary} for r in results],
            "access": [
                f"{i.payload.get('resource')} ({i.payload.get('level_label')}, {i.payload.get('ttl_h')}h)"
                for i in await office.interactions.pending(ticket_id=ticket.id)
                if i.kind == InteractionKind.ENV_ACCESS
            ],
            "approvals": [],
            "rules": [{"id": r.id, "title": r.title, "roles": r.spec.get("roles")} for r in extra or []],
            "review": gate_items(next((r.data for r in results if r.id == "review_clean"), None)),
            "review_acs": await self.review_acs(ticket.id) if stage.id in ("build", "pr") else [],
            "ui_evidence": await self.ui_evidence(ticket.id) if stage.id in ("build", "pr") else [],
        }
        if gate == "knowledge":
            drafts = [i for i in await office.knowledge.list(ticket_id=ticket.id) if i.status == "draft"]
            payload["kb"] = [f"{n}. [{i.kind}] {i.title}: {i.content[:240]}" for n, i in enumerate(drafts, 1)]
        return await office.interactions.create(InteractionKind.APPROVE_STAGE, payload, ticket_id=ticket.id, run_id=run_id)

    async def answer_gate(self, item: Interaction, response: dict[str, Any], *, by: str) -> Interaction:
        """A person approves or rejects a stage gate. Role-checked; `min` approvals needed to pass."""
        office = self.office
        if item.kind != InteractionKind.APPROVE_STAGE or item.ticket_id is None:
            raise StageError("Không phải thẻ duyệt giai đoạn")
        ticket = await office.tickets.get(item.ticket_id)
        if ticket.stage != item.payload.get("stage"):
            await office.interactions.cancel(item.id)
            raise Conflict(f"{ticket.key} đã sang giai đoạn khác")
        roles = list(item.payload.get("roles") or [])
        if not await self._may_answer(by, ticket, roles):
            raise PolicyDenied(f"Cần vai {item.payload.get('roles_label') or ', '.join(roles)} để duyệt")
        approved = bool(response.get("approved"))
        note = str(response.get("note") or "").strip()
        name = await self._name(by)
        if item.payload.get("gate") == "knowledge":
            # Knowledge is confirmed, not redone: "bỏ 2, 5" keeps the rest; a plain rejection drops everything.
            partial = not approved and bool(re.search(r"\d", note))
            resolved = await office.interactions.resolve(item.id, {"approved": approved or partial, "note": note, "by_name": name}, by=by)
            result = await office.knowledge.review_close(ticket.id, approved=approved or partial, note=note if partial or approved else "", by=by)
            await office.tickets.add_comment(
                ticket.id, f"📚 {name}: xác nhận {len(result['verified'])} mục kiến thức, bỏ {len(result['rejected'])}", author_kind="system", author_id=None
            )
            await self.advance(ticket.id, by=by)
            from .knowledge import safe_publish

            asyncio.get_running_loop().create_task(safe_publish(office, ticket.id))
            return resolved
        if approved:
            approvals = [a for a in item.payload.get("approvals", []) if a.get("by") != by] + [{"by": by, "name": name, "at": utcnow()}]
            if len(approvals) < int(item.payload.get("min") or 1):
                await office.db.execute(
                    "UPDATE interactions SET payload_json = json_set(payload_json, '$.approvals', json(?)) WHERE id = ?",
                    (json.dumps(approvals, ensure_ascii=False), item.id),
                )
                refreshed = await office.interactions.get(item.id)
                office.bus.publish("interaction.updated", interaction=refreshed.model_dump())
                return refreshed
        resolved = await office.interactions.resolve(item.id, {"approved": approved, "note": note, "by_name": name}, by=by)
        await office.audit.record(
            "stage:gate", "approved" if approved else "rejected", ticket_id=ticket.id, target=f"{ticket.key}:{item.payload.get('gate')}", detail={"by": by, "note": note}
        )
        if approved:
            for doc in item.payload.get("docs", []):
                await office.db.execute(
                    "UPDATE documents SET approved_revision = ? WHERE ticket_id = ? AND key = ?", (doc["revision"], ticket.id, doc["key"])
                )
            comment = f"👍 {name} duyệt giai đoạn {stage_title(ticket.stage)}" + (f": {note}" if note else "")
            await office.tickets.add_comment(ticket.id, comment, author_kind="system", author_id=None)
            if item.payload.get("gate") == "requirement" and ticket.jira_key:
                await office.jira.requirements_approved(ticket.id, name)
            if item.payload.get("gate") == "plan":
                granted = await office.access.approve_with_plan(ticket.id, by=by)
                if granted:
                    await office.tickets.add_comment(ticket.id, f"🔑 Cấp quyền cùng plan: {', '.join(granted)}", author_kind="system", author_id=None)
            await self.advance(ticket.id, by=by)
        else:
            comment = f"👎 {name} chưa duyệt {stage_title(ticket.stage)}" + (f": {note}" if note else "")
            await office.tickets.add_comment(ticket.id, comment, author_kind="system", author_id=None)
            await self._redo(ticket, reason=note or "người duyệt yêu cầu làm lại", by=by)
        return resolved

    async def _may_answer(self, by: str, ticket: Ticket, roles: list[str]) -> bool:
        if by == LOCAL_OWNER_ID:
            return True
        return await self.office.teams.can_act(by, ticket, roles)

    async def _name(self, user_id: str) -> str:
        try:
            return (await self.office.teams.get_user(user_id)).name
        except NotFound:
            return user_id

    async def external_passed(self, ref: int | str, gate: str, *, by: str, note: str = "") -> Ticket:
        """An external gate passed (PR merged, automation approved): move to the next stage."""
        office = self.office
        ticket = await office.tickets.get(ref)
        if not ticket.stage:
            return ticket
        stage = await self.stage_def(ticket)
        if stage.gate != gate:
            return ticket
        srun = await self.current_run(ticket)
        if srun is None or srun.outcome != StageOutcome.COMPLETED.value:
            return ticket  # the AI has not finished the stage yet; it will see the event when it does
        await office.tickets.add_comment(ticket.id, f"🔓 {gate}: {note or 'đã qua'} ({await self._name(by)})", author_kind="system", author_id=None)
        if gate == "merge":
            await self._stale_after_merge(ticket)
        return await self.advance(ticket.id, by=by)

    async def _stale_after_merge(self, ticket: Ticket) -> None:
        """Code notes and flows anchored to files this ticket changed must be re-checked before they are trusted."""
        from .checks import changed_files

        office = self.office
        root = await office.tickets.root_of(ticket.id)
        workspace = office.settings.workspaces_dir / root.key
        if not (workspace / ".git").exists():
            return
        project = await office.projects.get(ticket.project_id) if ticket.project_id else None
        base = await self.merged_base(root) or (project.main_branch if project else "main")
        files = await changed_files(workspace, base)
        stale = await office.knowledge.mark_stale(team_id=ticket.team_id, changed_files=files)
        if stale:
            await office.tickets.add_comment(ticket.id, f"⚠️ {len(stale)} ghi chú kiến thức có thể đã cũ sau thay đổi này: #{', #'.join(map(str, stale))}", author_kind="system", author_id=None)

    async def merged_base(self, root: Ticket) -> str | None:
        """Main as it was before the office merged the root's branch (`<merge sha>^1`), or None when it did not."""
        merges = [p for p in await self.office.tickets.work_products(root.id) if p["kind"] == "merge" and p["metadata"].get("sha")]
        # Merged by the office: main already holds the branch, so diff against main as it was before the merge.
        return f"{merges[-1]['metadata']['sha']}^1" if merges else None

    async def advance(self, ticket_id: int, *, by: str, run_id: str | None = None) -> Ticket:
        office = self.office
        ticket = await office.tickets.get(ticket_id)
        tpl = await self.template_of(ticket)
        assert ticket.stage
        nxt = tpl.next_after(ticket.stage)
        if nxt is None:
            await self._set_status(ticket.id, S.DONE, run_id=run_id, comment=f"Hoàn tất quy trình {tpl.name}.")
            from actoroffice.engine.workspace import cleanup_ticket

            root = await office.tickets.root_of(ticket.id)
            asyncio.get_running_loop().create_task(cleanup_ticket(ticket, office.settings.workspaces_dir / root.key))
            await office.audit.record("stage:done", "done", ticket_id=ticket.id, target=ticket.key, detail={"by": by})
            return await self._publish(ticket.id)
        await office.db.execute("UPDATE tickets SET stage = ?, updated_at = ? WHERE id = ?", (nxt.id, utcnow(), ticket.id))
        await self._open_run(ticket.id, nxt.id)
        await office.tickets.add_comment(ticket.id, f"➡️ Sang giai đoạn {stage_title(nxt.id)}", author_kind="system", author_id=None)
        await self._set_status(ticket.id, S.TODO, run_id=run_id, comment=None)
        if ticket.assignee_actor_id:
            await office.wakes.enqueue(ticket.assignee_actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"stage": nxt.id})
        office.scheduler.kick()
        return await self._publish(ticket.id)

    async def _redo(self, ticket: Ticket, *, reason: str, by: str) -> None:
        office = self.office
        current = await self.current_run(ticket)
        if current is not None and current.finished_at and current.outcome == StageOutcome.COMPLETED.value:
            await office.db.execute("UPDATE stage_runs SET outcome = ? WHERE id = ?", (StageOutcome.REJECTED.value, current.id))
        assert ticket.stage
        await self._open_run(ticket.id, ticket.stage)
        await self._set_status(ticket.id, S.TODO, run_id=None, comment=None)
        if ticket.assignee_actor_id:
            await office.wakes.enqueue(
                ticket.assignee_actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"stage": ticket.stage, "redo": reason, "by": by}
            )
        office.scheduler.kick()
        await self._publish(ticket.id)

    async def switch_template(self, ref: int | str, template: str, *, reason: str, by: str, run_id: str | None = None) -> Ticket:
        """Move a ticket between the full and the quick workflow (before its build is done); it restarts at the new
        workflow's first stage. Documents already written stay."""
        from actoroffice.domain.stage import SWITCHABLE, TEMPLATE_TITLES

        office = self.office
        ticket = await office.tickets.get(ref)
        if SWITCHABLE.get(ticket.stage_template or "") != template:
            raise StageError(f"Chỉ chuyển được giữa quy trình code và quick (đang là {ticket.stage_template})")
        if ticket.stage in ("pr", "close"):
            raise StageError("Đã tới pull request: không đổi quy trình nữa")
        tpl = await office.teams.template(ticket.model_copy(update={"stage_template": template}))
        if tpl is None or not tpl.stages:
            raise StageError(f"Không có mẫu giai đoạn '{template}'")
        current = await self.current_run(ticket)
        if current is not None and current.finished_at is None:
            await office.db.execute(
                "UPDATE stage_runs SET finished_at = ?, outcome = ?, summary = ? WHERE id = ?",
                (utcnow(), StageOutcome.SWITCHED.value, reason[:2000], current.id),
            )
        if run_id:
            self.finished_runs.add(run_id)
        first = tpl.stages[0]
        await office.db.execute(
            "UPDATE tickets SET stage = ?, stage_template = ?, updated_at = ? WHERE id = ?", (first.id, tpl.name, utcnow(), ticket.id)
        )
        await self._open_run(ticket.id, first.id)
        await office.tickets.add_comment(
            ticket.id,
            f"🔀 Chuyển sang quy trình {TEMPLATE_TITLES.get(tpl.name, tpl.name)} ({await self._name(by)}): {reason}. {self._route(tpl)}",
            author_kind="system",
            author_id=None,
        )
        await self._set_status(ticket.id, S.TODO, run_id=run_id, comment=None)
        if ticket.assignee_actor_id:
            await office.wakes.enqueue(
                ticket.assignee_actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"stage": first.id, "switched": reason, "from": ticket.stage_template}
            )
        office.scheduler.kick()
        await office.audit.record("stage:switch", "switched", ticket_id=ticket.id, target=tpl.name, detail={"by": by, "reason": reason})
        return await self._publish(ticket.id)

    async def reopen(self, ref: int | str, target: str, *, reason: str, by: str, run_id: str | None = None) -> Ticket:
        """Go back to an earlier stage (e.g. a missing requirement found while building)."""
        office = self.office
        ticket = await office.tickets.get(ref)
        tpl = await self.template_of(ticket)
        ids = [s.id for s in tpl.stages]
        if target not in ids:
            raise StageError(f"Không có giai đoạn '{target}' (có: {', '.join(ids)})")
        if ticket.stage in ids and ids.index(target) > ids.index(ticket.stage):
            raise StageError("Chỉ quay lại được giai đoạn trước")
        current = await self.current_run(ticket)
        if current is not None and current.finished_at is None:
            await office.db.execute(
                "UPDATE stage_runs SET finished_at = ?, outcome = ?, summary = ? WHERE id = ?",
                (utcnow(), StageOutcome.REOPENED.value, reason[:2000], current.id),
            )
        if run_id:
            self.finished_runs.add(run_id)
        await office.db.execute("UPDATE tickets SET stage = ?, updated_at = ? WHERE id = ?", (target, utcnow(), ticket.id))
        if ticket.status in (S.DONE, S.CANCELLED):
            # a closed ticket may still carry ⏹ Stop's pause: the scheduler would skip it after reopening
            await office.db.execute("UPDATE tickets SET paused = 0 WHERE id = ?", (ticket.id,))
        await self._open_run(ticket.id, target)
        await office.tickets.add_comment(
            ticket.id, f"↩️ Quay lại giai đoạn {stage_title(target)} ({await self._name(by)}): {reason}", author_kind="system", author_id=None
        )
        await self._set_status(ticket.id, S.TODO, run_id=run_id, comment=None)
        if ticket.assignee_actor_id:
            await office.wakes.enqueue(ticket.assignee_actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"stage": target, "reopened": reason})
        office.scheduler.kick()
        return await self._publish(ticket.id)

    # ---- controls -------------------------------------------------------------------------------------

    async def pause(self, ref: int | str, *, by: str) -> Ticket:
        office = self.office
        ticket = await office.tickets.get(ref)
        await office.db.execute("UPDATE tickets SET paused = 1, updated_at = ? WHERE id = ?", (utcnow(), ticket.id))
        for run in await office.runs.for_ticket(ticket.id, limit=5):
            if run.status.value == "running":
                await office.scheduler.stop_run(run.id)
        await office.tickets.add_comment(ticket.id, f"⏸ Tạm dừng bởi {await self._name(by)}", author_kind="system", author_id=None)
        return await self._publish(ticket.id)

    async def resume(self, ref: int | str, *, by: str) -> Ticket:
        office = self.office
        ticket = await office.tickets.get(ref)
        await office.db.execute("UPDATE tickets SET paused = 0, updated_at = ? WHERE id = ?", (utcnow(), ticket.id))
        await office.tickets.add_comment(ticket.id, f"▶️ Tiếp tục ({await self._name(by)})", author_kind="system", author_id=None)
        if ticket.assignee_actor_id and ticket.status not in (S.DONE, S.CANCELLED):
            if ticket.status == S.IN_PROGRESS and ticket.checkout_run_id is None:
                await self._set_status(ticket.id, S.TODO, run_id=None, comment=None)
            await office.wakes.enqueue(ticket.assignee_actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"resumed": True})
        office.scheduler.kick()
        return await self._publish(ticket.id)

    async def steer(self, ref: int | str, text: str, *, by: str) -> Ticket:
        """Extra direction for the AI: a running run hears it at its next tool call; otherwise the next wake."""
        office = self.office
        ticket = await office.tickets.get(ref)
        text = text.strip()
        if not text:
            raise StageError("Chỉ dẫn trống")
        name = await self._name(by)
        await office.tickets.add_comment(ticket.id, f"🧭 Chỉ dẫn từ {name}: {text}", author_kind="steer", author_id=by)
        if ticket.assignee_actor_id and ticket.status not in (S.DONE, S.CANCELLED):
            await office.wakes.enqueue(ticket.assignee_actor_id, WakeSource.COMMENT, ticket_id=ticket.id, payload={"steer": text, "by": name})
            office.scheduler.kick()
        return await self._publish(ticket.id)

    async def retry(self, ref: int | str, *, by: str) -> Ticket:
        office = self.office
        ticket = await office.tickets.get(ref)
        if not ticket.stage:
            raise StageError(f"{ticket.key} không chạy theo quy trình giai đoạn")
        current = await self.current_run(ticket)
        if current is not None and current.finished_at is None:
            await office.db.execute(
                "UPDATE stage_runs SET finished_at = ?, outcome = ?, summary = ? WHERE id = ?", (utcnow(), StageOutcome.STOPPED.value, "retry", current.id)
            )
        await self._open_run(ticket.id, ticket.stage)
        await office.db.execute("UPDATE tickets SET paused = 0 WHERE id = ?", (ticket.id,))
        await self._set_status(ticket.id, S.TODO, run_id=None, comment=f"🔁 Làm lại giai đoạn {stage_title(ticket.stage)} ({await self._name(by)})")
        if ticket.assignee_actor_id:
            await office.wakes.enqueue(ticket.assignee_actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"retry": True})
        office.scheduler.kick()
        return await self._publish(ticket.id)

    async def stop(self, ref: int | str, *, by: str, reason: str = "") -> Ticket:
        office = self.office
        ticket = await office.tickets.get(ref)
        for run in await office.runs.for_ticket(ticket.id, limit=5):
            if run.status.value == "running":
                await office.scheduler.stop_run(run.id)
        current = await self.current_run(ticket)
        if current is not None and current.finished_at is None:
            await office.db.execute(
                "UPDATE stage_runs SET finished_at = ?, outcome = ?, summary = ? WHERE id = ?",
                (utcnow(), StageOutcome.STOPPED.value, reason[:2000], current.id),
            )
        await office.db.execute("UPDATE tickets SET paused = 1 WHERE id = ?", (ticket.id,))
        await office.tickets.add_comment(ticket.id, f"⏹ Dừng giai đoạn bởi {await self._name(by)}" + (f": {reason}" if reason else ""), author_kind="system", author_id=None)
        return await self._publish(ticket.id)

    # ---- status (derived) --------------------------------------------------------------------------------

    async def status(self, ref: int | str | Ticket, *, viewer: str | None = None) -> dict[str, Any]:
        office = self.office
        ticket = ref if isinstance(ref, Ticket) else await office.tickets.get(ref)
        srun = await self.current_run(ticket)
        pending = [i for i in await office.interactions.pending(ticket_id=ticket.id) if i.kind.value in WAITING_KINDS]
        running = [r for r in await office.runs.for_ticket(ticket.id, limit=5) if r.status.value == "running"]
        who: list[str] = []
        needs_viewer = False
        status = StageStatus.IDLE
        if ticket.status == S.DONE:
            status = StageStatus.DONE
        elif not ticket.stage:
            status = StageStatus.NOT_STARTED
        elif ticket.paused:
            status = StageStatus.PAUSED
        elif pending:
            status = StageStatus.NEEDS_YOU
            for item in pending:
                names, ids = await self._askees(item, ticket)
                who += [n for n in names if n not in who]
                if viewer and (viewer in ids or viewer == LOCAL_OWNER_ID):  # the local owner is admin: may answer anything
                    needs_viewer = True
        elif running:
            status = StageStatus.WORKING
        elif ticket.assignee_actor_id and await office.wakes.pending_for(ticket.assignee_actor_id, ticket.id):
            status = StageStatus.QUEUED
        elif ticket.status == S.BLOCKED or await office.tickets.open_blockers(ticket.id):
            status = StageStatus.BLOCKED
        elif srun is not None and srun.outcome == StageOutcome.COMPLETED.value and ticket.status == S.IN_REVIEW:
            status = StageStatus.WAITING_EXTERNAL
        elif ticket.status == S.IN_REVIEW:
            # the AI stopped for a person without a question card (its last comment says what for)
            status = StageStatus.NEEDS_YOU
            with contextlib.suppress(NotFound):
                who = [(await office.teams.get_user(ticket.owner_user_id)).name] if ticket.owner_user_id else []
            needs_viewer = bool(viewer and viewer in (ticket.owner_user_id, LOCAL_OWNER_ID))
        else:
            last = await office.runs.for_ticket(ticket.id, limit=1)
            if last and last[0].status.value == "failed":
                status = StageStatus.FAILED
        steps = srun.steps if srun else []
        return {
            "ticket": ticket.key,
            "title": ticket.title,
            "stage": ticket.stage,
            "stage_title": stage_title(ticket.stage),
            "template": ticket.stage_template,
            "status": status.value,
            "status_label": STAGE_STATUS_VI[status],
            "who": who,
            "needs_viewer": needs_viewer,
            "run_id": running[0].id if running else None,
            "attempt": srun.attempt if srun else 0,
            "steps": [s.model_dump() for s in steps],
            "steps_done": sum(1 for s in steps if s.done),
            "usage": srun.usage if srun else {},
            "paused": ticket.paused,
            "jira_key": ticket.jira_key,
            "pr_url": ticket.pr_url,
        }

    async def _askees(self, item: Interaction, ticket: Ticket) -> tuple[list[str], list[str]]:
        """Names and ids of the people an interaction waits for."""
        p = item.payload
        if p.get("approver_names"):
            return list(p["approver_names"]), list(p.get("approver_ids") or [])
        roles = list(p.get("roles") or [])
        if roles:
            from actoroffice.domain.stage import GatePolicy

            people = await self.office.teams.approvers(ticket, GatePolicy(roles=roles))
            if people:
                return [u.name for u in people], [u.id for u in people]
            return [" / ".join(ROLE_VI.get(r, r) for r in roles)], []  # type: ignore[call-overload]
        return ["người phụ trách"], []

    # ---- helpers ----------------------------------------------------------------------------------------

    async def _set_status(self, ticket_id: int, to: TicketStatus, *, run_id: str | None, comment: str | None) -> None:
        office = self.office
        ticket = await office.tickets.get(ticket_id)
        if ticket.status == to:
            if comment:
                await office.tickets.add_comment(ticket.id, comment, author_kind="system", author_id=None)
            return
        if ticket.checkout_run_id and ticket.checkout_run_id != run_id:
            # Another run owns the ticket right now (e.g. a comment woke the AI). Leave the status to that run;
            # the stage change and the queued wake are already recorded.
            if comment:
                await office.tickets.add_comment(ticket.id, comment, author_kind="system", author_id=None)
            return
        try:
            await office.tickets.transition(ticket.id, to, by="office", run_id=run_id, comment=comment)
        except InvalidTransition:
            # e.g. blocked → done is not a manual transition; stages own the ticket status, so set it directly.
            await office.db.execute(
                "UPDATE tickets SET status = ?, checkout_run_id = NULL, updated_at = ? WHERE id = ?", (to.value, utcnow(), ticket.id)
            )
            if comment:
                await office.tickets.add_comment(ticket.id, comment, author_kind="system", author_id=None)

    async def _publish(self, ticket_id: int) -> Ticket:
        ticket = await self.office.tickets.get(ticket_id)
        self.office.bus.publish("ticket.updated", ticket=ticket.model_dump())
        self.office.bus.publish("ticket.stage", ticket_id=ticket.id, stage=ticket.stage)
        return ticket


def _stage_run(row: dict[str, Any]) -> StageRun:
    return StageRun(
        id=row["id"],
        ticket_id=row["ticket_id"],
        stage=row["stage"],
        attempt=row["attempt"],
        session_id=row["session_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome=row["outcome"],
        summary=row["summary"],
        usage=json.loads(row["usage_json"] or "{}"),
        cost_usd=row["cost_usd"] or 0.0,
        rules=json.loads(row["rules_json"] or "[]"),
        checks=json.loads(row["checks_json"] or "[]"),
        steps=[Step.model_validate(s) for s in json.loads(row["steps_json"] or "[]")],
        review=json.loads(row["review_json"] or "{}"),
    )
