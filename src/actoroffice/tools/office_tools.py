"""Tools every actor gets: tickets, documents, asking the user, finding colleagues."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.run import WakeSource
from actoroffice.domain.work import InteractionKind, Priority, ProjectAccess, TicketStatus

from .registry import ToolCtx, ToolDef, ToolError, schema, source_ref

S = TicketStatus


async def _ticket_ref(args: dict[str, Any], ctx: ToolCtx) -> int:
    key = args.get("ticket")
    if key:
        return (await ctx.office.tickets.get(str(key))).id
    if ctx.ticket_id is None:
        raise ToolError("No current ticket; pass `ticket` (e.g. AO-12).")
    return ctx.ticket_id


async def ticket_get(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await office.tickets.get(await _ticket_ref(args, ctx))
    docs = await office.tickets.list_documents(ticket.id)
    comments = await office.tickets.comments(ticket.id, limit=200)
    children = await office.tickets.list(parent_id=ticket.id)
    blockers = await office.tickets.open_blockers(ticket.id)
    assignee = (await office.org.get_actor(ticket.assignee_actor_id)).name if ticket.assignee_actor_id else None
    return json.dumps(
        {
            "key": ticket.key,
            "title": ticket.title,
            "type": ticket.type,
            "status": ticket.status.value,
            "priority": ticket.priority.value,
            "due": ticket.due_at,
            "parent": (await office.tickets.get(ticket.parent_id)).key if ticket.parent_id else None,
            "assignee": assignee,
            "description": ticket.description,
            "open_blockers": blockers,
            "documents": [{"key": d.key, "revision": d.revision, "updated_by": d.updated_by} for d in docs],
            "sub_tickets": [{"key": c.key, "title": c.title, "status": c.status.value, "due": c.due_at} for c in children],
            "recent_comments": [
                {"by": c.author_id or c.author_kind, "kind": c.author_kind, "body": c.body} for c in comments[-int(args.get("comments", 15)) :]
            ],
        },
        ensure_ascii=False,
        indent=1,
    )


async def ticket_comment(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket_id = await _ticket_ref(args, ctx)
    comment = await ctx.office.tickets.add_comment(ticket_id, args["body"], author_kind="actor", author_id=ctx.actor.name)
    return f"Comment #{comment.id} added."


async def ticket_set_status(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Final disposition. Mirrors Paperclip's rule: a waiting status needs a real waiting path."""
    office = ctx.office
    ticket_id = await _ticket_ref(args, ctx)
    status = S(args["status"])
    summary = (args.get("comment") or "").strip()
    if status not in (S.DONE, S.IN_REVIEW, S.BLOCKED, S.TODO):
        raise ToolError("status must be one of: done, in_review, blocked, todo")
    if not summary:
        raise ToolError("comment is required: say what was done, or what you are waiting for and from whom.")
    current = await office.tickets.get(ticket_id)
    if current.stage and status == S.DONE:
        raise ToolError("This ticket runs through stages: finish the stage with stage_complete (people pass the gates).")
    if status == S.DONE:
        open_children = [c.key for c in await office.tickets.list(parent_id=ticket_id) if c.status not in (S.DONE, S.CANCELLED)]
        if open_children:
            raise ToolError(
                f"Sub-tickets {', '.join(open_children)} are not finished. Set blocked with blocked_by them (you are woken "
                "when they finish), or cancel the ones no longer needed."
            )
        missing = await _missing_docs(ctx, ticket_id) if ctx.position.done_docs else []
        if CLEAN_GIT in ctx.position.done_docs and ctx.workspace is not None:
            dirty = await _uncommitted(ctx.workspace)
            if dirty:
                raise ToolError(
                    "Definition of done: the branch has uncommitted changes:\n" + dirty[:800] + "\nCommit them (lock files "
                    "such as uv.lock belong in the commit) or ignore generated files in .gitignore, then set done."
                )
        if missing:
            raise ToolError(
                f"Definition of done for {ctx.position.title}: write the document(s) {', '.join(missing)} first "
                "(doc_put, template in skill doc-standards), then set done."
            )
    if status == S.IN_REVIEW and current.stage:
        # A ticket on stages may wait for a person without a question card (open a PR, give access, "stop here"):
        # the comment says what for. It does not continue by itself; an answer, a comment or a direction wakes it.
        try:
            ticket = await office.tickets.transition(ticket_id, S.IN_REVIEW, by=ctx.actor.name, run_id=ctx.run_id, comment=summary)
        except OfficeError as exc:
            raise ToolError(str(exc)) from exc
        return f"{ticket.key} now waits for a person. End your turn; you are woken when someone answers, comments or steers."
    if status == S.IN_REVIEW and not await office.interactions.pending(ticket_id=ticket_id):
        answered = [i for i in await office.interactions.for_run(ctx.run_id) if i.status.value == "answered"]
        if answered:
            raise ToolError(
                "Your question was already answered while you were working. Set the ticket to todo with a short note "
                "and end your turn: you will be woken right away with the answer."
            )
        raise ToolError(
            "in_review needs a real waiting path: call ask_user first (question/confirmation), "
            "or hand the ticket to a colleague with ticket_create / blocked."
        )
    if status == S.BLOCKED:
        blocked_by = [str(k) for k in args.get("blocked_by") or []]
        if not blocked_by:
            raise ToolError("blocked needs blocked_by: the ticket keys that must finish first.")
        for key in blocked_by:
            await office.tickets.add_blocker(ticket_id, (await office.tickets.get(key)).id)
    try:
        ticket = await office.tickets.transition(ticket_id, status, by=ctx.actor.name, run_id=ctx.run_id, comment=summary)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    return f"{ticket.key} is now {ticket.status.value}."


async def _missing_docs(ctx: ToolCtx, ticket_id: int) -> list[str]:
    office = ctx.office
    ticket = await office.tickets.get(ticket_id)
    holders = {ticket.id, (await office.tickets.root_of(ticket.id)).id}
    if ticket.parent_id:
        holders.add(ticket.parent_id)
    present = {d.key for t in holders for d in await office.tickets.list_documents(t)}
    return [need for need in ctx.position.done_docs if need != CLEAN_GIT and not set(need.split("|")) & present]


CLEAN_GIT = "clean-git"  # done_docs item: a git workspace must have no uncommitted changes


async def _uncommitted(workspace: Path) -> str:
    if not (workspace / ".git").exists():
        return ""  # not a git workspace (scratch folder): nothing to check
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(workspace), "status", "--porcelain", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env={**os.environ, "GIT_CEILING_DIRECTORIES": str(workspace.parent)},
    )
    out, _ = await proc.communicate()
    return out.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""


async def ticket_create(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    assignee_id = None
    if name := args.get("assignee"):
        actor = await office.org.find_actor(str(name))
        if actor is None:
            raise ToolError(f"No actor named {name}. Use list_actors.")
        assignee_id = actor.id
    blocked_by = [(await office.tickets.get(str(k))).id for k in args.get("blocked_by") or []]
    parent_id = ctx.ticket_id if args.get("as_sub_ticket", True) else None
    ticket = await office.tickets.create(
        args["title"],
        description=args.get("description", ""),
        type=args.get("type", "task"),
        priority=Priority(args.get("priority", "medium")),
        assignee_actor_id=assignee_id,
        parent_id=parent_id,
        created_by=ctx.actor.name,
        status=S.BLOCKED if blocked_by else S.TODO,
        blocked_by=blocked_by,
        due_at=_due(args.get("due")),
    )
    return f"Created {ticket.key} ({ticket.status.value}), assigned to {args.get('assignee') or 'nobody'}."


MANAGERS = {"pm", "lead"}  # positions that may change anyone's ticket (on the user's or their own decision)
UPDATE_TO = {"todo": S.TODO, "blocked": S.BLOCKED, "backlog": S.BACKLOG, "cancelled": S.CANCELLED}


async def ticket_update(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Apply a decision about an existing ticket: resume it, cancel it, block it, park it, or hand it to someone."""
    office = ctx.office
    if not args.get("ticket"):
        raise ToolError("Pass `ticket` (e.g. AO-12). For your own current ticket use ticket_set_status.")
    ticket = await office.tickets.get(str(args["ticket"]))
    comment = str(args.get("comment") or "").strip()
    if not comment:
        raise ToolError("comment is required: what changes and why (quote the user's decision when it came from them).")
    manager = ctx.position.name in MANAGERS
    own = ticket.assignee_actor_id == ctx.actor.id or ticket.created_by == ctx.actor.name
    if not (manager or own):
        raise ToolError(
            f"Only the assignee, the creator, a PM or a lead can change {ticket.key}. Ask one of them (@mention), "
            "or tell the user they can change it on the ticket page."
        )
    if ticket.id == ctx.ticket_id:
        raise ToolError("This is your current ticket: use ticket_set_status.")
    if ticket.checkout_run_id:
        raise ToolError(f"{ticket.key} is being worked on right now. Leave a ticket_comment (it is read on the next step) or try later.")
    done: list[str] = []
    try:
        if args.get("assignee"):
            actor = await office.org.find_actor(str(args["assignee"]))
            if actor is None:
                raise ToolError(f"No actor named {args['assignee']}. Use list_actors.")
            ticket = await office.tickets.assign(ticket.key, actor.id, by=ctx.actor.name)
            done.append(f"giao cho {actor.name}")
        status = args.get("status")
        if status:
            to = UPDATE_TO.get(str(status))
            if to is None:
                raise ToolError(f"status must be one of {', '.join(UPDATE_TO)} (done is set by the assignee with ticket_set_status).")
            if to == S.BLOCKED:
                keys = [str(k) for k in args.get("blocked_by") or []]
                if not keys:
                    raise ToolError("blocked needs blocked_by: the ticket keys that must finish first.")
                for key in keys:
                    await office.tickets.add_blocker(ticket.id, (await office.tickets.get(key)).id)
            ticket = await office.tickets.transition(ticket.key, to, by=ctx.actor.name, comment=comment)
            if to == S.TODO and ticket.assignee_actor_id:
                # "todo" by itself wakes nobody: the assignee must hear that the ticket goes on.
                await office.wakes.enqueue(ticket.assignee_actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"resumed_by": ctx.actor.name})
            done.append(f"trạng thái {to.value}")
        else:
            await office.tickets.add_comment(ticket.key, comment, author_kind="actor", author_id=ctx.actor.name)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    if not done:
        raise ToolError("Nothing to change: pass status and/or assignee.")
    office.scheduler.kick()
    return f"{ticket.key}: " + ", ".join(done) + "."


async def project_access_request(args: dict[str, Any], ctx: ToolCtx) -> str:
    """The project is read-only but the work must change its files: ask the user to allow it (a card with buttons)."""
    from actoroffice.services.projects import git_branch

    office = ctx.office
    ticket = await office.tickets.get(ctx.ticket_id) if ctx.ticket_id else None
    project_id = ticket.project_id if ticket else None
    if project_id is None and ctx.conversation_id:
        project_id = (await office.chat.get(ctx.conversation_id)).project_id
    if project_id is None:
        raise ToolError("There is no project here.")
    project = await office.projects.get(project_id)
    if not project.path:
        raise ToolError("This project has no folder. Ask the user to set the project folder first.")
    if project.access != ProjectAccess.READ_ONLY:
        return f"{project.name} already allows changes ({project.access.value}); just do the work."
    for item in await office.interactions.pending():
        if item.kind == InteractionKind.APPROVE_ACCESS and item.payload.get("project_id") == project.id:
            return "A request for this project is already waiting for the user. Set your ticket in_review with a short note."
    is_repo = await asyncio.to_thread(git_branch, Path(project.path).resolve())
    options = (["worktree"] if is_repo else []) + ["direct"]
    await office.interactions.create(
        InteractionKind.APPROVE_ACCESS,
        {
            "project_id": project.id, "project": project.name, "path": project.path, "options": options,
            "reason": str(args.get("reason") or "").strip(), "actor": ctx.actor.name,
            "ticket": ticket.key if ticket else None, "conversation_id": ctx.conversation_id,
        },
        actor_id=ctx.actor.id,
        ticket_id=ctx.ticket_id,
        run_id=ctx.run_id,
    )
    if ticket:
        return (
            "The request is shown to the user with buttons. Set your ticket in_review now with a short note and end your "
            "turn: you are woken as soon as they decide, and the project folder is then writable."
        )
    return "The request is shown to the user with buttons. Tell them briefly why you need it."


def _due(value: Any) -> str | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value).strip()[:10]).isoformat()
    except ValueError as exc:
        raise ToolError("due must be a date YYYY-MM-DD") from exc


async def doc_get(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket_id = await _ticket_ref(args, ctx)
    doc = await ctx.office.tickets.get_document(ticket_id, args["key"])
    if doc is None:
        return f"No document '{args['key']}' yet (use base_revision null to create it)."
    return json.dumps({"key": doc.key, "revision": doc.revision, "updated_by": doc.updated_by, "body": doc.body}, ensure_ascii=False)


async def doc_put(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket_id = await _ticket_ref(args, ctx)
    base = args.get("base_revision")
    try:
        doc = await ctx.office.tickets.put_document(
            ticket_id, args["key"], args["body"], author=ctx.actor.name, base_revision=int(base) if base is not None else None
        )
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    await ctx.office.docs.on_saved(await ctx.office.tickets.get(ticket_id), doc.key)  # what this revision is written from
    return f"Saved '{doc.key}' revision {doc.revision}."


async def ask_user(args: dict[str, Any], ctx: ToolCtx) -> str:
    options = [str(o) for o in args.get("options") or []]
    item = await ctx.office.interactions.create(
        InteractionKind.QUESTION,
        {"question": args["question"], "options": options, "asked_by": ctx.actor.name},
        actor_id=ctx.actor.id,
        ticket_id=ctx.ticket_id,
        run_id=ctx.run_id,
    )
    return (
        f"Question {item.id} was sent to the user. Do not wait or poll. Save your progress "
        "(comment or document), set the ticket to in_review with a short note, and end your turn. "
        "You will be woken with the answer."
    )


async def list_actors(_args: dict[str, Any], ctx: ToolCtx) -> str:
    org = ctx.office.org
    positions = {p.id: p for p in await org.list_positions()}
    rows = [
        {"name": a.name, "position": positions[a.position_id].title, "specialty": a.specialty, "status": a.status.value}
        for a in await org.list_actors()
    ]
    return json.dumps(rows, ensure_ascii=False)


async def _project_root(ctx: ToolCtx) -> Path | None:
    office = ctx.office
    project_id = None
    if ctx.ticket_id is not None:
        project_id = (await office.tickets.get(ctx.ticket_id)).project_id
    elif ctx.conversation_id:
        project_id = (await office.chat.get(ctx.conversation_id)).project_id
    if not project_id:
        return None
    project = await office.projects.get(project_id)
    return Path(project.path).resolve() if project.path else None


async def list_dir(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Folder listing inside the project folder (Glob only matches files, so folders are hard to see)."""
    root = await _project_root(ctx)
    if root is None:
        raise ToolError("This project has no folder. Use Glob in your working directory instead.")
    target = (root / str(args.get("path") or ".")).resolve()
    if target != root and root not in target.parents:
        raise ToolError(f"Only paths inside the project folder {root} can be listed.")
    if not target.is_dir():
        raise ToolError(f"{target} is not a folder.")
    rows = []
    entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    for entry in entries[:200]:
        try:
            if entry.is_dir():
                count = sum(1 for _ in zip(range(1000), entry.iterdir(), strict=False))
                rows.append(f"{entry.name}/  ({count if count < 1000 else '1000+'} items)")
            else:
                rows.append(f"{entry.name}  ({entry.stat().st_size} bytes)")
        except OSError:
            rows.append(f"{entry.name}  (unreadable)")
    more = f"\n… {len(entries) - 200} more" if len(entries) > 200 else ""
    return f"{target}\n" + "\n".join(rows) + more


async def work_product_add(args: dict[str, Any], ctx: ToolCtx) -> str:
    ticket_id = await _ticket_ref(args, ctx)
    await ctx.office.tickets.add_work_product(ticket_id, args["kind"], args["ref"], {"note": args.get("note", "")})
    return "Work product recorded."


TICKET_ARG = {"ticket": {"type": "string", "description": "Ticket key like AO-12. Defaults to your current ticket."}}

TICKET_UPDATE = ToolDef(
    "ticket_update",
    "Change an EXISTING ticket other than your current one: resume it (todo), cancel it, block it (blocked_by), park it "
    "(backlog) or hand it to someone (assignee). Use it when the user decides something about a ticket in chat, "
    "instead of asking them to click. Allowed for the ticket's assignee or creator, and for PM/lead on any ticket.",
    schema(
        {
            "ticket": {"type": "string"},
            "status": {"type": "string", "enum": list(UPDATE_TO)},
            "assignee": {"type": "string", "description": "Name of the colleague who takes the ticket over"},
            "blocked_by": {"type": "array", "items": {"type": "string"}},
            "comment": {"type": "string", "description": "What changes and why; quote the user's decision"},
        },
        required=["ticket", "comment"],
    ),
    ticket_update,
)

PROJECT_ACCESS_REQUEST = ToolDef(
    "project_access_request",
    "The project folder is read-only but the work must change files there: ask the user to allow changes (they get "
    "buttons: git worktree or direct edits). Never draft the files somewhere else instead, and never work around it.",
    schema({"reason": {"type": "string", "description": "What you need to change there and why, in one or two sentences"}}, required=["reason"]),
    project_access_request,
)

OFFICE_TOOLS: list[ToolDef] = [
    ToolDef(
        "ticket_get",
        "Read a ticket: description, status, documents, sub-tickets, blockers and recent comments.",
        schema({**TICKET_ARG, "comments": {"type": "integer"}}, required=[]),
        ticket_get,
        read_only=True,
        activity="read",
        reads_source=source_ref("ticket", "ticket", "path"),  # runs from before SI-39 kept the key in `path` only
    ),
    ToolDef(
        "ticket_comment",
        "Add a progress comment to a ticket. Use it to leave durable progress, findings and decisions.",
        schema({**TICKET_ARG, "body": {"type": "string"}}, required=["body"]),
        ticket_comment,
    ),
    ToolDef(
        "ticket_set_status",
        "Set the final disposition of your ticket before ending your turn: done (with what was done and how it was "
        "verified), in_review (only after ask_user), blocked (with blocked_by keys) or todo.",
        schema(
            {
                **TICKET_ARG,
                "status": {"type": "string", "enum": ["done", "in_review", "blocked", "todo"]},
                "comment": {"type": "string"},
                "blocked_by": {"type": "array", "items": {"type": "string"}},
            },
            required=["status", "comment"],
        ),
        ticket_set_status,
    ),
    ToolDef(
        "ticket_create",
        "Create a ticket (by default a sub-ticket of yours) and assign it to a colleague by name. "
        "Use blocked_by to order work.",
        schema(
            {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string"},
                "type": {"type": "string"},
                "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "blocked_by": {"type": "array", "items": {"type": "string"}},
                "as_sub_ticket": {"type": "boolean"},
                "due": {"type": "string", "description": "Due date YYYY-MM-DD (from your plan/timeline)"},
            },
            required=["title", "description"],
        ),
        ticket_create,
    ),
    TICKET_UPDATE,
    PROJECT_ACCESS_REQUEST,
    ToolDef(
        "doc_get",
        "Read a ticket document (spec, plan, handoff, test-report, …) with its revision.",
        schema({**TICKET_ARG, "key": {"type": "string"}}, required=["key"]),
        doc_get,
        read_only=True,
        activity="read",
        reads_source=source_ref("ticket", "ticket"),  # without `ticket` it reads the run's own ticket: not a source
    ),
    ToolDef(
        "doc_put",
        "Create or replace a ticket document. Pass the revision you read as base_revision (null to create).",
        schema(
            {**TICKET_ARG, "key": {"type": "string"}, "body": {"type": "string"}, "base_revision": {"type": ["integer", "null"]}},
            required=["key", "body"],
        ),
        doc_put,
    ),
    ToolDef(
        "ask_user",
        "Ask the human owner a question when information or a decision is missing. Offer options when you can.",
        schema({"question": {"type": "string"}, "options": {"type": "array", "items": {"type": "string"}}}, required=["question"]),
        ask_user,
    ),
    ToolDef("list_actors", "List colleagues: name, position, specialty.", schema({}, required=[]), list_actors, read_only=True),
    ToolDef(
        "list_dir",
        "List a folder of the project (sub-folders first, with item counts). Path is relative to the project folder.",
        schema({"path": {"type": "string"}}, required=[]),
        list_dir,
        read_only=True,
        activity="read",
    ),
    ToolDef(
        "work_product_add",
        "Record a deliverable on the ticket: kind (file, document, dataset, commit, branch, link, report) and ref.",
        schema({**TICKET_ARG, "kind": {"type": "string"}, "ref": {"type": "string"}, "note": {"type": "string"}}, required=["kind", "ref"]),
        work_product_add,
    ),
]
