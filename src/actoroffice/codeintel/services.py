"""The service map: which deployable units exist in the indexed repositories and how they reach each other (HTTP,
topics, shared tables, outside systems).

Built on demand from the fact edges of the index, for the repositories one team may see. What people declare in
`.office/services.yaml` wins over what is detected.

Three levels, each a small graph: the system (services and what is between them), one service (its modules, its
endpoints, topics and tables, and who it talks to), one module (its files and their main symbols).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml

from actoroffice.ports.intel import EdgeInfo, NodeInfo, Subgraph, cite

from .extractors import norm_path
from .query import CONFIDENCE_RANK, CodeQuery

DECLARED_FILE = ".office/services.yaml"
BUILD_MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts", "package.json", "pyproject.toml", "go.mod", "pubspec.yaml")


@dataclass
class Declared:
    services: list[dict[str, Any]] = field(default_factory=list)
    aliases: dict[str, str] = field(default_factory=dict)
    external: list[dict[str, Any]] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    ignore: list[dict[str, Any]] = field(default_factory=list)


def load_declared(text: str | None) -> Declared:
    """`.office/services.yaml` of a repository. A file that is not valid YAML declares nothing."""
    if not text:
        return Declared()
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return Declared()
    if not isinstance(data, dict):
        return Declared()
    return Declared(
        services=[s for s in data.get("services") or [] if isinstance(s, dict) and s.get("name")],
        aliases={str(k): str(v) for k, v in (data.get("aliases") or {}).items()},
        external=[e for e in data.get("external") or [] if isinstance(e, dict) and e.get("name")],
        edges=[e for e in data.get("edges") or [] if isinstance(e, dict) and e.get("from") and e.get("to")],
        ignore=[e for e in data.get("ignore") or [] if isinstance(e, dict)],
    )


@dataclass(frozen=True)
class ServiceDef:
    name: str
    repo: str
    path: str = ""  # folder inside the repository ("" = the whole repository)
    declared: bool = False


def detect(slug: str, files: list[str], declared: Declared) -> list[ServiceDef]:
    """Declared services; else one per folder with a Dockerfile; else one per folder with a build file when the root
    has none; else the repository itself."""
    if declared.services:
        return [ServiceDef(str(s["name"]), slug, str(s.get("path") or "").strip("/"), True) for s in declared.services]
    base = slug.rsplit("/", 1)[-1]
    folders = sorted({f.rsplit("/", 1)[0] if "/" in f else "" for f in files if f.rsplit("/", 1)[-1] == "Dockerfile"})
    if len(folders) <= 1:
        root_build = any(f in BUILD_MARKERS for f in files)
        modules = sorted({f.rsplit("/", 1)[0] for f in files if "/" in f and f.rsplit("/", 1)[-1] in BUILD_MARKERS and f.count("/") <= 2})
        folders = modules if not root_build and len(modules) > 1 else []
    if len(folders) > 1:
        return [ServiceDef(folder.rsplit("/", 1)[-1] or base, slug, folder) for folder in folders]
    return [ServiceDef(base, slug)]


def service_of(path: str, defs: list[ServiceDef]) -> str:
    best = ""
    name = defs[0].name if defs else ""
    for d in defs:
        if (not d.path or path == d.path or path.startswith(d.path + "/")) and len(d.path) >= len(best):
            best, name = d.path, d.name
    return name


class _Map:
    def __init__(self) -> None:
        self.ids: dict[tuple[str, str], int] = {}
        self.nodes: dict[int, NodeInfo] = {}
        self.edges: dict[tuple[int, int, str], EdgeInfo] = {}

    def node(self, kind: str, name: str, meta: dict[str, Any] | None = None) -> int:
        key = (kind, name)
        if key not in self.ids:
            self.ids[key] = len(self.ids) + 1
            self.nodes[self.ids[key]] = NodeInfo(id=self.ids[key], kind=kind, name=name, qualified=name, meta=dict(meta or {}))
        elif meta:
            self.nodes[self.ids[key]].meta.update(meta)
        return self.ids[key]

    def edge(self, src: int, dst: int, kind: str, confidence: str, where: str, source: str) -> None:
        if src == dst:
            return
        key = (src, dst, kind)
        old = self.edges.get(key)
        if old is None:
            self.edges[key] = EdgeInfo(id=len(self.edges) + 1, src=src, dst=dst, kind=kind, confidence=confidence, cite=where, source=source)
            return
        old.count += 1
        if CONFIDENCE_RANK.get(confidence, 0) > CONFIDENCE_RANK.get(old.confidence, 0):
            old.confidence, old.cite, old.source = confidence, where, source


SOURCE_KINDS = ("source",)
MODULE_ROOT = "(gốc)"  # files that sit directly in the folder the modules are under
MAX_MODULE_NODES = 160


class Modules:
    """The modules of one service: its code split by folder until no folder holds most of it. A folder that everything
    is under, that only leads to one other folder, or that holds more than half of the code and has folders of its
    own is passed through, so `src/main/java/com/acme/pay/refund` and `…/pay/limit` are the modules `refund` and
    `limit`, and `tests` or `scripts` next to `src` are modules of their own."""

    def __init__(self, rows: list[dict[str, Any]], service: ServiceDef) -> None:
        self.base = service.path
        inside = [r for r in rows if not self.base or str(r["path"]).startswith(self.base + "/")]
        code = [self._folder(str(r["path"])) for r in inside if r["kind"] in SOURCE_KINDS] or [self._folder(str(r["path"])) for r in inside]
        self.through: set[str] = {""}  # folders that are not modules themselves: their folders are
        for _ in range(16):
            sizes: dict[str, int] = {}
            for parts in code:
                folder, direct = self._walk(parts)
                if not direct:
                    sizes[folder] = sizes.get(folder, 0) + 1
            grow = None
            for folder, size in sorted(sizes.items(), key=lambda kv: -kv[1]):
                kids = {"/".join(parts[: folder.count("/") + 2]) for parts in code if len(parts) > folder.count("/") + 1 and "/".join(parts[: folder.count("/") + 1]) == folder}
                alone = size == len(code)
                step = len(kids) == 1 and not any("/".join(parts) == folder for parts in code)  # it only leads to one other folder
                if kids and (alone or step or (size * 2 > len(code) and len(kids) >= 2)):
                    grow = folder
                    break
            if grow is None:
                break
            self.through.add(grow)
        self.files: dict[str, list[str]] = {}
        self.folder: dict[str, str] = {}  # module → its folder in the repository
        for r in inside:
            name, folder = self._place(str(r["path"]))
            self.files.setdefault(name, []).append(str(r["path"]))
            self.folder.setdefault(name, folder)

    def _folder(self, path: str) -> list[str]:
        rel = path[len(self.base) :].lstrip("/") if self.base else path
        return rel.split("/")[:-1]

    def _walk(self, parts: list[str]) -> tuple[str, bool]:
        """The module folder of a file in `parts`, and whether the file sits directly in a passed-through folder."""
        at = 0
        while at < len(parts) and "/".join(parts[:at]) in self.through:
            at += 1
        folder = "/".join(parts[:at])
        return folder, folder in self.through

    def _place(self, path: str) -> tuple[str, str]:
        folder, direct = self._walk(self._folder(path))
        above = max((t for t in self.through if t and (folder == t or folder.startswith(t + "/")) and (direct or t != folder)), key=len, default="")
        if direct:  # a file next to the modules: the folder itself, named by its last step
            return (folder.rsplit("/", 1)[-1] if folder else MODULE_ROOT), "/".join(p for p in (self.base, folder) if p)
        name = folder[len(above) :].lstrip("/") if above else folder
        return name, "/".join(p for p in (self.base, folder) if p)

    def of(self, path: str) -> str:
        return self._place(path)[0]

    def path_of(self, module: str) -> str:
        return self.folder.get(module, "")


def find_service(defs: dict[str, list[ServiceDef]], name: str) -> ServiceDef | None:
    return next((d for ds in defs.values() for d in ds if d.name == name), None)


def build_map(
    query: CodeQuery, repos: list[str], declared: dict[str, Declared] | None = None, *, tables: str = "shared", service: str | None = None, module: str | None = None
) -> Subgraph:
    """Services of `repos` and the edges between them. `tables`: "shared" shows a table only when two services use
    it, "all" shows every table, "none" hides them.

    With `service`: the inside of that service instead (its modules, endpoints, topics, tables) and the services it
    talks to. With `service` and `module`: the files and main symbols of that module. Raises KeyError when the service
    or the module does not exist."""
    declared = declared or {}
    defs: dict[str, list[ServiceDef]] = {slug: detect(slug, query.files(slug), declared.get(slug, Declared())) for slug in repos}
    aliases: dict[str, str] = {}
    for d in declared.values():
        aliases.update({k.lower(): v for k, v in d.aliases.items()})
    m = _Map()
    names = {s.name for ds in defs.values() for s in ds}
    focus_def = find_service(defs, service) if service else None
    if service and focus_def is None:
        raise KeyError(f"không có service '{service}' (có: {', '.join(sorted(names))})")
    mods = Modules(query.file_rows(focus_def.repo), focus_def) if focus_def else None
    if module is not None:
        assert focus_def is not None and mods is not None
        if module not in mods.files:
            raise KeyError(f"service '{service}' không có module '{module}' (có: {', '.join(sorted(mods.files))})")
        return _module_view(query, repos, defs, focus_def, mods, module)

    def home(repo: str, path: str) -> int:
        """The node a piece of code belongs to at this level: its service, or its module inside the service looked at."""
        owner = service_of(path, defs[repo])
        if focus_def is None or mods is None or owner != focus_def.name or repo != focus_def.repo:
            return m.node("service", owner)
        name = mods.of(path)
        return m.node("module", name, {"service": owner, "repo": repo, "path": mods.path_of(name), "files": len(mods.files.get(name, []))})

    for ds in defs.values():
        for s in ds:
            if focus_def is None:
                m.node("service", s.name, {"repo": s.repo, "path": s.path, "declared": s.declared})
    for d in declared.values():
        for e in d.external:
            m.node("external", str(e["name"]), {k: v for k, v in e.items() if k != "name"})

    facts = query.fact_edges(repos)
    owners: dict[str, set[str]] = {}  # endpoint → services that expose it
    for f in facts:
        if f["kind"] == "exposes":
            owners.setdefault(_endpoint_key(f["node_name"]), set()).add(service_of(f["path"], defs[f["repo"]]))

    def target_of(hint: str) -> str | None:
        hint = (hint or "").lower().strip()
        if not hint:
            return None
        if hint in aliases:
            return aliases[hint]
        return next((n for n in names if n.lower() == hint or n.lower() == hint.removesuffix("-service") or hint in (n.lower() + "-service", n.lower() + "-api")), None)

    table_users: dict[str, set[str]] = {}
    for f in facts:
        if f["node_kind"] == "table":
            table_users.setdefault(f["node_name"], set()).add(service_of(f["path"], defs[f["repo"]]))

    inside_service = focus_def.name if focus_def else None
    exposed_here = {_endpoint_key(f["node_name"]): f["node_name"] for f in facts if f["kind"] == "exposes" and service_of(f["path"], defs[f["repo"]]) == inside_service}

    def callee(target: str, key: str) -> int:
        """Where a call to a service lands: at system level the service; inside the service looked at, its endpoint."""
        if target == inside_service and key in exposed_here:
            return m.node("endpoint", exposed_here[key])
        return m.node("service", target)

    for f in facts:
        svc = service_of(f["path"], defs[f["repo"]])
        src = home(f["repo"], f["path"])
        where = cite(f["repo"], f["path"], f["line"] or None)
        kind, node_kind, name, meta = f["kind"], f["node_kind"], f["node_name"], f["meta"]
        named = target_of(str(meta.get("service") or "")) if f["source"] == "deploy" else None
        if named and named in names and named != svc:  # one descriptor (a compose file) describes several services
            svc = named
            src = src if named == inside_service else m.node("service", named)
        if kind == "exposes" and svc == inside_service:
            m.edge(src, m.node("endpoint", name), "exposes", f["confidence"], where, f["source"])
        elif kind == "calls_http" and node_kind == "endpoint":
            hinted = target_of(str(meta.get("target") or "")) or target_of(_host(str(meta.get("url") or "")))
            key = _endpoint_key(name)
            exposing = owners.get(key, set()) - {svc}
            if hinted and hinted in names:
                m.edge(src, callee(hinted, key), "calls_http", "resolved" if hinted in exposing or not exposing else "heuristic", where, f["source"])
            elif hinted:
                m.edge(src, m.node("external", hinted), "calls_http", "resolved", where, f["source"])
            elif len(exposing) == 1:
                m.edge(src, callee(next(iter(exposing)), key), "calls_http", "heuristic", where, f["source"])
            elif meta.get("target"):
                m.edge(src, m.node("external", str(meta["target"])), "calls_http", f["confidence"], where, f["source"])
        elif kind == "calls_http" and node_kind == "external":
            hinted = target_of(name) or target_of(name.split(":")[0])
            dst = m.node("service", hinted) if hinted and hinted in names else m.node("external", hinted or name)
            m.edge(src, dst, "calls_http", f["confidence"], where, f["source"])
        elif kind == "depends_on" and node_kind == "external":
            hinted = target_of(name) or target_of(name.split(":")[0])
            dst = callee(hinted, "") if hinted and hinted in names else m.node("external", hinted or name)
            m.edge(src, dst, "depends_on", f["confidence"], where, f["source"])
        elif kind == "publishes":
            m.edge(src, m.node("topic", name, {k: v for k, v in meta.items() if k == "broker"}), "publishes", f["confidence"], where, f["source"])
        elif kind == "subscribes":
            m.edge(m.node("topic", name, {k: v for k, v in meta.items() if k == "broker"}), src, "subscribes", f["confidence"], where, f["source"])
        elif kind in ("reads", "writes") and tables != "none":
            shared = len(table_users.get(name, set())) > 1
            if shared or tables == "all" or svc == inside_service:  # inside a service every table it uses is shown
                m.edge(src, m.node("table", name, {"shared": shared}), kind, f["confidence"], where, f["source"])

    for slug, d in declared.items():
        for e in d.edges:
            src = m.node("service" if e["from"] in names else "external", str(e["from"]))
            dst = m.node("service" if e["to"] in names else "external", str(e["to"]))
            m.edge(src, dst, str(e.get("kind") or "calls_http"), "declared", cite(slug, DECLARED_FILE), "declared")
            m.edges[(src, dst, str(e.get("kind") or "calls_http"))].confidence = "declared"
        for e in d.ignore:
            a, b = m.ids.get(("service", str(e.get("from")))), m.ids.get(("service", str(e.get("to")))) or m.ids.get(("external", str(e.get("to"))))
            for key in [k for k in m.edges if k[0] == a and k[1] == b]:
                del m.edges[key]

    states = {s.repo: s.indexed_commit or "" for s in query.state(repos)}
    if focus_def is not None and mods is not None:
        for name in mods.files:  # every module shows, also one nothing points at
            m.node("module", name, {"service": focus_def.name, "repo": focus_def.repo, "path": mods.path_of(name), "files": len(mods.files[name])})
        for e in query.code_edges([focus_def.repo]):
            if e["repo"] != focus_def.repo or e["dst_repo"] != focus_def.repo:
                continue
            if service_of(e["src_path"], defs[focus_def.repo]) != focus_def.name or service_of(e["dst_path"], defs[focus_def.repo]) != focus_def.name:
                continue
            m.edge(home(e["repo"], e["src_path"]), home(e["repo"], e["dst_path"]), e["kind"], e["confidence"], cite(e["repo"], e["src_path"], e["line"] or None), e["source"])
        own = {n.id for n in m.nodes.values() if n.kind == "module" or (n.kind == "endpoint" and n.name in exposed_here.values()) or (n.kind == "service" and n.name == focus_def.name)}
        edges = [e for e in m.edges.values() if e.src in own or e.dst in own]
        # one more step through what is shared: who else listens to a topic this service publishes, who else uses its tables.
        # What happens between two other services is not this service's picture.
        shared = {i for e in edges for i in (e.src, e.dst) if m.nodes[i].kind in ("topic", "table")}
        edges += [e for e in m.edges.values() if (e.src in shared or e.dst in shared) and e not in edges]
        linked = {e.src for e in edges} | {e.dst for e in edges}
        return Subgraph(nodes=[n for n in m.nodes.values() if n.kind == "module" or n.id in linked], edges=edges, commit=states)
    linked = {e.src for e in m.edges.values()} | {e.dst for e in m.edges.values()}
    nodes = [n for n in m.nodes.values() if n.kind == "service" or n.id in linked]
    return Subgraph(nodes=nodes, edges=list(m.edges.values()), commit=states)


CODE_SYMBOLS = ("class", "interface", "function", "method", "enum", "struct", "record", "trait", "type")


def _module_view(
    query: CodeQuery, repos: list[str], defs: dict[str, list[ServiceDef]], service: ServiceDef, mods: Modules, module: str, *, files_only: bool = False
) -> Subgraph:
    """One module: its files, the main symbols of each, what calls what inside it, and what it reaches outside
    (another module, another service, an endpoint, a topic, a table). A module too large to draw symbol by symbol is
    drawn file by file (`files_only`) and marked truncated."""
    m = _Map()
    paths = mods.files[module]
    rows = query.code_nodes(service.repo, paths)
    by_id = {int(r["id"]): r for r in rows}
    file_of = {r["path"]: int(r["id"]) for r in rows if r["kind"] == "file"}
    shown: dict[int, int] = {}  # index node id → node of this view

    def top(node_id: int) -> int | None:
        """The file or top-level symbol an index node is shown as."""
        seen = 0
        if files_only and node_id in by_id:
            return file_of.get(by_id[node_id]["path"])
        while node_id in by_id and seen < 50:
            row = by_id[node_id]
            parent = row["parent"]
            if row["kind"] == "file" or parent is None or (int(parent) in by_id and by_id[int(parent)]["kind"] == "file"):
                return node_id
            node_id, seen = int(parent), seen + 1
        return None

    def show(node_id: int) -> int:
        if node_id not in shown:
            row = by_id[node_id]
            if row["kind"] == "file":
                shown[node_id] = m.node("file", row["path"], {"repo": service.repo, "path": row["path"]})
                m.nodes[shown[node_id]].path = row["path"]
            else:
                shown[node_id] = m.node(row["sub_kind"] or "symbol", row["qualified"], {"repo": service.repo, "path": row["path"]})
                m.nodes[shown[node_id]].path, m.nodes[shown[node_id]].line = row["path"], row["start_line"]
                m.nodes[shown[node_id]].name = row["name"]
        return shown[node_id]

    def outside(repo: str, path: str) -> int:
        owner = service_of(path, defs[repo]) if repo in defs else ""
        if repo == service.repo and owner == service.name:
            other = mods.of(path)
            return m.node("module", other, {"service": owner, "repo": repo, "path": mods.path_of(other)})
        return m.node("service", owner or repo)

    for r in rows:
        if r["kind"] == "file":
            show(int(r["id"]))
    wanted = set(paths)
    for e in query.code_edges([service.repo]):
        src_in, dst_in = e["src_path"] in wanted and e["repo"] == service.repo, e["dst_path"] in wanted and e["dst_repo"] == service.repo
        if not src_in and not dst_in:
            continue
        a = top(int(e["src"])) if src_in else None
        b = top(int(e["dst"])) if dst_in else None
        src = show(a) if a is not None else outside(e["repo"], e["src_path"])
        dst = show(b) if b is not None else outside(e["dst_repo"] or e["repo"], e["dst_path"])
        m.edge(src, dst, e["kind"], e["confidence"], cite(e["repo"], e["src_path"], e["line"] or None), e["source"])
    for f in query.fact_edges([service.repo]):
        if f["repo"] != service.repo or f["path"] not in wanted:
            continue
        a = top(int(f["src"])) if int(f["src"]) in by_id else file_of.get(f["path"])
        if a is None:
            continue
        dst = m.node(f["node_kind"], f["node_name"])
        where = cite(f["repo"], f["path"], f["line"] or None)
        if f["kind"] == "subscribes":
            m.edge(dst, show(a), "subscribes", f["confidence"], where, f["source"])
        else:
            m.edge(show(a), dst, f["kind"], f["confidence"], where, f["source"])
    # a file holds its main symbols: the ones something points at or from, then the rest while there is room
    linked = {e.src for e in m.edges.values()} | {e.dst for e in m.edges.values()}
    truncated = files_only
    for r in rows:
        node_id = int(r["id"])
        if files_only or r["kind"] != "symbol" or top(node_id) != node_id or (r["sub_kind"] or "") not in CODE_SYMBOLS:
            continue
        if node_id not in shown:
            if len(m.nodes) >= MAX_MODULE_NODES:
                truncated = True
                continue
            show(node_id)
        if r["path"] in file_of:
            m.edge(show(file_of[r["path"]]), shown[node_id], "contains", "exact", cite(service.repo, r["path"], r["start_line"]), "index")
    if truncated and not files_only:
        keep = {n.id for n in m.nodes.values() if n.id in linked or n.kind == "file"}
        edges = [e for e in m.edges.values() if e.src in keep and e.dst in keep]
        nodes = [n for n in m.nodes.values() if n.id in keep]
    else:
        edges, nodes = list(m.edges.values()), list(m.nodes.values())
    if not files_only and len(nodes) > MAX_MODULE_NODES:
        return _module_view(query, repos, defs, service, mods, module, files_only=True)
    states = {s.repo: s.indexed_commit or "" for s in query.state(repos)}
    return Subgraph(nodes=nodes, edges=edges, commit=states, truncated=truncated)


def diff_maps(base: Subgraph, head: Subgraph) -> dict[str, Any]:
    """What `head` adds to and takes from `base`. Nodes are the same when their kind and name are; edges when both
    ends and the kind are. The answer is one graph to draw, each node and edge marked `same`, `added` or `removed`."""

    def keyed(graph: Subgraph) -> tuple[dict[tuple[str, str], NodeInfo], dict[tuple[tuple[str, str], tuple[str, str], str], EdgeInfo]]:
        by_id = {n.id: (n.kind, n.name) for n in graph.nodes}
        nodes = {(n.kind, n.name): n for n in graph.nodes}
        edges = {(by_id[e.src], by_id[e.dst], e.kind): e for e in graph.edges if e.src in by_id and e.dst in by_id}
        return nodes, edges

    old_nodes, old_edges = keyed(base)
    new_nodes, new_edges = keyed(head)
    ids: dict[tuple[str, str], int] = {}
    nodes, edges = [], []
    for key in [*new_nodes, *[k for k in old_nodes if k not in new_nodes]]:
        ids[key] = len(ids) + 1
        node = new_nodes.get(key) or old_nodes[key]
        mark = "same" if key in old_nodes and key in new_nodes else "added" if key in new_nodes else "removed"
        nodes.append({**node.model_dump(), "id": ids[key], "diff": mark})
    for key in [*new_edges, *[k for k in old_edges if k not in new_edges]]:
        edge = new_edges.get(key) or old_edges[key]
        mark = "same" if key in old_edges and key in new_edges else "added" if key in new_edges else "removed"
        edges.append({**edge.model_dump(), "id": len(edges) + 1, "src": ids[key[0]], "dst": ids[key[1]], "diff": mark})
    count = {mark: {"nodes": sum(n["diff"] == mark for n in nodes), "edges": sum(e["diff"] == mark for e in edges)} for mark in ("added", "removed")}
    return {"nodes": nodes, "edges": edges, **count, "base": base.commit, "head": head.commit}


def _endpoint_key(name: str) -> str:
    method, _, path = name.partition(" ")
    return f"{'*' if method == 'ANY' else method} {norm_path(path)}"


def _host(url: str) -> str:
    return url.split("://", 1)[-1].split("/", 1)[0].split(":")[0] if url else ""


def focus(graph: Subgraph, name: str, depth: int = 1) -> Subgraph:
    """The part of the map within `depth` edges of one node."""
    start = {n.id for n in graph.nodes if n.name == name}
    if not start:
        return graph
    keep = set(start)
    for _ in range(max(1, depth)):
        keep |= {e.dst for e in graph.edges if e.src in keep} | {e.src for e in graph.edges if e.dst in keep}
    return Subgraph(nodes=[n for n in graph.nodes if n.id in keep], edges=[e for e in graph.edges if e.src in keep and e.dst in keep], commit=graph.commit)


SHAPES = {"service": ('["', '"]'), "external": ('(["', '"])'), "topic": ('{{"', '"}}'), "table": ('[("', '")]'), "module": ('[["', '"]]'), "endpoint": ('>"', '"]'), "file": ('["', '"]')}
LABELS = {
    "calls_http": "HTTP", "publishes": "publish", "subscribes": "subscribe", "reads": "read", "writes": "write", "depends_on": "needs",
    "exposes": "expose", "calls": "call", "imports": "import", "extends": "extends", "implements": "implements", "contains": "has",
}  # fmt: skip


def to_mermaid(graph: Subgraph) -> str:
    """The map as a Mermaid flowchart. Edges found by name only are dashed."""
    lines = ["flowchart LR"]
    for n in sorted(graph.nodes, key=lambda n: (n.kind, n.name)):
        left, right = SHAPES.get(n.kind, ('["', '"]'))
        lines.append(f"  n{n.id}{left}{n.name.replace(chr(34), chr(39))}{right}")
    for e in sorted(graph.edges, key=lambda e: (e.src, e.dst, e.kind)):
        label = LABELS.get(e.kind, e.kind) + (f" ×{e.count}" if e.count > 1 else "")
        arrow = "-.->" if e.confidence == "heuristic" else "-->"
        lines.append(f"  n{e.src} {arrow}|{label}| n{e.dst}")
    return "\n".join(lines)
