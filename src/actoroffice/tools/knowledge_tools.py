"""Memory and skills: how actors learn and reuse what they learned."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.knowledge import MemoryKind, MemoryScope, SkillRef, SkillScope
from actoroffice.services.skills import slug

from .registry import ToolCtx, ToolDef, ToolError, schema


async def memory_save(args: dict[str, Any], ctx: ToolCtx) -> str:
    scope = MemoryScope(args.get("scope", "actor"))
    if scope == MemoryScope.SHARED:
        raise ToolError("Only the user can save office-wide memories; use scope 'actor' or 'position'.")
    try:
        memory = await ctx.office.memory.add(
            ctx.actor,
            str(args["content"]),
            kind=MemoryKind(args.get("kind", "lesson")),
            scope=scope,
            created_by=ctx.actor.name,
            source_ticket_id=ctx.ticket_id,
            conversation_id=ctx.conversation_id,
        )
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    return f"Saved memory #{memory.id}."


async def memory_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    found = await ctx.office.memory.search(ctx.actor, str(args["query"]), limit=int(args.get("limit", 8)))
    if not found:
        return "No matching memories."
    return "\n".join(f"- #{m.id} [{m.kind.value}/{m.scope.value}] {m.content}" for m in found)


async def history_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    found = await ctx.office.memory.history(ctx.actor, str(args["query"]), limit=min(int(args.get("limit", 10)), 30))
    if not found:
        return "Nothing found in past tickets, documents or your chats."
    lines = []
    for h in found:
        day = str(h["at"])[:10]
        if h["kind"] == "msg":
            where = f'chat "{h["conv_title"] or h["ref"]}"'
        elif h["kind"] == "doc":
            where = f'{h["ticket_key"]} document {str(h["ref"]).split(":", 1)[-1]}'
        else:
            where = f'{h["ticket_key"]} {h["kind"]} ({h["ticket_title"]})'
        lines.append(f"- [{day}] {where}: {h['snip']}")
    return "\n".join(lines)


async def skill_list(_args: dict[str, Any], ctx: ToolCtx) -> str:
    skills = ctx.office.skills.for_actor(ctx.actor, ctx.position)
    if not skills:
        return "No skills yet."
    return "\n".join(f"- {s.ref.name} ({s.ref.scope.value}): {s.description}" + (f" [files: {', '.join(s.files)}]" if s.files else "") for s in skills)


async def skill_read(args: dict[str, Any], ctx: ToolCtx) -> str:
    try:
        skill = ctx.office.skills.resolve(ctx.actor, ctx.position, str(args["name"]))
        if args.get("file"):
            return ctx.office.skills.read_file(skill.ref, str(args["file"]))
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    files = f"\n\nSub-skill / reference files (read with file=...): {', '.join(skill.files)}" if skill.files else ""
    return f"# {skill.ref.name}\n{skill.description}\n\n{skill.body}{files}"


async def skill_save(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Actors create and improve skills in their own scope; position/shared skills are managed by the user."""
    ref = SkillRef(scope=SkillScope.ACTOR, owner=ctx.actor.name, name=slug(str(args["name"])))
    try:
        if args.get("file"):
            skill = await ctx.office.skills.save_file(ref, str(args["file"]), str(args["body"]), author=ctx.actor.name)
        else:
            skill = await ctx.office.skills.save(
                ref,
                str(args.get("description", "")),
                str(args["body"]),
                author=ctx.actor.name,
                context={"conversation_id": ctx.conversation_id, "ticket_id": ctx.ticket_id},
            )
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    return json.dumps({"saved": skill.ref.name, "files": skill.files}, ensure_ascii=False)


KNOWLEDGE_TOOLS: list[ToolDef] = [
    ToolDef(
        "memory_save",
        "Remember something useful for future work: a lesson from a mistake, a project fact, the user's preference, "
        "or a decision. One short, self-contained sentence in the language you use with the user (name the project "
        "if it is project-specific). Never store secrets or personal data.",
        schema(
            {
                "content": {"type": "string"},
                "kind": {"type": "string", "enum": [k.value for k in MemoryKind]},
                "scope": {"type": "string", "enum": ["actor", "position"], "description": "position = shared with colleagues in the same role"},
            },
            required=["content"],
        ),
        memory_save,
    ),
    ToolDef(
        "memory_search",
        "Search your long-term memory (yours, your role's and the office's).",
        schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, required=["query"]),
        memory_search,
        read_only=True,
    ),
    ToolDef(
        "history_search",
        "Search past work: tickets, ticket comments and documents across the office, and the chats you take part in. "
        "Use it to find how something was done or decided before; read the ticket for the full story.",
        schema({"query": {"type": "string"}, "limit": {"type": "integer"}}, required=["query"]),
        history_search,
        read_only=True,
    ),
    ToolDef("skill_list", "List the skills you can use (yours, your role's, the office's).", schema({}, required=[]), skill_list, read_only=True),
    ToolDef(
        "skill_read",
        "Read a skill's instructions before doing a task it covers; pass file= to read one of its sub-skill files.",
        schema({"name": {"type": "string"}, "file": {"type": "string"}}, required=["name"]),
        skill_read,
        read_only=True,
    ),
    ToolDef(
        "skill_save",
        "Create or improve one of YOUR skills when you found a reusable procedure (steps, checklists, templates). "
        "Without file: writes SKILL.md (description = when to use it). With file (e.g. 'checklists/release.md'): "
        "writes a sub-skill file inside the skill.",
        schema(
            {"name": {"type": "string"}, "description": {"type": "string"}, "body": {"type": "string"}, "file": {"type": "string"}},
            required=["name", "body"],
        ),
        skill_save,
    ),
]
