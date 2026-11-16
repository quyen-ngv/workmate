"""What the office already knows around the documents of a ticket in a pack flow, worked out by the office so the AI
does not have to find it (or miss it, or make it up):

- `reach`: what depends on the code a document says it changes (callers, endpoints, topics, tables), from the code
  graph. The check `impact_covered` and the AI cross-check both read it.
- `grounding`: whether what an item says can be found in what it cites: the names it writes in backticks, the numbers
  it states, the lines it quotes. No model is asked (check `grounded`).
- `context_block`: the "Context" part of a stage prompt: the approved spec in full when the stage writes a design,
  where the ticket sits (parent, sibling tasks), the outline of the parent's documents, what is already settled
  (answered questions, decisions), other open tickets whose documents cite the same files, and the knowledge that
  bears on it with how far each line can be trusted. What does not fit is named, with how to get it.
- `redo_block`: what a run that starts after a rejection is told besides the reviewer's note.
- `on_approved`: approved decisions, terms and facts about the code become draft items of the team's knowledge base,
  so they outlive the ticket.

Reading is deterministic: the same office state gives the same text.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from actoroffice.doccore import Evidence, Finding, ParsedDoc, parse
from actoroffice.doccore.model import Item, make_finding
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.stage import StageDef, StageRun
from actoroffice.domain.work import Document, Interaction, Ticket, TicketStatus

if TYPE_CHECKING:
    from actoroffice.app import Office
    from actoroffice.services.docs import DocSet

log = logging.getLogger(__name__)

SPEC_CAP = 6_000  # characters of an own document given inline
CONTEXT_CAP = 10_000  # characters of the block besides the inline document (about 2.5k tokens)
SIBLINGS = 12
OVERLAPS = 8
KB_LINES = 8
DEPENDENCY_LINES = 6
OUTLINE_ITEMS = 40  # items of one parent document named in its outline
SETTLED = 20
REDO_FINDINGS = 12
CHANGE_KIND = "C"  # the item kind of a document that says what will be changed in the code
DECISION_KIND = "D"
FACT_KIND = "F"
GLOSSARY = "thuật ngữ"  # the section of an analysis or a spec that fixes what words mean
IMPACT_TOOLS = ("code_impact", "code_refs")  # what the AI asks the code graph with
CALLER_DEPTH = 1  # direct callers must be dealt with by name; what is further away shows through the facts below
FACT_DEPTH = 3  # endpoints, topics and tables reached through up to this many calls
TEST_GLOBS = ("tests/*", "test/*", "*/tests/*", "*/test/*", "*/__tests__/*", "*_test.*", "*.test.*", "*.spec.*", "*/test_*", "test_*", "*Test.*", "*Tests.*")
TASK_OF = re.compile(r"\(mục ([A-Z][A-Z0-9]*-[\w.]+) của ")  # how services/docs.py `spawn` marks the breakdown item of a task
KIND_VI = {"caller": "nơi gọi", "endpoint": "endpoint", "topic": "topic", "table": "bảng"}
TRUST = {"verified": "✓ đã xác nhận", "draft": "chưa xác nhận", "stale": "có thể đã cũ"}

# ---- grounding: what counts as a name, a number, a quote -------------------------------------------------------------
G_IDENT, G_NUMBER, G_QUOTE = "G-IDENT", "G-NUMBER", "G-QUOTE"
BACKTICK = re.compile(r"`([^`\n]{2,120})`")
ITEM_ID = re.compile(r"(?:[\w-]+/)?[A-Z][A-Z0-9]*-\d+(?:\.\d+)*")
# A name of something in the code: it has a separator or mixed case (`refund_allowed`, `LimitService.check`,
# `MAX_PER_DAY`, `createRefund`, `src/shop/limits.py`, `/v1/refunds/{id}`, `shop.refund`), or it is a call (`check()`).
# A plain word (`true`, `refund`) says too little to be held against the author.
NAME = re.compile(r"(?:(?:GET|POST|PUT|PATCH|DELETE) )?[\w$./{}:<>*-]+(?:\(\))?")
UNITS = (
    "lần|ngày|giờ|phút|giây|tuần|tháng|năm|ms|s|sec|seconds?|minutes?|hours?|days?|times|%|kb|mb|gb|bytes?|ký tự|kí tự|characters?|bản ghi|records?|rows?|"
    "items?|mục|phần tử|requests?|yêu cầu|đ|vnd|vnđ|usd"
)
NUMBER_UNIT = re.compile(rf"(?<![\w.,/:-])(\d+(?:[.,]\d+)?)[ \t]?(?:{UNITS})(?![\w])", re.IGNORECASE)
NUMBER_CMP = re.compile(r"(?:>=|<=|≥|≤|>|<|==|!=)[ \t]?(\d+(?:[.,]\d+)?)(?![\w.])")
QUOTE_MARKS = "\"'“”‘’`«» "


@dataclass
class Reached:
    """One thing that depends on code a document changes."""

    kind: str  # caller | endpoint | topic | table
    name: str
    qualified: str
    path: str
    cite: str
    items: list[str] = field(default_factory=list)  # the items of the document whose cited code reaches it

    def mentioned_in(self, text: str) -> bool:
        """The document names it: by its name, its qualified name, the file it is in, or (an endpoint) its path."""
        low = text.casefold()
        names = {self.name, self.qualified, self.path.rsplit("/", 1)[-1]}
        if self.kind == "endpoint":
            names.add(self.name.split(" ", 1)[-1])
        return any(len(n) >= 3 and n.casefold() in low for n in names)

    def line(self) -> str:
        return f"{self.kind} {self.qualified or self.name}" + (f" ({self.cite})" if self.cite else "")


@dataclass
class Reach:
    found: list[Reached] = field(default_factory=list)
    targets: list[str] = field(default_factory=list)  # cited places the code graph knows
    unknown: list[str] = field(default_factory=list)  # cited places it has no symbol for (a constant, a config file)
    skipped: str = ""  # why nothing could be looked up (Vietnamese); empty when the graph was read


@dataclass
class Grounding:
    findings: list[Finding] = field(default_factory=list)
    checked: int = 0  # items that have a pinned source and were looked at
    notes: list[str] = field(default_factory=list)  # what could not be checked, and why (Vietnamese)


def is_test(path: str) -> bool:
    return any(fnmatch.fnmatch(path, g) for g in TEST_GLOBS)


def squash(text: str) -> str:
    return " ".join(text.split())


def is_name(token: str) -> bool:
    """A backticked token that names something in the code (see NAME)."""
    t = token.strip()
    if not NAME.fullmatch(t) or ITEM_ID.fullmatch(t) or t.replace(".", "").replace(",", "").isdigit():
        return False
    core = t.removesuffix("()")
    mixed_case = core != core.lower() and core != core.upper() and not core.istitle()
    return t.endswith("()") or any(c in core for c in "_./") or mixed_case


def name_forms(token: str) -> list[str]:
    """What to look for: the token as written, without `()`, and the last part of a dotted or `::` name."""
    core = token.strip().removesuffix("()")
    tail = re.split(r"[.:]+", core)[-1] if "/" not in core else core
    return list(dict.fromkeys(f for f in (core, tail) if len(f) >= 2))


def numbers_of(text: str) -> list[tuple[str, str]]:
    """(the number, how it is written) for every number with a unit or a comparison in a text."""
    found = [(m.group(1), m.group(0).strip()) for m in NUMBER_UNIT.finditer(text)] + [(m.group(1), m.group(0).strip()) for m in NUMBER_CMP.finditer(text)]
    return list(dict.fromkeys(found))


def has_number(number: str, text: str) -> bool:
    return re.search(rf"(?<![\w.,-]){re.escape(number)}(?![\w]|[.,]\d)", text) is not None


def prose_of(body: str) -> str:
    """A document without the numbers that only number its parts (`## 7. Change log`, `3. bước ba`)."""
    return "\n".join(re.sub(r"^\s*(?:#+\s*)?\d+(?:\.\d+)*[.)]\s+", "", line) for line in body.splitlines())


def glossary(body: str) -> list[tuple[str, str]]:
    """(term, meaning) of the "Thuật ngữ" section of a document: the rows of its table, or `- term: meaning` lines."""
    out: list[tuple[str, str]] = []
    inside, level = False, 0
    for line in body.splitlines():
        head = re.match(r"^(#+)\s*(?:\d+(?:\.\d+)*\.?\s*)?(.*)$", line)
        if head:
            if inside and len(head.group(1)) <= level:
                break
            if not inside and head.group(2).strip().casefold().startswith(GLOSSARY):
                inside, level = True, len(head.group(1))
            continue
        if not inside:
            continue
        row = [c.strip().strip("*_`") for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else None
        if row is not None:
            if len(row) >= 2 and row[0] and row[1] and not set(row[0]) <= set("-: ") and row[0].casefold() != GLOSSARY:
                out.append((row[0], row[1]))
            continue
        bullet = re.match(r"^\s*[-*+]\s+(?:\*\*|__)?(.+?)(?:\*\*|__)?\s*(?::|—|–)\s+(.+)$", line)
        if bullet:
            out.append((bullet.group(1).strip(), bullet.group(2).strip()))
    return [(t, m) for t, m in out if not set(t) <= set("….") and not set(m) <= set("….")]


def capped(lines: list[str], limit: int, more: str) -> list[str]:
    """At most `limit` lines; what is left out is counted and `more` says how to get it. Never cut without saying."""
    if len(lines) <= limit:
        return lines
    return lines[:limit] + [f"- … (còn {len(lines) - limit} mục: {more})"]


class DocContext:
    def __init__(self, office: Office) -> None:
        self.office = office

    # ---- what depends on the code a document changes ---------------------------------------------------------

    async def reach(self, ticket: Ticket, evidence: list[Evidence]) -> Reach:
        """What the code graph says depends on the places these source lines cite: direct callers (tests left out:
        the build stage deals with them), and the endpoints, topics and tables reached from there."""
        office = self.office
        out = Reach()
        repos = await office.codeintel.for_ticket(ticket)
        if not repos:
            out.skipped = "ticket không có repo nào để lập chỉ mục"
            return out
        try:
            states = {s.repo: s for s in await office.codeintel.ensure(repos)}
            found: dict[tuple[str, str, str], Reached] = {}
            seen: set[tuple[str, str, str]] = set()
            for e in evidence:
                slug, _, path = e.ref.partition(":")
                target = path + (e.locator if e.locator.startswith(":") else "")
                if (slug, target, e.item) in seen:
                    continue
                seen.add((slug, target, e.item))
                state = states.get(slug)
                if state is None or state.state == "error" or state.level == "text":
                    why = "không có chỉ mục" if state is None else f"chỉ mục lỗi ({state.error})" if state.state == "error" else "chỉ mục ở mức chữ, chưa có đồ thị gọi"
                    out.unknown.append(f"{e.ref}{e.locator} ({why})")
                    continue
                near = office.codeintel.query.impact([target], [slug], depth=CALLER_DEPTH)
                if near.unknown or not near.targets:
                    out.unknown.append(f"{e.ref}{e.locator}")
                    continue
                if f"{e.ref}{e.locator}" not in out.targets:
                    out.targets.append(f"{e.ref}{e.locator}")
                far = office.codeintel.query.impact([target], [slug], depth=FACT_DEPTH)
                groups = (("caller", near.callers), ("endpoint", far.endpoints), ("topic", far.topics), ("table", far.tables))
                for kind, nodes in groups:
                    for n in nodes:
                        if kind == "caller" and is_test(n.path):
                            continue
                        key = (kind, n.qualified or n.name, n.path)
                        hit = found.setdefault(
                            key, Reached(kind, n.name, n.qualified, n.path, f"{n.repo}:{n.path}" + (f":{n.line}" if n.line else "") if n.path else "")
                        )
                        if e.item not in hit.items:
                            hit.items.append(e.item)
            out.found = [found[k] for k in sorted(found)]
        except Exception as exc:  # the index is a help, never a reason for a check to crash: say why it was not read
            out.skipped = f"không đọc được đồ thị code: {type(exc).__name__}: {office.redact(str(exc))[:200]}"
            return out
        if not out.targets:
            out.skipped = "chỉ mục code không có ký hiệu nào ở chỗ được trích: " + ", ".join(out.unknown[:6])
        return out

    # ---- grounding -------------------------------------------------------------------------------------------

    async def _in_index(self, forms: list[str], slugs: list[str], files: set[str]) -> bool:
        """The code index knows the name: a symbol, a file, or a piece of code that holds it as written."""
        query = self.office.codeintel.query
        for form in forms:
            if "/" in form and any(f == form.lstrip("/") or f.endswith("/" + form.lstrip("/")) for f in files):
                return True
            if any(form in (s.name, s.qualified) or s.qualified.endswith("." + form) for s in query.symbol(form, slugs, limit=5)):
                return True
            if any(form in hit.text for hit in query.search(form, slugs, limit=5)):
                return True
        return False

    async def _source_text(self, ticket: Ticket, e: Evidence, cache: dict[str, str]) -> tuple[str, bool]:
        """(the text of a cited source, whether it is the whole source as it is now). Code is read whole from the
        repository; anything else is the excerpt that was pinned."""
        if e.category != "code":
            return e.excerpt, False
        if e.ref not in cache:
            slug, _, path = e.ref.partition(":")
            text = ""
            for repo in await self.office.codeintel.for_ticket(ticket):
                if repo.slug != slug:
                    continue
                try:
                    got = await self.office.codeintel.read(repo, path)
                    text = "" if got["truncated"] else got["text"]
                except OfficeError:
                    text = ""
            cache[e.ref] = text
        return (cache[e.ref], True) if cache[e.ref] else (e.excerpt, False)

    async def grounding(self, ticket: Ticket, stage: StageDef) -> Grounding:
        """For every item of the stage's documents that has a pinned source: the names it writes in backticks exist
        in what it cites, in the documents it traces to, or in the code index; the numbers it states (with a unit or
        a comparison, when it cites code) are in what it cites or traces to; the lines it quotes are in its sources
        word for word. What cannot be looked up is said in `notes`."""
        office = self.office
        out = Grounding()
        ds = await office.docs.docset(ticket)
        keys = sorted(office.docs.output_keys(ticket, stage, ds))
        evidence = [e for e in await office.docs.evidence(ticket, ds) if e.doc in keys and not e.error]
        pack = office.docs.pack_of(ticket)
        new_word = pack.syntax.tags.get("new", "")
        keywords = [w.casefold() for ws in (pack.syntax.trace, pack.syntax.source, pack.syntax.changes, pack.syntax.answer) for w in ws]
        slugs: list[str] = []
        files: set[str] = set()
        try:
            repos = await office.codeintel.for_ticket(ticket)
            states = await office.codeintel.ensure(repos) if repos else []
            slugs = [s.repo for s in states if s.state != "error"]
            for slug in slugs:
                files |= set(office.codeintel.query.files(slug))
            if not slugs:
                out.notes.append("không có chỉ mục code: tên không thấy trong nguồn đã trích thì không kết luận được")
        except Exception as exc:
            slugs = []
            out.notes.append(f"không đọc được chỉ mục code ({type(exc).__name__}): tên không thấy trong nguồn đã trích thì không kết luận được")
        bodies = {k: await self._body_of(ds, k) for k in ds.docs}
        unjudged: list[str] = []
        excerpt_only: list[str] = []
        unsourced = 0
        cache: dict[str, str] = {}
        for key in keys:
            for item in ds.docs[key].items:
                cited = [e for e in evidence if e.doc == key and e.item == item.id]
                quotes = [squash(q.strip(QUOTE_MARKS)) for q in self._quotes(item) if q.strip(QUOTE_MARKS)]
                if not cited:
                    unsourced += 1 if (quotes or BACKTICK.search(item.body)) else 0
                    continue
                out.checked += 1
                excerpts = "\n".join(e.excerpt for e in cited)
                traced = "\n".join(prose_of(bodies[k]) for k in self._traced(ds, key, item) if k in bodies)
                prose = "\n".join(line for line in item.body.splitlines() if not any(line.strip().lstrip("-*+ ").casefold().startswith(w) for w in keywords))
                # (a) names
                for m in BACKTICK.finditer(prose):
                    token = m.group(1).strip()
                    if not is_name(token):
                        continue
                    after = prose[m.end() : m.end() + 16].casefold()
                    if "new" in item.tags or (new_word and new_word.casefold() in after):
                        continue  # the item, or the text right after the name, says it does not exist yet
                    forms = name_forms(token)
                    if any(f in excerpts or f in traced for f in forms):
                        continue
                    if not slugs:
                        unjudged.append(f"{item.id} `{token}`")
                    elif not await self._in_index(forms, slugs, files):
                        msg = (
                            f"`{token}` không có trong nguồn đã trích lẫn trong chỉ mục code: bỏ, sửa cho đúng tên, hoặc trích nguồn có nó"
                            + (f" (tên sẽ tạo mới thì ghi [{new_word}] ngay sau nó)" if new_word else "")
                        )
                        out.findings.append(make_finding(G_IDENT, "error", key, item.id, msg, token))
                # (b) numbers, when the item says something about code
                if any(e.category == "code" for e in cited):
                    for number, written in numbers_of(prose):
                        if not has_number(number, excerpts) and not has_number(number, traced):
                            msg = f'"{written}" không thấy trong đoạn nguồn đã trích của {item.id} hay trong tài liệu nó truy vết tới: kiểm lại con số, hoặc trích nguồn có nó'
                            out.findings.append(make_finding(G_NUMBER, "warn", key, item.id, msg, written))
                # (c) quotes
                for quote in quotes:
                    texts = [await self._source_text(ticket, e, cache) for e in cited]
                    if any(quote in squash(text) for text, _ in texts):
                        continue
                    if not all(whole for _, whole in texts):
                        excerpt_only.append(item.id)
                    msg = f'trích dẫn không có trong nguồn: "{quote[:160]}" không nằm trong {", ".join(e.source for e in cited)}. Chép đúng nguyên văn từ nguồn, hoặc bỏ dòng trích'
                    out.findings.append(make_finding(G_QUOTE, "error", key, item.id, msg, quote[:80]))
        if unjudged:
            out.notes.append(f"{len(unjudged)} tên không kiểm được vì không có chỉ mục code: {', '.join(unjudged[:8])}")
        if excerpt_only:
            out.notes.append(f"dòng trích của {', '.join(sorted(set(excerpt_only)))} chỉ so được với đoạn đã ghim (không đọc được toàn văn nguồn)")
        if unsourced:
            out.notes.append(f"{unsourced} mục có tên trong dấu ` hoặc dòng trích nhưng không có nguồn ghim nên không kiểm")
        return out

    @staticmethod
    def _quotes(item: Item) -> list[str]:
        """The lines an item quotes from its sources (`Trích:`), however the pack's parser hands them over."""
        found = getattr(item, "quotes", None) or []
        lines = getattr(item, "lines", None) or {}
        return [str(q) for q in [*found, *(lines.get("quote") or []), *(lines.get("quotes") or [])] if str(q).strip()]

    @staticmethod
    def _traced(ds: DocSet, key: str, item: Item) -> list[str]:
        """The documents an item traces to (or says it changes): the ones that hold an item of that id."""
        out = []
        for ref in [*item.traces, *item.changes]:
            ticket_key, _, item_id = ref.rpartition("/")
            for other, doc in ds.docs.items():
                if other != key and doc.item(item_id) is not None and (not ticket_key or other.startswith(ticket_key + "/") or other.startswith(ticket_key + ".")):
                    out.append(other)
        return list(dict.fromkeys(out))

    async def _body_of(self, ds: DocSet, key: str) -> str:
        """A document of the set as the checks read it: the ticket's own as it is, its parent's at the approved revision."""
        office = self.office
        if key in ds.own:
            return ds.own[key].body
        ticket_id, stored_key = ds.stored[key]
        doc = await office.tickets.get_document(ticket_id, stored_key)
        if doc is None:
            return ""
        return await office.docs._body(ticket_id, stored_key, doc.approved_revision) if doc.approved_revision else doc.body

    # ---- the Context block of a stage prompt -----------------------------------------------------------------

    def _outputs(self, stage: StageDef) -> set[str]:
        return {o for out in stage.outputs for o in out.split("|")}

    async def _approved_or_current(self, doc: Document) -> str:
        return await self.office.docs._body(doc.ticket_id, doc.key, doc.approved_revision) if doc.approved_revision else doc.body

    async def _inline(self, ticket: Ticket, stage: StageDef) -> list[tuple[Document, str, bool]]:
        """(document, the text given, whether it was cut) for the ticket's own documents the stage's outputs are based
        on: the approved spec of a task whose design is being written."""
        office = self.office
        if not office.docs.applies(ticket):
            return []
        pack = office.docs.pack_of(ticket)
        outputs = self._outputs(stage)
        wanted = [b for out in sorted(outputs) if out in pack.doc_types for b in pack.doc_types[out].based_on if b not in outputs]
        found = []
        for name in dict.fromkeys(wanted):
            doc = await office.tickets.get_document(ticket.id, name)
            if doc is None:
                continue
            body = await self._approved_or_current(doc)
            found.append((doc, body[:SPEC_CAP], len(body) > SPEC_CAP))
        return found

    async def inlined(self, ticket: Ticket, stage: StageDef) -> set[tuple[int, str]]:
        """(ticket id, document key) of the documents the stage prompt holds in full."""
        return {(ticket.id, doc.key) for doc, _, cut in await self._inline(ticket, stage) if not cut}

    @staticmethod
    def _revisions(doc: Document) -> str:
        if doc.approved_revision == doc.revision:
            return f"`{doc.key}` rev {doc.revision} (approved)"
        return f"`{doc.key}` rev {doc.revision} ({f'approved rev {doc.approved_revision}' if doc.approved_revision else 'not approved'})"

    async def _cited(self, ticket_ids: list[int]) -> dict[str, list[tuple[int, str, str]]]:
        """Code files the documents of these tickets cite → (ticket id, document key, item id) that cite them."""
        if not ticket_ids:
            return {}
        rows = await self.office.db.fetchall(
            f"SELECT DISTINCT ticket_id, doc_key, item_id, ref FROM doc_evidence WHERE category = 'code' AND error IS NULL "
            f"AND ticket_id IN ({','.join('?' * len(ticket_ids))}) ORDER BY ref, ticket_id, doc_key, item_id",
            tuple(ticket_ids),
        )
        out: dict[str, list[tuple[int, str, str]]] = {}
        for r in rows:
            out.setdefault(str(r["ref"]), []).append((int(r["ticket_id"]), str(r["doc_key"]), str(r["item_id"])))
        return out

    @staticmethod
    def _outline(doc: ParsedDoc, how: str) -> list[str]:
        """The headings of a document and, under each, its items with their titles: enough to ask for the right part."""
        by_section: dict[str, list[str]] = {}
        for item in doc.items[:OUTLINE_ITEMS]:
            by_section.setdefault(item.section, []).append(f"{item.id} {squash(item.title)[:70]}".strip())
        lines = [f"- {section}" + (": " + "; ".join(by_section[section]) if section in by_section else "") for section in doc.sections]
        lines += [f"- {section or '(no heading)'}: " + "; ".join(items) for section, items in by_section.items() if section not in doc.sections]
        if len(doc.items) > OUTLINE_ITEMS:
            lines.append(f"- … (còn {len(doc.items) - OUTLINE_ITEMS} mục: {how})")
        return lines

    def _settled(self, owner: str, doc: ParsedDoc, question_kinds: set[str]) -> list[str]:
        """Answered questions and decisions of a document: what a new session must not ask again or go against."""
        out = []
        for item in doc.items:
            if item.kind in question_kinds and item.answer.strip():
                out.append(f"- {owner}{item.id}: {squash(item.title)[:140]} → {squash(item.answer)[:240]}")
            elif item.kind == DECISION_KIND:
                choice = "; ".join((getattr(item, "lines", None) or {}).get("choice") or [])
                out.append(f"- {owner}{item.id} (decision): {squash(item.title)[:140]}" + (f" → {squash(choice)[:240]}" if choice else ""))
        return out

    async def _knowledge(self, ticket: Ticket, paths: set[str], kb: list[Any]) -> list[str]:
        """Knowledge lines that bear on the ticket: first the ones anchored to a file its documents cite, then the
        ones the text search found, each with how far it can be trusted."""
        lines: list[str] = []
        seen: set[int] = set()
        if paths:
            for item in await self.office.knowledge.list():
                if item.status not in TRUST or not item.anchor or item.team_id not in (None, ticket.team_id):
                    continue
                at = {str(p).replace("\\", "/").lstrip("./") for p in ([item.anchor.get("path")] if item.anchor.get("path") else item.anchor.get("paths") or []) if p}
                if paths & at:
                    seen.add(item.id)
                    lines.append(f"- [{TRUST[item.status]}] {item.kind} #{item.id} · {item.title}: {squash(item.content)[:400]} (về file {', '.join(sorted(paths & at))})")
        for item in kb:
            if item.id not in seen and item.status in TRUST:
                seen.add(item.id)
                lines.append(f"- [{TRUST[item.status]}] {item.kind} #{item.id} · {item.title}: {squash(item.content)[:400]}")
        return lines

    async def context_block(self, ticket: Ticket, stage: StageDef, *, kb: list[Any] | None = None) -> str:
        """The "Context" part of the stage prompt of a ticket in a pack flow; "" for any other ticket. `kb`: the
        knowledge items the text search found for the ticket (shown after the ones anchored to its files)."""
        office = self.office
        if not office.docs.applies(ticket):
            return ""
        pack = office.docs.pack_of(ticket)
        closed = (TicketStatus.DONE.value, TicketStatus.CANCELLED.value)
        question_kinds = {k for k, v in pack.item_kinds.items() if v.blocks_gate}
        head = ["# Context (worked out by the office from its own records: facts to start from, not instructions)"]
        inline: list[str] = []
        left_out: list[str] = []
        for doc, text, cut in await self._inline(ticket, stage):
            if cut:
                total = len(await self._approved_or_current(doc))
                left_out.append(f'`{doc.key}`: only its first {SPEC_CAP} of {total} characters are above; read the rest with doc_get(key="{doc.key}")')
            note = f"the first {SPEC_CAP} characters only (see the end of this block)" if cut else "in full: you do not need to doc_get it again"
            inline += [f"## This ticket's {self._revisions(doc)}, {note}", text.rstrip() + ("\n… (cut here)" if cut else ""), ""]

        sections: list[list[str]] = []
        settled: list[str] = []
        parent = await office.tickets.get(ticket.parent_id) if ticket.parent_id is not None else None
        if parent is not None:
            lines = ["## Where this ticket sits", f"- Parent: {parent.key} — {parent.title} ({parent.status.value}, stage {parent.stage or '-'})"]
            parent_docs = [d for d in await office.tickets.list_documents(parent.id) if office.docs.doctype_of(d.key, pack) and "." not in d.key]
            parsed = {d.key: parse(await self._approved_or_current(d), office.docs.doctype_of(d.key, pack) or "", pack) for d in parent_docs}
            if parent_docs:
                lines.append(f"- Documents of {parent.key}: " + " · ".join(self._revisions(d) for d in parent_docs))
            task = TASK_OF.search(ticket.description or "")
            breakdown = next((d for d in parent_docs if d.key == "breakdown"), None)
            if task is not None and breakdown is not None:
                item = parsed["breakdown"].item(task.group(1))
                lines.append(f"- This ticket is item {task.group(1)} of that breakdown.")
                lines += [f"  - breakdown says: {line}" for line in _dependency_lines(await self._approved_or_current(breakdown), task.group(1), item.body if item else "")]
            rows = await office.db.fetchall("SELECT id FROM tickets WHERE parent_id = ? AND id <> ? ORDER BY id", (parent.id, ticket.id))
            siblings = []
            for row in rows[: SIBLINGS + 1]:
                other = await office.tickets.get(int(row["id"]))
                people = [m.user.name for m in await office.teams.participants(other.id) if "dev" in m.roles]
                docs = [self._revisions(d) for d in await office.tickets.list_documents(other.id) if office.docs.doctype_of(d.key, pack)]
                siblings.append(
                    f"  - {other.key} · {other.title} · {other.status.value}, stage {other.stage or '-'} · "
                    f"{'assigned to ' + ', '.join(people) if people else 'not assigned'} · documents: {', '.join(docs) or 'none'}"
                )
            if siblings:
                lines.append(f"- Sibling tasks ({len(rows)}):")
                lines += siblings[:SIBLINGS]
                if len(rows) > SIBLINGS:
                    lines.append(f"  - … (còn {len(rows) - SIBLINGS} task: ticket_get {parent.key} liệt kê đủ)")
            sections.append(lines)
            for d in parent_docs:
                how = f'doc_get(ticket="{parent.key}", key="{d.key}")'
                sections.append([f"## Outline of {parent.key} {self._revisions(d)}: headings and items only; the text is in {how}"] + self._outline(parsed[d.key], how))
                settled += self._settled(f"{parent.key}/", parsed[d.key], question_kinds)

        for doc in await office.tickets.list_documents(ticket.id):  # what earlier approved revisions of its own documents settled
            doctype = office.docs.doctype_of(doc.key, pack)
            if doctype is not None and doc.approved_revision and "." not in doc.key:
                settled += self._settled("", parse(await office.docs._body(ticket.id, doc.key, doc.approved_revision), doctype, pack), question_kinds)
        if settled:
            more = "the documents above hold the rest"
            sections.append(["## Điều đã chốt (settled by people: do not ask it again, do not go against it; to change one, ask with ask_human)"] + capped(settled, SETTLED, more))

        own = await self._cited([ticket.id])
        if own:
            where, params = ["t.id <> ?", "t.status NOT IN (?, ?)"], [ticket.id, *closed]
            scope: list[str] = []
            scope_params: list[Any] = []
            if parent is not None:
                scope.append("t.parent_id = ?")
                scope_params.append(parent.id)
                where.append("t.id <> ?")
                params.append(parent.id)
            if ticket.project_id:
                scope.append("t.project_id = ?")
                scope_params.append(ticket.project_id)
            if scope:
                rows = await office.db.fetchall(
                    f"SELECT id FROM tickets t WHERE {' AND '.join(where)} AND ({' OR '.join(scope)}) ORDER BY id", (*params, *scope_params)
                )
                others = await self._cited([int(r["id"]) for r in rows])
                overlap = []
                for ref in sorted(set(own) & set(others)):
                    for ticket_id, doc_key in sorted({(t, d) for t, d, _ in others[ref]}):
                        items = sorted({i for t, d, i in others[ref] if (t, d) == (ticket_id, doc_key)})
                        overlap.append(f"- {(await office.tickets.get(ticket_id)).key} `{doc_key}` ({', '.join(items)}) also cites {ref}")
                if overlap:
                    how = 'doc_get(ticket="<key>", key="<document>") của từng ticket'
                    sections.append(["## Other open tickets whose documents cite the same files as yours: read theirs before changing the same place"] + capped(overlap, OVERLAPS, how))

        cited = set(own) | set(await self._cited([parent.id])) if parent is not None else set(own)
        known = await self._knowledge(ticket, {ref.split(":", 1)[-1] for ref in cited}, kb or [])
        if known:
            title = "## Team knowledge that bears on this ticket. A knowledge line is a pointer, not a source: open the code or document it names before relying on it"
            sections.append([title] + capped(known, KB_LINES, "kb_search / memory_search với tên file hoặc từ khoá"))

        kept: list[str] = []
        size = 0
        for lines in sections:  # a section that does not fit is left out whole, and said so: never half a list
            text = "\n".join(lines)
            if size + len(text) > CONTEXT_CAP and kept:
                left_out.append(f"{lines[0].lstrip('# ').split(':')[0]}: left out (no room); ask with ticket_get / doc_get")
                continue
            kept.append(text)
            size += len(text)
        if not inline and not kept:
            return ""
        tail = ["## Not in this block: fetch it yourself"] + [f"- {line}" for line in left_out]
        tail.append("- The text of every document that is only outlined or listed above: the doc_get call named next to it.")
        tail.append("- The code: nothing here replaces reading it (code_pack, code_search, code_read, code_impact).")
        return "\n".join(head + inline + ["\n\n".join(kept), ""] + tail).strip()

    # ---- after a rejection -----------------------------------------------------------------------------------

    async def _comments(self, ticket: Ticket, reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The approvers' remarks on single items: the ones that came with the rejection, else the ones still open on
        the ticket's documents. None when the office keeps no such remarks."""
        found = [c for r in reasons for c in (r.get("comments") or []) if isinstance(c, dict)]
        if found:
            return found
        read = getattr(self.office.docs, "comments", None)
        try:
            return [c for c in (await read(ticket) if read is not None else []) if isinstance(c, dict)]
        except OfficeError:
            return []

    async def redo_block(self, ticket: Ticket, stage: StageDef, stage_run: StageRun, reasons: list[dict[str, Any]]) -> str:
        """What a run that starts after the gate sent the stage back needs besides the reviewer's note; "" when the
        ticket is not in a pack flow or the stage has no document of the pack."""
        office = self.office
        if not office.docs.applies(ticket):
            return ""
        ds = await office.docs.docset(ticket)
        keys = sorted(office.docs.output_keys(ticket, stage, ds))
        if not keys:
            return ""
        lines = ["Revise, do not start over:"]
        lines.append("- The documents of this stage as they are now: " + " · ".join(self._revisions(ds.own[k]) for k in keys) + ". Open each with doc_get first.")
        lines.append(
            "- Change what the note and the findings below ask for and leave the rest as it is. Keep every item id (never renumber; strike an "
            "item that no longer holds), raise `Phiên bản` by one and add a Change log row saying what changed and why."
        )
        lines.append(
            "- In the stage_complete summary answer the reviewer's note point by point: for each point, what you changed (document and item id) "
            "or why you did not."
        )
        comments = await self._comments(ticket, reasons)
        if comments:
            lines.append("Reviewer's comments on single items (answer each one too):")
            lines += [f"- {c.get('doc') or '?'}{' · ' + str(c['item']) if c.get('item') else ''}: {str(c.get('text') or '').strip()[:600]}" for c in comments[:20]]
            if len(comments) > 20:
                lines.append(f"- … (còn {len(comments) - 20} mục: xem trên ticket)")
        still: list[Finding] = [f for f in await office.docs.check(ticket, stage) if f.status == "open"]
        still += [f for f in await office.crosscheck.previous(ticket, stage_run) if f.status == "open"]
        if still:
            lines.append("Findings that were open when the stage was sent back (the checks will look at each again):")
            lines += [f"- [{f.id[:8]}] {f.rule} · {f.doc}{' · ' + f.item if f.item else ''}: {f.message[:400]}" for f in still[:REDO_FINDINGS]]
            if len(still) > REDO_FINDINGS:
                lines.append(f"- … (còn {len(still) - REDO_FINDINGS} mục: doc_check liệt kê các điểm của luật)")
        return "\n".join(lines)

    # ---- memory that outlives the ticket ---------------------------------------------------------------------

    async def on_approved(self, ticket: Ticket, item: Interaction) -> list[int]:
        """A gate approved these documents: what they settle goes to the team's knowledge base as draft items of this
        ticket (the knowledge gate at close confirms them; an item with the same title is updated, not doubled):
        each decision of a design, each term of an analysis or a spec, each fact about the code with its source (anchored
        to the file and commit, so it is marked stale when that file changes). Returns the ids of the items."""
        office = self.office
        if not office.docs.applies(ticket):
            return []
        ticket = await office.tickets.get(ticket.id)
        approved = {str(d.get("key")): d.get("revision") for d in item.payload.get("docs", []) if isinstance(d, dict)}
        ds = await office.docs.docset(ticket)
        evidence = [e for e in await office.docs.evidence(ticket, ds) if e.category == "code" and not e.error]
        saved: list[int] = []

        async def note(**fields: Any) -> None:
            try:
                kb = await office.knowledge.note(by="office", team_id=ticket.team_id, project_id=ticket.project_id, ticket_id=ticket.id, **fields)
                saved.append(kb.id)
            except OfficeError as exc:  # one item that cannot be kept is no reason to lose the others
                log.warning("knowledge of %s not kept (%s): %s", ticket.key, fields.get("title"), exc)

        for key, doc in sorted(ds.own.items()):
            if doc.key not in approved:
                continue
            parsed = ds.docs[key]
            where = f"{ticket.key}/{doc.key} bản {approved[doc.key] or doc.revision}"
            paths = sorted({e.ref.split(":", 1)[-1] for e in evidence if e.doc == key})
            for term, meaning in glossary(doc.body):
                await note(kind="fact", title=f"Thuật ngữ: {term}"[:200], content=meaning, sources=[where])
            for it in parsed.items:
                text = "\n".join(line for line in it.body.splitlines()[1:] if line.strip()).strip()
                if it.kind == DECISION_KIND:
                    choice = "; ".join((getattr(it, "lines", None) or {}).get("choice") or [])
                    content = (f"Chọn: {choice}\n" if choice else "") + text
                    await note(kind="decision", title=squash(it.title)[:200], content=content or it.title, sources=[f"{where} {it.id}"], anchor={"paths": paths} if paths else None)
                elif it.kind == FACT_KIND:
                    for e in [e for e in evidence if e.doc == key and e.item == it.id][:1]:
                        path = e.ref.split(":", 1)[-1]
                        await note(
                            kind="code_note", title=squash(it.title)[:200], content=text or it.title, sources=[f"{where} {it.id}", f"{e.ref}{e.locator}@{e.version[:10]}"],
                            anchor={"path": path, "commit": e.version, "lines": e.locator.lstrip(":")},
                        )  # fmt: skip
        return saved


def _dependency_lines(breakdown: str, task_id: str, own_body: str = "") -> list[str]:
    """Lines of a breakdown that relate this task to another one: a line of its own item that names another item of
    the same kind, and a line anywhere else that names both."""
    kind = task_id.split("-", 1)[0]
    this = re.compile(rf"(?<![\w-]){re.escape(task_id)}(?![\w-]|\.\d)")
    other = re.compile(rf"(?<![\w-]){re.escape(kind)}-\d[\w.]*")

    def names_another(line: str) -> bool:
        return any(m.group(0).rstrip(".") != task_id for m in other.finditer(line))

    out = [line.strip()[:300] for line in own_body.splitlines()[1:] if names_another(line)]
    for raw in breakdown.splitlines():
        line = raw.strip()
        if not line.startswith("#") and this.search(line) and names_another(line) and line[:300] not in out:
            out.append(line[:300])
    return out[:DEPENDENCY_LINES]
