"""The indexing pipeline: source at a commit → manifest → text chunks → symbols → raw graph facts → resolved graph.

Only files whose blob changed since the last pass are read and parsed again. The graph edges are resolved again for
the whole repository after every pass (cheap: names and imports are already in the raw tables).
"""

from __future__ import annotations

import fnmatch
import json
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from actoroffice.ports.intel import Extractor, IndexState, ParsedFile, RepoFacts

from . import extractors as extractor_registry
from . import graph, parse
from .store import IndexStore
from .sync import Source, SourceFile, SyncError

LEVELS = ("text", "symbols", "graph")
CHUNK_LINES = 60
BIG_SYMBOL_LINES = 120
SKIP_PARTS = {
    "node_modules", "vendor", "dist", "build", "target", "out", ".git", "__pycache__", ".venv", "venv", ".idea", ".gradle",
    "generated", ".next", ".nuxt", "coverage", ".dart_tool", "bin", "obj",
}  # fmt: skip
SKIP_NAMES = re.compile(r"(?i)(\.min\.(js|css)|\.lock|-lock\.json|\.map|\.snap|\.pb\.go|_pb2\.py)$|^(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|poetry\.lock)$")
# Never indexed, whatever the size: they may hold secrets
SECRET_NAMES = re.compile(r"(?i)(^\.env(\..+)?$|\.pem$|\.key$|\.p12$|\.jks$|\.keystore$|credentials|secret|id_rsa|id_ed25519)")
TEST_PATH = re.compile(r"(?i)(^|/)(tests?|__tests__|spec|specs|testdata|fixtures)(/|$)|(^|/)test_[^/]+$|[^/]+(_test|\.test|\.spec|Test|Tests|IT)\.[A-Za-z]+$")
DOC_LANGS = {"markdown"}
CONFIG_LANGS = {"yaml", "json", "toml", "properties", "xml", "gradle", "dockerfile"}
MARKERS = ("pom.xml", "build.gradle", "build.gradle.kts", "package.json", "pyproject.toml", "Dockerfile", "docker-compose.yml", "docker-compose.yaml", "go.mod", "pubspec.yaml")


@dataclass
class IndexOptions:
    level: str = "graph"
    max_file_kb: int = 512
    ignore: list[str] = field(default_factory=list)  # glob patterns on the path
    extractors: list[str] | None = None  # None = every extractor installed


def classify(path: str, lang: str) -> str:
    """source | test | config | doc | skip."""
    parts = path.split("/")
    name = parts[-1]
    if SKIP_PARTS & set(parts[:-1]) or SKIP_NAMES.search(name) or SECRET_NAMES.search(name):
        return "skip"
    if not lang:
        return "skip"
    if TEST_PATH.search(path):
        return "test"
    if lang in DOC_LANGS:
        return "doc"
    if lang in CONFIG_LANGS:
        return "config"
    return "source"


def chunks_of(parsed: ParsedFile) -> list[tuple[int, int, str]]:
    """(start, end, symbol) spans that cover the file: one per function or method, the rest in runs of CHUNK_LINES.
    A chunk never cuts a short function in two, so a hit comes back as a readable unit."""
    lines = parsed.text.count("\n") + 1
    spans: list[tuple[int, int, str]] = []
    leaves = [s for s in parsed.symbols if s.kind in ("function", "method")]
    covered = [False] * (lines + 2)
    for s in leaves:
        if any(covered[s.start : s.end + 1]):  # a nested function: its parent already covers it
            continue
        for start in range(s.start, s.end + 1, BIG_SYMBOL_LINES):
            spans.append((start, min(start + BIG_SYMBOL_LINES - 1, s.end), s.qualified))
        for i in range(s.start, min(s.end, lines) + 1):
            covered[i] = True
    run_start: int | None = None
    for i in range(1, lines + 2):
        free = i <= lines and not covered[i]
        if free and run_start is None:
            run_start = i
        if run_start is not None and (not free or i - run_start >= CHUNK_LINES):
            end = i - 1 if not free else i
            spans.append((run_start, end, _owner(parsed, run_start)))
            run_start = None if not free else (i + 1 if i < lines else None)
    return sorted(s for s in spans if s[0] <= s[1])


def _owner(parsed: ParsedFile, line: int) -> str:
    inside = [s for s in parsed.symbols if s.start <= line <= s.end and s.kind != "constant"]
    return max(inside, key=lambda s: s.start).qualified if inside else ""


def index_repo(store: IndexStore, slug: str, source: Source, options: IndexOptions | None = None, **repo_fields: object) -> IndexState:
    """Bring the index of one repository to the source's commit. Returns its state; a failure is recorded on the
    repository row and returned, never raised (one broken repository must not stop the others)."""
    opts = options or IndexOptions()
    level = opts.level if opts.level in LEVELS else "graph"
    if level != "text" and not parse.available():
        level = "text"  # tree-sitter is not installed
    started = time.monotonic()
    repo_id = store.ensure_repo(slug, **repo_fields)
    try:
        commit = source.commit()
        listed = source.files()
    except SyncError as exc:
        store.update_repo(repo_id, state="error", error=str(exc), updated_at=_now())
        return state_of(store, slug)
    before = store.repo(slug)
    assert before is not None
    relevel = before["level"] != level  # another level: every file is derived again
    store.update_repo(repo_id, state="indexing", head_commit=commit, error=None)

    wanted: dict[str, tuple[SourceFile, str, str]] = {}
    for f in listed:
        lang = parse.language_of(f.path)
        kind = classify(f.path, lang)
        if kind == "skip" or f.size > opts.max_file_kb * 1024 or any(fnmatch.fnmatch(f.path, pat) for pat in opts.ignore):
            continue
        wanted[f.path] = (f, lang, kind)
    known = {r["path"]: r for r in store.all("SELECT id, path, blob_sha FROM files WHERE repo_id = ?", (repo_id,))}
    removed = [r for path, r in known.items() if path not in wanted]
    changed = [v for path, v in wanted.items() if relevel or path not in known or known[path]["blob_sha"] != v[0].blob_sha]

    active: list[Extractor] = extractor_registry.load(opts.extractors) if level == "graph" else []
    facts = RepoFacts(
        slug=slug,
        files=sorted(wanted),
        languages=sorted({lang for _, lang, _ in wanted.values()}),
        markers=sorted({p for p in wanted if p.rsplit("/", 1)[-1] in MARKERS}),
    )
    active = [e for e in active if e.applies(facts)]
    errors: list[str] = []
    store.begin()
    try:
        for row in removed:
            store.drop_file(int(row["id"]))
            store.run("DELETE FROM files WHERE id = ?", (row["id"],))
        by_path = {v[0].path: v for v in changed}
        for f, data in source.read_many([v[0] for v in changed]):
            _, lang, kind = by_path[f.path]
            old = known.get(f.path)
            if old is not None:
                store.drop_file(int(old["id"]))
                store.run("DELETE FROM files WHERE id = ?", (old["id"],))
            if b"\0" in data[:8192]:
                continue  # binary
            text = data.decode("utf-8", "replace")
            try:
                parsed = parse.parse(f.path, text, lang) if level != "text" else ParsedFile(path=f.path, lang=lang, text=text)
            except Exception as exc:  # one file the grammar chokes on: keep its text, go on
                errors.append(f"{f.path}: {type(exc).__name__}: {exc}"[:200])
                parsed = ParsedFile(path=f.path, lang=lang, text=text)
            _write_file(store, repo_id, f, kind, parsed, active, errors, facts)
        store.commit()
    except BaseException as exc:
        store.rollback()
        store.update_repo(repo_id, state="error", error=f"{type(exc).__name__}: {exc}"[:300], updated_at=_now())
        if isinstance(exc, Exception):
            return state_of(store, slug)
        raise

    if level == "graph":
        store.begin()
        try:
            graph.resolve(store, repo_id)
            store.commit()
        except Exception as exc:
            store.rollback()
            errors.append(f"graph: {type(exc).__name__}: {exc}"[:200])
    stats = {
        "files": int(store.one("SELECT count(*) FROM files WHERE repo_id = ?", (repo_id,))[0]),  # type: ignore[index]
        "symbols": int(store.one("SELECT count(*) FROM nodes WHERE repo_id = ? AND kind = 'symbol'", (repo_id,))[0]),  # type: ignore[index]
        "edges": int(store.one("SELECT count(*) FROM edges WHERE repo_id = ?", (repo_id,))[0]),  # type: ignore[index]
        "changed": len(changed),
        "removed": len(removed),
        "took_s": round(time.monotonic() - started, 2),
        "errors": errors[:20],
        "extractors": [e.name for e in active],
    }
    store.update_repo(repo_id, state="ready", level=level, indexed_commit=commit, stats=stats, updated_at=_now())
    return state_of(store, slug)


def _write_file(
    store: IndexStore, repo_id: int, f: SourceFile, kind: str, parsed: ParsedFile, active: list[Extractor], errors: list[str], facts: RepoFacts
) -> None:
    text = parsed.text
    lines = text.split("\n")
    cur = store.run(
        "INSERT INTO files(repo_id, path, blob_sha, lang, kind, size, lines) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (repo_id, f.path, f.blob_sha, parsed.lang, kind, f.size, len(lines)),
    )
    file_id = int(cur.lastrowid or 0)
    for start, end, symbol in chunks_of(parsed):
        body = "\n".join(lines[start - 1 : end])
        if body.strip():
            store.run(
                "INSERT INTO chunks(text, repo_id, file_id, path, start_line, end_line, symbol) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (body, repo_id, file_id, f.path, start, end, symbol),
            )
    file_node = store.run(
        "INSERT INTO nodes(repo_id, kind, name, qualified, file_id, start_line, end_line, meta_json) VALUES (?, 'file', ?, ?, ?, 1, ?, ?)",
        (repo_id, f.path.rsplit("/", 1)[-1], f.path, file_id, len(lines), json.dumps({"module": parse.module_name(f.path, parsed.lang, text), "kind": kind})),
    ).lastrowid
    ids: dict[str, int] = {}
    for s in parsed.symbols:
        cur = store.run(
            "INSERT INTO nodes(repo_id, kind, sub_kind, name, qualified, file_id, start_line, end_line, signature, doc, parent, meta_json) "
            "VALUES (?, 'symbol', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_id, s.kind, s.name, s.qualified, file_id, s.start, s.end, s.signature, s.doc, ids.get(s.parent, file_node),
             json.dumps({"decorators": s.decorators} if s.decorators else {}, ensure_ascii=False)),
        )  # fmt: skip
        ids.setdefault(s.qualified, int(cur.lastrowid or 0))
        for base in s.bases:
            store.run("INSERT INTO raw_bases(file_id, repo_id, symbol, base, line) VALUES (?, ?, ?, ?, ?)", (file_id, repo_id, s.qualified, base, s.start))
    for c in parsed.calls:
        store.run("INSERT INTO raw_calls(file_id, repo_id, caller, name, line) VALUES (?, ?, ?, ?, ?)", (file_id, repo_id, c.caller, c.name, c.line))
    for imp in parsed.imports:
        store.run(
            "INSERT INTO raw_imports(file_id, repo_id, module, names_json, alias_json, line) VALUES (?, ?, ?, ?, ?, ?)",
            (file_id, repo_id, imp.module, json.dumps(imp.names), json.dumps(imp.alias), imp.line),
        )
    for extractor in active:
        try:
            found = extractor.extract(parsed, facts)
        except Exception as exc:  # a broken extractor loses its facts for this file only
            errors.append(f"{extractor.name} {f.path}: {type(exc).__name__}: {exc}"[:200])
            continue
        for fact in found:
            store.run(
                "INSERT INTO raw_facts(file_id, repo_id, extractor, fact_json) VALUES (?, ?, ?, ?)",
                (file_id, repo_id, extractor.name, fact.model_dump_json()),
            )


def state_of(store: IndexStore, slug: str) -> IndexState:
    row = store.repo(slug)
    if row is None:
        return IndexState(repo=slug)
    stats = json.loads(row["stats_json"] or "{}")
    state = row["state"]
    if state == "ready" and row["head_commit"] and row["indexed_commit"] != row["head_commit"]:
        state = "stale"
    return IndexState(
        repo=slug, indexed_commit=row["indexed_commit"], head_commit=row["head_commit"], state=state, level=row["level"],
        files=int(stats.get("files", 0)), symbols=int(stats.get("symbols", 0)), edges=int(stats.get("edges", 0)),
        took_s=float(stats.get("took_s", 0.0)), error=row["error"],
    )  # fmt: skip


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
