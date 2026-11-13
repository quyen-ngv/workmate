"""Extractors: framework knowledge that turns a parsed file into graph facts (an endpoint a method exposes, a topic it
publishes to, a table a statement reads). One small class per framework; others come from packages through the
entry point group `actoroffice.extractors`.

They read syntax only. A fact is `exact` when the name is a literal in the source, `heuristic` when it is put together
from something else (a URL from a variable, a table found by a pattern in SQL).
"""

from __future__ import annotations

import json
import re
from importlib import metadata

from actoroffice.ports.intel import Extractor, Fact, ParsedFile, ParsedSymbol, RepoFacts

HTTP_METHODS = ("get", "post", "put", "patch", "delete")


def norm_path(path: str) -> str:
    """One spelling for a route: `/v1/pay/{id}`, `/v1/pay/:id`, `/v1/pay/${id}` and `/v1/pay/<int:id>` are the same."""
    path = re.sub(r"\?.*$", "", path.strip())
    path = re.sub(r"\$\{[^}]*\}|\{[^}]*\}|<[^>]*>|:[A-Za-z_]\w*", "{}", path)
    path = "/" + path.strip("/")
    return re.sub(r"/{2,}", "/", path)


def endpoint(method: str, path: str) -> str:
    return f"{method.upper()} {norm_path(path)}"


def _symbol_at(file: ParsedFile, line: int) -> str:
    inside = [s for s in file.symbols if s.start <= line <= s.end and s.kind != "constant"]
    return max(inside, key=lambda s: s.start).qualified if inside else ""


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _string_arg(text: str) -> str | None:
    m = re.search(r"""["']([^"']*)["']""", text)
    return m.group(1) if m else None


class FastApi:
    """`@app.get("/x")`, `@router.post("/x")` on a function → the function exposes the endpoint."""

    name = "fastapi"
    ROUTE = re.compile(r"^[\w.]+\.(get|post|put|patch|delete|websocket)\(\s*[frb]?[\"']([^\"']*)[\"']")

    def applies(self, repo: RepoFacts) -> bool:
        return "python" in repo.languages

    def extract(self, file: ParsedFile, repo: RepoFacts) -> list[Fact]:
        if file.lang != "python" or ("fastapi" not in file.text and "APIRouter" not in file.text):
            return []
        facts = []
        for s in file.symbols:
            for d in s.decorators:
                m = self.ROUTE.match(d)
                if m:
                    method = "WS" if m.group(1) == "websocket" else m.group(1)
                    facts.append(Fact(node_kind="endpoint", name=endpoint(method, m.group(2)), edge="exposes", line=s.start, symbol=s.qualified))
        return facts


class HttpClient:
    """Calls of an HTTP client with a literal URL: httpx / requests (Python), axios / fetch (JS, TS)."""

    name = "http_client"
    CALL = re.compile(
        r"""\b(?:httpx|requests|axios|session|client|http|self\.http|self\.client|self\._http|this\.http|this\.httpClient)\s*\.\s*(get|post|put|patch|delete)\s*\(\s*[frb]?(["'`])([^"'`\n]+)\2"""
    )
    FETCH = re.compile(r"""\bfetch\s*\(\s*(["'`])([^"'`\n]+)\1(?:\s*,\s*\{[^}]*?method\s*:\s*["'](\w+)["'])?""", re.S)
    URL = re.compile(r"^https?://([^/]+)(/.*)?$")

    def applies(self, repo: RepoFacts) -> bool:
        return bool({"python", "javascript", "typescript", "tsx"} & set(repo.languages))

    def extract(self, file: ParsedFile, repo: RepoFacts) -> list[Fact]:
        if file.lang not in ("python", "javascript", "typescript", "tsx"):
            return []
        facts = []
        found = [(m.start(), m.group(1), m.group(3)) for m in self.CALL.finditer(file.text)]
        if file.lang != "python":
            found += [(m.start(), m.group(3) or "get", m.group(2)) for m in self.FETCH.finditer(file.text)]
        for pos, method, url in found:
            line = _line_of(file.text, pos)
            symbol = _symbol_at(file, line)
            m = self.URL.match(url)
            if m:
                host, path = m.group(1), m.group(2) or "/"
                if "{" in host or "$" in host:
                    continue
                facts.append(Fact(node_kind="external", name=host, edge="calls_http", line=line, symbol=symbol, meta={"endpoint": endpoint(method, path)}))
            elif url.startswith("/"):
                facts.append(Fact(node_kind="endpoint", name=endpoint(method, url), edge="calls_http", line=line, symbol=symbol, confidence="heuristic"))
        return facts


class Spring:
    """Spring MVC mappings, Feign clients, Kafka and RabbitMQ listeners and senders."""

    name = "spring"
    MAPPING = re.compile(r"^(Get|Post|Put|Patch|Delete|Request)Mapping\b(.*)$", re.S)
    SEND = re.compile(r"""\b\w*[kK]afka\w*\s*\.\s*send\s*\(\s*(?:"([^"]+)"|([A-Z][A-Z0-9_]+))""")
    RABBIT_SEND = re.compile(r"""\b\w*[rR]abbit\w*\s*\.\s*convertAndSend\s*\(\s*"([^"]+)"(?:\s*,\s*"([^"]+)")?""")

    def applies(self, repo: RepoFacts) -> bool:
        return "java" in repo.languages

    @staticmethod
    def _mapping(decorators: list[str]) -> tuple[str, str] | None:
        for d in decorators:
            m = Spring.MAPPING.match(d)
            if not m:
                continue
            verb, args = m.group(1), m.group(2)
            path = _string_arg(args) or ""
            method = verb.upper()
            if verb == "Request":
                rm = re.search(r"RequestMethod\.(\w+)", args)
                method = rm.group(1) if rm else "ANY"
            return method, path
        return None

    @staticmethod
    def _attr(annotation: str, *names: str) -> str | None:
        for name in names:
            m = re.search(rf"""\b{name}\s*=\s*\{{?\s*"([^"]+)\"""", annotation)
            if m:
                return m.group(1)
        return None

    def extract(self, file: ParsedFile, repo: RepoFacts) -> list[Fact]:
        if file.lang != "java":
            return []
        facts: list[Fact] = []
        classes = {s.qualified: s for s in file.symbols if s.kind in ("class", "interface")}
        for s in file.symbols:
            owner = classes.get(s.parent)
            if s.kind == "method" and owner is not None:
                mapped = self._mapping(s.decorators)
                if mapped:
                    prefix = (self._mapping(owner.decorators) or ("", ""))[1]
                    name = endpoint(mapped[0], f"{prefix}/{mapped[1]}")
                    feign = next((d for d in owner.decorators if d.startswith("FeignClient")), None)
                    if feign is not None:
                        target = self._attr(feign, "name", "value", "contextId") or _string_arg(feign) or ""
                        base = self._attr(feign, "path") or ""
                        name = endpoint(mapped[0], f"{base}/{prefix}/{mapped[1]}")
                        facts.append(Fact(node_kind="endpoint", name=name, edge="calls_http", line=s.start, symbol=s.qualified, confidence="resolved",
                                          meta={"target": target, "url": self._attr(feign, "url") or ""}))  # fmt: skip
                    else:
                        facts.append(Fact(node_kind="endpoint", name=name, edge="exposes", line=s.start, symbol=s.qualified))
            for d in s.decorators:
                if d.startswith("KafkaListener"):
                    for topic in re.findall(r'"([^"]+)"', (re.search(r"topics?\s*=\s*(\{[^}]*\}|\"[^\"]+\")", d) or re.search(r"(.*)", d)).group(1)):  # type: ignore[union-attr]
                        facts.append(Fact(node_kind="topic", name=topic, edge="subscribes", line=s.start, symbol=s.qualified))
                elif d.startswith("RabbitListener"):
                    for queue in re.findall(r'"([^"]+)"', (re.search(r"queues?\s*=\s*(\{[^}]*\}|\"[^\"]+\")", d) or re.search(r"(.*)", d)).group(1)):  # type: ignore[union-attr]
                        facts.append(Fact(node_kind="topic", name=queue, edge="subscribes", line=s.start, symbol=s.qualified, meta={"broker": "rabbitmq"}))
                elif d.startswith("Table"):
                    table = self._attr(d, "name") or _string_arg(d)
                    if table:
                        facts.append(Fact(node_kind="table", name=table.lower(), edge="reads", line=s.start, symbol=s.qualified, meta={"entity": True}))
        constants = dict(re.findall(r'\b([A-Z][A-Z0-9_]+)\s*=\s*"([^"]+)"', file.text))
        for m in self.SEND.finditer(file.text):
            topic = m.group(1) or constants.get(m.group(2))
            line = _line_of(file.text, m.start())
            if topic:
                facts.append(Fact(node_kind="topic", name=topic, edge="publishes", line=line, symbol=_symbol_at(file, line),
                                  confidence="exact" if m.group(1) else "resolved"))  # fmt: skip
        for m in self.RABBIT_SEND.finditer(file.text):
            line = _line_of(file.text, m.start())
            facts.append(Fact(node_kind="topic", name=m.group(2) or m.group(1), edge="publishes", line=line, symbol=_symbol_at(file, line), meta={"broker": "rabbitmq"}))
        return facts


SQL_READ = re.compile(r"\b(?:from|join)\s+([A-Za-z_][\w.]*)", re.I)
SQL_WRITE = re.compile(r"\b(?:insert\s+into|update|delete\s+from|merge\s+into)\s+([A-Za-z_][\w.]*)", re.I)
SQL_WORDS = {"select", "where", "set", "values", "dual", "lateral", "unnest", "table"}


def sql_tables(sql: str) -> tuple[set[str], set[str]]:
    """(tables read, tables written) of one statement, found by pattern: enough for a graph, not a SQL parser."""
    sql = re.sub(r"<!\[CDATA\[|\]\]>|<[^>]+>|--[^\n]*|#\{[^}]*\}|\$\{[^}]*\}", " ", sql)
    writes = {t.split(".")[-1].lower() for t in SQL_WRITE.findall(sql)} - SQL_WORDS
    reads = {t.split(".")[-1].lower() for t in SQL_READ.findall(sql)} - SQL_WORDS - writes
    if re.match(r"\s*delete\s+from", sql, re.I):
        reads -= writes
    return reads, writes


class MyBatis:
    """Mapper XML (`<select id=…>`) and `@Select("…")` annotations → tables read and written. A statement belongs to
    the mapper interface method `namespace.id`, so the graph joins it to the Java method."""

    name = "mybatis"
    STATEMENT = re.compile(r"<(select|insert|update|delete)\b([^>]*)>(.*?)</\1>", re.S | re.I)
    ANNOTATION = re.compile(r"^(Select|Insert|Update|Delete)\b(.*)$", re.S)

    def applies(self, repo: RepoFacts) -> bool:
        return "java" in repo.languages or "xml" in repo.languages

    def extract(self, file: ParsedFile, repo: RepoFacts) -> list[Fact]:
        facts: list[Fact] = []
        if file.lang == "xml" and "<mapper" in file.text:
            ns = re.search(r"<mapper[^>]*\bnamespace\s*=\s*\"([^\"]+)\"", file.text)
            namespace = ns.group(1) if ns else ""
            for m in self.STATEMENT.finditer(file.text):
                sid = re.search(r"\bid\s*=\s*\"([^\"]+)\"", m.group(2))
                symbol = f"{namespace}.{sid.group(1)}" if namespace and sid else ""
                facts += self._facts(m.group(3), _line_of(file.text, m.start()), symbol)
        elif file.lang == "java":
            for s in file.symbols:
                for d in s.decorators:
                    m = self.ANNOTATION.match(d)
                    if m and '"' in m.group(2):
                        facts += self._facts(" ".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(2))), s.start, s.qualified)
        return facts

    @staticmethod
    def _facts(sql: str, line: int, symbol: str) -> list[Fact]:
        reads, writes = sql_tables(sql)
        return [Fact(node_kind="table", name=t, edge="reads", line=line, symbol=symbol, confidence="heuristic") for t in sorted(reads)] + [
            Fact(node_kind="table", name=t, edge="writes", line=line, symbol=symbol, confidence="heuristic") for t in sorted(writes)
        ]


class BuildDeps:
    """Libraries a build file names: pyproject.toml, package.json, pom.xml."""

    name = "build_deps"

    def applies(self, repo: RepoFacts) -> bool:
        return True

    def extract(self, file: ParsedFile, repo: RepoFacts) -> list[Fact]:
        base = file.path.rsplit("/", 1)[-1]
        names: list[tuple[str, int]] = []
        if base == "pyproject.toml":
            block = re.search(r"(?ms)^dependencies\s*=\s*\[(.*?)^\]", file.text)
            if block:
                names = [(re.split(r"[<>=!~\[; ]", n, maxsplit=1)[0], _line_of(file.text, block.start(1) + m.start())) for m in re.finditer(r'"([^"]+)"', block.group(1)) for n in [m.group(1)]]
        elif base == "package.json":
            try:
                data = json.loads(file.text)
            except ValueError:
                data = {}
            names = [(n, 1) for n in (data.get("dependencies") or {})] if isinstance(data, dict) else []
        elif base == "pom.xml":
            for m in re.finditer(r"<dependency>\s*<groupId>([^<]+)</groupId>\s*<artifactId>([^<]+)</artifactId>", file.text):
                names.append((f"{m.group(1).strip()}:{m.group(2).strip()}", _line_of(file.text, m.start())))
        return [Fact(node_kind="module", name=n, edge="depends_on", line=line, meta={"library": True}) for n, line in names if n]


BUILTIN: dict[str, type] = {c.name: c for c in (FastApi, HttpClient, Spring, MyBatis, BuildDeps)}


def load(names: list[str] | None = None) -> list[Extractor]:
    """The extractors to run: built in plus installed plugins, all of them or the ones named."""
    found: dict[str, type] = dict(BUILTIN)
    for ep in metadata.entry_points(group="actoroffice.extractors"):
        try:
            found[ep.name] = ep.load()
        except Exception:  # a broken plugin must not stop indexing
            continue
    wanted = names if names is not None else list(found)
    return [found[n]() for n in wanted if n in found]


def owner_symbol(symbols: list[ParsedSymbol], line: int) -> str:
    inside = [s for s in symbols if s.start <= line <= s.end]
    return max(inside, key=lambda s: s.start).qualified if inside else ""
