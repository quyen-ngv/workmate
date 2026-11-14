"""Questions the index answers: search, outline, symbols, who calls what, what a change touches, a reading pack.

Every answer names the place it came from in Doc Core's `Nguồn:` syntax (`repo:path:start-end`) and the commit the
index was built at, so it can be pinned as evidence as it is. `repos` is always the list of repository slugs the
asker may see; nothing outside it is ever returned.
"""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict, deque
from typing import Any

from actoroffice.ports.intel import ContextPack, EdgeInfo, Hit, Impact, IndexState, NodeInfo, Outline, Subgraph, SymbolInfo, cite

from .index import state_of
from .store import IndexStore

MAX_HIT_LINES = 40
KIND_WEIGHT = {"source": 1.0, "config": 0.8, "test": 0.6, "doc": 0.5}
FACT_KINDS = ("endpoint", "topic", "table", "external")
CONFIDENCE_RANK = {"heuristic": 0, "resolved": 1, "exact": 2, "declared": 3}
STOP = {"the", "and", "for", "với", "của", "các", "cho", "này", "what", "where", "how", "does", "nào", "làm", "trong", "that", "this", "from"}


def approx_tokens(text: str) -> int:
    return len(text) // 4 + 1


class CodeQuery:
    def __init__(self, store: IndexStore) -> None:
        self.store = store

    # ---- scope -----------------------------------------------------------------------------------------------

    def _repo_ids(self, repos: list[str]) -> dict[int, Any]:
        if not repos:
            return {}
        marks = ",".join("?" * len(repos))
        return {int(r["id"]): r for r in self.store.all(f"SELECT * FROM repos WHERE slug IN ({marks})", repos)}

    @staticmethod
    def _in(ids: list[int] | set[int]) -> str:
        return ",".join(str(int(i)) for i in ids) or "-1"

    def state(self, repos: list[str]) -> list[IndexState]:
        return [state_of(self.store, slug) for slug in repos]

    # ---- search ----------------------------------------------------------------------------------------------

    def search(self, query: str, repos: list[str], *, kinds: list[str] | None = None, path: str | None = None, limit: int = 12) -> list[Hit]:
        """Chunks that contain the words of the query (any part of an identifier matches), best first. A symbol whose
        name is one of the words counts more than the same word inside a body; tests and documents count less."""
        scope = self._repo_ids(repos)
        if not scope:
            return []
        terms = [t for t in dict.fromkeys(re.findall(r"[\w$]{3,}", query)) if t.lower() not in STOP][:12]
        if not terms:
            return []
        # Candidates are gathered word by word and a word counts by how rare it is: with one OR query, the common words
        # of a question ("ticket", "description") would fill the list and bury the one chunk that has the rare word.
        in_scope = self._in(set(scope))
        total = int(self.store.one(f"SELECT count(*) FROM files WHERE repo_id IN ({in_scope})")[0]) * 4 + 1  # type: ignore[index]
        weight: dict[str, float] = {}
        found: dict[int, Any] = {}
        for t in terms:
            quoted = '"' + t.replace('"', "") + '"'
            count = int(self.store.one(f"SELECT count(*) FROM chunks WHERE chunks MATCH ? AND repo_id IN ({in_scope})", (quoted,))[0])  # type: ignore[index]
            if count == 0:
                continue
            weight[t.lower()] = math.log(1 + total / count) ** 2  # squared: one rare word must beat several common ones
            for r in self.store.all(
                "SELECT c.rowid AS rid, c.text, c.repo_id, c.file_id, c.path, c.start_line, c.end_line, c.symbol, bm25(chunks) AS rank, f.kind AS fkind "
                f"FROM chunks c JOIN files f ON f.id = c.file_id WHERE chunks MATCH ? AND c.repo_id IN ({in_scope}) ORDER BY rank LIMIT ?",
                (quoted, max(limit * 4, 40)),
            ):
                found.setdefault(int(r["rid"]), r)
        rows = list(found.values())
        if not rows:
            return []
        lowered = [t.lower() for t in terms]
        like = " OR ".join("name LIKE ?" for _ in terms)
        defined: dict[int, list[int]] = defaultdict(list)  # file → lines where a symbol named like a word is defined
        for d in self.store.all(
            f"SELECT file_id, start_line FROM nodes WHERE kind = 'symbol' AND ({like}) AND repo_id IN ({self._in(set(scope))}) LIMIT 400",
            [f"%{t}%" for t in terms],
        ):
            defined[int(d["file_id"])].append(int(d["start_line"]))
        scored: list[tuple[float, Any]] = []
        for r in rows:
            if kinds and r["fkind"] not in kinds:
                continue
            if path and not r["path"].startswith(path.strip("/")):
                continue
            text = r["text"].lower()
            score = sum(w for t, w in weight.items() if t in text) * KIND_WEIGHT.get(r["fkind"], 0.7)
            score *= 1 + min(-float(r["rank"]), 20.0) / 100  # among equals, the denser match first
            leaf = (r["symbol"] or "").rsplit(".", 1)[-1].lower()
            if any(int(r["start_line"]) <= line <= int(r["end_line"]) for line in defined.get(int(r["file_id"]), ())):
                score *= 2.0  # the place where the thing asked for is defined
            elif leaf and any(t == leaf or t in leaf for t in lowered):
                score *= 1.6
            if any(t in r["path"].lower() for t in lowered):
                score *= 1.2
            scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        hits: list[Hit] = []
        for score, r in scored[:limit]:
            repo = scope[int(r["repo_id"])]
            start, end, body = self._focus(r["text"], int(r["start_line"]), lowered)
            hits.append(
                Hit(repo=repo["slug"], path=r["path"], start=start, end=end, text=body, symbol=r["symbol"] or "",
                    cite=cite(repo["slug"], r["path"], start, end), commit=repo["indexed_commit"] or "", score=round(score, 3))
            )  # fmt: skip
        return hits

    @staticmethod
    def _focus(text: str, first_line: int, terms: list[str]) -> tuple[int, int, str]:
        """A long chunk is cut to the window around the first matching line."""
        lines = text.split("\n")
        if len(lines) <= MAX_HIT_LINES:
            return first_line, first_line + len(lines) - 1, text
        at = next((i for i, line in enumerate(lines) if any(t in line.lower() for t in terms)), 0)
        start = max(0, min(at - 5, len(lines) - MAX_HIT_LINES))
        return first_line + start, first_line + start + MAX_HIT_LINES - 1, "\n".join(lines[start : start + MAX_HIT_LINES])

    # ---- symbols ---------------------------------------------------------------------------------------------

    def _symbol_info(self, n: Any, slug: str, path: str) -> SymbolInfo:
        return SymbolInfo(
            id=int(n["id"]), repo=slug, kind=n["sub_kind"], name=n["name"], qualified=n["qualified"], path=path, start=int(n["start_line"] or 0),
            end=int(n["end_line"] or 0), signature=n["signature"], doc=n["doc"], cite=cite(slug, path, n["start_line"], n["end_line"]),
        )  # fmt: skip

    def outline(self, repo: str, path: str) -> Outline | None:
        """The skeleton of a file: every symbol with its signature and lines, no bodies."""
        scope = self._repo_ids([repo])
        if not scope:
            return None
        repo_id, row = next(iter(scope.items()))
        f = self.store.one("SELECT * FROM files WHERE repo_id = ? AND path = ?", (repo_id, path.replace("\\", "/").strip("/")))
        if f is None:
            return None
        nodes = self.store.all("SELECT * FROM nodes WHERE file_id = ? AND kind = 'symbol' ORDER BY start_line, end_line DESC", (f["id"],))
        return Outline(
            repo=repo, path=f["path"], lang=f["lang"], lines=int(f["lines"]), commit=row["indexed_commit"] or "",
            symbols=[self._symbol_info(n, repo, f["path"]) for n in nodes],
        )  # fmt: skip

    def symbol(self, name: str, repos: list[str], *, limit: int = 10) -> list[SymbolInfo]:
        """Definitions by exact name, by qualified name (or its tail), then by a part of the name."""
        scope = self._repo_ids(repos)
        if not scope or not name.strip():
            return []
        name = name.strip()
        base = f"SELECT n.*, f.path, f.kind AS fkind FROM nodes n JOIN files f ON f.id = n.file_id WHERE n.kind = 'symbol' AND n.repo_id IN ({self._in(set(scope))}) AND "
        rows: list[Any] = []
        for where, params in (
            ("n.qualified = ?", (name,)),
            ("n.name = ?", (name,)),
            ("n.qualified LIKE ?", (f"%.{name}",)),
            ("n.name LIKE ?", (f"%{name}%",)),
        ):
            rows = self.store.all(base + where + " ORDER BY (f.kind = 'source') DESC, length(n.qualified) LIMIT ?", (*params, limit))
            if rows:
                break
        return [self._symbol_info(n, scope[int(n["repo_id"])]["slug"], n["path"]) for n in rows]

    def _node(self, n: Any, scope: dict[int, Any], paths: dict[int, str]) -> NodeInfo:
        return NodeInfo(
            id=int(n["id"]), kind=n["kind"] if n["kind"] != "symbol" else n["sub_kind"] or "symbol", name=n["name"], qualified=n["qualified"],
            repo=scope[int(n["repo_id"])]["slug"] if int(n["repo_id"]) in scope else "", path=paths.get(int(n["file_id"] or 0), ""),
            line=n["start_line"], meta=json.loads(n["meta_json"] or "{}"),
        )  # fmt: skip

    def _nodes(self, ids: set[int] | list[int], scope: dict[int, Any]) -> dict[int, NodeInfo]:
        if not ids:
            return {}
        rows = self.store.all(f"SELECT * FROM nodes WHERE id IN ({self._in(set(ids))})")
        files = {int(r["file_id"]) for r in rows if r["file_id"]}
        paths = {int(f["id"]): f["path"] for f in self.store.all(f"SELECT id, path FROM files WHERE id IN ({self._in(files)})")}
        return {int(r["id"]): self._node(r, scope, paths) for r in rows if int(r["repo_id"]) in scope}

    def _edge(self, e: Any, scope: dict[int, Any], paths: dict[int, str]) -> EdgeInfo:
        slug = scope[int(e["repo_id"])]["slug"] if int(e["repo_id"]) in scope else ""
        path = paths.get(int(e["file_id"] or 0), "")
        return EdgeInfo(id=int(e["id"]), src=int(e["src"]), dst=int(e["dst"]), kind=e["kind"], confidence=e["confidence"],
                        cite=cite(slug, path, e["line"]) if path else "", source=e["source"])  # fmt: skip

    def _edges(self, where: str, scope: dict[int, Any]) -> list[EdgeInfo]:
        rows = self.store.all(f"SELECT * FROM edges WHERE {where} AND repo_id IN ({self._in(set(scope))})")
        files = {int(r["file_id"]) for r in rows if r["file_id"]}
        paths = {int(f["id"]): f["path"] for f in self.store.all(f"SELECT id, path FROM files WHERE id IN ({self._in(files)})")}
        return [self._edge(r, scope, paths) for r in rows]

    def neighbors(
        self, node_ids: list[int], repos: list[str], *, direction: str = "both", edge_kinds: list[str] | None = None, depth: int = 1,
        min_confidence: str = "heuristic", limit: int = 200,
    ) -> Subgraph:  # fmt: skip
        """The graph around some nodes: `out` follows edges from them (what they call or use), `in` towards them."""
        scope = self._repo_ids(repos)
        kinds = "" if not edge_kinds else " AND kind IN (" + ",".join("'" + k.replace("'", "") + "'" for k in edge_kinds) + ")"
        floor = CONFIDENCE_RANK.get(min_confidence, 0)
        seen: set[int] = set(node_ids)
        frontier = set(node_ids)
        edges: dict[int, EdgeInfo] = {}
        truncated = False
        for _ in range(max(1, min(depth, 4))):
            if not frontier:
                break
            found: list[EdgeInfo] = []
            if direction in ("out", "both"):
                found += self._edges(f"src IN ({self._in(frontier)}){kinds}", scope)
            if direction in ("in", "both"):
                found += self._edges(f"dst IN ({self._in(frontier)}){kinds}", scope)
            frontier = set()
            for e in found:
                if CONFIDENCE_RANK.get(e.confidence, 0) < floor or e.id in edges:
                    continue
                if len(edges) >= limit:
                    truncated = True
                    break
                edges[e.id] = e
                for nid in (e.src, e.dst):
                    if nid not in seen:
                        seen.add(nid)
                        frontier.add(nid)
        nodes = self._nodes(seen, scope)
        return Subgraph(
            nodes=list(nodes.values()), edges=[e for e in edges.values() if e.src in nodes and e.dst in nodes],
            commit={r["slug"]: r["indexed_commit"] or "" for r in scope.values()}, truncated=truncated,
        )  # fmt: skip

    def references(self, name: str, repos: list[str], *, direction: str = "callers", depth: int = 1, limit: int = 60) -> tuple[list[SymbolInfo], Subgraph]:
        """Who calls a symbol (`callers`) or what it calls (`callees`). Returns the symbols the name matched and the graph."""
        found = self.symbol(name, repos, limit=5)
        exact = [s for s in found if s.qualified == name or s.name == name] or found[:1]
        graph = self.neighbors([s.id for s in exact], repos, direction="in" if direction == "callers" else "out", edge_kinds=["calls", "extends"], depth=depth, limit=limit)
        return exact, graph

    # ---- impact ----------------------------------------------------------------------------------------------

    def _targets(self, target: str, scope: dict[int, Any]) -> list[int]:
        """A target is a symbol name, a file path, or `path:line` (also `repo:path:line-line`)."""
        t = target.strip()
        for slug in (r["slug"] for r in scope.values()):
            t = t.removeprefix(f"{slug}:")
        m = re.match(r"^(?P<path>[^:]+?)(?::(?P<a>\d+)(?:-(?P<b>\d+))?)?$", t)
        if m and ("/" in m.group("path") or "." in m.group("path")):
            f = self.store.one(f"SELECT id FROM files WHERE path = ? AND repo_id IN ({self._in(set(scope))})", (m.group("path").replace("\\", "/"),))
            if f is not None:
                if m.group("a"):
                    a, b = int(m.group("a")), int(m.group("b") or m.group("a"))
                    rows = self.store.all(
                        "SELECT id FROM nodes WHERE file_id = ? AND kind = 'symbol' AND sub_kind <> 'constant' AND start_line <= ? AND end_line >= ? ORDER BY start_line DESC",
                        (f["id"], b, a),
                    )
                    inner = [int(r["id"]) for r in rows]
                    return inner[:1] if inner and a == b else inner
                top_level = "SELECT id FROM nodes WHERE file_id = ? AND (kind = 'file' OR parent IN (SELECT id FROM nodes WHERE file_id = ? AND kind = 'file'))"
                return [int(r["id"]) for r in self.store.all(top_level, (f["id"], f["id"]))]
        return [s.id for s in self.symbol(t, [r["slug"] for r in scope.values()], limit=3) if s.qualified == t or s.name == t or s.qualified.endswith("." + t)]

    def impact(self, targets: list[str], repos: list[str], *, depth: int = 3) -> Impact:
        """What a change to these places reaches: the callers (up to `depth` calls away), then the endpoints, topics
        and tables those expose or use. Members of a changed class count as changed."""
        scope = self._repo_ids(repos)
        out = Impact()
        start: set[int] = set()
        for t in targets:
            ids = self._targets(t, scope)
            if not ids:
                out.unknown.append(t)
            start.update(ids)
        if not start:
            return out
        members = {int(r["id"]) for r in self.store.all(f"SELECT id FROM nodes WHERE parent IN ({self._in(start)}) AND kind = 'symbol'")}
        changed = start | members
        seen = set(changed)
        queue = deque((i, 0) for i in changed)
        while queue:
            node, d = queue.popleft()
            if d >= depth:
                continue
            for r in self.store.all("SELECT src FROM edges WHERE dst = ? AND kind IN ('calls', 'extends')", (node,)):
                if int(r["src"]) not in seen:
                    seen.add(int(r["src"]))
                    queue.append((int(r["src"]), d + 1))
        facts = self.store.all(
            f"SELECT DISTINCT n.* FROM edges e JOIN nodes n ON n.id = e.dst WHERE e.src IN ({self._in(seen)}) AND n.kind IN ('endpoint', 'topic', 'table', 'external')"
        )
        infos = self._nodes(seen | {int(f["id"]) for f in facts}, scope)
        out.targets = [infos[i] for i in sorted(start) if i in infos]
        out.callers = sorted((infos[i] for i in seen - changed if i in infos and infos[i].kind != "file"), key=lambda n: n.qualified)
        for f in facts:
            info = infos.get(int(f["id"]))
            if info is not None:
                {"endpoint": out.endpoints, "topic": out.topics, "table": out.tables}.get(f["kind"], out.services).append(info)
        return out

    # ---- reading pack ----------------------------------------------------------------------------------------

    def pack(self, question: str, repos: list[str], *, budget_tokens: int = 6000) -> ContextPack:
        """A first read for a question, within a token budget: the outlines of the files that matter most, then the
        best matching pieces of code."""
        hits = self.search(question, repos, kinds=["source", "config"], limit=24) or self.search(question, repos, limit=24)
        pack = ContextPack(question=question)
        by_file: dict[tuple[str, str], float] = defaultdict(float)
        for h in hits:
            by_file[(h.repo, h.path)] += h.score
        used = 0
        for (repo, path), _ in sorted(by_file.items(), key=lambda x: -x[1])[:3]:
            outline = self.outline(repo, path)
            if outline is None or not outline.symbols:
                continue
            cost = sum(approx_tokens(s.signature) + 6 for s in outline.symbols)
            if used + cost > budget_tokens // 3:
                continue
            pack.outline.append(outline)
            used += cost
        for h in hits:
            cost = approx_tokens(h.text) + 12
            if used + cost > budget_tokens:
                pack.truncated = True
                continue
            pack.items.append(h)
            used += cost
        pack.tokens = used
        return pack

    # ---- facts for the service map ---------------------------------------------------------------------------

    def fact_edges(self, repos: list[str]) -> list[dict[str, Any]]:
        """Every edge from code to an endpoint, topic, table or outside host, with the file it sits in."""
        scope = self._repo_ids(repos)
        rows = self.store.all(
            "SELECT e.id, e.repo_id, e.src, e.kind, e.confidence, e.line, e.source, e.meta_json, n.kind AS node_kind, n.name AS node_name, n.id AS node_id, f.path "
            f"FROM edges e JOIN nodes n ON n.id = e.dst LEFT JOIN files f ON f.id = e.file_id WHERE e.repo_id IN ({self._in(set(scope))}) "
            "AND n.kind IN ('endpoint', 'topic', 'table', 'external')"
        )
        return [
            {**dict(r), "repo": scope[int(r["repo_id"])]["slug"], "meta": json.loads(r["meta_json"] or "{}"), "path": r["path"] or ""} for r in rows
        ]

    def files(self, repo: str) -> list[str]:
        scope = self._repo_ids([repo])
        return [r["path"] for r in self.store.all(f"SELECT path FROM files WHERE repo_id IN ({self._in(set(scope))}) ORDER BY path")]

    # ---- the inside of a service (the map's service and module levels) -----------------------------------------

    def file_rows(self, repo: str) -> list[dict[str, Any]]:
        scope = self._repo_ids([repo])
        return [dict(r) for r in self.store.all(f"SELECT path, kind, lang, lines FROM files WHERE repo_id IN ({self._in(set(scope))}) ORDER BY path")]

    def code_edges(self, repos: list[str]) -> list[dict[str, Any]]:
        """Every edge between two pieces of code (calls, imports, extends, implements) with the file of each end."""
        scope = self._repo_ids(repos)
        rows = self.store.all(
            "SELECT e.kind, e.confidence, e.line, e.source, e.repo_id, e.src, e.dst, sf.path AS src_path, df.path AS dst_path, dn.repo_id AS dst_repo_id "
            "FROM edges e JOIN nodes sn ON sn.id = e.src JOIN nodes dn ON dn.id = e.dst JOIN files sf ON sf.id = sn.file_id JOIN files df ON df.id = dn.file_id "
            f"WHERE e.repo_id IN ({self._in(set(scope))}) AND e.kind IN ('calls', 'imports', 'extends', 'implements')"
        )
        return [{**dict(r), "repo": scope[int(r["repo_id"])]["slug"], "dst_repo": scope[int(r["dst_repo_id"])]["slug"] if int(r["dst_repo_id"]) in scope else ""} for r in rows]

    def code_nodes(self, repo: str, paths: list[str]) -> list[dict[str, Any]]:
        """The file nodes and symbol nodes of some files of one repository."""
        scope = self._repo_ids([repo])
        if not scope or not paths:
            return []
        out: list[dict[str, Any]] = []
        for start in range(0, len(paths), 400):
            batch = paths[start : start + 400]
            marks = ",".join("?" * len(batch))
            out += [
                dict(r)
                for r in self.store.all(
                    "SELECT n.id, n.kind, n.sub_kind, n.name, n.qualified, n.parent, n.start_line, n.end_line, f.path FROM nodes n JOIN files f ON f.id = n.file_id "
                    f"WHERE n.repo_id IN ({self._in(set(scope))}) AND n.kind IN ('file', 'symbol') AND f.path IN ({marks}) ORDER BY n.id",
                    batch,
                )
            ]
        return out
