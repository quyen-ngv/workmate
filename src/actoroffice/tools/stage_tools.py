"""Tools for a ticket that runs through stages (team copilot): ask people by role, declare and tick steps,
finish or reopen a stage, keep the dossier, report plan deviations, link the pull request."""

from __future__ import annotations

import json
import re
from typing import Any

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.team import ROLE_VI
from actoroffice.domain.work import InteractionKind

from .registry import ToolCtx, ToolDef, ToolError, schema

DOSSIER_SECTIONS = {
    "context": "Bối cảnh",
    "requirements": "Yêu cầu & AC",
    "qa": "Hỏi đáp",
    "decisions": "Quyết định",
    "technical": "Kỹ thuật",
    "access": "Truy cập môi trường",
    "tests": "Kiểm thử",
    "deviations": "Lệch kế hoạch",
    "links": "Liên kết",
}
ROLE_CHOICES = ["ba", "dev", "qe", "lead", "owner"]


async def _stage_ctx(ctx: ToolCtx) -> Any:
    if ctx.ticket_id is None:
        raise ToolError("This tool works inside a ticket.")
    ticket = await ctx.office.tickets.get(ctx.ticket_id)
    if not ticket.stage:
        raise ToolError(f"{ticket.key} is not running through stages.")
    return ticket


def _roles(value: Any) -> list[str]:
    roles = [str(r).strip().lower() for r in (value if isinstance(value, list) else [value]) if str(r).strip()]
    bad = [r for r in roles if r not in ROLE_CHOICES]
    if bad or not roles:
        raise ToolError(f"roles must be a non-empty list of: {', '.join(ROLE_CHOICES)}")
    return roles


async def ask_human(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await _stage_ctx(ctx)
    roles = _roles(args.get("roles"))
    question = str(args.get("question") or "").strip()
    if not question:
        raise ToolError("question is empty")
    options = [str(o).strip() for o in args.get("options") or [] if str(o).strip()][:6]
    recommended = str(args.get("recommended") or "").strip()
    if recommended and options and recommended not in options:
        raise ToolError("recommended must be one of the options")
    blocking = bool(args.get("blocking", True))
    label = " / ".join(ROLE_VI.get(r, r) for r in roles)  # type: ignore[call-overload]
    from actoroffice.domain.stage import GatePolicy

    people = await office.teams.approvers(ticket, GatePolicy(roles=roles))
    item = await office.interactions.create(
        InteractionKind.QUESTION,
        {
            "question": question,
            "options": options,
            "recommended": recommended,
            "roles": roles,
            "roles_label": label,
            "approver_ids": [u.id for u in people],
            "approver_names": [u.name for u in people],
            "asked_by": ctx.actor.name,
            "ticket": ticket.key,
            "stage": ticket.stage,
            "blocking": blocking,
            "context": str(args.get("why") or "").strip()[:600],
        },
        actor_id=ctx.actor.id,
        ticket_id=ticket.id,
        run_id=ctx.run_id,
    )
    who = ", ".join(u.name for u in people) or label
    if not blocking:
        return f"Question {item.id} sent to {who}. Not blocking: continue with your recommended answer and note the assumption in the dossier (section qa)."
    if ticket.mode == "assume" and recommended:
        return (
            f"Question {item.id} sent to {who}. This ticket runs in 'assume' mode: continue with '{recommended}', record it as an "
            "assumption in the dossier (section qa), and adapt if the answer differs."
        )
    return (
        f"Question {item.id} sent to {who}. Do not wait or poll: save your progress in the dossier, then end your turn "
        "(the office parks the ticket). You are woken with the answer."
    )


async def plan_steps(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket = await _stage_ctx(ctx)
    steps = [str(s) for s in args.get("steps") or []]
    srun = await ctx.office.stages.ensure_run(ticket)
    try:
        saved = await ctx.office.stages.set_steps(srun.id, steps)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    await ctx.office.runs.event(ctx.run_id, "steps", {"steps": [s.model_dump() for s in saved]})
    return "Steps:\n" + "\n".join(f"{i}. [{'x' if s.done else ' '}] {s.text}" for i, s in enumerate(saved, 1))


async def step_done(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket = await _stage_ctx(ctx)
    srun = await ctx.office.stages.ensure_run(ticket)
    try:
        steps = await ctx.office.stages.step_done(srun.id, int(args["index"]), str(args.get("note") or ""))
    except (OfficeError, ValueError, KeyError) as exc:
        raise ToolError(str(exc)) from exc
    await ctx.office.runs.event(ctx.run_id, "steps", {"steps": [s.model_dump() for s in steps], "done": int(args["index"])})
    left = [i for i, s in enumerate(steps, 1) if not s.done]
    return f"{len(steps) - len(left)}/{len(steps)} done." + (f" Next: {left[0]}. {steps[left[0] - 1].text}" if left else " All steps done.")


async def stage_complete(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await _stage_ctx(ctx)
    summary = str(args.get("summary") or "").strip()
    if not summary:
        raise ToolError("summary is required: what this stage produced and how it was verified.")
    try:
        result = await office.stages.complete(ticket.id, run_id=ctx.run_id, summary=summary, workspace=ctx.workspace)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    if not result["ok"]:
        lines = [f"- {c['id']}: {c['summary']}" for c in result["failed"]]
        return "The stage cannot finish yet. Failed checks:\n" + "\n".join(lines) + "\nFix them, then call stage_complete again."
    if result.get("interaction_id"):
        who = ", ".join(result.get("approvers") or []) or "the approvers"
        return f"Checks passed. The stage now waits for {who} at gate '{result['gate']}'. End your turn now."
    if result.get("external"):
        return f"Checks passed. Waiting for the external gate '{result['gate']}'. End your turn now."
    if result.get("done"):
        return "Checks passed. The ticket's workflow is complete. End your turn now."
    return f"Checks passed. Next stage: {result['advanced_to']} (a fresh session starts it). End your turn now."


async def stage_reopen(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket = await _stage_ctx(ctx)
    try:
        updated = await ctx.office.stages.reopen(
            ticket.id, str(args["stage"]), reason=str(args.get("reason") or ""), by=ctx.actor.name, run_id=ctx.run_id
        )
    except (OfficeError, KeyError) as exc:
        raise ToolError(str(exc)) from exc
    return f"{updated.key} is back at stage {updated.stage}. End your turn now; a fresh session continues there."


async def dossier_update(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    if ctx.ticket_id is None:
        raise ToolError("This tool works inside a ticket.")
    section = str(args.get("section") or "").strip().lower()
    if section not in DOSSIER_SECTIONS:
        raise ToolError(f"section must be one of: {', '.join(DOSSIER_SECTIONS)}")
    body = str(args.get("body") or "").strip()
    sources = [str(s).strip() for s in args.get("sources") or [] if str(s).strip()]
    for _ in range(3):  # optimistic revisions: retry when someone else just wrote
        doc = await office.tickets.get_document(ctx.ticket_id, "dossier")
        sections = parse_dossier(doc.body if doc else "")
        sections[section] = body + (("\n\n_Nguồn: " + "; ".join(sources) + "_") if sources else "")
        try:
            saved = await office.tickets.put_document(
                ctx.ticket_id, "dossier", render_dossier(sections), author=ctx.actor.name, base_revision=doc.revision if doc else None
            )
        except OfficeError:
            continue
        return f"Dossier section '{section}' saved (revision {saved.revision})."
    raise ToolError("The dossier keeps changing; read it again and retry.")


def parse_dossier(text: str) -> dict[str, str]:
    by_title = {v.lower(): k for k, v in DOSSIER_SECTIONS.items()}
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in text.splitlines():
        m = re.match(r"^##\s+(.+?)\s*$", line)
        if m and (m.group(1).lower() in by_title or m.group(1).lower() in DOSSIER_SECTIONS):
            if current:
                sections[current] = "\n".join(buf).strip()
            current = by_title.get(m.group(1).lower(), m.group(1).lower())
            buf = []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return sections


def render_dossier(sections: dict[str, str]) -> str:
    parts = ["# Dossier"]
    for key, title in DOSSIER_SECTIONS.items():
        if sections.get(key, "").strip():
            parts.append(f"## {title}\n\n{sections[key].strip()}")
    return "\n\n".join(parts) + "\n"


async def plan_deviation(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await _stage_ctx(ctx)
    what = str(args.get("what") or "").strip()
    why = str(args.get("why") or "").strip()
    if not what or not why:
        raise ToolError("what and why are required")
    major = bool(args.get("major"))
    await office.tickets.add_comment(ticket.id, f"↪️ Lệch plan{' (lớn)' if major else ''}: {what} — vì {why}", author_kind="actor", author_id=ctx.actor.name)
    await office.runs.event(ctx.run_id, "deviation", {"what": what, "why": why, "major": major})
    doc = await office.tickets.get_document(ticket.id, "dossier")
    sections = parse_dossier(doc.body if doc else "")
    sections["deviations"] = (sections.get("deviations", "") + f"\n- {what} (vì {why})").strip()
    try:
        await office.tickets.put_document(ticket.id, "dossier", render_dossier(sections), author=ctx.actor.name, base_revision=doc.revision if doc else None)
    except OfficeError:
        pass
    if not major:
        return "Deviation recorded. Continue."
    return await ask_human(
        {
            "roles": ["dev", "lead"],
            "question": f"Mình cần lệch khỏi plan đã duyệt: {what}. Lý do: {why}. Đồng ý không?",
            "options": ["Đồng ý", "Không, giữ đúng plan"],
            "recommended": "Đồng ý",
            "blocking": True,
        },
        ctx,
    )


async def pr_link(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await _stage_ctx(ctx)
    url = str(args.get("url") or "").strip()
    if not re.match(r"^https?://", url):
        raise ToolError("url must be the pull request's http(s) link")
    await office.db.execute("UPDATE tickets SET pr_url = ? WHERE id = ?", (url, ticket.id))
    await office.tickets.add_work_product(ticket.id, "link", url, {"note": "pull request"})
    await office.tickets.add_comment(ticket.id, f"🔗 PR: {url}", author_kind="actor", author_id=ctx.actor.name)
    office.bus.publish("ticket.updated", ticket=(await office.tickets.get(ticket.id)).model_dump())
    return f"PR linked to {ticket.key}."


async def stage_info(_args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await _stage_ctx(ctx)
    tpl = await office.stages.template_of(ticket)
    stage = tpl.stage(ticket.stage)
    srun = await office.stages.ensure_run(ticket)
    return json.dumps(
        {
            "ticket": ticket.key,
            "template": tpl.name,
            "stages": [s.id for s in tpl.stages],
            "current": ticket.stage,
            "attempt": srun.attempt,
            "outputs": stage.outputs if stage else [],
            "checks": stage.checks if stage else [],
            "gate": stage.gate if stage else None,
            "steps": [s.model_dump() for s in srun.steps],
            "mode": ticket.mode,
            "pr_url": ticket.pr_url,
        },
        ensure_ascii=False,
        indent=1,
    )


STAGE_TOOLS: list[ToolDef] = [
    ToolDef(
        "ask_human",
        "Ask the people of this ticket a question, by role (ba = business rules/scope, dev = technical/feasibility, "
        "qe = testability/edge cases, lead, owner = the requester). Prefer multiple choice: up to 6 options and your "
        "recommended one. Batch related questions (max 5 per round). Ask only what you cannot find in code, docs or KB.",
        schema(
            {
                "roles": {"type": "array", "items": {"type": "string", "enum": ROLE_CHOICES}},
                "question": {"type": "string"},
                "options": {"type": "array", "items": {"type": "string"}},
                "recommended": {"type": "string"},
                "why": {"type": "string", "description": "What you found that raised the question (evidence, file:line, page)."},
                "blocking": {"type": "boolean", "description": "true (default): wait for the answer before continuing"},
            },
            required=["roles", "question"],
        ),
        ask_human,
    ),
    ToolDef(
        "plan_steps",
        "Declare the steps of the current stage as a checklist people watch (3–10 short steps). Call again to revise.",
        schema({"steps": {"type": "array", "items": {"type": "string"}}}, required=["steps"]),
        plan_steps,
    ),
    ToolDef(
        "step_done",
        "Tick a step of the current stage (1-based index) with an optional short note.",
        schema({"index": {"type": "integer"}, "note": {"type": "string"}}, required=["index"]),
        step_done,
    ),
    ToolDef(
        "stage_complete",
        "Finish the current stage: the office runs the stage's checks; when they pass it opens the gate for the "
        "right people or moves to the next stage. If checks fail you get the list to fix.",
        schema({"summary": {"type": "string"}}, required=["summary"]),
        stage_complete,
    ),
    ToolDef(
        "stage_reopen",
        "Go back to an earlier stage when something essential is missing (e.g. a requirement found missing while building).",
        schema({"stage": {"type": "string"}, "reason": {"type": "string"}}, required=["stage", "reason"]),
        stage_reopen,
    ),
    ToolDef(
        "dossier_update",
        "Write one section of the ticket dossier (the living record people read later): "
        + ", ".join(DOSSIER_SECTIONS)
        + ". Replaces that section. Cite sources (msg/comment ids, file@commit, page titles).",
        schema(
            {
                "section": {"type": "string", "enum": list(DOSSIER_SECTIONS)},
                "body": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
            },
            required=["section", "body"],
        ),
        dossier_update,
    ),
    ToolDef(
        "plan_deviation",
        "Record that you must deviate from the approved plan. major=true asks a Dev to approve first.",
        schema({"what": {"type": "string"}, "why": {"type": "string"}, "major": {"type": "boolean"}}, required=["what", "why"]),
        plan_deviation,
    ),
    ToolDef("pr_link", "Link the pull request you opened for this ticket.", schema({"url": {"type": "string"}}), pr_link),
    ToolDef("stage_info", "Show the ticket's stages, the current stage's required outputs, checks, gate and steps.", schema({}, required=[]), stage_info, read_only=True),
]
