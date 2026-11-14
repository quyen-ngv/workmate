"""Tools for a ticket that runs through stages (team copilot): ask people by role, declare and tick steps,
finish or reopen a stage, keep the dossier, report plan deviations, link the pull request."""

from __future__ import annotations

import contextlib
import json
import re
from typing import Any

from actoroffice.domain.dossier import DOSSIER_SECTIONS, SYNC_SECTION, keep_sync_line, parse_dossier, render_dossier
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.team import ROLE_VI
from actoroffice.domain.work import InteractionKind

from .registry import ToolCtx, ToolDef, ToolError, schema

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
        text = body + (("\n\n_Nguồn: " + "; ".join(sources) + "_") if sources else "")
        # The office's "Đã cập nhật theo <main> …" line survives the AI rewriting the tests section (SI-21)
        sections[section] = keep_sync_line(sections.get(section, ""), text) if section == SYNC_SECTION else text
        try:
            saved = await office.tickets.put_document(
                ctx.ticket_id, "dossier", render_dossier(sections), author=ctx.actor.name, base_revision=doc.revision if doc else None
            )
        except OfficeError:
            continue
        return f"Dossier section '{section}' saved (revision {saved.revision})."
    raise ToolError("The dossier keeps changing; read it again and retry.")


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


async def source_skip(args: dict[str, Any], ctx: ToolCtx) -> str:
    """A source of the ticket does not bear on it: record why (SI-39). Reading is only ever taken from the activity."""
    ticket = await _stage_ctx(ctx)
    try:
        skipped = await ctx.office.sources.skip(ticket, str(args.get("ref") or ""), str(args.get("reason") or ""))
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    if not skipped:
        return "That source is already read: nothing to skip."
    return f"Recorded as not relevant: {', '.join(skipped)}. Write it in dossier section links with the reason."


STAGE_TOOLS.append(
    ToolDef(
        "source_skip",
        "Record that one source of this ticket (a ticket key, file or URL listed under 'Sources of this ticket') does "
        "not bear on it, with the reason (required). Sources you read are recorded from your activity: do not call this for them.",
        schema({"ref": {"type": "string"}, "reason": {"type": "string"}}, required=["ref", "reason"]),
        source_skip,
    )
)


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
    target = await _save_artifact(ctx, ticket, source, kind, str(args.get("caption") or ""), args.get("ac"))
    return f"Saved {kind} {target.name} with {ticket.key} (people see it in the ticket topic)."


async def _save_artifact(ctx: ToolCtx, ticket: Any, source: Any, kind: str, caption: str, ac: Any) -> Any:
    """Copy a checked file to data_dir/artifacts/<KEY>/, record it as a work product, tell the channels."""
    import shutil

    office = ctx.office
    folder = office.settings.data_dir / "artifacts" / ticket.key
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    n = 1
    while target.exists():
        target = folder / f"{source.stem}-{n}{source.suffix}"
        n += 1
    shutil.copy2(source, target)
    caption = caption.strip()[:300]
    await office.tickets.add_work_product(ticket.id, kind, str(target), {"note": caption, "ac": ac})
    office.bus.publish("ticket.artifact", ticket_id=ticket.id, path=str(target), kind=kind, caption=caption)
    return target


def _slug(text: str) -> str:
    """A file name part without accents: "AC-9 Bấm ảnh" → "ac-9-bam-anh"."""
    import unicodedata

    plain = unicodedata.normalize("NFKD", text.replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:50].strip("-")


async def ui_screenshot(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Screenshot of a page of the ticket's temporary app (project profile `ui`), saved as an artifact of an AC."""
    from urllib.parse import urljoin, urlsplit

    from actoroffice.engine.browser import BrowserError, is_loopback, parse_actions
    from actoroffice.services.checks import ac_id
    from actoroffice.services.ui_preview import UiEnvError, ui_spec

    office = ctx.office
    if ctx.ticket_id is None or ctx.workspace is None:
        raise ToolError("ui_screenshot works inside a ticket.")
    path = str(args.get("path") or "").strip()
    if "://" in path and not is_loopback(path):
        # checked before anything starts: the browser only ever opens this machine (no outside network)
        raise ToolError(f"Refused: only loopback addresses (127.0.0.1, localhost, ::1) may be opened, not {path[:120]}")
    try:
        actions = parse_actions(args.get("actions"))
    except ValueError as exc:
        raise ToolError(f"actions: {exc}") from exc
    width = max(320, min(int(args.get("width") or 1280), 3840))
    height = max(240, min(int(args.get("height") or 800), 2160))
    ac = ac_id(args.get("ac"))
    caption = str(args.get("caption") or "").strip()
    ticket = await office.tickets.get(ctx.ticket_id)
    project = await office.projects.get(ticket.project_id) if ticket.project_id else None
    if ui_spec(project.profile if project else None) is None:
        raise ToolError("The project has no UI profile (ui.start and ui.url): ask a Dev to add it in Settings, or write 'không chụp được: …' in the handoff.")

    async def env_error(reason: str, tail: str = "") -> ToolError:
        await office.tickets.add_work_product(ticket.id, "ui_error", reason, {"ac": ac, "reason": reason})
        note = f"\nLast output lines:\n{office.redact(tail)}" if tail else ""
        return ToolError(
            f"Environment error, no screenshot: {reason}.{note}\nIt is recorded for {ac or 'this ticket'}; if it cannot be fixed, "
            "write 'không chụp được: <lý do>' on the AC's line in the handoff."
        )

    try:
        preview = await office.ui.ensure(ctx.run_id, ticket, project, ctx.workspace)
    except UiEnvError as exc:
        raise await env_error(exc.reason, exc.tail) from None
    entry = preview.url
    parts = urlsplit(entry)
    target = path if "://" in path else (urljoin(f"{parts.scheme}://{parts.netloc}/", path.lstrip("/")) if path else entry)
    if not is_loopback(target):
        raise ToolError("Refused: only loopback addresses may be opened")
    slug = _slug(f"{ac or 'ui'} {caption}") or "ui"
    out = preview.shots / f"{slug}.png"
    n = 1
    while out.exists():
        out = preview.shots / f"{slug}-{n}.png"
        n += 1
    try:
        result = await office.ui.browser.screenshot(entry, target, out, width=width, height=height, full_page=bool(args.get("full_page")), actions=actions)
    except BrowserError as exc:
        raise await env_error(str(exc)) from None
    except Exception as exc:  # a selector that never shows up, a page error: the AI fixes its call
        raise ToolError(preview.mask(office.redact(f"Screenshot failed: {type(exc).__name__}: {str(exc)[:600]}"))) from None
    saved = await _save_artifact(ctx, ticket, out, "screenshot", caption, ac)
    blocked = f" Blocked {len(result.blocked)} request(s) to other hosts: {', '.join(result.blocked[:3])}." if result.blocked else ""
    shown = preview.mask(target).replace(f"{parts.scheme}://{parts.netloc}", "")
    return f"Saved screenshot {saved.name} of {shown or '/'} ({width}x{height}) for {ac or 'no AC'} with {ticket.key}.{blocked}"


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
STAGE_TOOLS.append(
    ToolDef(
        "ui_screenshot",
        "Screenshot a page of this ticket's UI for an acceptance criterion tagged [ui]. The office starts the project's "
        "app on this machine (profile ui: seed, start, url; signed in), opens path in a headless browser (only "
        "localhost; requests to other hosts are blocked), runs the actions in order, saves a PNG as an artifact of the "
        "AC that people see at the pr pane, the gate card and the PR description. path: a path of the app like "
        "'/team' (empty = the profile url). actions: [{\"click\": css}, {\"fill\": css, \"text\": \"…\"}, "
        "{\"wait\": css or milliseconds}]. The app stops when your turn ends.",
        schema(
            {
                "path": {"type": "string"},
                "ac": {"type": "string", "description": "AC id it proves, e.g. AC-3"},
                "caption": {"type": "string"},
                "width": {"type": "integer"},
                "height": {"type": "integer"},
                "full_page": {"type": "boolean"},
                "actions": {"type": "array", "items": {"type": "object"}},
            },
            required=["ac", "caption"],
        ),
        ui_screenshot,
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


async def doc_check(args: dict[str, Any], ctx: ToolCtx) -> str:
    """The Doc Core rules on the documents of the current stage (or on one document), before the gate does it."""
    office = ctx.office
    ticket = await _stage_ctx(ctx)
    key = str(args.get("key") or "").strip()
    try:
        if key:
            findings = await office.docs.check(ticket, keys={office.docs.core_key(ticket, key)})
        else:
            findings = await office.docs.check(ticket, await office.stages.stage_def(ticket))
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    errors = [f for f in findings if f.status == "open" and f.severity == "error"]
    warnings = [f for f in findings if f.status == "open" and f.severity != "error"]
    if not errors and not warnings:
        return "Tài liệu đúng mẫu và khớp: không có điểm nào cần sửa."
    lines = [f"- [{'LỖI' if f.severity == 'error' else 'cảnh báo'}] {f.rule} · {f.doc}{' · ' + f.item if f.item else ''}: {f.message}" for f in errors + warnings]
    tail = "Sửa mọi LỖI rồi gọi lại doc_check." if errors else "Không còn lỗi. Cảnh báo sẽ hiện trên thẻ duyệt: sửa nếu sửa được."
    return f"{len(errors)} lỗi, {len(warnings)} cảnh báo.\n" + "\n".join(lines) + f"\n{tail}"


async def doc_template(args: dict[str, Any], ctx: ToolCtx) -> str:
    try:
        return ctx.office.docs.template_text(str(args.get("type") or "").strip(), await ctx.office.tickets.get(ctx.ticket_id) if ctx.ticket_id else None)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc


STAGE_TOOLS += [
    ToolDef(
        "doc_check",
        "Check the documents of the current stage against the office's document standard: template, coverage (every requirement, AC and rule is "
        "traced), sources that exist and still say what you read, open questions. Returns every problem with the exact place and how to fix it. "
        "Call it before stage_complete in the analyze, breakdown and design stages.",
        schema({"key": {"type": "string", "description": "One document key (e.g. design, spec.T-2). Omit to check the stage's documents."}}, required=[]),
        doc_check,
        read_only=True,
    ),
    ToolDef(
        "doc_template",
        "The standard template of a document type (analysis, breakdown, spec, design): the headings to keep and how items, traces and sources are written.",
        schema({"type": {"type": "string", "enum": ["analysis", "breakdown", "spec", "design"]}}, required=["type"]),
        doc_template,
        read_only=True,
    ),
]
