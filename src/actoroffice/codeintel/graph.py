"""Resolve what parsing found into graph edges: which symbol a call points to, which file an import names, which class
a base is, and the facts of the extractors.

There is no type information, so every edge says how it was found:
  exact      read from the syntax (an import of a file, a route literal)
  resolved   through an import, the same file or the same class
  heuristic  by name only (a unique name in the repository, `limitService` → class `LimitService`)
A call to something that is not defined in the repository (a builtin, a library) gives no edge.
"""

from __future__ import annotations

import json
import posixpath
import re
from collections import defaultdict
from typing import Any

from .store import IndexStore

SELF = {"self", "this", "cls", "super"}
CLASS_KINDS = {"class", "interface", "enum", "record"}
CALLABLE = {"function", "method", "class", "interface", "record", "enum"}
MAX_CANDIDATES = 3  # a name with more definitions than this says nothing about which one is called
JS_EXTENSIONS = ("", ".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.tsx", "/index.js")


def _norm(name: str) -> str:
    return re.sub(r"[_\W]", "", name).lower()


class _Symbols:
    def __init__(self, store: IndexStore, repo_id: int) -> None:
        self.by_qualified: dict[str, int] = {}
        self.by_name: dict[str, list[int]] = defaultdict(list)
        self.by_norm_class: dict[str, list[int]] = defaultdict(list)
        self.info: dict[int, Any] = {}
        self.members: dict[int, dict[str, int]] = defaultdict(dict)  # class id → member name → id
        self.file_node: dict[int, int] = {}
        self.file_module: dict[int, str] = {}
        self.module_file: dict[str, int] = {}  # module name → file id
        self.path_file: dict[str, int] = {}
        self.file_lang: dict[int, str] = {}
        for f in store.all("SELECT id, path, lang FROM files WHERE repo_id = ?", (repo_id,)):
            self.path_file[f["path"]] = int(f["id"])
            self.file_lang[int(f["id"])] = f["lang"]
        for n in store.all("SELECT * FROM nodes WHERE repo_id = ? AND kind IN ('file', 'symbol') ORDER BY id", (repo_id,)):
            nid = int(n["id"])
            self.info[nid] = n
            if n["kind"] == "file":
                self.file_node[int(n["file_id"])] = nid
                module = json.loads(n["meta_json"] or "{}").get("module", "")
                self.file_module[int(n["file_id"])] = module
                if module:
                    self.module_file.setdefault(module, int(n["file_id"]))
                continue
            self.by_qualified.setdefault(n["qualified"], nid)
            self.by_name[n["name"]].append(nid)
            if n["sub_kind"] in CLASS_KINDS:
                self.by_norm_class[_norm(n["name"])].append(nid)
            parent = n["parent"]
            if parent is not None and parent in self.info and self.info[parent]["kind"] == "symbol":
                self.members[int(parent)].setdefault(n["name"], nid)

    def owner_class(self, node_id: int | None) -> int | None:
        while node_id is not None and node_id in self.info:
            n = self.info[node_id]
            if n["kind"] == "symbol" and n["sub_kind"] in CLASS_KINDS:
                return node_id
            node_id = n["parent"]
        return None

    def unique(self, name: str, kinds: set[str]) -> list[int]:
        found = [i for i in self.by_name.get(name, []) if self.info[i]["sub_kind"] in kinds]
        return found if 0 < len(found) <= MAX_CANDIDATES else []


def _import_table(store: IndexStore, repo_id: int, sym: _Symbols) -> tuple[dict[int, dict[str, str]], list[tuple[int, int, int]]]:
    """Per file: local name → qualified name (of a symbol or a module) it was imported as. And the file → file edges."""
    names: dict[int, dict[str, str]] = defaultdict(dict)
    file_edges: list[tuple[int, int, int]] = []
    for r in store.all("SELECT * FROM raw_imports WHERE repo_id = ?", (repo_id,)):
        file_id = int(r["file_id"])
        lang = sym.file_lang.get(file_id, "")
        module = r["module"]
        imported = json.loads(r["names_json"])
        alias = json.loads(r["alias_json"])
        if lang == "python":
            module = _py_absolute(module, sym.file_module.get(file_id, ""), _is_init(sym, file_id))
        elif lang in ("javascript", "typescript", "tsx"):
            module = _js_module(module, file_id, sym)
        target = sym.module_file.get(module)
        if target is not None and target != file_id:
            file_edges.append((file_id, target, int(r["line"])))
        table = names[file_id]
        if not imported:
            local = module.rsplit(".", 1)[-1] if lang == "python" else module
            table.setdefault(local, module)
            if lang == "python":
                table.setdefault(module, module)
        for name in imported:
            sep = "." if module else ""
            table[name] = f"{module}{sep}{name}"
            if lang == "python" and f"{module}.{name}" in sym.module_file:  # `from pkg import module`
                sub = sym.module_file[f"{module}.{name}"]
                if sub != file_id:
                    file_edges.append((file_id, sub, int(r["line"])))
        for local, real in alias.items():
            table[local] = f"{module}.{real}" if real in imported and module else real
    return names, file_edges


def _is_init(sym: _Symbols, file_id: int) -> bool:
    node = sym.info.get(sym.file_node.get(file_id, -1))
    return bool(node is not None and node["name"] == "__init__.py")


def _py_absolute(module: str, current: str, is_package: bool) -> str:
    if not module.startswith("."):
        return module
    dots = len(module) - len(module.lstrip("."))
    base = current.split(".") if current else []
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (dots - 1)] if dots > 1 else base
    rest = module.lstrip(".")
    return ".".join([*base, rest] if rest else base)


def _js_module(module: str, file_id: int, sym: _Symbols) -> str:
    if not module.startswith("."):
        return module
    node = sym.info.get(sym.file_node.get(file_id, -1))
    here = posixpath.dirname(node["qualified"]) if node is not None else ""
    joined = posixpath.normpath(posixpath.join(here, module))
    for ext in JS_EXTENSIONS:
        if joined + ext in sym.path_file:
            return (joined + ext).rsplit(".", 1)[0] if ext else joined
    return joined


def _resolve_call(name: str, caller: int | None, file_id: int, sym: _Symbols, imports: dict[str, str]) -> list[tuple[int, str]]:
    parts = name.split(".")
    last = parts[-1]
    module = sym.file_module.get(file_id, "")
    if len(parts) == 1:
        cls = sym.owner_class(caller)
        if cls is not None and last in sym.members[cls] and sym.file_lang.get(file_id) == "java":
            return [(sym.members[cls][last], "resolved")]  # an unqualified call inside a Java class is a method of it
        for qualified in (f"{module}.{last}", imports.get(last, "")):
            if qualified in sym.by_qualified:
                return [(sym.by_qualified[qualified], "resolved")]
        return [(i, "heuristic") for i in sym.unique(last, CALLABLE)]
    head = parts[0]
    if head in SELF:
        cls = sym.owner_class(caller)
        if cls is not None and len(parts) == 2 and last in sym.members[cls]:
            return [(sym.members[cls][last], "resolved")]
        if len(parts) == 2:
            return [(i, "heuristic") for i in sym.unique(last, {"method"})]
        head, parts = parts[1], parts[1:]  # self.service.check → service.check
    # an imported module or class, then its member: stage.default_templates(), LimitService.check()
    target = imports.get(head) or (f"{module}.{head}" if f"{module}.{head}" in sym.by_qualified else "")
    if target:
        qualified = ".".join([target, *parts[1:]])
        if qualified in sym.by_qualified:
            return [(sym.by_qualified[qualified], "resolved")]
        owner = sym.by_qualified.get(target)
        if owner is not None and last in sym.members[owner]:
            return [(sym.members[owner][last], "resolved")]
    # a variable named after a class: limitService.check(), limit_service.check()
    owner_name = parts[-2] if len(parts) >= 2 else head
    classes = sym.by_norm_class.get(_norm(owner_name), [])
    hits = [sym.members[c][last] for c in classes if last in sym.members[c]]
    if 0 < len(hits) <= MAX_CANDIDATES:
        return [(i, "heuristic") for i in hits]
    # an attribute named after the start of a class name: office.sources.sync() → SourceService.sync
    stem = _norm(owner_name).rstrip("s")
    if len(stem) >= 4:
        hits = [sym.members[c][last] for norm, ids in sym.by_norm_class.items() if norm.startswith(stem) for c in ids if last in sym.members[c]]
        if 0 < len(hits) <= MAX_CANDIDATES:
            return [(i, "heuristic") for i in hits]
    return [(i, "heuristic") for i in sym.unique(last, {"method"})] if len(sym.by_name.get(last, [])) == 1 else []


def resolve(store: IndexStore, repo_id: int) -> None:
    """Rebuild every edge and every fact node of the repository (inside the caller's transaction)."""
    store.run("DELETE FROM edges WHERE repo_id = ?", (repo_id,))
    store.run("DELETE FROM nodes WHERE repo_id = ? AND kind NOT IN ('file', 'symbol')", (repo_id,))
    sym = _Symbols(store, repo_id)
    imports, file_edges = _import_table(store, repo_id, sym)

    def add(src: int, dst: int, kind: str, confidence: str, file_id: int | None, line: int | None, source: str, meta: dict[str, Any] | None = None) -> None:
        store.run(
            "INSERT INTO edges(repo_id, src, dst, kind, confidence, file_id, line, source, meta_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (repo_id, src, dst, kind, confidence, file_id, line, source, json.dumps(meta or {}, ensure_ascii=False)),
        )

    seen: set[tuple[int, int]] = set()
    for src_file, dst_file, line in file_edges:
        if (src_file, dst_file) not in seen and src_file in sym.file_node and dst_file in sym.file_node:
            seen.add((src_file, dst_file))
            add(sym.file_node[src_file], sym.file_node[dst_file], "imports", "exact", src_file, line, "parse")

    done: set[tuple[int, int]] = set()
    for r in store.all("SELECT * FROM raw_calls WHERE repo_id = ?", (repo_id,)):
        file_id = int(r["file_id"])
        caller = sym.by_qualified.get(r["caller"]) if r["caller"] else None
        src = caller if caller is not None else sym.file_node.get(file_id)
        if src is None:
            continue
        for dst, confidence in _resolve_call(r["name"], caller, file_id, sym, imports.get(file_id, {})):
            if dst != src and (src, dst) not in done:
                done.add((src, dst))
                add(src, dst, "calls", confidence, file_id, int(r["line"]), "parse")

    for r in store.all("SELECT * FROM raw_bases WHERE repo_id = ?", (repo_id,)):
        file_id = int(r["file_id"])
        src = sym.by_qualified.get(r["symbol"])
        if src is None:
            continue
        base = r["base"]
        table = imports.get(file_id, {})
        module = sym.file_module.get(file_id, "")
        qualified = next((q for q in (table.get(base, ""), f"{module}.{base}", ".".join([table.get(base.split(".")[0], ""), *base.split(".")[1:]])) if q in sym.by_qualified), "")
        targets = [(sym.by_qualified[qualified], "resolved")] if qualified else [(i, "heuristic") for i in sym.unique(base.split(".")[-1], CLASS_KINDS)]
        for dst, confidence in targets:
            if dst != src:
                add(src, dst, "extends", confidence, file_id, int(r["line"]), "parse")

    fact_nodes: dict[tuple[str, str], int] = {}
    for r in store.all("SELECT * FROM raw_facts WHERE repo_id = ?", (repo_id,)):
        fact = json.loads(r["fact_json"])
        file_id = int(r["file_id"])
        key = (fact["node_kind"], fact["name"])
        if key not in fact_nodes:
            cur = store.run(
                "INSERT INTO nodes(repo_id, kind, name, qualified, meta_json) VALUES (?, ?, ?, ?, ?)",
                (repo_id, key[0], key[1], key[1], json.dumps({k: v for k, v in (fact.get("meta") or {}).items() if k in ("library", "broker")})),
            )
            fact_nodes[key] = int(cur.lastrowid or 0)
        src = sym.by_qualified.get(fact.get("symbol") or "") or sym.file_node.get(file_id)
        if src is not None:
            add(src, fact_nodes[key], fact["edge"], fact.get("confidence", "exact"), file_id, int(fact.get("line") or 0), r["extractor"], fact.get("meta"))
