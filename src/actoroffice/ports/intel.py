"""Code intelligence: what the index and the graph answer, and how extractors add facts to the graph."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

NODE_KINDS = ("repo", "service", "module", "file", "symbol", "endpoint", "topic", "table", "external", "config_key")
EDGE_KINDS = (
    "contains", "imports", "calls", "extends", "implements", "exposes", "calls_http", "publishes", "subscribes",
    "reads", "writes", "depends_on", "configured_by",
)  # fmt: skip
# declared (people said so) > exact (read from syntax) > resolved (import + name) > heuristic (matched by name or string)
CONFIDENCE = ("heuristic", "resolved", "exact", "declared")


def cite(repo: str, path: str, start: int | None = None, end: int | None = None) -> str:
    """A source reference in Doc Core's `Nguồn:` syntax: `repo:path:start-end`."""
    place = f"{repo}:{path}" if repo else path
    if start is None:
        return place
    return f"{place}:{start}" if end is None or end == start else f"{place}:{start}-{end}"


class Hit(BaseModel):
    repo: str
    path: str
    start: int
    end: int
    text: str
    symbol: str = ""  # the symbol that contains the hit
    cite: str = ""
    commit: str = ""
    score: float = 0.0


class SymbolInfo(BaseModel):
    id: int
    repo: str
    kind: str  # class | interface | function | method | field | constant…
    name: str
    qualified: str
    path: str
    start: int
    end: int
    signature: str = ""
    doc: str = ""
    cite: str = ""


class Outline(BaseModel):
    repo: str
    path: str
    lang: str = ""
    lines: int = 0
    commit: str = ""
    symbols: list[SymbolInfo] = Field(default_factory=list)


class NodeInfo(BaseModel):
    id: int
    kind: str
    name: str
    qualified: str = ""
    repo: str = ""
    path: str = ""
    line: int | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class EdgeInfo(BaseModel):
    id: int = 0
    src: int
    dst: int
    kind: str
    confidence: str = "exact"
    cite: str = ""  # evidence: path:line at the indexed commit
    source: str = ""  # extractor name | "declared" | "scip" | "link"
    count: int = 1  # edges folded into this one (service level)


class Subgraph(BaseModel):
    nodes: list[NodeInfo] = Field(default_factory=list)
    edges: list[EdgeInfo] = Field(default_factory=list)
    commit: dict[str, str] = Field(default_factory=dict)  # repo → indexed commit
    truncated: bool = False


class Impact(BaseModel):
    targets: list[NodeInfo] = Field(default_factory=list)
    callers: list[NodeInfo] = Field(default_factory=list)
    endpoints: list[NodeInfo] = Field(default_factory=list)
    topics: list[NodeInfo] = Field(default_factory=list)
    tables: list[NodeInfo] = Field(default_factory=list)
    services: list[NodeInfo] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)  # targets not found in the index


class ContextPack(BaseModel):
    question: str
    items: list[Hit] = Field(default_factory=list)
    outline: list[Outline] = Field(default_factory=list)
    tokens: int = 0
    truncated: bool = False


class IndexState(BaseModel):
    repo: str
    indexed_commit: str | None = None
    head_commit: str | None = None
    state: str = "none"  # none | indexing | ready | stale | error
    level: str = "text"  # text | symbols | graph
    files: int = 0
    symbols: int = 0
    edges: int = 0
    took_s: float = 0.0
    error: str | None = None


class Fact(BaseModel):
    """What an extractor found in one file: a node, and an edge to it from a symbol of that file (or from the file)."""

    node_kind: str  # endpoint | topic | table | external | config_key | service | module
    name: str  # "POST /v1/refund", "payments.refund.requested", "payment"
    edge: str  # exposes | calls_http | publishes | subscribes | reads | writes | configured_by | depends_on | contains
    line: int = 0
    symbol: str = ""  # qualified name of the symbol the fact belongs to ("" = the file)
    confidence: str = "exact"
    meta: dict[str, Any] = Field(default_factory=dict)


class ParsedSymbol(BaseModel):
    kind: str
    name: str
    qualified: str
    start: int
    end: int
    signature: str = ""
    doc: str = ""
    parent: str = ""  # qualified name of the enclosing symbol
    decorators: list[str] = Field(default_factory=list)
    bases: list[str] = Field(default_factory=list)


class ParsedCall(BaseModel):
    caller: str  # qualified name of the calling symbol ("" = module level)
    name: str  # the called name as written: "check", "limit_service.check", "LimitService"
    line: int


class ParsedImport(BaseModel):
    module: str  # "actoroffice.domain.stage", "./limit", "com.acme.pay.LimitService"
    names: list[str] = Field(default_factory=list)  # imported names ([] = the module itself)
    alias: dict[str, str] = Field(default_factory=dict)  # local name → imported name
    line: int = 0


class ParsedFile(BaseModel):
    path: str
    lang: str
    text: str = ""
    symbols: list[ParsedSymbol] = Field(default_factory=list)
    calls: list[ParsedCall] = Field(default_factory=list)
    imports: list[ParsedImport] = Field(default_factory=list)


class RepoFacts(BaseModel):
    """What an extractor may know about the repository as a whole."""

    slug: str
    files: list[str] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list)
    markers: list[str] = Field(default_factory=list)  # "pom.xml", "package.json", "pyproject.toml", "Dockerfile"…


class Extractor(Protocol):
    name: str

    def applies(self, repo: RepoFacts) -> bool: ...

    def extract(self, file: ParsedFile, repo: RepoFacts) -> list[Fact]: ...
