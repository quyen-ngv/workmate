"""Tools for a ticket that runs through stages (team copilot): ask people by role, declare and tick steps,
finish or reopen a stage, keep the dossier, report plan deviations, link the pull request."""

from __future__ import annotations

import contextlib
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


async def _stage_run(ctx: ToolCtx, ticket: Any) -> Any:
    """The stage run this run works on. The executor opened it when the run started; after a passing stage_complete
    it is the run just finished, and a late step_done/stage_info must not open a new attempt (which would cancel the
    gate card people are about to answer, SI-16)."""
    return await ctx.office.stages.current_run(ticket) or await ctx.office.stages.ensure_run(ticket)


async def plan_steps(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket = await _stage_ctx(ctx)
    steps = [str(s) for s in args.get("steps") or []]
    srun = await _stage_run(ctx, ticket)
    try:
        saved = await ctx.office.stages.set_steps(srun.id, steps)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    await ctx.office.runs.event(ctx.run_id, "steps", {"steps": [s.model_dump() for s in saved]})
    return "Steps:\n" + "\n".join(f"{i}. [{'x' if s.done else ' '}] {s.text}" for i, s in enumerate(saved, 1))


async def step_done(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket = await _stage_ctx(ctx)
    srun = await _stage_run(ctx, ticket)
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


async def review_reply(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket = await _stage_ctx(ctx)
    try:
        finding = await ctx.office.review.reply(ticket.id, str(args.get("id") or ""), str(args.get("action") or ""), str(args.get("note") or ""))
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    return f"Reply saved for {finding['id']} ({finding['reply']['action']}). People see it at the gate. Call stage_complete when every medium/high finding has a reply."


async def stage_reopen(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket = await _stage_ctx(ctx)
    try:
        updated = await ctx.office.stages.reopen(
            ticket.id, str(args["stage"]), reason=str(args.get("reason") or ""), by=ctx.actor.name, run_id=ctx.run_id
        )
    except (OfficeError, KeyError) as exc:
        raise ToolError(str(exc)) from exc
    return f"{updated.key} is back at stage {updated.stage}. End your turn now; a fresh session continues there."


async def stage_switch(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket = await _stage_ctx(ctx)
    try:
        updated = await ctx.office.stages.switch_template(
            ticket.id, str(args["template"]), reason=str(args.get("reason") or ""), by=ctx.actor.name, run_id=ctx.run_id
        )
    except (OfficeError, KeyError) as exc:
        raise ToolError(str(exc)) from exc
    return f"{updated.key} now follows workflow '{updated.stage_template}' from stage {updated.stage}. End your turn now; a fresh session continues there."


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
    with contextlib.suppress(OfficeError):
        await office.tickets.put_document(ticket.id, "dossier", render_dossier(sections), author=ctx.actor.name, base_revision=doc.revision if doc else None)
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
    srun = await _stage_run(ctx, ticket)
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
        "review_reply",
        "Answer one finding of the independent review (check review_clean) by its id (F1, F2…): action fixed after you "
        "fixed it (the next review round re-checks it), or wontfix with the reason in note. Every medium/high finding "
        "needs a reply before the stage can finish; people see the replies at the gate.",
        schema(
            {
                "id": {"type": "string"},
                "action": {"type": "string", "enum": ["fixed", "wontfix"]},
                "note": {"type": "string", "description": "What you changed (fixed) or why you do not fix it (wontfix, required)."},
            },
            required=["id", "action", "note"],
        ),
        review_reply,
    ),
    ToolDef(
        "stage_reopen",
        "Go back to an earlier stage when something essential is missing (e.g. a requirement found missing while building).",
        schema({"stage": {"type": "string"}, "reason": {"type": "string"}}, required=["stage", "reason"]),
        stage_reopen,
    ),
    ToolDef(
        "stage_switch",
        "Move the ticket to the other workflow: 'quick' for a small, clear change (one short plan, one approval), "
        "'code' when it turns out bigger or has decisions people must make (requirements, plan with options). Put what "
        "you found in reason (cause with file:line, what to change): the next session starts from it.",
        schema({"template": {"type": "string", "enum": ["quick", "code"]}, "reason": {"type": "string"}}, required=["template", "reason"]),
        stage_switch,
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


# ---- rules ---------------------------------------------------------------------------------------------


async def rule_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    rules = await ctx.office.rules.search(str(args.get("query") or ""), limit=int(args.get("limit") or 8))
    if not rules:
        return "No active rule matches."
    return "\n\n".join(f"[{r.id} · {r.kind.value} · {r.scope} · stage {r.stage}] {r.title}\n{r.body[:1500]}" for r in rules)


async def rule_propose(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    scope = str(args.get("scope") or "")
    if not scope and ctx.ticket_id is not None:
        ticket = await office.tickets.get(ctx.ticket_id)
        scope = f"team:{ticket.team_id}" if ticket.team_id else "global"
    try:
        rule = await office.rules.propose(
            title=str(args.get("title") or ""),
            body=str(args.get("body") or ""),
            kind="guideline",
            scope=scope or "global",
            stage=str(args.get("stage") or "any"),
            by=ctx.actor.name,
            ai=True,
        )
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    return f"Proposed {rule.id} (draft). The scope's owner decides; it does not apply until approved."


STAGE_TOOLS += [
    ToolDef(
        "rule_search",
        "Search the team's active rules (guidelines, checks, policies, gates) by keywords.",
        schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, required=["query"]),
        rule_search,
        read_only=True,
    ),
    ToolDef(
        "rule_propose",
        "Propose a team guideline learned from this work (e.g. a convention reviewers keep asking for). It stays a "
        "draft until the scope's owner approves it.",
        schema(
            {"title": {"type": "string"}, "body": {"type": "string"}, "stage": {"type": "string"}, "scope": {"type": "string"}},
            required=["title", "body"],
        ),
        rule_propose,
    ),
]


# ---- artifacts ---------------------------------------------------------------------------------------------

ARTIFACT_KINDS = {"screenshot", "video", "trace", "report", "file"}
ARTIFACT_MAX = 45 * 1024 * 1024


async def artifact_add(args: dict[str, Any], ctx: ToolCtx) -> str:
    """A file people should see (UI screenshot per AC, test video/trace, report): kept with the ticket, shown on
    Telegram and the web, linked from the PR description."""
    import shutil
    from pathlib import Path

    office = ctx.office
    if ctx.ticket_id is None or ctx.workspace is None:
        raise ToolError("artifact_add works inside a ticket.")
    kind = str(args.get("kind") or "file")
    if kind not in ARTIFACT_KINDS:
        raise ToolError(f"kind: {', '.join(sorted(ARTIFACT_KINDS))}")
    source = Path(str(args.get("path") or ""))
    source = (ctx.workspace / source).resolve() if not source.is_absolute() else source.resolve()
    if not source.is_relative_to(ctx.workspace.resolve()) or not source.is_file():
        raise ToolError("path must be a file inside your working directory")
    if source.stat().st_size > ARTIFACT_MAX:
        raise ToolError("file too large (max 45 MB)")
    ticket = await office.tickets.get(ctx.ticket_id)
    folder = office.settings.data_dir / "artifacts" / ticket.key
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    n = 1
    while target.exists():
        target = folder / f"{source.stem}-{n}{source.suffix}"
        n += 1
    shutil.copy2(source, target)
    caption = str(args.get("caption") or "").strip()[:300]
    await office.tickets.add_work_product(ticket.id, kind, str(target), {"note": caption, "ac": args.get("ac")})
    office.bus.publish("ticket.artifact", ticket_id=ticket.id, path=str(target), kind=kind, caption=caption)
    return f"Saved {kind} {target.name} with {ticket.key} (people see it in the ticket topic)."


STAGE_TOOLS.append(
    ToolDef(
        "artifact_add",
        "Attach a file people should see: a UI screenshot or video per acceptance criterion (Playwright), a test "
        "trace or report. Give the AC id it proves when there is one.",
        schema(
            {
                "path": {"type": "string"},
                "kind": {"type": "string", "enum": sorted(ARTIFACT_KINDS)},
                "caption": {"type": "string"},
                "ac": {"type": "string"},
            },
            required=["path", "kind"],
        ),
        artifact_add,
    )
)


# ---- shared environments -----------------------------------------------------------------------------------


async def access_list(_args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await office.tickets.get(ctx.ticket_id) if ctx.ticket_id else None
    resources = await office.access.resources_for(ticket.team_id if ticket else None)
    active = {g.resource_id: g for g, _r in (await office.access.active(ticket.id) if ticket else [])}
    if not resources:
        return "No shared environments are registered for this team. Use local services (docker compose, test containers)."
    lines = []
    for r in resources:
        grant = active.get(r.id)
        if grant:
            env = ", ".join((grant.envelope.get("env") or {}).keys()) or grant.envelope.get("env_secret") or "(tools only)"
            state = f"GRANTED {grant.level.value} until {grant.expires_at[:16]} → env {env}"
        else:
            state = "not granted"
        lines.append(f"- {r.name} [{r.kind.value}, {r.env}, max {r.max_level.value}]: {state}")
    return "\n".join(lines)


async def env_access(args: dict[str, Any], ctx: ToolCtx) -> str:
    if ctx.ticket_id is None:
        raise ToolError("env_access works inside a ticket.")
    try:
        item = await ctx.office.access.request(
            ctx.ticket_id,
            str(args.get("resource") or ""),
            str(args.get("level") or "read"),
            str(args.get("reason") or ""),
            float(args.get("ttl_h") or 4),
            actor_id=ctx.actor.id,
            run_id=ctx.run_id,
        )
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    who = ", ".join(item.payload.get("approver_names") or []) or "a Dev/Lead"
    ticket = await ctx.office.tickets.get(ctx.ticket_id)
    if ticket.stage == "plan":
        return f"Access request {item.id} recorded: {who} approves it together with the plan. Continue the plan."
    return f"Access request {item.id} sent to {who}. Save your progress and end your turn; you are woken when it is decided."


STAGE_TOOLS += [
    ToolDef("access_list", "Shared environments (services, databases, brokers, logs) and what this ticket may reach now.", schema({}, required=[]), access_list, read_only=True),
    ToolDef(
        "env_access",
        "Ask for time-limited access to a shared environment (never production). level: read | write-sandboxed | "
        "write-shared. Say exactly why (which test or check needs it).",
        schema(
            {
                "resource": {"type": "string"},
                "level": {"type": "string", "enum": ["read", "write-sandboxed", "write-shared"]},
                "reason": {"type": "string"},
                "ttl_h": {"type": "number"},
            },
            required=["resource", "level", "reason"],
        ),
        env_access,
    ),
]


# ---- knowledge base ----------------------------------------------------------------------------------------


async def kb_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await office.tickets.get(ctx.ticket_id) if ctx.ticket_id else None
    kinds = [str(k) for k in args.get("kinds") or []] or None
    items = await office.knowledge.search(str(args.get("query") or ""), team_id=ticket.team_id if ticket else None, kinds=kinds, limit=min(int(args.get("limit") or 10), 25))
    if not items:
        return "Nothing in the knowledge base."
    out = []
    for item in items:
        extra = f"\n  data: {json.dumps(item.data, ensure_ascii=False)[:1500]}" if item.data else ""
        anchor = f"\n  anchor: {json.dumps(item.anchor)}" if item.anchor else ""
        out.append(f"- {item.line()}{anchor}{extra}")
    return "\n".join(out)


async def kb_note(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await office.tickets.get(ctx.ticket_id) if ctx.ticket_id else None
    anchor = args.get("anchor") if isinstance(args.get("anchor"), dict) else None
    if anchor and anchor.get("path") and not anchor.get("commit") and ctx.workspace is not None:
        import asyncio as _asyncio

        proc = await _asyncio.create_subprocess_exec(
            "git", "-C", str(ctx.workspace), "rev-parse", "--short", "HEAD", stdout=_asyncio.subprocess.PIPE, stderr=_asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            anchor = {**anchor, "commit": out.decode().strip()}
    try:
        item = await office.knowledge.note(
            kind=str(args.get("kind") or ""),
            title=str(args.get("title") or ""),
            content=str(args.get("content") or ""),
            by=ctx.actor.name,
            team_id=ticket.team_id if ticket else None,
            project_id=ticket.project_id if ticket else None,
            ticket_id=ticket.id if ticket else None,
            sources=[str(s) for s in args.get("sources") or []],
            anchor=anchor,
            data=args.get("data") if isinstance(args.get("data"), dict) else None,
        )
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    return f"KB #{item.id} saved ({item.kind}, {item.status}). People confirm it at the end of the ticket."


STAGE_TOOLS += [
    ToolDef(
        "kb_search",
        "Search the team's knowledge base (business facts, code notes, decisions, service contracts, business flows).",
        schema({"query": {"type": "string"}, "kinds": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}}, required=["query"]),
        kb_search,
        read_only=True,
    ),
    ToolDef(
        "kb_note",
        "Save or update a reusable knowledge item (draft until people confirm). kind: fact | code_note (anchor.path "
        "required) | decision | contract (data: spec, auth kind, owner, samples, quirks — never secrets) | flow (data: "
        "steps with service, trigger, writes, emits, consumes, state_before, invariants).",
        schema(
            {
                "kind": {"type": "string", "enum": ["fact", "code_note", "decision", "contract", "flow"]},
                "title": {"type": "string"},
                "content": {"type": "string"},
                "sources": {"type": "array", "items": {"type": "string"}},
                "anchor": {"type": "object"},
                "data": {"type": "object"},
            },
            required=["kind", "title", "content"],
        ),
        kb_note,
    ),
]
