"""Where everything sits in the documents repository, and how a file is written and read. No I/O.

    README.md                      the catalogue: every epic and ticket, one line each (written by the office)
    AGENTS.md                      how the repository is organised and how its files are linked, for people and AI tools
    epics/<KEY>-<slug>/            a ticket that has, or will have, tasks under it
        README.md                  the ticket: what it is, where it stands, its documents, its links (written by the office)
        analysis.md  breakdown.md  its documents, one file each: `<document key>.md`
        spec.T-1.md                the business spec of a task that is not a ticket yet
        stories/<KEY>-<slug>/      a task of the epic: README.md, spec.md, design.md
    tickets/<KEY>-<slug>/          a ticket with documents that is neither
    knowledge/
        README.md                  the catalogue of the team's knowledge
        decisions/ facts/ code-notes/ contracts/ flows/     one file per knowledge item: `<id>-<slug>.md`
        library/                   anything people add: indexed and searchable, never rewritten by the office

A file the office writes starts with a YAML block between two `---` lines (the front matter): what the file is and
which revision it holds. People edit what is under it; the block is rewritten by the office.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

import yaml

FRONT = "---"
README = "README.md"
AGENTS = "AGENTS.md"
EPICS, STORIES, TICKETS, KNOWLEDGE, LIBRARY = "epics", "stories", "tickets", "knowledge", "library"
KB_DIRS = {"decision": "decisions", "fact": "facts", "code_note": "code-notes", "contract": "contracts", "flow": "flows"}
KB_KIND_OF = {folder: kind for kind, folder in KB_DIRS.items()}
DOC_KEY_RE = re.compile(r"[A-Za-z][\w.-]{0,60}")
TEXT_SUFFIXES = (".md", ".markdown", ".txt")
NOTE = "Do văn phòng ghi. Sửa phần thân bên dưới; khối đầu này được ghi lại mỗi lần đồng bộ."
STATE_VI = {"approved": "đã duyệt", "draft": "chưa duyệt", "changed": "đã sửa sau khi duyệt"}


def slug(text: str, limit: int = 48) -> str:
    """`Hạn mức hoàn tiền/ngày` → `han-muc-hoan-tien-ngay`: ASCII, lower case, safe as a folder name on every system."""
    text = unicodedata.normalize("NFD", text.replace("đ", "d").replace("Đ", "D"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower()
    return text[:limit].rstrip("-")


def folder(key: str, title: str) -> str:
    tail = slug(title)
    return f"{key}-{tail}" if tail else key


@dataclass(frozen=True)
class Node:
    """A ticket as the tree sees it."""

    key: str
    title: str
    is_epic: bool = False  # has, or will have, tasks under it


def ticket_dir(chain: list[Node]) -> str:
    """The folder of the last ticket of `chain` (root first): tasks sit under `stories/` of the ticket above."""
    root = chain[0]
    path = f"{EPICS if root.is_epic or len(chain) > 1 else TICKETS}/{folder(root.key, root.title)}"
    for node in chain[1:]:
        path += f"/{STORIES}/{folder(node.key, node.title)}"
    return path


def doc_path(ticket_folder: str, doc_key: str) -> str:
    return f"{ticket_folder}/{doc_key}.md"


def kb_path(kind: str, item_id: int, title: str) -> str:
    return f"{KNOWLEDGE}/{KB_DIRS.get(kind, kind)}/{folder(f'{item_id:04d}', title)}.md"


def doc_key_of(name: str) -> str | None:
    """`design.md` → `design`; None for a file that cannot be a document (the README, another suffix, a strange name)."""
    if name == README or not name.endswith(".md"):
        return None
    stem = name[: -len(".md")]
    return stem if DOC_KEY_RE.fullmatch(stem) else None


def relative(from_dir: str, to_path: str) -> str:
    """The link from a file in `from_dir` to `to_path`, both relative to the repository root, with forward slashes."""
    a, b = [p for p in from_dir.split("/") if p], [p for p in to_path.split("/") if p]
    while a and b and a[0] == b[0]:
        a, b = a[1:], b[1:]
    return "/".join([".."] * len(a) + b) or "."


# ---- a file: front matter + body ---------------------------------------------------------------------------------------


def norm(body: str) -> str:
    """A body as it is compared: line ends alike, no blank lines at either end. Two texts that differ only there are
    the same document."""
    return body.replace("\r\n", "\n").replace("\r", "\n").strip("\n").rstrip()


def render(meta: dict[str, Any], body: str, *, note: str = NOTE) -> str:
    head = yaml.safe_dump({k: v for k, v in meta.items() if v not in (None, "", [], {})}, allow_unicode=True, sort_keys=False, width=1000).strip()
    return f"{FRONT}\n# {note}\n{head}\n{FRONT}\n\n{norm(body)}\n"


def split(text: str) -> tuple[dict[str, Any], str]:
    """(front matter, body). A file without a front matter block, or with one that is not valid YAML, is all body."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    if not text.startswith(FRONT + "\n"):
        return {}, text
    end = text.find(f"\n{FRONT}\n", len(FRONT))
    if end < 0:
        return {}, text
    try:
        meta = yaml.safe_load(text[len(FRONT) + 1 : end]) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, text[end + len(FRONT) + 2 :].lstrip("\n")


def body_offset(text: str) -> int:
    """How many lines of `text` come before its body (the front matter and the blank lines after it)."""
    _, body = split(text)
    lead = text.replace("\r\n", "\n").replace("\r", "\n")
    return lead[: len(lead) - len(body)].count("\n") if body and lead.endswith(body) else 0


# ---- what the office writes --------------------------------------------------------------------------------------------


@dataclass
class DocInfo:
    key: str
    revision: int
    approved_revision: int | None
    updated_by: str = ""
    updated_at: str = ""
    based_on: dict[str, int] = field(default_factory=dict)

    @property
    def state(self) -> str:
        if not self.approved_revision:
            return "draft"
        return "approved" if self.approved_revision == self.revision else "changed"


@dataclass
class LinkInfo:
    label: str  # "Phụ thuộc", "Được phụ thuộc bởi"
    key: str
    title: str
    status: str
    path: str  # folder of the other ticket, "" when it has none in the repository
    item: str = ""
    detail: str = ""


@dataclass
class TicketInfo:
    key: str
    title: str
    type: str
    status: str
    stage: str | None
    flow: str | None
    description: str
    path: str  # its folder
    team: str = ""
    project: str = ""
    parent: tuple[str, str, str] | None = None  # (key, title, folder)
    item: str = ""  # the breakdown item it came from
    people: list[str] = field(default_factory=list)
    children: list[tuple[str, str, str, str, str]] = field(default_factory=list)  # (key, title, status, stage, folder)
    docs: list[DocInfo] = field(default_factory=list)
    links: list[LinkInfo] = field(default_factory=list)
    updated_at: str = ""


def doc_file(ticket: TicketInfo, doc: DocInfo, body: str) -> str:
    meta = {
        "ticket": ticket.key, "doc": doc.key, "title": ticket.title, "revision": doc.revision, "approved_revision": doc.approved_revision,
        "state": doc.state, "based_on": doc.based_on, "updated_by": doc.updated_by, "updated_at": doc.updated_at,
    }  # fmt: skip
    return render(meta, body)


def ticket_file(t: TicketInfo) -> str:
    """The card of a ticket: front matter a tool can read, and the same as text with links a person can follow."""
    links: dict[str, list[str]] = {}
    for link in t.links:
        links.setdefault(link.label, []).append(f"{link.key}/{link.item}" if link.item else link.key)
    meta = {
        "key": t.key, "type": t.type, "title": t.title, "status": t.status, "stage": t.stage, "flow": t.flow, "team": t.team, "project": t.project,
        "parent": t.parent[0] if t.parent else None, "item": t.item, "people": t.people, "children": [c[0] for c in t.children],
        "documents": {d.key: {"revision": d.revision, "approved_revision": d.approved_revision} for d in t.docs}, "links": links,
        "updated_at": t.updated_at,
    }  # fmt: skip
    lines = [f"# {t.key} · {t.title}", ""]
    where = f"Trạng thái: {t.status}" + (f" · giai đoạn: {t.stage}" if t.stage else "") + (f" · luồng: {t.flow}" if t.flow else "")
    lines += [where, ""]
    if t.parent:
        key, title, path = t.parent
        came = f" (mục {t.item} của breakdown)" if t.item else ""
        lines += [f"Thuộc: [{key} · {title}]({relative(t.path, path)}/{README}){came}", ""]
    if t.people:
        lines += ["Người làm: " + ", ".join(t.people), ""]
    if t.description.strip():
        lines += ["## Mô tả", "", t.description.strip(), ""]
    if t.docs:
        lines += ["## Tài liệu", "", "| Tài liệu | Bản | Trạng thái | Cập nhật |", "|---|---|---|---|"]
        for d in t.docs:
            state = STATE_VI[d.state] + (f" (bản {d.approved_revision})" if d.state == "changed" else "")
            lines.append(f"| [{d.key}]({d.key}.md) | {d.revision} | {state} | {d.updated_at[:10]} {d.updated_by} |".rstrip())
        lines.append("")
    if t.children:
        lines += ["## Task", "", "| Task | Trạng thái | Giai đoạn |", "|---|---|---|"]
        for key, title, status, stage, path in t.children:
            target = f"[{key} · {title}]({relative(t.path, path)}/{README})" if path else f"{key} · {title}"
            lines.append(f"| {target} | {status} | {stage or '-'} |")
        lines.append("")
    if t.links:
        lines += ["## Liên kết", ""]
        for link in t.links:
            name = f"{link.key} · {link.title}"
            target = f"[{name}]({relative(t.path, link.path)}/{README})" if link.path else name
            extra = (f", mục {link.item}" if link.item else "") + (f" — ghi ở {link.detail}" if link.detail else "")
            lines.append(f"- {link.label}: {target} ({link.status}){extra}")
        lines.append("")
    return render(meta, "\n".join(lines), note="Do văn phòng ghi từ dữ liệu của ticket: sửa ở đây sẽ bị ghi đè. Tài liệu của ticket là các file bên cạnh.")


def kb_file(item: dict[str, Any], ticket_key: str = "") -> str:
    meta = {
        "kb": item["id"], "kind": item["kind"], "title": item["title"], "status": item["status"], "sources": item.get("sources") or [],
        "anchor": item.get("anchor"), "ticket": ticket_key, "verified_by": item.get("verified_by"), "updated_at": item.get("updated_at"),
    }  # fmt: skip
    body = f"# {item['title']}\n\n{str(item['content']).strip()}\n"
    if item.get("data"):
        body += "\n## Dữ liệu\n\n```yaml\n" + yaml.safe_dump(item["data"], allow_unicode=True, sort_keys=False, width=1000).strip() + "\n```\n"
    return render(meta, body, note="Do văn phòng ghi. Sửa tiêu đề (dòng #) và nội dung bên dưới; khối đầu này và mục 'Dữ liệu' được ghi lại.")


def kb_parts(body: str) -> tuple[str, str]:
    """(title, content) of a knowledge file's body: the first `# ` line, and what is under it up to `## Dữ liệu`."""
    title, content, seen = "", [], False
    for line in norm(body).split("\n"):
        if not seen and line.startswith("# "):
            title, seen = line[2:].strip(), True
            continue
        if line.strip() == "## Dữ liệu":
            break
        content.append(line)
    return title, "\n".join(content).strip()


def index_file(epics: list[tuple[TicketInfo, int]], others: list[TicketInfo], kb_counts: dict[str, int], library: int) -> str:
    """The catalogue at the root: what to open first."""
    lines = ["# Kho tài liệu", "", f"Cách tổ chức và quy ước liên kết: [{AGENTS}]({AGENTS}). File này do văn phòng ghi lại mỗi lần đồng bộ.", ""]
    if epics:
        lines += ["## Epic", "", "| Epic | Trạng thái | Giai đoạn | Task | Tài liệu |", "|---|---|---|---|---|"]
        for t, tasks in epics:
            docs = ", ".join(f"[{d.key}]({t.path}/{d.key}.md)" for d in t.docs if "." not in d.key) or "-"
            lines.append(f"| [{t.key} · {t.title}]({t.path}/{README}) | {t.status} | {t.stage or '-'} | {tasks} | {docs} |")
        lines.append("")
    if others:
        lines += ["## Ticket khác", "", "| Ticket | Trạng thái | Tài liệu |", "|---|---|---|"]
        for t in others:
            docs = ", ".join(f"[{d.key}]({t.path}/{d.key}.md)" for d in t.docs) or "-"
            lines.append(f"| [{t.key} · {t.title}]({t.path}/{README}) | {t.status} | {docs} |")
        lines.append("")
    lines += ["## Kiến thức", "", f"[Danh mục kiến thức]({KNOWLEDGE}/{README}): " + (", ".join(f"{n} {k}" for k, n in kb_counts.items()) or "chưa có mục nào")]
    lines.append(f"· {library} file trong `{KNOWLEDGE}/{LIBRARY}/` (tài liệu mọi người tự thêm).")
    return "\n".join(lines) + "\n"


def kb_index_file(items: list[tuple[dict[str, Any], str]], kind_titles: dict[str, str], library: list[tuple[str, str]]) -> str:
    """The catalogue of the knowledge: every item with its state, by kind; then what people added to the library."""
    lines = ["# Kiến thức của team", "", "Mỗi mục một file. `✓` = người đã xác nhận, `nháp` = AI ghi, chưa ai xác nhận, `cũ?` = code nó nói tới đã đổi.", ""]
    mark = {"verified": "✓", "draft": "nháp", "stale": "cũ?"}
    for kind, folder_name in KB_DIRS.items():
        mine = [(i, p) for i, p in items if i["kind"] == kind]
        if not mine:
            continue
        lines += [f"## {kind_titles.get(kind, kind)} (`{folder_name}/`)", ""]
        lines += [f"- [{i['title']}]({relative(KNOWLEDGE, p)}) · {mark.get(i['status'], i['status'])}" for i, p in mine]
        lines.append("")
    lines += [f"## Thư viện (`{LIBRARY}/`)", "", "Tài liệu mọi người tự thêm: quy trình, biên bản, tài liệu hệ thống… Thêm file `.md` vào đây là tìm được ngay."]
    lines += [""] + [f"- [{title or path}]({relative(KNOWLEDGE, path)})" for path, title in library]
    return "\n".join(lines).rstrip() + "\n"


def agents_file(relations: dict[str, list[str]], keywords: dict[str, str], kinds: dict[str, str]) -> str:
    """How the repository is organised: read by people, and by any AI tool that opens the folder (Kiro, Claude Code…)."""
    rel = "\n".join(f"- `{words[0]}: <ID>` ({name})" for name, words in relations.items() if words)
    kind_lines = "\n".join(f"- `{kind}-<số>`: {title}" for kind, title in kinds.items())
    return f"""# Quy ước của kho tài liệu

Kho này là bản dạng file của mọi tài liệu trong văn phòng (ActorOffice): epic, task, tài liệu của từng ticket và kiến
thức của team. Văn phòng ghi file ngay khi tài liệu đổi; bạn sửa file ở đây thì văn phòng nhận lại thành một bản sửa mới.

## Cây thư mục

- `{README}`: danh mục, mở đầu tiên.
- `{EPICS}/<MÃ>-<tên>/`: một epic. `{README}` là thẻ của ticket (trạng thái, tài liệu, liên kết); `analysis.md`,
  `breakdown.md` là tài liệu của epic; `spec.T-n.md` là nghiệp vụ của task chưa thành ticket.
- `{EPICS}/<MÃ>-<tên>/{STORIES}/<MÃ>-<tên>/`: một task của epic: `spec.md`, `design.md`.
- `{TICKETS}/<MÃ>-<tên>/`: ticket có tài liệu nhưng không thuộc epic nào.
- `{KNOWLEDGE}/`: kiến thức của team, mỗi mục một file theo loại ({", ".join(f"`{v}/`" for v in KB_DIRS.values())});
  `{KNOWLEDGE}/{LIBRARY}/` là tài liệu mọi người tự thêm.

## Được sửa gì

- Sửa được: phần thân của file tài liệu (`analysis.md`, `spec.md`, `design.md`…), tiêu đề và nội dung của file kiến thức,
  mọi thứ trong `{KNOWLEDGE}/{LIBRARY}/`. Thêm file `<tên>.md` vào thư mục của một ticket là thêm một tài liệu cho ticket đó.
- Không sửa: khối đầu file giữa hai dòng `---`, các file `{README}` và file này: văn phòng ghi đè.
- Tài liệu đã duyệt mà bị sửa thì phải được duyệt lại; văn phòng ghi rõ điều đó trên ticket.

## Mục và liên kết (máy đọc)

Một mục là một heading hoặc dòng danh sách bắt đầu bằng ID:

{kind_lines}

Ngay dưới mục, mỗi dòng một loại liên kết:

- `{keywords.get("trace", "Truy vết")}: <ID>`: mục này đáp ứng / hiện thực mục kia.
- `{keywords.get("source", "Nguồn")}: <file:dòng | mã ticket | URL>`: căn cứ, mở ra được.
- `{keywords.get("changes", "Thay đổi")}: <ID>`: cố ý làm khác hiện trạng đã ghi.
{rel}

ID của ticket khác viết kèm mã ticket: `AO-12/REQ-2`. Một ticket viết bằng mã của nó: `AO-14`.

## Tìm

- Theo mã: `grep -rn "AO-14" .` thấy mọi nơi nhắc ticket đó; `grep -rn "AO-12/REQ-2" .` thấy mọi mục truy vết tới yêu cầu đó.
- Theo ticket: mở `{README}` của nó; mục "Liên kết" dẫn tới ticket nó phụ thuộc và ticket phụ thuộc nó.
- Theo chủ đề: bắt đầu từ `{README}` ở gốc và `{KNOWLEDGE}/{README}`.
"""
