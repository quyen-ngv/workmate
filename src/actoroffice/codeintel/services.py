"""The service map: which deployable units exist in the indexed repositories and how they reach each other (HTTP,
topics, shared tables, outside systems).

Built on demand from the fact edges of the index, for the repositories one team may see. What people declare in
`.office/services.yaml` wins over what is detected.
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


def build_map(query: CodeQuery, repos: list[str], declared: dict[str, Declared] | None = None, *, tables: str = "shared") -> Subgraph:
    """Services of `repos` and the edges between them. `tables`: "shared" shows a table only when two services use
    it, "all" shows every table, "none" hides them."""
    declared = declared or {}
    defs: dict[str, list[ServiceDef]] = {slug: detect(slug, query.files(slug), declared.get(slug, Declared())) for slug in repos}
    aliases: dict[str, str] = {}
    for d in declared.values():
        aliases.update({k.lower(): v for k, v in d.aliases.items()})
    m = _Map()
    names = {s.name for ds in defs.values() for s in ds}
    for ds in defs.values():
        for s in ds:
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

    for f in facts:
        svc = service_of(f["path"], defs[f["repo"]])
        src = m.node("service", svc)
        where = cite(f["repo"], f["path"], f["line"] or None)
        kind, node_kind, name, meta = f["kind"], f["node_kind"], f["node_name"], f["meta"]
        if kind == "calls_http" and node_kind == "endpoint":
            hinted = target_of(str(meta.get("target") or "")) or target_of(_host(str(meta.get("url") or "")))
            exposing = owners.get(_endpoint_key(name), set()) - {svc}
            if hinted and hinted in names:
                m.edge(src, m.node("service", hinted), "calls_http", "resolved" if hinted in exposing or not exposing else "heuristic", where, f["source"])
            elif hinted:
                m.edge(src, m.node("external", hinted), "calls_http", "resolved", where, f["source"])
            elif len(exposing) == 1:
                m.edge(src, m.node("service", next(iter(exposing))), "calls_http", "heuristic", where, f["source"])
            elif meta.get("target"):
                m.edge(src, m.node("external", str(meta["target"])), "calls_http", f["confidence"], where, f["source"])
        elif kind == "calls_http" and node_kind == "external":
            hinted = target_of(name) or target_of(name.split(":")[0])
            dst = m.node("service", hinted) if hinted and hinted in names else m.node("external", hinted or name)
            m.edge(src, dst, "calls_http", f["confidence"], where, f["source"])
        elif kind == "publishes":
            m.edge(src, m.node("topic", name, {k: v for k, v in meta.items() if k == "broker"}), "publishes", f["confidence"], where, f["source"])
        elif kind == "subscribes":
            m.edge(m.node("topic", name, {k: v for k, v in meta.items() if k == "broker"}), src, "subscribes", f["confidence"], where, f["source"])
        elif kind in ("reads", "writes") and tables != "none":
            shared = len(table_users.get(name, set())) > 1
            if shared or tables == "all":
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

    linked = {e.src for e in m.edges.values()} | {e.dst for e in m.edges.values()}
    nodes = [n for n in m.nodes.values() if n.kind == "service" or n.id in linked]
    states = {s.repo: s.indexed_commit or "" for s in query.state(repos)}
    return Subgraph(nodes=nodes, edges=list(m.edges.values()), commit=states)


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


SHAPES = {"service": ('["', '"]'), "external": ('(["', '"])'), "topic": ('{{"', '"}}'), "table": ('[("', '")]')}
LABELS = {"calls_http": "HTTP", "publishes": "publish", "subscribes": "subscribe", "reads": "read", "writes": "write", "depends_on": "lib"}


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
