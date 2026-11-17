"""Tools over the documents repository (search, exact search, read a section, the tree) and over the links between
tickets. The repository tools are offered only when the office keeps one (`docs_repo` in the settings)."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.domain.errors import OfficeError

from .registry import ToolCtx, ToolDef, ToolError, schema


def _repo_on(office: Any) -> bool:
    return bool(getattr(office, "docrepo", None) and office.docrepo.enabled)


async def docs_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    try:
        hits = await ctx.office.docrepo.search(str(args.get("query") or ""), under=str(args.get("under") or ""), limit=int(args.get("limit") or 10))
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    if not hits:
        return "Không có gì khớp. Thử từ khác, hoặc docs_grep với mã (ví dụ AO-12/REQ-2)."
    lines = [f"- {h['path']}:{h['line']} · {h['heading'] or '(đầu file)'}\n  {h['snippet']}" for h in hits]
    return "\n".join(lines) + '\n(docs_read(path=..., heading="...") đọc đúng mục đó)'


async def docs_grep(args: dict[str, Any], ctx: ToolCtx) -> str:
    try:
        hits = await ctx.office.docrepo.grep(str(args.get("pattern") or ""), under=str(args.get("under") or ""), limit=int(args.get("limit") or 60))
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    if not hits:
        return "Không dòng nào khớp."
    return "\n".join(f"{h['path']}:{h['line']}: {h['text']}" for h in hits)


async def docs_read(args: dict[str, Any], ctx: ToolCtx) -> str:
    lines = None
    if args.get("start"):
        start = int(args["start"])
        lines = (start, int(args.get("end") or start + 200))
    try:
        got = await ctx.office.docrepo.read(str(args.get("path") or ""), heading=str(args.get("heading") or ""), lines=lines)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    head = f"{got['path']} dòng {got['first']}-{got['last']} / {got['total']}" + (" (cắt bớt)" if got["truncated"] else "")
    return f"{head}\n\n{got['text']}"


async def docs_tree(args: dict[str, Any], ctx: ToolCtx) -> str:
    try:
        entries = await ctx.office.docrepo.tree(str(args.get("under") or ""), int(args.get("depth") or 2))
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    return "\n".join(entries[:400]) + (f"\n… (còn {len(entries) - 400})" if len(entries) > 400 else "") if entries else "(trống)"


async def ticket_links(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ref = args.get("ticket") or ctx.ticket_id
    if not ref:
        raise ToolError("ticket: mã ticket")
    try:
        ticket = await office.tickets.get(ref)
        other = str(args.get("other") or "").strip()
        if other:
            target = await office.tickets.get(other)
            relation = str(args.get("relation") or "relates")
            if args.get("remove"):
                await office.links.remove(ticket, target, relation)
            else:
                await office.links.add(ticket, target, relation, by=ctx.actor.name)
        view = await office.links.view(ticket)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    if not view:
        return f"{ticket.key} chưa liên kết với ticket nào."
    return json.dumps(view, ensure_ascii=False, indent=1)


DOCS_TOOLS: list[ToolDef] = [
    ToolDef(
        "docs_search",
        "Search every document of the office (epics, specs, designs of all tickets), the team's knowledge and the library people added, by "
        "meaning words (accents ignored). Each hit names the file, the heading it is under and its line. Use it to find what was already "
        "written or decided about a subject before you write; then docs_read the section.",
        schema(
            {
                "query": {"type": "string"},
                "under": {"type": "string", "description": "Only under this folder, e.g. epics/AO-12-hoan-tien or knowledge"},
                "limit": {"type": "integer"},
            },
            required=["query"],
        ),
        docs_search,
        read_only=True,
        activity="search",
        enabled=_repo_on,
    ),
    ToolDef(
        "docs_grep",
        "Exact search over the documents repository: every line that matches a regular expression. Use it for ids and names: "
        "`AO-12/REQ-2` finds every item that traces to that requirement, `AO-14` every place that names the ticket.",
        schema({"pattern": {"type": "string"}, "under": {"type": "string"}, "limit": {"type": "integer"}}, required=["pattern"]),
        docs_grep,
        read_only=True,
        activity="search",
        enabled=_repo_on,
    ),
    ToolDef(
        "docs_read",
        "Read a file of the documents repository: one section (heading=words of its heading), a range of lines (start, end), or all of it.",
        schema(
            {"path": {"type": "string"}, "heading": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}},
            required=["path"],
        ),
        docs_read,
        read_only=True,
        activity="read",
        enabled=_repo_on,
    ),
    ToolDef(
        "docs_tree",
        "The folders and files of the documents repository (epics/, tickets/, knowledge/), a few levels deep. README.md at the root is the catalogue.",
        schema({"under": {"type": "string"}, "depth": {"type": "integer"}}, required=[]),
        docs_tree,
        read_only=True,
        activity="read",
        enabled=_repo_on,
    ),
    ToolDef(
        "ticket_links",
        "The links of a ticket to other tickets (depends_on, relates, supersedes; both directions), and add or remove one: give `other` and "
        "`relation` (and `remove: true` to remove). A link written in a document (`Phụ thuộc: AO-14`) is changed by editing the document.",
        schema(
            {
                "ticket": {"type": "string", "description": "Default: the ticket of this run"},
                "other": {"type": "string"},
                "relation": {"type": "string", "enum": ["depends_on", "relates", "supersedes"]},
                "remove": {"type": "boolean"},
            },
            required=[],
        ),
        ticket_links,
    ),
]
