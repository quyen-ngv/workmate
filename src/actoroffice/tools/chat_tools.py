"""Tools available in chat turns. Chat is for talking; real work goes through confirmed tickets."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.domain.work import InteractionKind, Priority, TicketStatus

from .office_tools import PROJECT_ACCESS_REQUEST, TICKET_UPDATE, doc_get, list_actors, list_dir, ticket_get
from .proposal_tools import PROPOSE_TICKETS
from .registry import ToolCtx, ToolDef, ToolError, schema, source_ref


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


# Pages of the team UI a chat message may send people to (web/static/team/team.js renders them as buttons).
UI_PAGES = {
    "new_task": "giao việc mới (form)",
    "repo": "thêm / sửa repo, dự án (Cài đặt)",
    "connections": "kết nối Jira, Confluence, Bitbucket, Oracle… (Kết nối)",
    "rules": "rules của team (Rules)",
    "kb": "kiến thức và kho tài liệu (Kiến thức)",
    "standard": "chuẩn tài liệu: mẫu, luật, hướng dẫn (Chuẩn tài liệu)",
    "people": "người và team, vai trò (Người)",
    "map": "bản đồ dịch vụ, code (Bản đồ)",
    "setup": "cài đặt chung: model AI, Telegram (Cài đặt)",
}


async def suggest_actions(args: dict[str, Any], ctx: ToolCtx) -> str:
    if not ctx.conversation_id:
        raise ToolError("suggest_actions only works in a chat")
    actions = []
    for raw in args.get("actions") or []:
        page = str((raw or {}).get("page") or "").strip()
        if page not in UI_PAGES:
            raise ToolError(f"page must be one of: {', '.join(UI_PAGES)}")
        actions.append({"page": page, "label": str(raw.get("label") or UI_PAGES[page])[:80]})
    if not actions:
        raise ToolError("Give at least one action")
    text = str(args.get("text") or "").strip() or "Bạn có thể làm ở đây:"
    await ctx.office.chat.post_actor(ctx.conversation_id, ctx.actor, text[:1000], meta={"actions": actions[:6]}, wake=False)
    return f"Posted {len(actions)} button(s) under your message. Do not repeat them in your reply."


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
        activity="read",
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
        activity="search",
    ),
    ToolDef(
        "ticket_get",
        "Read one ticket in detail (description, documents, sub-tickets, recent comments).",
        schema({"ticket": {"type": "string"}, "comments": {"type": "integer"}}, required=["ticket"]),
        ticket_get,
        read_only=True,
        activity="read",
        reads_source=source_ref("ticket", "ticket", "path"),  # runs from before SI-39 kept the key in `path` only
    ),
    ToolDef(
        "doc_get",
        "Read a document of a ticket (requirements, design, plan, test-report, …).",
        schema({"ticket": {"type": "string"}, "key": {"type": "string"}}, required=["ticket", "key"]),
        doc_get,
        read_only=True,
        activity="read",
        reads_source=source_ref("ticket", "ticket"),  # without `ticket` it reads the run's own ticket: not a source
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
    PROPOSE_TICKETS,
    ToolDef(
        "suggest_actions",
        "Put one or more buttons in the chat that open a page of the team UI, with a short line saying why. Use it when the person "
        "has to do something there themselves (add a repository, connect Jira or Bitbucket, add a rule, a knowledge item, change the "
        "document standard, add people). Pages: " + "; ".join(f"{k} = {v}" for k, v in UI_PAGES.items()) + ".",
        schema(
            {
                "text": {"type": "string", "description": "One short line: what to do there and why"},
                "actions": {
                    "type": "array",
                    "items": {"type": "object", "properties": {"page": {"type": "string", "enum": list(UI_PAGES)}, "label": {"type": "string"}}, "required": ["page"]},
                },
            },
            required=["actions"],
        ),
        suggest_actions,
        activity="quiet",
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
