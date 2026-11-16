"""Markdown → items with ids, their links and their cited sources.

The document stays markdown people read; the machine reads three things in it (spec 4.2): a heading or a list line
that starts with an id is an item, the lines under it that start with a keyword of the pack (`Truy vết:`, `Nguồn:`,
`Thay đổi:`, `Trả lời:`) are its links, and `[tag]` on its first line are its tags. Which ids count, which words are
keywords and which headings are required all come from the pack.

An item ends where the next item starts, so a user story does not swallow its acceptance criteria and the hash of
each one changes only when its own text does.

What the machine must not miss, it says: an id of one of the document's kinds that sits in a table row or on a
checkbox line and is not an item anywhere is reported (`unparsed_id`), because every rule would otherwise pass on a
document it cannot read. Nothing inside a fenced code block is read: not an id, not a keyword line.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .model import Item, ParsedDoc, Problem, SourceRef
from .pack import StandardPack

HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$")
BULLET_RE = re.compile(r"^([ \t]*)(?:[-*+]|\d+[.)])[ \t]+(.*)$")
FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
NUMBERING_RE = re.compile(r"^\d+(?:\.\d+)*[.)]?[ \t]+")
TAG_RE = re.compile(r"\[([^\]\[]+)\]")
# An id in a `Truy vết:` / `Thay đổi:` line, with the ticket in front when it is another ticket's: EP-4/REQ-2
REF_RE = re.compile(r"(?<![\w/-])(?:([A-Z][A-Z0-9]*-\d+)/)?([A-Z][A-Z0-9]*-\d+(?:\.\d+)*)(?![\w-])")

# Source forms, after domain/sources.py (copied: the core imports nothing of the office).
_SECTION = r"§[ \t]?\d+(?:\.\d+)*"
URL_SRC_RE = re.compile(rf"(https?://\S+?)(?:[ \t]+({_SECTION}))?")
FILE_SRC_RE = re.compile(rf"(?:([\w.-]+):)?((?:[\w.-]+/)*[\w-]+(?:\.[\w-]+)*\.[A-Za-z0-9]+)(?:[ \t]?({_SECTION})|(:\d+(?:-\d+)?)|(#L\d+(?:-L?\d+)?))?")
TICKET_SRC_RE = re.compile(r"[A-Z][A-Z0-9]*-\d+")
SOURCE_SPLIT_RE = re.compile(r"\s*(?:;|,(?=\s|$))\s*|\s+(?:và|and)\s+")
QUOTE_RE = re.compile(r"`([^`]+)`|\"([^\"]+)\"|“([^”]+)”")
CHECKBOX_RE =re.compile(r"^\[[ xX]\][ \t]+")
LINE_START_RE = re.compile(r"^[ \t]*(?:(?:[-*+]|\d+[.)])[ \t]+)?(?:\*\*|__)?")
# What may follow the word of a `syntax.lines` line: a short label, then the colon (`Phương án A: …`).
LINE_REST_RE = re.compile(r"^(?![\w-])[^:\n]{0,40}:(?:\*\*|__)?[ \t]*(.*)$")


@dataclass
class _Open:
    id: str
    kind: str
    head: str  # the first line after the id
    level: int | None  # heading level; None for a list item
    indent: int
    section: str
    lines: list[str] = field(default_factory=list)
    plain: list[str] = field(default_factory=list)  # the lines outside code fences: only these are read for keywords
    blank: bool = False  # a blank line was seen: an unindented line after it is no longer part of a list item


def parse(body: str, doctype: str, pack: StandardPack) -> ParsedDoc:
    if doctype not in pack.doc_types:
        raise ValueError(f"Loại tài liệu '{doctype}' không có trong bộ chuẩn '{pack.name}'")
    spec = pack.doc_types[doctype]
    item_re = _item_re(spec.items)
    mention_re = _mention_re(spec.items)
    changelog = pack.syntax.changelog.strip().casefold()

    sections: list[str] = []
    done: list[_Open] = []
    current: _Open | None = None
    section = ""
    open_sections: list[tuple[int, str]] = []  # the headings we are under: (level, name)
    boxed: dict[str, None] = {}  # ids seen in a table row or on a checkbox line, in order
    fenced = in_changelog = has_changelog = has_table = False

    def close() -> None:
        nonlocal current
        if current:
            done.append(current)
        current = None

    for line in (body or "").splitlines():
        if FENCE_RE.match(line):
            fenced = not fenced
        if fenced or FENCE_RE.match(line):
            if current:
                current.lines.append(line)
            continue
        heading = HEADING_RE.match(line)
        if heading:
            level, text = len(heading.group(1)), heading.group(2)
            found = item_re.match(text) if item_re else None
            if found:
                close()
                # An item heading ends a sub-section of its own level ("### Ngoài phạm vi"): it belongs to the section above.
                while open_sections and open_sections[-1][0] >= level:
                    open_sections.pop()
                section = open_sections[-1][1] if open_sections else ""
                in_changelog = False
                current = _Open(found.group("id"), found.group("kind"), text[found.end() :], level, 0, section)
            elif current and current.level is not None and level > current.level:
                current.lines.append(line)  # a sub-heading inside a heading item
            else:
                close()
                section = _clean_heading(text)
                sections.append(section)
                while open_sections and open_sections[-1][0] >= level:
                    open_sections.pop()
                open_sections.append((level, section))
                in_changelog = bool(changelog) and section.casefold().startswith(changelog)
                has_changelog = has_changelog or in_changelog
            continue
        if in_changelog and line.lstrip().startswith("|"):
            has_table = True
        bullet = BULLET_RE.match(line)
        if mention_re and not in_changelog and (line.lstrip().startswith("|") or (bullet and CHECKBOX_RE.match(bullet.group(2)))):
            boxed.update(dict.fromkeys(mention_re.findall(line)))
        if bullet:
            indent, text = len(bullet.group(1).expandtabs(4)), bullet.group(2)
            found = item_re.match(text) if item_re else None
            if found:
                close()
                current = _Open(found.group("id"), found.group("kind"), text[found.end() :], None, indent, section)
                continue
            if current and current.level is None and indent <= current.indent:
                close()  # a sibling bullet that is not an item
        elif current and current.level is None:
            if not line.strip():
                current.blank = True
            elif current.blank and not line.startswith((" ", "\t")):
                close()
        if current:
            current.lines.append(line)
            current.plain.append(line)
    close()

    items = [_item(o, pack, spec.items) for o in done]
    problems: list[Problem] = []
    for required in spec.sections:
        if not any(s.casefold().startswith(required.casefold()) for s in sections):
            problems.append(Problem(code="missing_section", detail=required, message=f'Thiếu mục "{required}". Thêm heading "## {required}" (mẫu {doctype} yêu cầu mục này).'))
    version_line = bool(re.search(pack.syntax.version_line, body or ""))
    if not version_line:
        problems.append(Problem(code="no_version", message=f"Thiếu dòng phiên bản. Thêm ngay dưới tiêu đề một dòng khớp `{pack.syntax.version_line}`, ví dụ: Phiên bản: 1"))
    if changelog and not has_changelog and not any(r.casefold().startswith(changelog) for r in spec.sections):
        problems.append(Problem(code="no_changelog", message=f'Thiếu mục "{pack.syntax.changelog}". Thêm heading "## {pack.syntax.changelog}" kèm bảng ghi từng lần sửa.'))
    elif changelog and has_changelog and not has_table:
        problems.append(
            Problem(code="no_changelog", message=f'Mục "{pack.syntax.changelog}" chưa có bảng. Thêm bảng, mỗi lần sửa một dòng: | Phiên bản | Ngày | Thay đổi | Người |')
        )
    seen: set[str] = set()
    for it in items:
        if it.id in seen:
            problems.append(Problem(code="duplicate_id", item=it.id, message=f"ID {it.id} xuất hiện hơn một lần. Mỗi mục một ID riêng: đổi số của mục viết sau."))
        seen.add(it.id)
    for it in dict.fromkeys(i.id for i in items):
        kind = pack.kind_of(it)
        if kind and not pack.id_pattern(kind).fullmatch(it):
            problems.append(Problem(code="bad_id", item=it, message=f"ID {it} không đúng mẫu của loại {kind} (`{pack.id_pattern(kind).pattern}`). Đặt lại ID theo mẫu đó."))
    for it in boxed:
        if it not in seen:
            message = (
                f'{it} nằm trong bảng/checkbox: viết thành mục riêng (heading "### {it}: …" hoặc dòng danh sách "- {it}: …", '
                f"các dòng {pack.syntax.trace[0]} / {pack.syntax.source[0]} ngay dưới nó) thì máy mới đọc được; bảng chỉ để tóm tắt các mục đã có."
            )
            problems.append(Problem(code="unparsed_id", item=it, message=message))
    return ParsedDoc(doctype=doctype, version_line=version_line, sections=sections, items=items, problems=problems)


def parse_sources(text: str, pack: StandardPack | None = None, kinds: list[str] | tuple[str, ...] = ()) -> list[SourceRef]:
    """The entries of one `Nguồn:` line (separated by `;`, `, ` or " và "), each split into what it points to and where in it.

    With a pack, an entry that is an item id is a source of kind `item` (a requirement the BA stated in an answer cites
    the question: `Nguồn: Q-2`): a bare id of one of `kinds` (the kinds the document holds), or any kind of the pack
    when the ticket is written in front (`EP-4/Q-2`). Anything else that looks like `ABC-12` is a ticket key.
    """
    refs: list[SourceRef] = []
    for piece in SOURCE_SPLIT_RE.split(text or ""):
        raw = piece.strip().strip("`").strip().rstrip(".")
        if not raw:
            continue
        if pack and (m := REF_RE.fullmatch(raw)) and _is_item(pack, m.group(1), m.group(2), kinds):
            refs.append(SourceRef(raw=raw, kind="item", ref=raw))
        elif m := URL_SRC_RE.fullmatch(raw):
            refs.append(SourceRef(raw=raw, kind="url", ref=m.group(1), locator=_squeeze(m.group(2))))
        elif m := FILE_SRC_RE.fullmatch(raw):
            refs.append(SourceRef(raw=raw, kind="file", ref=m.group(2).removeprefix("./"), locator=_squeeze(m.group(3) or m.group(4) or m.group(5)), repo=m.group(1) or ""))
        elif TICKET_SRC_RE.fullmatch(raw):
            refs.append(SourceRef(raw=raw, kind="ticket", ref=raw))
        else:
            refs.append(SourceRef(raw=raw, kind="other", ref=raw))
    return refs


def _is_item(pack: StandardPack, ticket: str, target: str, kinds: list[str] | tuple[str, ...]) -> bool:
    kind = pack.kind_of(target)
    return bool(kind and (ticket or kind in kinds) and pack.id_pattern(kind).fullmatch(target))


def parse_quotes(text: str) -> list[str]:
    """The quoted pieces of one `Trích:` line (in backticks or quotation marks); the whole line when nothing is quoted."""
    found = [next(g for g in groups if g).strip() for groups in QUOTE_RE.findall(text or "")]
    return [q for q in found if q] or ([text.strip()] if (text or "").strip() else [])


def item_hash(body: str) -> str:
    return hashlib.sha256(" ".join(body.split()).encode()).hexdigest()


def _item_re(kinds: list[str]) -> re.Pattern[str] | None:
    """An id at the start of a heading or list line, loosely: a wrong number is still an item, reported as a bad id."""
    if not kinds:
        return None
    names = "|".join(re.escape(k) for k in sorted(kinds, key=len, reverse=True))
    return re.compile(rf"(?:\*\*|__)?(?P<id>(?P<kind>{names})-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)(?![\w-])(?:\*\*|__)?")


def _mention_re(kinds: list[str]) -> re.Pattern[str] | None:
    """A well-formed id of these kinds anywhere in a line; an id of another ticket (`EP-4/REQ-2`) is not this document's."""
    if not kinds:
        return None
    names = "|".join(re.escape(k) for k in sorted(kinds, key=len, reverse=True))
    return re.compile(rf"(?<![\w/-])((?:{names})-\d+(?:\.\d+)*)(?![\w-])")


def _named_lines(lines: list[str], named: dict[str, list[str]]) -> dict[str, list[str]]:
    """The lines that start with a word of `syntax.lines`, by name. The longest word wins: `Chọn bởi:` is not `Chọn:`."""
    words = sorted(((w.strip(), name) for name, ws in named.items() for w in ws if w.strip()), key=lambda pair: -len(pair[0]))
    out: dict[str, list[str]] = {}
    for line in lines:
        text = LINE_START_RE.sub("", line, count=1)
        for word, name in words:
            rest = LINE_REST_RE.match(text[len(word) :]) if text.casefold().startswith(word.casefold()) else None
            if rest:
                out.setdefault(name, []).append(rest.group(1).strip())
                break
    return out


def _keyword_re(words: list[str]) -> re.Pattern[str]:
    names = "|".join(re.escape(w.strip()) for w in words if w.strip())
    return re.compile(rf"^[ \t]*(?:[-*+][ \t]+)?(?:\*\*|__)?(?:{names})[ \t]*(?::(?:\*\*|__)?|(?:\*\*|__):)[ \t]*(.*)$", re.IGNORECASE)


def _item(o: _Open, pack: StandardPack, kinds: list[str]) -> Item:
    syntax = pack.syntax
    tags = [name for name in (pack.tag_name(t) for t in TAG_RE.findall(o.head)) if name]
    title = TAG_RE.sub(lambda m: "" if pack.tag_name(m.group(1)) else m.group(0), o.head)
    title = re.sub(r"^[\s:—–-]+", "", title).strip().removesuffix("**").strip()
    body = "\n".join([o.head.strip(), *o.lines]).strip()

    def values(words: list[str]) -> list[str]:
        pattern = _keyword_re(words)
        return [m.group(1).strip() for line in o.plain if (m := pattern.match(line))]

    def ids(words: list[str]) -> list[str]:
        found = [f"{ticket}/{item}" if ticket else item for value in values(words) for ticket, item in REF_RE.findall(value)]
        return list(dict.fromkeys(found))

    return Item(
        id=o.id,
        kind=o.kind,
        title=" ".join(title.split()),
        body=body,
        hash=item_hash(body),
        section=o.section,
        traces=ids(syntax.trace),
        sources=[ref for value in values(syntax.source) for ref in parse_sources(value, pack, kinds)],
        quotes=[quote for value in values(syntax.quote) for quote in parse_quotes(value)],
        changes=ids(syntax.changes),
        answer=" ".join(v for v in values(syntax.answer) if v),
        tags=list(dict.fromkeys(tags)),
        lines=_named_lines(o.plain, syntax.lines),
    )


def _clean_heading(text: str) -> str:
    return NUMBERING_RE.sub("", text.strip()).strip()


def _squeeze(locator: str | None) -> str:
    return (locator or "").replace(" ", "").replace("\t", "")
