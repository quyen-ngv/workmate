"""See and change a document standard (pack) from the web: the template of each document type, the instructions the
AI follows when it writes them, the criteria of the AI quality review, and the whole pack.yaml for those who want it.

The built-in pack is never written. Changing it means making a pack of the unit's own under `<data_dir>/packs/<name>/`
that says `extends: <the pack in use>` and holds only what differs: a template becomes a file of its own, an
instruction or a list of criteria a key of its pack.yaml. Every save is loaded and checked before it stays; a save
that does not load is undone and every problem is named. The packs are read again at the next check (`docflow.pack_at`
watches pack.yaml), no restart.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from actoroffice.doccore import PackError, StandardPack, load_pack
from actoroffice.doccore.pack import PACK_FILE
from actoroffice.domain.errors import OfficeError

from .docflow import pack_at, pack_path

NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


class PackEditError(OfficeError):
    code = 400


def _folder(name: str, data_dir: Path) -> Path:
    if name == "default" or not NAME_RE.fullmatch(name):
        raise PackEditError(f"Tên bộ chuẩn '{name}' không dùng được: chữ thường, số, '-' hoặc '_', tối đa 40 ký tự, khác 'default'.")
    return data_dir / "packs" / name


def view(name: str | None, data_dir: Path) -> dict[str, Any]:
    """Everything people may read of a pack: its document types with their templates, the AI's instructions, the review
    criteria, the rules, and its own pack.yaml (empty for the built-in one, which cannot be edited)."""
    path = pack_path(name, data_dir)
    pack = pack_at(path)
    own = (path / PACK_FILE).read_text(encoding="utf-8") if path is not None and (path / PACK_FILE).is_file() else ""
    extends = (yaml.safe_load(own) or {}).get("extends") if own else None

    def template(doctype: str) -> str:
        try:
            return pack.template_text(doctype)
        except FileNotFoundError:
            return ""

    return {
        "name": name or "default", "title": pack.name, "version": pack.version, "editable": path is not None, "extends": extends,
        "doc_types": [
            {"id": key, "sections": dt.sections, "items": dt.items, "based_on": dt.based_on, "template": template(key)}
            for key, dt in pack.doc_types.items()
        ],
        "item_kinds": {key: kind.title for key, kind in pack.item_kinds.items()},
        "instructions": dict(pack.instructions),
        "reviews": {key: {"doc": r.doc, "at": r.at, "rubric": r.rubric} for key, r in pack.reviews.items()},
        "rules": [{"id": key, "type": r.type, "severity": r.severity} for key, r in pack.rules.items()],
        "yaml": own,
    }


def save(
    name: str,
    data_dir: Path,
    *,
    base: str | None = None,
    templates: dict[str, str] | None = None,
    instructions: dict[str, str] | None = None,
    reviews: dict[str, list[str]] | None = None,
    yaml_text: str | None = None,
) -> StandardPack:
    """Create the pack `name` (extending `base`, default the built-in one) or change it. Only what differs from the pack
    as it is now is written. Returns the pack as loaded after the save; raises PackEditError, with the pack as it was,
    when the result does not load."""
    folder = _folder(name, data_dir)
    file = folder / PACK_FILE
    backup: dict[Path, str | None] = {}

    def write(path: Path, text: str) -> None:
        if path not in backup:
            backup[path] = path.read_text(encoding="utf-8") if path.is_file() else None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    if file.is_file():
        current = pack_at(folder)
        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    else:
        if base and base != "default":
            _folder(base, data_dir)  # a valid name
        current = pack_at(pack_path(base, data_dir))
        data = {"name": name, "version": 1, "extends": base or "default"}
    if yaml_text is not None and yaml_text.strip() and yaml_text.strip() != (file.read_text(encoding="utf-8").strip() if file.is_file() else ""):
        try:
            data = yaml.safe_load(yaml_text) or {}
        except yaml.YAMLError as exc:
            raise PackEditError(f"YAML không đọc được: {exc}") from None
        if not isinstance(data, dict):
            raise PackEditError("pack.yaml phải là một bảng khoá: giá trị")
    for doctype, text in (templates or {}).items():
        if doctype not in current.doc_types:
            raise PackEditError(f"Bộ chuẩn không có loại tài liệu '{doctype}'")
        try:
            same = current.template_text(doctype).replace("\r\n", "\n").strip() == text.replace("\r\n", "\n").strip()
        except FileNotFoundError:
            same = False
        if not same:
            rel = f"templates/{doctype}.md"
            write(folder / rel, text.replace("\r\n", "\n").rstrip() + "\n")
            data.setdefault("doc_types", {}).setdefault(doctype, {})["template"] = rel
    for key, text in (instructions or {}).items():
        if text.replace("\r\n", "\n").strip() != (current.instructions.get(key) or "").strip():
            data.setdefault("instructions", {})[key] = text.replace("\r\n", "\n").rstrip() + "\n"
    for key, rubric in (reviews or {}).items():
        lines = [line.strip() for line in rubric if line.strip()]
        if key not in current.reviews:
            raise PackEditError(f"Bộ chuẩn không có lượt soát '{key}'")
        if lines != current.reviews[key].rubric:
            data.setdefault("reviews", {}).setdefault(key, {})["rubric"] = lines
    write(file, yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120))
    try:
        return load_pack(folder)
    except PackError as exc:
        for path, text in backup.items():
            if text is None:
                path.unlink(missing_ok=True)
            else:
                path.write_text(text, encoding="utf-8", newline="\n")
        raise PackEditError("Không lưu: bộ chuẩn sau khi sửa không hợp lệ.\n" + "\n".join(f"- {p}" for p in exc.problems)) from None
