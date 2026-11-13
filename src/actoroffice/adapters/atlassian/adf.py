"""Atlassian Document Format (Jira Cloud descriptions and comments) ↔ text. Pure functions."""

from __future__ import annotations

from typing import Any

INLINE_TYPES = frozenset({"text", "hardBreak", "mention", "emoji", "inlineCard", "status", "date", "mediaInline", "placeholder"})
PANEL_LABELS = {"info": "Thông tin", "note": "Lưu ý", "warning": "Cảnh báo", "error": "Lỗi", "success": "Thành công", "tip": "Mẹo"}
MEDIA_PLACEHOLDER = "*(tệp đính kèm: AI không đọc được)*"


def adf_to_markdown(node: Any) -> str:
    """ADF → markdown. A plain string passes through (Jira Server / REST v2)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, dict) and node.get("type") == "doc":
        node = node.get("content") or []
    return "\n\n".join(_blocks(node if isinstance(node, list) else [node]))


def _attrs(node: dict[str, Any]) -> dict[str, Any]:
    return node.get("attrs") or {}


def _content(node: dict[str, Any]) -> list[Any]:
    return node.get("content") or []


def _blocks(nodes: list[Any]) -> list[str]:
    out: list[str] = []
    run: list[Any] = []

    def flush() -> None:
        text = _inline(run).strip()
        if text:
            out.append(text)
        run.clear()

    for node in nodes:
        if not isinstance(node, dict):
            continue
        if node.get("type") in INLINE_TYPES:
            run.append(node)
            continue
        flush()
        block = _block(node)
        if block.strip():
            out.append(block)
    flush()
    return out


def _quote(text: str) -> str:
    return "\n".join(("> " + line) if line else ">" for line in text.split("\n"))


def _block(node: dict[str, Any]) -> str:
    kind = node.get("type")
    content = _content(node)
    if kind == "paragraph":
        return _inline(content).strip()
    if kind == "heading":
        level = min(6, max(1, int(_attrs(node).get("level") or 2)))
        return "#" * level + " " + _inline(content).strip().replace("\n", " ")
    if kind in ("bulletList", "orderedList", "taskList", "decisionList"):
        return _list(node)
    if kind == "codeBlock":
        code = "".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        fence = "````" if "```" in code else "```"
        return f"{fence}{_attrs(node).get('language') or ''}\n{code}\n{fence}"
    if kind == "blockquote":
        return _quote("\n\n".join(_blocks(content)))
    if kind == "panel":
        label = PANEL_LABELS.get(str(_attrs(node).get("panelType") or "info"), "Ghi chú")
        return _quote("\n\n".join([f"**{label}:**", *_blocks(content)]))
    if kind == "rule":
        return "---"
    if kind == "table":
        return _table(node)
    if kind in ("expand", "nestedExpand"):
        title = str(_attrs(node).get("title") or "").strip()
        return "\n\n".join(([f"**{title}**"] if title else []) + _blocks(content))
    if kind in ("mediaSingle", "mediaGroup", "media"):
        return MEDIA_PLACEHOLDER
    if kind in ("blockCard", "embedCard"):
        return str(_attrs(node).get("url") or "")
    return "\n\n".join(_blocks(content))


def _list(node: dict[str, Any]) -> str:
    kind = node.get("type")
    start = int(_attrs(node).get("order") or 1)
    lines: list[str] = []
    for index, item in enumerate(c for c in _content(node) if isinstance(c, dict)):
        if kind == "orderedList":
            marker = f"{start + index}. "
        elif kind == "taskList":
            marker = "- [x] " if _attrs(item).get("state") == "DONE" else "- [ ] "
        else:
            marker = "- "
        body = "\n".join(_blocks(_content(item))) or ""
        pad = " " * min(len(marker), 4)
        for n, line in enumerate(body.split("\n")):
            lines.append((marker if n == 0 else pad) + line if line or n == 0 else "")
    return "\n".join(lines)


def _table(node: dict[str, Any]) -> str:
    rows: list[list[str]] = []
    for row in _content(node):
        if not isinstance(row, dict):
            continue
        cells = []
        for cell in _content(row):
            text = "<br>".join(_blocks(_content(cell))) if isinstance(cell, dict) else ""
            cells.append(text.replace("\n", "<br>").replace("|", "\\|"))
        rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
    lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(lines)


def _wrap(text: str, mark: str) -> str:
    core = text.strip()
    if not core:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return f"{lead}{mark}{core}{mark}{trail}"


def _text(node: dict[str, Any]) -> str:
    text = str(node.get("text", ""))
    marks = {m.get("type"): (m.get("attrs") or {}) for m in node.get("marks") or [] if isinstance(m, dict)}
    if "code" in marks:
        text = _wrap(text, "`")
    else:
        if "strong" in marks:
            text = _wrap(text, "**")
        if "em" in marks:
            text = _wrap(text, "*")
        if "strike" in marks:
            text = _wrap(text, "~~")
    href = str(marks.get("link", {}).get("href") or "")
    if href and text.strip():
        text = f"[{text.strip()}]({href})"
    return text


def _inline(nodes: list[Any]) -> str:
    out: list[str] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        kind = node.get("type")
        attrs = _attrs(node)
        if kind == "text":
            out.append(_text(node))
        elif kind == "hardBreak":
            out.append("\n")
        elif kind == "mention":
            out.append("@" + str(attrs.get("text") or attrs.get("id") or "").lstrip("@"))
        elif kind == "emoji":
            out.append(str(attrs.get("text") or attrs.get("shortName") or ""))
        elif kind == "inlineCard":
            out.append(str(attrs.get("url") or ""))
        elif kind == "status":
            out.append(f"[{attrs.get('text') or ''}]")
        elif kind == "date":
            out.append(str(attrs.get("timestamp") or ""))
        elif kind == "mediaInline":
            out.append(MEDIA_PLACEHOLDER)
        elif kind not in INLINE_TYPES:
            out.append(" ".join(_blocks([node])).replace("\n", " "))
    return "".join(out)


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text → ADF for a comment: one paragraph per block, lines kept with hard breaks, ``` blocks as code."""
    blocks: list[dict[str, Any]] = []
    for i, part in enumerate(text.split("```")):
        if i % 2 == 1:
            code = part.split("\n", 1)[1] if "\n" in part else part
            blocks.append({"type": "codeBlock", "content": [{"type": "text", "text": code.strip("\n") or " "}]})
            continue
        for para in [p for p in part.split("\n\n") if p.strip()]:
            content: list[dict[str, Any]] = []
            for j, line in enumerate(para.strip("\n").split("\n")):
                if j:
                    content.append({"type": "hardBreak"})
                if line:
                    content.append({"type": "text", "text": line})
            blocks.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}
