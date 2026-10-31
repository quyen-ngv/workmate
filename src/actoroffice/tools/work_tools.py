"""Tools for enterprise work: asking colleagues, requirement changes, automations (flows), databases."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from actoroffice.core.schedule import from_iso
from actoroffice.domain.automation import AutomationStatus
from actoroffice.domain.chat import Conversation
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.run import WakeSource
from actoroffice.domain.work import InteractionKind, Ticket
from actoroffice.services.automations import schedule_text
from actoroffice.services.chat import handle

from .registry import ToolCtx, ToolDef, ToolError, schema

# ---- colleagues & meetings ---------------------------------------------------------------------


async def meeting_room(ctx: ToolCtx, root: Ticket, actor_ids: list[str]) -> Conversation:
    """One group chat per ticket tree: where the people working on a feature talk (the user is a member)."""
    room = await ctx.office.chat.ticket_room(root, actor_ids, by=ctx.actor.name)
    assert room is not None
    return room


def mention(name: str) -> str:
    return f"@{name}" if " " not in name.strip() else f"@{handle(name)}"


async def ask_colleague(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    if ctx.ticket_id is None:
        raise ToolError("ask_colleague works inside a ticket. In chat, @mention the colleague instead.")
    colleague = await office.org.find_actor(str(args.get("colleague", "")))
    if colleague is None:
        raise ToolError(f"No colleague named {args.get('colleague')}. Use list_actors.")
    if colleague.id == ctx.actor.id:
        raise ToolError("You cannot ask yourself.")
    question = str(args.get("question", "")).strip()
    if not question:
        raise ToolError("question is empty")
    ticket = await office.tickets.get(ctx.ticket_id)
    root = await office.tickets.root_of(ticket.id)
    room = await meeting_room(ctx, root, [ctx.actor.id, colleague.id])
    item = await office.interactions.create(
        InteractionKind.ASK_COLLEAGUE,
        {
            "question": question,
            "to_actor_id": colleague.id,
            "to": colleague.name,
            "asked_by": ctx.actor.name,
            "conversation_id": room.id,
            "ticket": ticket.key,
        },
        actor_id=ctx.actor.id,
        ticket_id=ticket.id,
        run_id=ctx.run_id,
    )
    tag = mention(colleague.name)
    text = question if tag.lower() in question.lower() else f"{tag} {question}"
    message = await office.chat.post_actor(room.id, ctx.actor, f"[{ticket.key}] {text}", meta={"interaction_id": item.id})
    # Always wake the colleague (even past the actor-chatter limit or while they are busy here): a question
    # nobody reads blocks the asker forever. Coalesces with a pending wake.
    await office.wakes.enqueue(
        colleague.id, WakeSource.MENTION, conversation_id=room.id, payload={"message_id": message.id, "question": item.id}
    )
    return (
        f"Asked {colleague.name} in the group '{room.title}'. Do not wait or poll: save your progress, set your ticket "
        "to in_review with a short note, and end your turn. You will be woken with the answer (the user may answer too)."
    )


async def change_notify(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    if ctx.ticket_id is None:
        raise ToolError("change_notify works inside a ticket.")
    summary = str(args.get("summary", "")).strip()
    if not summary:
        raise ToolError("summary is required: what changed, and what it means for the work.")
    targets = [str(t) for t in args.get("tickets") or []]
    if targets:
        tree = {t.key.upper() for t in await office.tickets.tree(ctx.ticket_id)}
        outside = [t for t in targets if t.upper() not in tree]
        if outside:
            raise ToolError(f"{', '.join(outside)} are not in this ticket tree; changes stay within the feature.")
    try:
        result = await office.tickets.change_notice(
            ctx.ticket_id, summary, by=ctx.actor.name, targets=targets or None, reopen_done=bool(args.get("reopen_done")), skip=ctx.ticket_id
        )
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps(result, ensure_ascii=False)


# ---- automations: flows the actor designs -----------------------------------------------------


async def _flow_conversation(ctx: ToolCtx) -> tuple[str | None, int | None, str | None]:
    """(conversation, ticket, project) the flow belongs to: where it was asked for."""
    office = ctx.office
    if ctx.conversation_id:
        return ctx.conversation_id, None, (await office.chat.get(ctx.conversation_id)).project_id
    assert ctx.ticket_id is not None
    ticket = await office.tickets.get(ctx.ticket_id)
    conv = ticket.origin_conversation_id or (await office.chat.dm(ctx.actor.id, ticket.project_id)).id
    return conv, ticket.id, ticket.project_id


async def automation_save(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    steps = args.get("steps")
    if isinstance(steps, str):
        try:
            steps = json.loads(steps)
        except json.JSONDecodeError as exc:
            raise ToolError("steps must be a list of step objects") from exc
    if not isinstance(steps, list):
        raise ToolError("steps must be a list of step objects")
    has_script = any(isinstance(s, dict) and s.get("type") == "script" for s in steps)
    if has_script and "Bash" not in ctx.position.tools:
        raise ToolError("Your position cannot run code, so it cannot add script steps. Use notify/agent steps or ask a colleague.")
    conversation_id, ticket_id, project_id = await _flow_conversation(ctx)
    try:
        automation = await office.automations.save(
            str(args.get("name", "")),
            steps=steps,
            description=str(args.get("description", "")).strip(),
            schedule=args.get("schedule"),
            when=args.get("when"),
            owner_actor_id=ctx.actor.id,
            conversation_id=conversation_id,
            workspace=ctx.workspace if ctx.ticket_id is not None else None,
            ticket_id=ticket_id,
            project_id=project_id,
            run_id=ctx.run_id,
        )
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    timing = schedule_text(automation.schedule, automation.next_at)
    if automation.needs_review:
        return (
            f"Automation '{automation.name}' ({timing}) was sent to the user for review; it starts once approved. "
            "Tell the user what it will do in one or two sentences. In a ticket: set in_review if you need to confirm "
            "its first run (you are woken when the user decides), otherwise finish and say it awaits review."
        )
    at = f", first at {from_iso(automation.next_at).astimezone():%a %Y-%m-%d %H:%M}" if automation.next_at else ""
    return f"Automation '{automation.name}' is active ({timing}{at}). Confirm it to the user with the exact time(s)."


async def automation_list(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    mine = None if args.get("all") else ctx.actor.id
    rows = []
    for a in await office.automations.list(owner_actor_id=mine, active_only=not args.get("include_finished")):
        last = (await office.automations.runs(a.id, limit=1)) or [None]
        rows.append(
            {
                "name": a.name,
                "status": a.status.value,
                "when": schedule_text(a.schedule, a.next_at),
                "next": f"{from_iso(a.next_at).astimezone():%a %Y-%m-%d %H:%M}" if a.next_at else None,
                "description": a.description,
                "steps": [s.type.value + (f":{s.file or s.actor}" if (s.file or s.actor) else "") for s in a.steps],
                "last_run": {"status": last[0].status, "at": last[0].started_at} if last[0] else None,
            }
        )
    return json.dumps(rows, ensure_ascii=False) if rows else "No active automations."


async def automation_run(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    try:
        automation = await office.automations.get(str(args.get("name", "")))
        if automation.status == AutomationStatus.PENDING_REVIEW:
            raise ToolError(f"'{automation.name}' is waiting for the user's review; it cannot run yet.")
        task = office.automations.start(automation, "actor")
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    done, _ = await asyncio.wait({task}, timeout=120)
    if not done:
        return f"'{automation.name}' is still running; its result will be posted in chat when it finishes."
    try:
        run = task.result()
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    tail = (run.output if run.status == "succeeded" else (run.error or run.output)) or ""
    return f"Run #{run.id}: {run.status} ({(run.duration_ms or 0) / 1000:.1f}s)\n{tail[-3000:]}"


async def automation_stop(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    try:
        automation = await office.automations.get(str(args.get("name", "")))
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    if automation.owner_actor_id != ctx.actor.id:
        raise ToolError("That automation belongs to a colleague; ask them or the user.")
    await office.automations.set_enabled(automation.id, False, by=ctx.actor.name)
    return f"Automation '{automation.name}' is stopped."


_STEP_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["notify", "script", "agent"]},
        "text": {"type": "string", "description": "notify: the message; may use {output} {date} {time}"},
        "file": {"type": "string", "description": "script: .py file in your working directory (tested first)"},
        "timeout_s": {"type": "integer"},
        "actor": {"type": "string", "description": "agent: the colleague who gets the task as a ticket"},
        "title": {"type": "string"},
        "prompt": {"type": "string", "description": "agent: what to do; the previous step's output is appended"},
    },
    "required": ["type"],
}

_AUTOMATION_SAVE = ToolDef(
    "automation_save",
    "Create or update an automation: a small flow the office runs on its own, on a schedule or once at a time. "
    "Design it from steps, cheapest first:\n"
    "- notify {text}: post a message here (reminders, nudges, a summary using {output}); no LLM, no review;\n"
    "- script {file}: run a Python script you wrote and tested (fetch, transform, compute, check); its printed "
    "output becomes {output}; needs the user's code review;\n"
    "- agent {actor, prompt}: give a colleague a ticket when judgment is needed (analyse, write); needs review.\n"
    "Timing: `when` = first/only run ('YYYY-MM-DD HH:MM', 'HH:MM', 'in 30m'); `schedule` = repeat (daily@HH:MM, "
    "weekdays@HH:MM, weekly:mon@HH:MM, every:N minutes). A reminder is one notify step with `when` (and maybe a "
    "schedule). Saving the same name replaces the flow.",
    schema(
        {
            "name": {"type": "string", "description": "short id, e.g. hop-giao-ban-thu-2"},
            "description": {"type": "string"},
            "steps": {"type": "array", "items": _STEP_SCHEMA},
            "when": {"type": "string"},
            "schedule": {"type": "string"},
        },
        required=["name", "description", "steps"],
    ),
    automation_save,
)
_AUTOMATION_LIST = ToolDef(
    "automation_list",
    "List automations (yours by default; all=true for the whole office): when they run next, steps, last result. "
    "Use it to answer 'what is scheduled'.",
    schema({"all": {"type": "boolean"}, "include_finished": {"type": "boolean"}}, required=[]),
    automation_list,
    read_only=True,
)
_AUTOMATION_RUN = ToolDef(
    "automation_run",
    "Run an active automation now and get its result (waits up to 2 minutes).",
    schema({"name": {"type": "string"}}, required=["name"]),
    automation_run,
)
_AUTOMATION_STOP = ToolDef(
    "automation_stop",
    "Stop (disable) one of your automations, e.g. when the user cancels a reminder.",
    schema({"name": {"type": "string"}}, required=["name"]),
    automation_stop,
)


# ---- databases (read-only SQLite) ---------------------------------------------------------------

QUERY_TIMEOUT_S = 15
MAX_ROWS = 500


async def _db_path(args: dict[str, Any], ctx: ToolCtx) -> Path:
    office = ctx.office
    raw = str(args.get("database", "")).strip().strip('"')
    if not raw:
        raise ToolError("database is required: path of a SQLite file (relative to the project folder or your working dir).")
    roots: list[Path] = []
    project_id = None
    if ctx.ticket_id is not None:
        project_id = (await office.tickets.get(ctx.ticket_id)).project_id
    elif ctx.conversation_id:
        project_id = (await office.chat.get(ctx.conversation_id)).project_id
    if project_id:
        project = await office.projects.get(project_id)
        if project.path:
            roots.append(Path(project.path).resolve())
    if ctx.workspace is not None:
        roots.append(ctx.workspace.resolve())
    for root in roots:
        candidate = (root / raw).resolve()
        if candidate.is_file() and (candidate == root or root in candidate.parents):
            for bad in office.policy.protected:
                if candidate == bad or bad in candidate.parents:
                    raise ToolError("That database is protected.")
            return candidate
    raise ToolError(f"No database '{raw}' inside the project folder or your working directory ({', '.join(map(str, roots)) or 'none'}).")


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    deadline = time.monotonic() + QUERY_TIMEOUT_S
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
    readonly = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_PRAGMA}

    def authorizer(action: int, arg1: Any, _arg2: Any, _db: Any, _src: Any) -> int:
        if action == sqlite3.SQLITE_PRAGMA and str(arg1).lower() not in ("table_info", "table_list", "index_list", "foreign_key_list"):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK if action in readonly else sqlite3.SQLITE_DENY

    conn.set_authorizer(authorizer)
    return conn


def _schema(path: Path) -> str:
    conn = _connect(path)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','view') AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        lines = [f"{path.name}: {len(tables)} tables/views"]
        for t in tables[:80]:
            cols = conn.execute(f'PRAGMA table_info("{t}")').fetchall()
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            except sqlite3.Error:
                count = "?"
            lines.append(f"- {t} ({count} rows): " + ", ".join(f"{c[1]} {c[2]}".strip() for c in cols))
        return "\n".join(lines)
    finally:
        conn.close()


def _query(path: Path, sql: str, limit: int) -> str:
    conn = _connect(path)
    try:
        cur = conn.execute(sql)
        if cur.description is None:
            return "(statement returned no rows)"
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(limit + 1)
        more = len(rows) > limit
        lines = ["\t".join(cols)] + ["\t".join("" if v is None else str(v) for v in row) for row in rows[:limit]]
        if more:
            lines.append(f"… more rows (showing {limit}); aggregate or add LIMIT")
        return "\n".join(lines)
    finally:
        conn.close()


async def db_schema(args: dict[str, Any], ctx: ToolCtx) -> str:
    path = await _db_path(args, ctx)
    try:
        return await asyncio.to_thread(_schema, path)
    except sqlite3.Error as exc:
        raise ToolError(f"SQLite error: {exc}") from exc


async def db_query(args: dict[str, Any], ctx: ToolCtx) -> str:
    path = await _db_path(args, ctx)
    sql = str(args.get("sql", "")).strip().rstrip(";")
    if not sql or ";" in sql:
        raise ToolError("Pass exactly one SELECT statement.")
    limit = max(1, min(int(args.get("limit") or 200), MAX_ROWS))
    try:
        return await asyncio.to_thread(_query, path, sql, limit)
    except sqlite3.Error as exc:
        raise ToolError(f"SQLite error (read-only, one SELECT): {exc}") from exc


# ---- definitions -------------------------------------------------------------------------------

_DB_SCHEMA = ToolDef(
    "db_schema",
    "Show the tables, columns and row counts of a SQLite database file in the project folder or your working directory.",
    schema({"database": {"type": "string"}}, required=["database"]),
    db_schema,
    read_only=True,
)
_DB_QUERY = ToolDef(
    "db_query",
    "Run ONE read-only SELECT on a SQLite database file (project folder or working directory). Returns TSV rows "
    "(default 200, max 500). Aggregate in SQL instead of pulling raw rows.",
    schema({"database": {"type": "string"}, "sql": {"type": "string"}, "limit": {"type": "integer"}}, required=["database", "sql"]),
    db_query,
    read_only=True,
)

WORK_TOOLS: list[ToolDef] = [
    ToolDef(
        "ask_colleague",
        "Ask a colleague a question about this ticket (e.g. the lead asks the BA about requirements). It is posted in "
        "the ticket's meeting group, where the user can follow. Then set in_review and end your turn; you are woken "
        "with the answer. Put all your questions for that colleague in one message.",
        schema({"colleague": {"type": "string"}, "question": {"type": "string"}}, required=["colleague", "question"]),
        ask_colleague,
    ),
    ToolDef(
        "change_notify",
        "Announce a requirement change on the affected tickets (default: the whole ticket tree) and wake their owners. "
        "Update the documents first. reopen_done=true reopens done tickets that must be redone.",
        schema(
            {"summary": {"type": "string"}, "tickets": {"type": "array", "items": {"type": "string"}}, "reopen_done": {"type": "boolean"}},
            required=["summary"],
        ),
        change_notify,
    ),
    _AUTOMATION_SAVE,
    _AUTOMATION_LIST,
    _AUTOMATION_RUN,
    _AUTOMATION_STOP,
    _DB_SCHEMA,
    _DB_QUERY,
]

CHAT_WORK_TOOLS: list[ToolDef] = [_AUTOMATION_SAVE, _AUTOMATION_LIST, _AUTOMATION_RUN, _AUTOMATION_STOP, _DB_SCHEMA, _DB_QUERY]
