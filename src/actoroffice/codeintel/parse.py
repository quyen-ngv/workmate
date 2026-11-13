"""Source file → symbols, calls, imports (tree-sitter). No type information: what a call points to is decided later
(graph.py) from imports and names.

A language is one entry in LANGS: which node types are definitions, calls and imports. Adding a language means adding
its grammar package and an entry here; nothing else changes. Without tree-sitter installed `available()` is False and
the index stays at the text level.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any

from actoroffice.ports.intel import ParsedCall, ParsedFile, ParsedImport, ParsedSymbol

SIGNATURE_LIMIT = 240
DOC_LIMIT = 300

EXTENSIONS = {
    ".py": "python", ".java": "java", ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".kt": "kotlin", ".go": "go", ".cs": "csharp", ".dart": "dart", ".rb": "ruby",
    ".rs": "rust", ".php": "php", ".sql": "sql", ".xml": "xml", ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".toml": "toml", ".md": "markdown", ".html": "html", ".css": "css", ".sh": "shell", ".gradle": "gradle",
    ".properties": "properties", ".scss": "css", ".vue": "vue",
}  # fmt: skip


def language_of(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    if name == "Dockerfile" or name.startswith("Dockerfile."):
        return "dockerfile"
    dot = name.rfind(".")
    return EXTENSIONS.get(name[dot:].lower(), "") if dot >= 0 else ""


@dataclass(frozen=True)
class Lang:
    grammar: Callable[[], Any]  # returns the tree-sitter language capsule
    classes: dict[str, str]  # node type → symbol kind
    functions: dict[str, str]
    calls: dict[str, str]  # node type → field that holds the called thing
    imports: Callable[[Any, bytes], list[ParsedImport]]
    separator: str = "."
    body_field: str = "body"
    extra: Callable[[Any, bytes, _Walk], bool] | None = None  # language-specific nodes; True = handled, do not descend


@dataclass
class _Walk:
    lang: Lang
    src: bytes
    module: str
    symbols: list[ParsedSymbol] = field(default_factory=list)
    calls: list[ParsedCall] = field(default_factory=list)

    def qualify(self, parent: str, name: str) -> str:
        return f"{parent or self.module}{self.lang.separator}{name}" if (parent or self.module) else name


def _text(node: Any, src: bytes) -> str:
    return src[node.start_byte : node.end_byte].decode("utf-8", "replace")


def _one_line(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _signature(node: Any, src: bytes, body_field: str) -> str:
    body = node.child_by_field_name(body_field)
    end = body.start_byte if body is not None else node.end_byte
    head = src[node.start_byte : end].decode("utf-8", "replace").rstrip("{: \n\t")
    head = re.sub(r"^(\s*@[\w.]+(\((?:[^()]|\([^()]*\))*\))?\s*)+", "", head)  # annotations are kept in `decorators`
    return _one_line(head, SIGNATURE_LIMIT)


def _name(node: Any, src: bytes) -> str:
    name = node.child_by_field_name("name")
    return _text(name, src) if name is not None else ""


def _visit(node: Any, w: _Walk, parent: str, decorators: list[str]) -> None:
    lang, src = w.lang, w.src
    kind = lang.classes.get(node.type) or lang.functions.get(node.type)
    if lang.extra is not None and kind is None and lang.extra(node, src, w):
        return
    if node.type == "decorated_definition":  # python: decorators sit on a wrapper node
        found = [_text(c, src).lstrip("@").strip() for c in node.children if c.type == "decorator"]
        inner = node.child_by_field_name("definition")
        if inner is not None:
            _visit(inner, w, parent, found)
        return
    if kind is not None:
        name = _name(node, src)
        if name:
            if kind == "function" and parent and any(s.qualified == parent and s.kind in CLASS_KINDS for s in w.symbols):
                kind = "method"
            qualified = w.qualify(parent, name)
            w.symbols.append(
                ParsedSymbol(
                    kind=kind, name=name, qualified=qualified, start=node.start_point[0] + 1, end=node.end_point[0] + 1,
                    signature=_signature(node, src, lang.body_field), doc=_doc(node, src, lang), parent=parent,
                    decorators=decorators + _annotations(node, src), bases=_bases(node, src),
                )
            )  # fmt: skip
            for child in node.children:
                _visit(child, w, qualified, [])
            return
    field_name = lang.calls.get(node.type)
    if field_name is not None:
        target = node.child_by_field_name(field_name)
        if target is not None:
            called = _text(target, src)
            if node.type == "method_invocation":  # java: object.name(…)
                obj = node.child_by_field_name("object")
                called = f"{_text(obj, src)}.{called}" if obj is not None else called
            called = _one_line(called, 120)
            if called and re.match(r"^[\w.$#?!]+$", called):
                w.calls.append(ParsedCall(caller=parent, name=called.replace("?.", ".").replace("!", ""), line=node.start_point[0] + 1))
    for child in node.children:
        _visit(child, w, parent, [])


CLASS_KINDS = {"class", "interface", "enum", "record"}


def _doc(node: Any, src: bytes, lang: Lang) -> str:
    body = node.child_by_field_name(lang.body_field)
    if body is not None and body.named_child_count:  # python docstring: the first statement is a string
        first = body.named_children[0]
        if first.type == "expression_statement" and first.named_child_count and first.named_children[0].type == "string":
            return _one_line(_text(first.named_children[0], src).strip("\"' \n"), DOC_LIMIT)
    prev = node.prev_named_sibling
    if node.parent is not None and node.parent.type == "decorated_definition":
        prev = node.parent.prev_named_sibling
    if prev is not None and prev.type in ("comment", "block_comment", "line_comment") and prev.end_point[0] >= node.start_point[0] - 1:
        return _one_line(re.sub(r"^[/*#\s]+|[*/\s]+$", "", _text(prev, src)).replace("\n *", " "), DOC_LIMIT)
    return ""


def _annotations(node: Any, src: bytes) -> list[str]:
    """Java annotations (in `modifiers`) and TypeScript decorators (children of the definition)."""
    out: list[str] = []
    for child in node.children:
        if child.type == "modifiers":
            out += [_one_line(_text(c, src).lstrip("@"), 200) for c in child.children if c.type in ("marker_annotation", "annotation")]
        elif child.type == "decorator":
            out.append(_one_line(_text(child, src).lstrip("@"), 200))
    return out


def _bases(node: Any, src: bytes) -> list[str]:
    out: list[str] = []
    for name in ("superclasses", "superclass", "interfaces"):
        part = node.child_by_field_name(name)
        if part is not None:
            out += re.findall(r"[A-Za-z_][\w.]*", re.sub(r"<[^>]*>", "", _text(part, src)))
    for child in node.children:
        if child.type in ("class_heritage", "extends_type_clause", "extends_interfaces", "super_interfaces"):
            out += re.findall(r"[A-Za-z_][\w.]*", re.sub(r"<[^>]*>", "", _text(child, src)))
    return [b for b in dict.fromkeys(out) if b not in ("extends", "implements", "object", "metaclass")]


# ---- python ------------------------------------------------------------------------------------------------------


def _py_imports(root: Any, src: bytes) -> list[ParsedImport]:
    out: list[ParsedImport] = []

    def walk(node: Any) -> None:
        if node.type == "import_statement":
            for child in node.named_children:
                if child.type == "dotted_name":
                    out.append(ParsedImport(module=_text(child, src), line=node.start_point[0] + 1))
                elif child.type == "aliased_import":
                    module = _text(child.child_by_field_name("name"), src)
                    out.append(ParsedImport(module=module, alias={_text(child.child_by_field_name("alias"), src): module}, line=node.start_point[0] + 1))
        elif node.type == "import_from_statement":
            module_node = node.child_by_field_name("module_name")
            module = _text(module_node, src) if module_node is not None else ""
            names: list[str] = []
            alias: dict[str, str] = {}
            for child in node.named_children:
                if module_node is not None and child.start_byte == module_node.start_byte:
                    continue
                if child.type == "dotted_name":
                    names.append(_text(child, src))
                elif child.type == "aliased_import":
                    real = _text(child.child_by_field_name("name"), src)
                    names.append(real)
                    alias[_text(child.child_by_field_name("alias"), src)] = real
            out.append(ParsedImport(module=module, names=names, alias=alias, line=node.start_point[0] + 1))
        elif node.type in ("function_definition", "class_definition"):
            pass  # imports inside functions still count
        for child in node.children:
            walk(child)

    walk(root)
    return out


def _py_extra(node: Any, src: bytes, w: _Walk) -> bool:
    """Module-level constants (`NAME = …`), so that settings and tables are found by name."""
    if node.type == "expression_statement" and node.parent is not None and node.parent.type == "module" and node.named_child_count:
        first = node.named_children[0]
        if first.type == "assignment":
            left = first.child_by_field_name("left")
            if left is not None and left.type == "identifier":
                name = _text(left, src)
                if name.isupper() and len(name) > 2:
                    w.symbols.append(
                        ParsedSymbol(kind="constant", name=name, qualified=w.qualify("", name), start=node.start_point[0] + 1,
                                     end=node.end_point[0] + 1, signature=_one_line(_text(node, src), 120))
                    )  # fmt: skip
    return False


# ---- java --------------------------------------------------------------------------------------------------------


def _java_imports(root: Any, src: bytes) -> list[ParsedImport]:
    out: list[ParsedImport] = []
    for node in root.children:
        if node.type == "import_declaration":
            path = re.sub(r"^import\s+(static\s+)?|;$", "", _text(node, src)).strip()
            module, _, name = path.rpartition(".")
            out.append(ParsedImport(module=module, names=[] if name == "*" else [name], line=node.start_point[0] + 1))
    return out


def _java_extra(node: Any, src: bytes, w: _Walk) -> bool:
    if node.type == "object_creation_expression":
        made = node.child_by_field_name("type")
        if made is not None:
            caller = _enclosing(node, w)
            w.calls.append(ParsedCall(caller=caller, name=re.sub(r"<.*", "", _text(made, src)), line=node.start_point[0] + 1))
    return False


def _enclosing(node: Any, w: _Walk) -> str:
    line = node.start_point[0] + 1
    inside = [s for s in w.symbols if s.start <= line <= s.end]
    return max(inside, key=lambda s: s.start).qualified if inside else ""


def java_package(text: str) -> str:
    m = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text)
    return m.group(1) if m else ""


# ---- javascript / typescript --------------------------------------------------------------------------------------


def _js_imports(root: Any, src: bytes) -> list[ParsedImport]:
    out: list[ParsedImport] = []

    def walk(node: Any) -> None:
        if node.type == "import_statement":
            source = node.child_by_field_name("source")
            module = _text(source, src).strip("'\"`") if source is not None else ""
            names: list[str] = []
            alias: dict[str, str] = {}
            for spec in _descend(node, {"import_specifier", "namespace_import", "identifier"}):
                if spec.type == "import_specifier":
                    real = _text(spec.child_by_field_name("name"), src)
                    names.append(real)
                    local = spec.child_by_field_name("alias")
                    if local is not None:
                        alias[_text(local, src)] = real
                elif spec.type == "identifier" and spec.parent is not None and spec.parent.type == "import_clause":
                    names.append(_text(spec, src))  # default import
                elif spec.type == "namespace_import":
                    alias[_text(spec.named_children[-1], src)] = module
            out.append(ParsedImport(module=module, names=names, alias=alias, line=node.start_point[0] + 1))
            return
        for child in node.children:
            walk(child)

    walk(root)
    return out


def _descend(node: Any, types: set[str]) -> list[Any]:
    found: list[Any] = []
    for child in node.children:
        if child.type in types:
            found.append(child)
        if child.type != "string":
            found += _descend(child, types)
    return found


def _js_extra(node: Any, src: bytes, w: _Walk) -> bool:
    """`const name = (…) => …` and `const name = function…` are functions."""
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        name = node.child_by_field_name("name")
        if value is not None and name is not None and name.type == "identifier" and value.type in ("arrow_function", "function_expression", "function"):
            parent = _enclosing(node, w)
            qualified = w.qualify(parent, _text(name, src))
            w.symbols.append(
                ParsedSymbol(kind="function", name=_text(name, src), qualified=qualified, start=node.start_point[0] + 1, end=node.end_point[0] + 1,
                             signature=_signature(value, src, "body") and _one_line(f"{_text(name, src)} = {_signature(value, src, 'body')}", SIGNATURE_LIMIT),
                             parent=parent)
            )  # fmt: skip
            for child in value.children:
                _visit(child, w, qualified, [])
            return True
    return False


def _grammar(module: str, attr: str = "language") -> Callable[[], Any]:
    def load() -> Any:
        import importlib

        return getattr(importlib.import_module(module), attr)()

    return load


_JS_CLASSES = {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum", "abstract_class_declaration": "class"}
_JS_FUNCTIONS = {"function_declaration": "function", "method_definition": "method", "generator_function_declaration": "function", "type_alias_declaration": "type"}
_JS_CALLS = {"call_expression": "function", "new_expression": "constructor"}

LANGS: dict[str, Lang] = {
    "python": Lang(
        _grammar("tree_sitter_python"), {"class_definition": "class"}, {"function_definition": "function"}, {"call": "function"}, _py_imports, extra=_py_extra
    ),
    "java": Lang(
        _grammar("tree_sitter_java"),
        {"class_declaration": "class", "interface_declaration": "interface", "enum_declaration": "enum", "record_declaration": "record", "annotation_type_declaration": "interface"},
        {"method_declaration": "method", "constructor_declaration": "method"},
        {"method_invocation": "name"},
        _java_imports,
        extra=_java_extra,
    ),
    "javascript": Lang(_grammar("tree_sitter_javascript"), _JS_CLASSES, _JS_FUNCTIONS, _JS_CALLS, _js_imports, extra=_js_extra),
    "typescript": Lang(_grammar("tree_sitter_typescript", "language_typescript"), _JS_CLASSES, _JS_FUNCTIONS, _JS_CALLS, _js_imports, extra=_js_extra),
    "tsx": Lang(_grammar("tree_sitter_typescript", "language_tsx"), _JS_CLASSES, _JS_FUNCTIONS, _JS_CALLS, _js_imports, extra=_js_extra),
}


@cache
def _parser(lang: str) -> Any:
    from tree_sitter import Language, Parser

    return Parser(Language(LANGS[lang].grammar()))


@cache
def available(lang: str | None = None) -> bool:
    """True when tree-sitter (and the grammar of `lang`) can be loaded."""
    try:
        import tree_sitter  # noqa: F401
    except ImportError:
        return False
    if lang is None:
        return True
    if lang not in LANGS:
        return False
    try:
        _parser(lang)
    except Exception:
        return False
    return True


def module_name(path: str, lang: str, text: str = "") -> str:
    """The name symbols of a file are qualified with: the Python module, the Java package, or the path without its
    extension. `src/` (and Maven's `src/main/java/`) are not part of it."""
    stem = path.rsplit(".", 1)[0]
    if lang == "java":
        return java_package(text) or stem.replace("/", ".")
    if lang == "python":
        for root in ("src/", "lib/"):
            if stem.startswith(root):
                stem = stem[len(root) :]
        return stem.removesuffix("/__init__").replace("/", ".")
    return stem


def parse(path: str, text: str, lang: str | None = None) -> ParsedFile:
    """Symbols, calls and imports of one file. A language without a grammar gives an empty ParsedFile (text only)."""
    lang = lang or language_of(path)
    parsed = ParsedFile(path=path, lang=lang, text=text)
    if not available(lang):
        return parsed
    src = text.encode("utf-8")
    tree = _parser(lang).parse(src)
    walk = _Walk(LANGS[lang], src, module_name(path, lang, text))
    _visit(tree.root_node, walk, "", [])
    parsed.symbols = sorted(walk.symbols, key=lambda s: (s.start, -s.end))
    known = {s.qualified for s in parsed.symbols}
    parsed.calls = [c if c.caller in known or not c.caller else c.model_copy(update={"caller": ""}) for c in walk.calls]
    parsed.imports = LANGS[lang].imports(tree.root_node, src)
    return parsed
