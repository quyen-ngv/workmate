"""propose_tickets (SI-24): a list of proposed tickets on one card, for chat turns and ticket stage sessions."""

from __future__ import annotations

from typing import Any

from actoroffice.services.proposals import MAX_ITEMS, ProposalError

from .registry import ToolCtx, ToolDef, ToolError, schema


async def propose_tickets(args: dict[str, Any], ctx: ToolCtx) -> str:
    try:
        item, withdrawn = await ctx.office.proposals.propose(
            args.get("items"),
            actor_id=ctx.actor.id,
            actor_name=ctx.actor.name,
            run_id=ctx.run_id,
            ticket_id=ctx.ticket_id,
            conversation_id=ctx.conversation_id,
        )
    except ProposalError as exc:
        raise ToolError(f"{exc}. Nothing was proposed; fix the list and call propose_tickets again.") from exc
    count = len(item.payload["items"])
    note = f" It replaces your earlier pending list ({len(withdrawn)} card withdrawn)." if withdrawn else ""
    return (
        f"Proposal card {item.id} with {count} ticket(s) is shown to people (an owner or lead approves it once).{note} "
        "No ticket exists until they approve; do not create these tickets yourself. Tell people briefly what you proposed."
    )


PROPOSE_TICKETS = ToolDef(
    "propose_tickets",
    f"Propose a list of 1..{MAX_ITEMS} tickets on one card (after a discussion or a research report). Nothing is "
    "created until an owner or lead approves; they can drop or edit items. Item 1 runs at once by default, the others "
    "wait in the backlog. Calling it again in the same chat or ticket replaces your pending card.",
    schema(
        {
            "items": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_ITEMS,
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "why": {"type": "string", "description": "Why it is needed, with the evidence (file:line, message, page)"},
                        "description": {"type": "string", "description": "What is wanted, with draft acceptance criteria"},
                        "workflow": {"type": "string", "enum": ["code", "quick"], "description": "Suggested workflow (default code)"},
                        "project": {"type": "string", "description": "Project name or id (default: the project of this chat/ticket)"},
                        "priority": {"type": "string", "enum": ["critical", "high", "medium", "low"]},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": ["integer", "string"]},
                            "description": "Numbers of earlier items in this list (1-based) or keys of existing tickets",
                        },
                    },
                    "required": ["title", "why", "description"],
                },
            }
        },
        required=["items"],
    ),
    propose_tickets,
)
