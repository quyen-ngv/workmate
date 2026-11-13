"""A standard pack: everything a unit means by "standard", as data in one folder (pack.yaml + templates).

A pack is checked when it is loaded, all of it at once: a pack that names a kind, a document type, a role or a rule
type that does not exist is refused with every problem and where it is, so a bad pack never reaches a running stage.
`extends: <pack>` lets a unit write only what differs: dicts merge key by key, anything else is replaced.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from .model import REACTIONS, RULE_TYPES, Crosscheck, DocType, GateConfig, ItemKind, OnChange, RuleDef, StageConfig, Syntax

PACK_FILE = "pack.yaml"
DEFAULT_DIR = Path(__file__).resolve().parent.parent / "templates" / "packs" / "default"
OWNER = "owner"  # the ticket owner: always a valid role in a gate, as in domain.stage
KIND_RE = re.compile(r"[A-Z][A-Z0-9]*")
SELECTOR_RE = re.compile(r"([\w-]+)\.([A-Z][A-Z0-9]*)")
# Parameters a rule type cannot work without. `evidence` needs one of two, checked apart.
REQUIRED = {
    "template": ("docs",),
    "cover": ("each", "by"),
    "traces_to": ("each", "to"),
    "evidence": (),
    "fresh": (),
    "baseline": ("docs",),
    "answered": ("kinds",),
    "declared_change": ("each", "against"),
}


class PackError(ValueError):
    """Every problem of a pack, each as "<where>: <what>"."""

    def __init__(self, problems: list[str], source: str = "") -> None:
        self.problems = problems
        self.source = source
        head = f"Bộ chuẩn {source} không hợp lệ" if source else "Bộ chuẩn không hợp lệ"
        super().__init__(head + ":\n" + "\n".join(f"- {p}" for p in problems))


class StandardPack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int = 1
    roles: list[str] = Field(default_factory=list)
    syntax: Syntax = Field(default_factory=Syntax)
    item_kinds: dict[str, ItemKind] = Field(default_factory=dict)
    doc_types: dict[str, DocType] = Field(default_factory=dict)
    rules: dict[str, RuleDef] = Field(default_factory=dict)
    crosschecks: dict[str, Crosscheck] = Field(default_factory=dict)
    on_change: OnChange = Field(default_factory=OnChange)
    flows: dict[str, list[StageConfig]] = Field(default_factory=dict)
    gates: dict[str, GateConfig] = Field(default_factory=dict)

    _roots: list[Path] = PrivateAttr(default_factory=list)  # this pack's folder, then the ones it extends

    def kind_of(self, item_id: str) -> str | None:
        """The kind an id belongs to, by its prefix (`AC-1.2` → `AC`); None when no kind of the pack has it."""
        prefix = item_id.rsplit("/", 1)[-1].split("-", 1)[0]
        return prefix if prefix in self.item_kinds else None

    def id_pattern(self, kind: str) -> re.Pattern[str]:
        return re.compile(self.item_kinds[kind].pattern or rf"{re.escape(kind)}-\d+")

    def doc_types_with(self, kind: str) -> list[str]:
        return [name for name, dt in self.doc_types.items() if kind in dt.items]

    def tag_name(self, written: str) -> str | None:
        """`[kỹ thuật]` → `technical`: rules speak in tag names, documents in the unit's language."""
        word = written.strip().casefold()
        return next((name for name, text in self.syntax.tags.items() if text.casefold() == word), None)

    def template_text(self, doctype: str) -> str:
        """The template of a document type; a pack that extends another finds the files it did not replace there."""
        rel = self.doc_types[doctype].template
        if not rel:
            raise FileNotFoundError(f"Loại tài liệu '{doctype}' không khai mẫu (doc_types.{doctype}.template)")
        for root in self._roots:
            path = root / rel
            if path.is_file():
                return path.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Không thấy mẫu '{rel}' của loại tài liệu '{doctype}' trong {[str(r) for r in self._roots]}")


def split_selector(selector: str) -> tuple[str, str | None]:
    """`spec.AC` → ("spec", "AC"); `spec` → ("spec", None)."""
    doctype, _, kind = selector.partition(".")
    return doctype, kind or None


def load_default() -> StandardPack:
    return load_pack(DEFAULT_DIR)


def load_pack(path: str | Path) -> StandardPack:
    """Load a pack from its folder (or its pack.yaml). Raises PackError with every problem found."""
    data, roots = _read(Path(path), [])
    return build_pack(data, roots, source=str(path))


def build_pack(data: dict[str, Any], roots: list[Path] | None = None, source: str = "") -> StandardPack:
    """A pack from already-merged data. `roots` are the folders templates are looked up in; none = not checked."""
    data = {k: v for k, v in data.items() if k != "extends"}
    try:
        pack = StandardPack.model_validate(data)
    except ValidationError as exc:
        raise PackError([f"{'.'.join(str(p) for p in e['loc']) or 'pack'}: {e['msg']}" for e in exc.errors()], source) from None
    pack._roots = list(roots or [])
    problems = _check(pack)
    if problems:
        raise PackError(problems, source)
    return pack


def _read(path: Path, seen: list[Path]) -> tuple[dict[str, Any], list[Path]]:
    file = path / PACK_FILE if path.is_dir() else path
    folder = file.parent.resolve()
    if folder in seen:
        raise PackError([f"extends: vòng lặp giữa các bộ chuẩn ({' → '.join(p.name for p in [*seen, folder])})"], str(path))
    if not file.is_file():
        raise PackError([f"{file}: không thấy file {PACK_FILE}"], str(path))
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise PackError([f"{file}: YAML không đọc được: {exc}"], str(path)) from None
    if not isinstance(data, dict):
        raise PackError([f"{file}: nội dung phải là một bảng khoá: giá trị"], str(path))
    parent = data.get("extends")
    if not parent:
        return data, [folder]
    base, roots = _read(_resolve_parent(str(parent), folder), [*seen, folder])
    return _merge(base, data), [folder, *roots]


def _resolve_parent(name: str, folder: Path) -> Path:
    """`extends: default` is the pack shipped with the office; anything else is a folder next to (or under) this pack."""
    if name == "default":
        return DEFAULT_DIR
    for candidate in (folder / name, folder.parent / name):
        if candidate.exists():
            return candidate
    return folder.parent / name


def _merge(base: Any, over: Any) -> Any:
    if isinstance(base, dict) and isinstance(over, dict):
        return {**base, **{k: _merge(base.get(k), v) if k in base else v for k, v in over.items()}}
    return over


def _check(pack: StandardPack) -> list[str]:
    problems: list[str] = []
    kinds, doctypes, roles = pack.item_kinds, pack.doc_types, {*pack.roles, OWNER}

    def regex(where: str, pattern: str) -> None:
        try:
            re.compile(pattern)
        except re.error as exc:
            problems.append(f"{where}: biểu thức '{pattern}' không hợp lệ ({exc})")

    def selector(where: str, value: str, extra: tuple[str, ...] = (), bare: bool = False) -> None:
        """`doctype.KIND` where the kind is one the document type holds; `bare` also allows a document type alone."""
        if value in extra or (bare and value in doctypes):
            return
        if not SELECTOR_RE.fullmatch(value):
            problems.append(f"{where}: '{value}' sai cú pháp, phải viết <loại tài liệu>.<LOẠI MỤC>, ví dụ spec.AC")
            return
        doctype, kind = split_selector(value)
        if doctype not in doctypes:
            problems.append(f"{where}: '{value}' nhắc loại tài liệu '{doctype}' không có trong doc_types")
        elif kind not in kinds:
            problems.append(f"{where}: '{value}' nhắc loại mục '{kind}' không có trong item_kinds")
        elif kind not in doctypes[doctype].items:
            problems.append(f"{where}: loại tài liệu '{doctype}' không chứa mục '{kind}' (xem doc_types.{doctype}.items)")

    def role(where: str, names: list[str]) -> None:
        for name in names:
            if name not in roles:
                problems.append(f"{where}: vai trò '{name}' không có trong roles ({', '.join(pack.roles) or 'trống'})")

    regex("syntax.version_line", pack.syntax.version_line)
    for field in ("trace", "source", "changes", "answer"):
        if not [w for w in getattr(pack.syntax, field) if w.strip()]:
            problems.append(f"syntax.{field}: cần ít nhất một từ khoá")

    for name, kind in kinds.items():
        if not KIND_RE.fullmatch(name):
            problems.append(f"item_kinds.{name}: tên loại mục phải là chữ in hoa và số, bắt đầu bằng chữ (ví dụ AC, BR)")
        if kind.pattern:
            regex(f"item_kinds.{name}.pattern", kind.pattern)

    for name, doctype in doctypes.items():
        for kind in doctype.items:
            if kind not in kinds:
                problems.append(f"doc_types.{name}.items: loại mục '{kind}' không có trong item_kinds")
        for base in doctype.based_on:
            if base not in doctypes:
                problems.append(f"doc_types.{name}.based_on: loại tài liệu '{base}' không có trong doc_types")
            elif base == name:
                problems.append(f"doc_types.{name}.based_on: tài liệu không thể dựa trên chính nó")
        if doctype.template and pack._roots and not any((root / doctype.template).is_file() for root in pack._roots):
            problems.append(f"doc_types.{name}.template: không thấy file '{doctype.template}'")

    for rule_id, rule in pack.rules.items():
        where = f"rules.{rule_id}"
        if rule.type not in RULE_TYPES:
            problems.append(f"{where}.type: kiểu luật '{rule.type}' không có; các kiểu: {', '.join(RULE_TYPES)}")
            continue
        for param in REQUIRED[rule.type]:
            if not getattr(rule, param):
                problems.append(f"{where}.{param}: luật kiểu '{rule.type}' bắt buộc có tham số này")
        if rule.type == "evidence" and not (rule.kinds or rule.also):
            problems.append(f"{where}: luật kiểu 'evidence' cần 'kinds' hoặc 'also'")
        for param in ("each", "by", "to", "also", "against"):
            for value in getattr(rule, param):
                selector(f"{where}.{param}", value)
        for value in rule.docs:
            if value != "*" and value not in doctypes:
                problems.append(f"{where}.docs: loại tài liệu '{value}' không có trong doc_types")
        for value in rule.kinds:
            if value not in kinds:
                problems.append(f"{where}.kinds: loại mục '{value}' không có trong item_kinds")
        if rule.unless_tag and rule.unless_tag not in pack.syntax.tags:
            problems.append(f"{where}.unless_tag: thẻ '{rule.unless_tag}' không có trong syntax.tags")

    for check_id, check in pack.crosschecks.items():
        selector(f"crosschecks.{check_id}.left", check.left, bare=True)
        selector(f"crosschecks.{check_id}.right", check.right, extra=("evidence",), bare=True)

    for event in ("upstream_changed", "evidence_stale"):
        for when, reaction in getattr(pack.on_change, event).items():
            if reaction not in REACTIONS:
                problems.append(f"on_change.{event}.{when}: '{reaction}' không hợp lệ; chọn một trong: {', '.join(REACTIONS)}")

    # Gate names of a stage are not checked against `gates`: a flow may use the office's own gates (merge, knowledge).
    for flow, stages in pack.flows.items():
        seen: set[str] = set()
        for index, stage in enumerate(stages):
            where = f"flows.{flow}[{index}]"
            if stage.id in seen:
                problems.append(f"{where}.id: stage '{stage.id}' bị lặp trong luồng '{flow}'")
            seen.add(stage.id)
            if stage.spawn:
                for target in stage.spawn.flows:
                    if target not in pack.flows:
                        problems.append(f"{where}.spawn.flows: luồng '{target}' không có trong flows")
                for doc in stage.spawn.copy_docs:
                    if doc not in doctypes:
                        problems.append(f"{where}.spawn.copy: loại tài liệu '{doc}' không có trong doc_types")
                role(f"{where}.spawn.assign", stage.spawn.assign)

    for name, gate in pack.gates.items():
        for field in ("roles", "submit_by", "accept_findings"):
            role(f"gates.{name}.{field}", getattr(gate, field))
    return problems
