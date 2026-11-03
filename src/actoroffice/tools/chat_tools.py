"""Tools available in chat turns. Chat is for talking; real work goes through confirmed tickets."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.domain.work import InteractionKind, Priority, TicketStatus

from .office_tools import PROJECT_ACCESS_REQUEST, TICKET_UPDATE, doc_get, list_actors, list_dir, ticket_get
from .registry import ToolCtx, ToolDef, ToolError, schema


async def propose_ticket(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    assignee = args.get("assignee") or ctx.actor.name
    actor = await office.org.find_actor(str(assignee))
    if actor is None:
        raise ToolError(f"No actor named {assignee}. Use list_actors.")
    priority = Priority(args.get("priority", "medium")).value
    withdrawn = []
    if args.get("replace_previous"):
        # The user changed their mind: the older proposals of this actor in this chat must not be confirmable any more.
        for old in await office.interactions.pending():
            mine = old.kind == InteractionKind.CONFIRM_TICKET and old.actor_id == ctx.actor.id
            same_chat = old.payload.get("conversation_id") == ctx.conversation_id
            if mine and same_chat and await office.interactions.cancel(old.id):
                withdrawn.append(str(old.payload.get("title")))
    parent = None
    if args.get("parent"):
        parent = (await office.tickets.get(str(args["parent"]))).key
    item = await office.interactions.create(
        InteractionKind.CONFIRM_TICKET,
        {
            "title": args["title"],
            "description": args.get("description", ""),
            "assignee": actor.name,
            "assignee_id": actor.id,
            "priority": priority,
            "proposed_by": ctx.actor.name,
            "conversation_id": ctx.conversation_id,
            "type": str(args.get("type") or "task"),
            "parent": parent,
        },
        actor_id=ctx.actor.id,
        run_id=ctx.run_id,
    )
    note = f" Withdrew the earlier proposal(s): {'; '.join(withdrawn)}." if withdrawn else ""
    return (
        f"Ticket proposal {item.id} is shown to the user to confirm.{note} Tell the user briefly what you proposed; "
        "work starts automatically once they confirm."
    )


async def chat_create_group(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ids = [ctx.actor.id]
    for name in args.get("members") or []:
        actor = await office.org.find_actor(str(name))
        if actor is None:
            raise ToolError(f"No actor named {name}. Use list_actors.")
        ids.append(actor.id)
    conv = await office.chat.create_group(args["title"], ids, created_by=ctx.actor.name)
    await office.chat.post_actor(conv.id, ctx.actor, args["message"])
    return f"Group '{conv.title}' created (the user is a member). Your opening message was posted there."


async def tickets_list(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    statuses = [TicketStatus(s) for s in args.get("statuses") or []] or None
    assignee_id = None
    if name := args.get("assignee"):
        actor = await office.org.find_actor(str(name))
        assignee_id = actor.id if actor else "-"
    names = {a.id: a.name for a in await office.org.list_actors()}
    rows = [
        {"key": t.key, "title": t.title, "status": t.status.value, "priority": t.priority.value, "assignee": names.get(t.assignee_actor_id or "")}
        for t in await office.tickets.list(status=statuses, assignee_actor_id=assignee_id, limit=int(args.get("limit", 30)))
    ]
    return json.dumps(rows, ensure_ascii=False)


CHAT_TOOLS: list[ToolDef] = [
    TICKET_UPDATE,
    PROJECT_ACCESS_REQUEST,
    ToolDef("list_actors", "List colleagues: name, position, specialty.", schema({}, required=[]), list_actors, read_only=True),
    ToolDef(
        "list_dir",
        "List a folder of the project (sub-folders first, with item counts). Path is relative to the project folder. "
        "Use it to show or explore the project's structure.",
        schema({"path": {"type": "string"}}, required=[]),
        list_dir,
        read_only=True,
    ),
    ToolDef(
        "tickets_list",
        "List tickets (newest first), optionally filtered by statuses and assignee name. Use it to answer progress questions.",
        schema(
            {
                "statuses": {"type": "array", "items": {"type": "string", "enum": [s.value for s in TicketStatus]}},
                "assignee": {"type": "string"},
                "limit": {"type": "integer"},
            },
            required=[],
        ),
        tickets_list,
        read_only=True,
    ),
    ToolDef(
        "ticket_get",
        "Read one ticket in detail (description, documents, sub-tickets, recent comments).",
        schema({"ticket": {"type": "string"}, "comments": {"type": "integer"}}, required=["ticket"]),
        ticket_get,
        read_only=True,
    ),
    ToolDef(
        "doc_get",
        "Read a document of a ticket (requirements, design, plan, test-report, …).",
        schema({"ticket": {"type": "string"}, "key": {"type": "string"}}, required=["ticket", "key"]),
        doc_get,
        read_only=True,
    ),
    ToolDef(
        "propose_ticket",
        "Propose a ticket for real work (the user confirms before it starts). Clarify the request in chat first, "
        "then propose a clear title, a description with acceptance criteria, and the assignee (you or a colleague).",
        schema(
            {
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string"},
                "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                "type": {
                    "type": "string",
                    "description": "feature, task, bug, research, analysis, content, automation, change_request",
                },
                "parent": {"type": "string", "description": "Existing ticket key this belongs to (e.g. a change request for AO-12)"},
                "replace_previous": {
                    "type": "boolean",
                    "description": "This proposal replaces your earlier unconfirmed proposal(s) in this chat: they are withdrawn",
                },
            },
            required=["title", "description"],
        ),
        propose_ticket,
    ),
    ToolDef(
        "chat_create_group",
        "Create a group chat with colleagues (the user is always added) and post your opening message, to discuss "
        "something together. @mention colleagues in the message to get their reply.",
        schema(
            {"title": {"type": "string"}, "members": {"type": "array", "items": {"type": "string"}}, "message": {"type": "string"}},
            required=["title", "members", "message"],
        ),
        chat_create_group,
    ),
]
