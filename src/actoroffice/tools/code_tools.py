"""Tools that read code through the index and the code graph instead of whole files: search, outline, symbols,
callers, impact, a reading pack, the service map.

Every answer carries a source reference in the form `repo:path:start-end` and the commit it was read at, ready to be
written on a `Nguồn:` line of a document. They only read; a ticket sees the repositories of its project and team.
"""

from __future__ import annotations

from typing import Any

from actoroffice.codeintel import services as service_map
from actoroffice.domain.errors import OfficeError
from actoroffice.ports.intel import IndexState, Subgraph
from actoroffice.services.codeintel import Repo

from .registry import ToolCtx, ToolDef, ToolError, schema

NOT_INDEXED = "Project này chưa có nguồn code để lập chỉ mục. Gắn một thư mục cục bộ cho project, hoặc kết nối Bitbucket / GitHub trong Cài đặt › Kết nối."


async def _repos(ctx: ToolCtx, name: str | None = None) -> list[Repo]:
    office = ctx.office
    ticket = await office.tickets.get(ctx.ticket_id) if ctx.ticket_id is not None else None
    repos = await office.codeintel.for_ticket(ticket)
    if name:
        wanted = name.strip().lower()
        repos = [r for r in repos if r.slug == wanted or r.slug.endswith("/" + wanted)]
        if not repos:
            raise ToolError(f"Không có repo '{name}' trong phạm vi của ticket. Gọi repo_list để xem các repo.")
    if not repos:
        raise ToolError(NOT_INDEXED)
    await office.codeintel.ensure(repos)
    return repos


def _need(args: dict[str, Any], *names: str) -> str:
    """A required argument, also under the names models tend to use for it. Missing: an error the model can fix."""
    for name in names:
        value = args.get(name)
        if value is not None and str(value).strip():
            return str(value)
    raise ToolError(f"Thiếu tham số `{names[0]}`.")


def _stale(states: list[IndexState]) -> str:
    notes = []
    for s in states:
        if s.state == "error":
            notes.append(f"⚠ {s.repo}: chỉ mục lỗi ({s.error})")
        elif s.state == "stale":
            notes.append(f"⚠ {s.repo}: chỉ mục ở {str(s.indexed_commit)[:10]}, nhánh chính đã tới {str(s.head_commit)[:10]}")
        elif s.level == "text":
            notes.append(f"ℹ {s.repo}: chỉ mục ở mức chữ (chưa cài tree-sitter), không có ký hiệu và đồ thị")
    return ("\n" + "\n".join(notes)) if notes else ""


def _numbered(text: str, start: int) -> str:
    return "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(text.split("\n")))


async def repo_list(_args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    ticket = await office.tickets.get(ctx.ticket_id) if ctx.ticket_id is not None else None
    repos = await office.codeintel.for_ticket(ticket)
    if not repos:
        return NOT_INDEXED
    lines = []
    for s in await office.codeintel.ensure(repos):
        lines.append(f"- {s.repo}: {s.state}, mức {s.level}, {s.files} file, {s.symbols} ký hiệu, {s.edges} cạnh, commit {str(s.indexed_commit)[:10]}")
    return "\n".join(lines)


async def code_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    repos = await _repos(ctx, args.get("repo"))
    office = ctx.office
    kinds = [str(k) for k in args.get("kinds") or []] or None
    hits = office.codeintel.query.search(_need(args, "query", "q", "text"), [r.slug for r in repos], kinds=kinds, path=args.get("path"), limit=min(int(args.get("limit") or 8), 20))
    if not hits:
        return "Không tìm thấy. Thử từ khác (tên hàm, tên lớp, một phần của tên đều được), hoặc bỏ lọc `kinds` / `path`." + _stale(await office.codeintel.status(repos))
    out = []
    for h in hits:
        head = f"### {h.cite}" + (f"  ({h.symbol})" if h.symbol else "")
        out.append(f"{head}\n{_numbered(h.text, h.start)}")
    return f"{len(hits)} kết quả (commit {hits[0].commit[:10]}).\n\n" + "\n\n".join(out) + _stale(await office.codeintel.status(repos))


async def code_outline(args: dict[str, Any], ctx: ToolCtx) -> str:
    repos = await _repos(ctx, args.get("repo"))
    path = _need(args, "path", "file")
    for repo in repos:
        outline = ctx.office.codeintel.query.outline(repo.slug, path)
        if outline is None:
            continue
        if not outline.symbols:
            return f"{repo.slug}:{outline.path} ({outline.lines} dòng, {outline.lang or 'không rõ ngôn ngữ'}): không có ký hiệu nào. Dùng code_read để đọc."
        depth = {s.qualified: s.qualified.count(".") for s in outline.symbols}
        base = min(depth.values())
        lines = [f"{'  ' * min(depth[s.qualified] - base, 4)}{s.start}-{s.end}  [{s.kind}] {s.signature or s.name}" + (f"  — {s.doc}" if s.doc else "") for s in outline.symbols]
        return f"{repo.slug}:{outline.path} · {outline.lines} dòng · commit {outline.commit[:10]}\n" + "\n".join(lines)
    raise ToolError(f"Không có file {path} trong chỉ mục. Kiểm tra đường dẫn bằng code_search.")


async def code_symbol(args: dict[str, Any], ctx: ToolCtx) -> str:
    repos = await _repos(ctx, args.get("repo"))
    found = ctx.office.codeintel.query.symbol(_need(args, "name", "symbol"), [r.slug for r in repos], limit=min(int(args.get("limit") or 8), 20))
    if not found:
        return "Không có ký hiệu nào tên như vậy. Thử code_search."
    return "\n".join(f"- [{s.kind}] {s.qualified}\n  {s.signature}\n  Nguồn: {s.cite}" + (f"\n  {s.doc}" if s.doc else "") for s in found)


def _graph_lines(graph: Subgraph, *, root_ids: set[int]) -> list[str]:
    nodes = {n.id: n for n in graph.nodes}
    lines = []
    for e in sorted(graph.edges, key=lambda e: (nodes[e.src].qualified, nodes[e.dst].qualified)):
        src, dst = nodes[e.src], nodes[e.dst]
        mark = "" if e.confidence in ("exact", "resolved", "declared") else "  (đoán theo tên)"
        lines.append(f"- {src.qualified or src.name} → {dst.qualified or dst.name}  [{e.kind}]{mark}  Nguồn: {e.cite}")
    return lines


async def code_refs(args: dict[str, Any], ctx: ToolCtx) -> str:
    repos = await _repos(ctx, args.get("repo"))
    direction = str(args.get("direction") or "callers")
    if direction not in ("callers", "callees"):
        raise ToolError("direction là 'callers' (ai gọi nó) hoặc 'callees' (nó gọi ai)")
    matched, graph = ctx.office.codeintel.query.references(_need(args, "symbol", "name"), [r.slug for r in repos], direction=direction, depth=min(int(args.get("depth") or 1), 3))
    if not matched:
        return "Không có ký hiệu nào tên như vậy. Thử code_symbol hoặc code_search."
    head = "Khớp: " + ", ".join(s.qualified for s in matched)
    lines = _graph_lines(graph, root_ids={s.id for s in matched})
    if not lines:
        return f"{head}\nKhông thấy {'ai gọi' if direction == 'callers' else 'lời gọi nào tới mã trong repo'}. Lời gọi qua biến không rõ kiểu có thể không được nhận ra: kiểm lại bằng code_search."
    return f"{head}\n" + "\n".join(lines) + ("\n(đã cắt bớt)" if graph.truncated else "")


async def code_read(args: dict[str, Any], ctx: ToolCtx) -> str:
    repos = await _repos(ctx, args.get("repo"))
    path = _need(args, "path", "file")
    start, end = args.get("start"), args.get("end")
    lines = (int(start), int(end or start)) if start else None
    last: Exception | None = None
    for repo in repos:
        try:
            got = await ctx.office.codeintel.read(repo, path, lines=lines, at=args.get("at"))
        except OfficeError as exc:
            last = exc
            continue
        more = f" (file có {got['total_lines']} dòng; đã cắt ở dòng {got['end']})" if got["truncated"] else f" (file có {got['total_lines']} dòng)"
        return f"Nguồn: {got['cite']} · commit {got['commit'][:10]}{more}\n{_numbered(got['text'], got['start'])}"
    raise ToolError(str(last) if last else f"Không đọc được {path}")


async def code_impact(args: dict[str, Any], ctx: ToolCtx) -> str:
    repos = await _repos(ctx, args.get("repo"))
    targets = [str(t) for t in args.get("targets") or []]
    if not targets:
        raise ToolError("targets: danh sách tên ký hiệu, đường dẫn file, hoặc `path:dòng`")
    impact = ctx.office.codeintel.query.impact(targets, [r.slug for r in repos], depth=min(int(args.get("depth") or 3), 5))
    out = []
    if impact.unknown:
        out.append("Không tìm thấy trong chỉ mục: " + ", ".join(impact.unknown))
    if impact.targets:
        out.append("Đích: " + ", ".join(n.qualified or n.name for n in impact.targets[:20]))
    groups = (
        ("Nơi gọi tới (trực tiếp và gián tiếp)", impact.callers), ("Endpoint bị chạm", impact.endpoints), ("Topic bị chạm", impact.topics),
        ("Bảng bị chạm", impact.tables), ("Hệ thống ngoài", impact.services),
    )  # fmt: skip
    for title, nodes in groups:
        if nodes:
            shown = [f"- {n.qualified or n.name}" + (f"  ({n.path}:{n.line})" if n.path else "") for n in nodes[:40]]
            out.append(f"{title} ({len(nodes)}):\n" + "\n".join(shown) + ("\n- …" if len(nodes) > 40 else ""))
    if len(out) <= 1 and not impact.callers:
        out.append("Không thấy nơi nào gọi tới. Cạnh gọi được suy từ cú pháp, không có thông tin kiểu: kiểm lại bằng code_search trước khi kết luận không ai dùng.")
    return "\n\n".join(out)


async def code_pack(args: dict[str, Any], ctx: ToolCtx) -> str:
    repos = await _repos(ctx, args.get("repo"))
    pack = ctx.office.codeintel.query.pack(_need(args, "question", "query"), [r.slug for r in repos], budget_tokens=min(max(int(args.get("budget") or 3500), 1000), 20000))
    if not pack.items:
        return "Không tìm thấy gì liên quan. Hỏi bằng từ có trong code (tên hàm, tên bảng, tên endpoint)."
    out = []
    for o in pack.outline:
        out.append(f"## Khung {o.repo}:{o.path} ({o.lines} dòng)\n" + "\n".join(f"{s.start}-{s.end}  [{s.kind}] {s.signature or s.name}" for s in o.symbols))
    for h in pack.items:
        out.append(f"### {h.cite}" + (f"  ({h.symbol})" if h.symbol else "") + f"\n{_numbered(h.text, h.start)}")
    return f"Gói đọc ~{pack.tokens} token" + (", đã cắt theo ngân sách" if pack.truncated else "") + f" · commit {pack.items[0].commit[:10]}\n\n" + "\n\n".join(out)


async def service_map_tool(args: dict[str, Any], ctx: ToolCtx) -> str:
    repos = await _repos(ctx)
    graph = await ctx.office.codeintel.map(repos, focus=args.get("focus"), depth=min(int(args.get("depth") or 1), 3), tables=str(args.get("tables") or "shared"))
    nodes = {n.id: n for n in graph.nodes}
    services = [n for n in graph.nodes if n.kind == "service"]
    lines = [f"Service ({len(services)}): " + ", ".join(sorted(n.name for n in services))]
    for e in sorted(graph.edges, key=lambda e: (nodes[e.src].name, nodes[e.dst].name)):
        mark = "  (đoán theo tên)" if e.confidence == "heuristic" else ""
        label = nodes[e.dst].kind if nodes[e.dst].kind != "service" else e.kind
        times = f" ×{e.count}" if e.count > 1 else ""
        lines.append(f"- {nodes[e.src].name} → {nodes[e.dst].name} [{label}{times}]{mark}  Nguồn: {e.cite}")
    if not graph.edges:
        lines.append("Không thấy quan hệ nào giữa các service (chưa có lời gọi HTTP, topic hay bảng dùng chung nào được nhận ra).")
    return "\n".join(lines) + "\n\n```mermaid\n" + service_map.to_mermaid(graph) + "\n```"


REPO_ARG = {"repo": {"type": "string", "description": "Repo name from repo_list. Omit to use every repo of the ticket."}}

CODE_TOOLS: list[ToolDef] = [
    ToolDef("repo_list", "List the code repositories this ticket can read and the state of their index.", schema({}, required=[]), repo_list, read_only=True, activity="search"),
    ToolDef(
        "code_search",
        "Search the code index (faster and cheaper than Grep + Read on a large repo). Any part of an identifier matches. Returns the best pieces of code "
        "with line numbers and a source reference `repo:path:start-end` you can cite.",
        schema(
            {
                "query": {"type": "string", "description": "Words that appear in the code: function, class, table, endpoint names"},
                **REPO_ARG,
                "kinds": {"type": "array", "items": {"type": "string", "enum": ["source", "test", "config", "doc"]}},
                "path": {"type": "string", "description": "Only files under this folder"},
                "limit": {"type": "integer"},
            },
            required=["query"],
        ),
        code_search,
        read_only=True,
        activity="search",
    ),
    ToolDef(
        "code_outline",
        "The skeleton of a file: every class and function with its signature and line range, no bodies. Read this before reading a whole file.",
        schema({"path": {"type": "string"}, **REPO_ARG}, required=["path"]),
        code_outline,
        read_only=True,
        activity="read",
    ),
    ToolDef(
        "code_symbol",
        "Find where a class, function or method is defined: signature, docstring and source reference.",
        schema({"name": {"type": "string", "description": "Name, qualified name, or part of a name"}, **REPO_ARG, "limit": {"type": "integer"}}, required=["name"]),
        code_symbol,
        read_only=True,
        activity="read",
    ),
    ToolDef(
        "code_refs",
        "Who calls a symbol (callers) or what it calls (callees), from the code graph. Edges found by name only are marked as guesses.",
        schema(
            {"symbol": {"type": "string"}, "direction": {"type": "string", "enum": ["callers", "callees"]}, "depth": {"type": "integer"}, **REPO_ARG},
            required=["symbol"],
        ),
        code_refs,
        read_only=True,
        activity="search",
    ),
    ToolDef(
        "code_read",
        "Read lines of a file at the indexed commit of the main branch (or at `at`). Use it for the exact piece you will cite as evidence.",
        schema(
            {"path": {"type": "string"}, "start": {"type": "integer"}, "end": {"type": "integer"}, "at": {"type": "string", "description": "Commit or branch"}, **REPO_ARG},
            required=["path"],
        ),
        code_read,
        read_only=True,
        activity="read",
    ),
    ToolDef(
        "code_impact",
        "What a change reaches: callers of the given symbols or files (direct and indirect), and the endpoints, topics and tables they expose or use.",
        schema(
            {"targets": {"type": "array", "items": {"type": "string"}, "description": "Symbol names, file paths, or path:line"}, "depth": {"type": "integer"}, **REPO_ARG},
            required=["targets"],
        ),
        code_impact,
        read_only=True,
        activity="search",
    ),
    ToolDef(
        "code_pack",
        "A first read for a question within a token budget: outlines of the files that matter most plus the best matching code. Use it at the start of an analysis.",
        schema({"question": {"type": "string"}, "budget": {"type": "integer", "description": "Tokens, default 3500"}, **REPO_ARG}, required=["question"]),
        code_pack,
        read_only=True,
        activity="search",
    ),
    ToolDef(
        "service_map",
        "The services in this team's repositories and how they reach each other (HTTP, topics, shared tables, outside systems), as text and Mermaid.",
        schema(
            {"focus": {"type": "string", "description": "A service name: only its neighbourhood"}, "depth": {"type": "integer"}, "tables": {"type": "string", "enum": ["shared", "all", "none"]}},
            required=[],
        ),
        service_map_tool,
        read_only=True,
        activity="search",
    ),
]
