"""Confluence storage format (XHTML with `ac:` / `ri:` elements) ↔ markdown. Pure functions, stdlib only.

`storage_to_markdown` returns `(markdown, links, notes, attachments_referenced)`:
- links: pages and Jira issues the page points to. A page link in storage format has a title and a space, not an id, so
  its ref is `"<SPACE>:<title>"`. A plain `<a href>` to an http(s) URL is returned with kind "" for the caller to classify.
- notes: what could not be turned into text (diagrams, images, merged cells, includes left unresolved), for people.
- attachments_referenced: file names the page shows or links.

An include / excerpt-include macro becomes one line `[[include:<SPACE>:<title>]]` (see `INCLUDE_RE`) plus a link and a
note; the adapter replaces the line with the included page.
"""

from __future__ import annotations

import html as _html
import re
from html.parser import HTMLParser

from actoroffice.adapters.atlassian import CONFLUENCE_KIND, JIRA_KIND
from actoroffice.ports.docs import DocRef

INCLUDE_RE = re.compile(r"^\[\[include:([^:\]\n]*):([^\n]+?)\]\]$", re.MULTILINE)
MERGED_CELLS_NOTE = "Bảng có ô gộp (colspan/rowspan): bố cục trong bản chữ có thể lệch so với trang gốc."
UNREADABLE = "AI không đọc được"

VOID_TAGS = frozenset({"br", "hr", "img", "col", "input", "meta", "link"})
INLINE_TAGS = frozenset(
    {"strong", "b", "em", "i", "u", "s", "del", "strike", "ins", "code", "tt", "kbd", "a", "br", "span", "sub", "sup", "small", "font", "time", "img"}
    | {"ac:link", "ac:image", "ac:emoticon", "ac:inline-comment-marker", "ac:placeholder"}
)
INLINE_MACROS = frozenset({"jira", "status", "anchor"})
CODE_MACROS = frozenset({"code", "noformat"})
PANEL_LABELS = {"info": "Thông tin", "note": "Lưu ý", "warning": "Cảnh báo", "tip": "Mẹo", "panel": "Khung"}
DIAGRAM_MACROS = {"drawio": "draw.io", "drawio-sketch": "draw.io", "inc-drawio": "draw.io", "gliffy": "Gliffy", "lucidchart": "Lucidchart"}
INCLUDE_MACROS = frozenset({"include", "excerpt-include"})
SILENT_MACROS = frozenset({"toc", "toc-zone", "anchor", "pagebreak"})
_CDATA_RE = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.DOTALL)
_WS_RE = re.compile(r"\s+")


def include_marker(space: str, title: str) -> str:
    return f"[[include:{space}:{title}]]"


def include_note(title: str) -> str:
    return f"Trang nhúng (include) «{title}» chưa được đưa vào: xem trang được liên kết."


class _Node:
    __slots__ = ("attrs", "children", "tag")

    def __init__(self, tag: str, attrs: dict[str, str]) -> None:
        self.tag = tag
        self.attrs = attrs
        self.children: list[_Node | str] = []

    def find(self, tag: str) -> _Node | None:
        for child in self.children:
            if isinstance(child, _Node):
                if child.tag == tag:
                    return child
                found = child.find(tag)
                if found is not None:
                    return found
        return None

    def child(self, tag: str) -> _Node | None:
        return next((c for c in self.children if isinstance(c, _Node) and c.tag == tag), None)

    def raw_text(self) -> str:
        return "".join(c if isinstance(c, str) else c.raw_text() for c in self.children)


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("#root", {})
        self.stack = [self.root]

    def _add(self, tag: str, attrs: list[tuple[str, str | None]]) -> _Node:
        node = _Node(tag, {k: v or "" for k, v in attrs})
        self.stack[-1].children.append(node)
        return node

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = self._add(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._add(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _parse(storage: str) -> _Node:
    # CDATA sections become escaped text first: html.parser's handling of CDATA differs between Python patch releases.
    builder = _TreeBuilder()
    builder.feed(_CDATA_RE.sub(lambda m: _html.escape(m.group(1), quote=False), storage))
    builder.close()
    return builder.root


def _span(node: _Node, name: str) -> int:
    try:
        return max(1, int(node.attrs.get(name) or 1))
    except ValueError:
        return 1


def _quote(text: str) -> str:
    return "\n".join(("> " + line) if line else ">" for line in text.split("\n"))


def _fence(code: str, language: str = "") -> str:
    fence = "````" if "```" in code else "```"
    return f"{fence}{language}\n{code}\n{fence}"


class _Renderer:
    def __init__(self, space: str) -> None:
        self.space = space
        self.links: list[DocRef] = []
        self.notes: list[str] = []
        self.files: list[str] = []

    # ---- collected facts

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def link(self, ref: str, kind: str, locator: str = "") -> None:
        found = DocRef(ref=ref, kind=kind, locator=locator)
        if ref and found not in self.links:
            self.links.append(found)

    def file(self, name: str) -> None:
        if name and name not in self.files:
            self.files.append(name)

    # ---- blocks

    def is_inline(self, node: _Node | str) -> bool:
        if isinstance(node, str):
            return True
        if node.tag == "ac:structured-macro":
            return node.attrs.get("ac:name", "") in INLINE_MACROS
        return node.tag in INLINE_TAGS

    def blocks(self, children: list[_Node | str]) -> list[str]:
        out: list[str] = []
        run: list[_Node | str] = []

        def flush() -> None:
            text = re.sub(r" *\n *", "\n", self.inline(run)).strip()
            if text:
                out.append(text)
            run.clear()

        for child in children:
            if self.is_inline(child):
                run.append(child)
            else:
                flush()
                out.extend(b for b in self.block(child) if b.strip())  # type: ignore[arg-type]
        flush()
        return out

    def flat(self, children: list[_Node | str]) -> str:
        return " ".join(self.blocks(children)).replace("\n", " ").strip()

    def block(self, node: _Node) -> list[str]:
        tag = node.tag
        if re.fullmatch(r"h[1-6]", tag):
            return ["#" * int(tag[1]) + " " + self.flat(node.children)]
        if tag in ("ul", "ol"):
            return [self.list_block(node)]
        if tag == "ac:task-list":
            return [self.task_list(node)]
        if tag == "table":
            return [self.table(node)]
        if tag == "pre":
            return [_fence(node.raw_text().strip("\n"))]
        if tag == "blockquote":
            return [_quote("\n\n".join(self.blocks(node.children)))]
        if tag == "hr":
            return ["---"]
        if tag == "ac:structured-macro":
            return self.macro(node)
        if tag == "ac:parameter":
            return []
        return self.blocks(node.children)

    def list_block(self, node: _Node) -> str:
        lines: list[str] = []
        index = 0
        for item in node.children:
            if not isinstance(item, _Node) or item.tag != "li":
                continue
            index += 1
            marker = f"{index}. " if node.tag == "ol" else "- "
            self._item(lines, marker, "\n".join(self.blocks(item.children)))
        return "\n".join(lines)

    def task_list(self, node: _Node) -> str:
        lines: list[str] = []
        for task in node.children:
            if not isinstance(task, _Node) or task.tag != "ac:task":
                continue
            status = task.child("ac:task-status")
            body = task.child("ac:task-body")
            done = status is not None and status.raw_text().strip() == "complete"
            self._item(lines, "- [x] " if done else "- [ ] ", "\n".join(self.blocks(body.children)) if body is not None else "")
        return "\n".join(lines)

    @staticmethod
    def _item(lines: list[str], marker: str, body: str) -> None:
        pad = " " * min(len(marker), 4)
        for n, line in enumerate(body.split("\n")):
            lines.append(((marker if n == 0 else pad) + line) if line or n == 0 else "")

    def _rows(self, node: _Node) -> list[_Node]:
        rows: list[_Node] = []
        for child in node.children:
            if isinstance(child, _Node):
                if child.tag == "tr":
                    rows.append(child)
                elif child.tag in ("thead", "tbody", "tfoot"):
                    rows.extend(self._rows(child))
        return rows

    def table(self, node: _Node) -> str:
        rows: list[list[str]] = []
        merged = False
        for row in self._rows(node):
            cells: list[str] = []
            for cell in row.children:
                if not isinstance(cell, _Node) or cell.tag not in ("td", "th"):
                    continue
                colspan = _span(cell, "colspan")
                merged = merged or colspan > 1 or _span(cell, "rowspan") > 1
                text = "<br>".join(self.blocks(cell.children)).replace("\n", "<br>").replace("|", "\\|")
                cells.extend([text] + [""] * (colspan - 1))
            rows.append(cells)
        if merged:
            self.note(MERGED_CELLS_NOTE)
        rows = [r for r in rows if r]
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join(["---"] * width) + " |"]
        lines += ["| " + " | ".join(r) + " |" for r in rows[1:]]
        return "\n".join(lines)

    # ---- macros

    @staticmethod
    def params(node: _Node) -> dict[str, str]:
        return {c.attrs.get("ac:name", ""): c.raw_text().strip() for c in node.children if isinstance(c, _Node) and c.tag == "ac:parameter"}

    def macro(self, node: _Node) -> list[str]:
        name = node.attrs.get("ac:name", "")
        params = self.params(node)
        rich = node.child("ac:rich-text-body")
        plain = node.child("ac:plain-text-body")
        body = self.blocks(rich.children) if rich is not None else []
        title = params.get("title", "")
        if name in CODE_MACROS:
            code = _fence(plain.raw_text().strip("\n") if plain is not None else "", params.get("language", ""))
            return ([f"**{title}**"] if title else []) + [code]
        if name in PANEL_LABELS:
            head = f"**{PANEL_LABELS[name]}{': ' + title if title else ''}**"
            return [_quote("\n\n".join([head, *body]))]
        if name == "expand":
            return ([f"**{title}**"] if title else []) + body
        if name in DIAGRAM_MACROS:
            attachment = node.find("ri:attachment")
            file = params.get("diagramName") or params.get("name") or (attachment.attrs.get("ri:filename", "") if attachment is not None else "") or "(không tên)"
            self.file(file if file != "(không tên)" else "")
            self.note(f"Sơ đồ {DIAGRAM_MACROS[name]} «{file}»: {UNREADABLE}.")
            return [f"*[Sơ đồ {DIAGRAM_MACROS[name]}: {file} — {UNREADABLE}]*"]
        if name in INCLUDE_MACROS:
            target = node.find("ri:page")
            page = target.attrs.get("ri:content-title", "") if target is not None else ""
            if not page:
                self.note(f"Macro «{name}» không chỉ rõ trang được nhúng.")
                return []
            space = (target.attrs.get("ri:space-key", "") if target is not None else "") or self.space
            self.link(f"{space}:{page}", CONFLUENCE_KIND)
            self.note(include_note(page))
            return [include_marker(space, page)]
        if name in SILENT_MACROS:
            return []
        if body:
            return body
        if plain is not None:
            return [_fence(plain.raw_text().strip("\n"))]
        attachment = node.find("ri:attachment")
        if attachment is not None:
            file = attachment.attrs.get("ri:filename", "")
            self.file(file)
            self.note(f"Tệp nhúng «{file}» (macro {name}): {UNREADABLE}.")
            return [f"*[Tệp nhúng: {file} — {UNREADABLE}]*"]
        self.note(f"Macro «{name}» không được chuyển sang chữ.")
        return []

    def inline_macro(self, node: _Node) -> str:
        name = node.attrs.get("ac:name", "")
        params = self.params(node)
        if name == "jira":
            key = params.get("key", "")
            if key:
                self.link(key, JIRA_KIND)
                return key
            self.note("Bảng issue Jira theo truy vấn (JQL) không được đưa vào bản chữ.")
            return ""
        if name == "status":
            return f"[{params.get('title', '')}]"
        return ""

    # ---- inline

    def wrap(self, children: list[_Node | str], mark: str) -> str:
        text = self.inline(children)
        core = text.strip()
        if not core:
            return text
        return f"{text[: len(text) - len(text.lstrip())]}{mark}{core}{mark}{text[len(text.rstrip()) :]}"

    def inline(self, nodes: list[_Node | str]) -> str:
        return "".join(_WS_RE.sub(" ", n) if isinstance(n, str) else self.inline_node(n) for n in nodes)

    def inline_node(self, node: _Node) -> str:
        tag = node.tag
        if tag == "br":
            return "\n"
        if tag in ("strong", "b"):
            return self.wrap(node.children, "**")
        if tag in ("em", "i"):
            return self.wrap(node.children, "*")
        if tag in ("s", "del", "strike"):
            return self.wrap(node.children, "~~")
        if tag in ("code", "tt", "kbd"):
            return self.wrap(node.children, "`")
        if tag == "a":
            href = node.attrs.get("href", "").strip()
            text = self.inline(node.children).strip()
            if not href:
                return text
            if href.lower().startswith(("http://", "https://")):
                self.link(href, "")
            return f"[{text or href}]({href})"
        if tag == "ac:link":
            return self.ac_link(node)
        if tag in ("ac:image", "img"):
            return self.image(node)
        if tag == "ac:emoticon":
            return f":{node.attrs.get('ac:name', '')}:"
        if tag == "time":
            return node.attrs.get("datetime", "")
        if tag == "ac:placeholder":
            return ""
        if tag == "ac:structured-macro":
            return self.inline_macro(node) if self.is_inline(node) else self.flat([node])
        if tag in INLINE_TAGS:
            return self.inline(node.children)
        return self.flat([node])

    def ac_link(self, node: _Node) -> str:
        body_node = node.child("ac:plain-text-link-body") or node.child("ac:link-body")
        body = self.inline(body_node.children).strip() if body_node is not None else ""
        anchor = node.attrs.get("ac:anchor", "")
        page = node.child("ri:page")
        if page is not None:
            title = page.attrs.get("ri:content-title", "")
            ref = page.attrs.get("ri:content-id", "") or f"{page.attrs.get('ri:space-key', '') or self.space}:{title}"
            self.link(ref, CONFLUENCE_KIND, "#" + anchor if anchor else "")
            return f"{body} ([[{title}]])" if body and body != title else f"[[{title}]]"
        attachment = node.child("ri:attachment")
        if attachment is not None:
            file = attachment.attrs.get("ri:filename", "")
            self.file(file)
            return f"[tệp: {body or file}]"
        url = node.child("ri:url")
        if url is not None:
            href = url.attrs.get("ri:value", "")
            return f"[{body or href}]({href})"
        if node.child("ri:user") is not None:
            return body or "@người dùng"
        return body or (f"#{anchor}" if anchor else "")

    def image(self, node: _Node) -> str:
        attachment = node.child("ri:attachment")
        url = node.child("ri:url")
        name = attachment.attrs.get("ri:filename", "") if attachment is not None else (url.attrs.get("ri:value", "") if url is not None else node.attrs.get("src", ""))
        if attachment is not None:
            self.file(name)
        name = name or "(không tên)"
        self.note(f"Ảnh «{name}»: {UNREADABLE}.")
        return f"*[Ảnh: {name} — {UNREADABLE}]*"


def storage_to_markdown(storage: str, *, space: str = "") -> tuple[str, list[DocRef], list[str], list[str]]:
    """Storage format → `(markdown, links, notes, attachments_referenced)`. `space` is the key of the page's own space,
    used for links that name a page without a space."""
    renderer = _Renderer(space)
    markdown = "\n\n".join(renderer.blocks(_parse(storage or "").children))
    return markdown, renderer.links, renderer.notes, renderer.files


# ---- markdown → storage format (publishing) ----------------------------------------------------------------


def _code_macro(lines: list[str]) -> str:
    code = "\n".join(lines).replace("]]>", "]]]]><![CDATA[>")
    return f'<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[{code}]]></ac:plain-text-body></ac:structured-macro>'


def _md_inline(text: str) -> str:
    out = _html.escape(text, quote=False)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", out)


def markdown_to_storage(md: str) -> str:
    """Small markdown → storage format: headings, paragraphs, bullet and numbered lists, code fences, tables, bold, inline code."""
    out: list[str] = []
    list_tag = ""
    in_code = in_table = False
    code: list[str] = []
    for line in md.replace("\r\n", "\n").split("\n"):
        if line.strip().startswith("```"):
            if in_code:
                out.append(_code_macro(code))
                code, in_code = [], False
            else:
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        bullet = re.match(r"^\s*[-*]\s+(.*)$", line)
        numbered = re.match(r"^\s*\d+[.)]\s+(.*)$", line)
        wanted = "ul" if bullet else "ol" if numbered else ""
        if list_tag and wanted != list_tag:
            out.append(f"</{list_tag}>")
            list_tag = ""
        if in_table and not line.strip().startswith("|"):
            out.append("</tbody></table>")
            in_table = False
        if m := re.match(r"^(#{1,6})\s+(.*)$", line):
            out.append(f"<h{len(m.group(1))}>{_md_inline(m.group(2))}</h{len(m.group(1))}>")
        elif item := (bullet or numbered):
            if not list_tag:
                out.append(f"<{wanted}>")
                list_tag = wanted
            out.append(f"<li>{_md_inline(item.group(1))}</li>")
        elif line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            cell_tag = "td" if in_table else "th"
            if not in_table:
                out.append("<table><tbody>")
                in_table = True
            out.append("<tr>" + "".join(f"<{cell_tag}>{_md_inline(c)}</{cell_tag}>" for c in cells) + "</tr>")
        elif line.strip():
            out.append(f"<p>{_md_inline(line)}</p>")
    if in_code:
        out.append(_code_macro(code))
    if list_tag:
        out.append(f"</{list_tag}>")
    if in_table:
        out.append("</tbody></table>")
    return "\n".join(out)
