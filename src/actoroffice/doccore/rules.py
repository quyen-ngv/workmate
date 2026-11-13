"""The rules a machine can check: eight primitive types here, the concrete rules in the pack.

    run_rules(pack, docs, evidence=None, baseline=None, approved=None, only_docs=None) -> list[Finding]

- `docs`: document key → ParsedDoc, the document(s) being checked plus the ones they are written from, each at the
  revision it is pinned to. A key is `"spec"` or `"EP-4/analysis"`; a selector of the pack (`spec.AC`) picks items by
  the document's `doctype`, so several documents of one type (the specs of an epic) are all looked at.
- `evidence`: one record per pinned `Nguồn:` (matched on document key, item id and the source as written). A record
  with `error` is a source that could not be resolved; `current_hash` is the hash at the source now. None = sources
  were not fetched: only "has a source" is checked. A source without a record is taken as not fetched yet.
- `baseline`: document key → what that document was written from.
- `approved`: document key → its latest approved revision and what changed since each pinned one.
- `only_docs`: keep only the findings reported on these documents (the other documents are context).

A rule is skipped, not failed, when a document it needs is not in `docs`. Nothing here does I/O.

Where a finding is reported: on the document a person has to edit. A `cover` finding ("REQ-2 has no AC") is reported
on the covering document with the uncovered id as its item.

`R-TRACE-VALID` is always on: an id in a `Truy vết:` / `Thay đổi:` line must exist.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .model import Approved, Baseline, Evidence, Finding, Item, MatchState, ParsedDoc, RuleDef, SourceRef, make_finding
from .pack import StandardPack, split_selector
from .trace import TraceGraph, doc_ticket, split_ref

TRACE_VALID = "R-TRACE-VALID"


@dataclass
class Context:
    pack: StandardPack
    docs: dict[str, ParsedDoc]
    evidence: list[Evidence] | None = None
    baseline: dict[str, Baseline] = field(default_factory=dict)
    approved: dict[str, Approved] = field(default_factory=dict)
    only: set[str] | None = None
    graph: TraceGraph = field(init=False)

    def __post_init__(self) -> None:
        self.graph = TraceGraph(self.docs)

    def keys_of(self, doctypes: set[str]) -> list[str]:
        return [key for key, doc in self.docs.items() if doc.doctype in doctypes or "*" in doctypes]

    def select(self, selectors: list[str]) -> list[tuple[str, Item]]:
        """Items picked by `doctype.KIND` selectors, each once, in document order."""
        picked: dict[tuple[str, str], tuple[str, Item]] = {}
        for selector in selectors:
            doctype, kind = split_selector(selector)
            for key in self.keys_of({doctype}):
                for item in self.docs[key].items:
                    if item.kind == kind:
                        picked.setdefault((key, item.id), (key, item))
        return list(picked.values())

    def targets(self, doc_key: str, ref: str) -> list[tuple[str, str]]:
        """The existing items a reference written in `doc_key` points at (see trace.py for how a bare id resolves)."""
        ticket, target = split_ref(ref)
        if ticket is None and self.docs[doc_key].item(target):
            return [(doc_key, target)]
        ticket = doc_ticket(doc_key) if ticket is None else ticket
        return [(key, target) for key, doc in self.docs.items() if doc_ticket(key) == ticket and doc.item(target)]


Primitive = Callable[[str, RuleDef, Context], list[Finding]]


def run_rules(
    pack: StandardPack,
    docs: dict[str, ParsedDoc],
    evidence: list[Evidence] | None = None,
    baseline: dict[str, Baseline] | None = None,
    approved: dict[str, Approved] | None = None,
    only_docs: set[str] | list[str] | None = None,
) -> list[Finding]:
    ctx = Context(pack, docs, evidence, baseline or {}, approved or {}, set(only_docs) if only_docs is not None else None)
    findings = _trace_valid(ctx)
    for rule_id, rule in pack.rules.items():
        findings += PRIMITIVES[rule.type](rule_id, rule, ctx)
    unique = {f.id: f for f in findings if ctx.only is None or f.doc in ctx.only}
    return list(unique.values())


def match_state(
    pack: StandardPack,
    doc_key: str,
    doc: ParsedDoc,
    findings: list[Finding] | None,
    baseline: Baseline | None = None,
    approved: dict[str, Approved] | None = None,
    evidence: list[Evidence] | None = None,
) -> MatchState:
    """The state of one document (spec 4.5), derived each time. `findings` = the result of the last run on the current
    revision, None when the rules have not run on it.

    `lech` and `cu` are read from the facts (baseline, evidence), not from findings, and win over `loi`: they say why
    the document needs work. A document whose upstream moved without saying which items changed counts as `lech`.
    Only open findings of severity error make `loi`; warnings do not stop `khop`.
    """
    if findings is None:
        return MatchState.CHUA_KIEM
    if baseline and _baseline_hits(TraceGraph({doc_key: doc}), doc_key, baseline, approved or {}):
        return MatchState.LECH
    providers = {p for rule in pack.rules.values() if rule.type == "fresh" for p in rule.providers}
    if any(_stale(e, providers) for e in evidence or [] if e.doc == doc_key):
        return MatchState.CU
    if any(f.doc == doc_key and f.status == "open" and f.severity == "error" for f in findings):
        return MatchState.LOI
    return MatchState.KHOP


# --- the eight primitives --------------------------------------------------------------------------------------


def _template(rule_id: str, rule: RuleDef, ctx: Context) -> list[Finding]:
    """Headings, version line, change log, id patterns, duplicate ids: what the parser already noted."""
    return [
        make_finding(rule_id, rule.severity, key, p.item, f"{key}: {p.message}", f"{p.code}:{p.detail}")
        for key in ctx.keys_of(set(rule.docs))
        for p in ctx.docs[key].problems
    ]


def _cover(rule_id: str, rule: RuleDef, ctx: Context) -> list[Finding]:
    """Every `each` item is traced to by at least one `by` item."""
    by_docs = ctx.keys_of(_doctypes(rule.by))
    if not by_docs or not ctx.keys_of(_doctypes(rule.each)):
        return []
    home = next((k for k in by_docs if ctx.only is None or k in ctx.only), by_docs[0])
    covering = {(key, item.id) for key, item in ctx.select(rule.by)}
    kinds, exempt = _kinds(rule.by), (rule.unless_section or "").casefold()
    findings = []
    for key, item in ctx.select(rule.each):
        if exempt and item.section.casefold().startswith(exempt):
            continue
        if any((link.doc, link.item) in covering for link in ctx.graph.links_to(key, item.id, ("trace",))):
            continue
        ref = _written(item.id, key, home)
        fix = f'Thêm dòng "{_kw(ctx, "trace")}: {ref}" dưới mục {kinds} đáp ứng nó'
        if rule.unless_section:
            fix += f', hoặc chuyển {item.id} vào mục "{rule.unless_section}" của {key} kèm lý do'
        findings.append(make_finding(rule_id, rule.severity, home, ref, f"{home}: {item.id} ({_short(item.title)}) của {key} chưa được mục {kinds} nào truy vết tới. {fix}.", key))
    return findings


def _traces_to(rule_id: str, rule: RuleDef, ctx: Context) -> list[Finding]:
    """Every `each` item traces to at least one `to` item."""
    if not ctx.keys_of(_doctypes(rule.to)):
        return []
    wanted = {(key, item.id) for key, item in ctx.select(rule.to)}
    kinds = _kinds(rule.to)
    findings = []
    for key, item in ctx.select(rule.each):
        if rule.unless_tag and rule.unless_tag in item.tags:
            continue
        if any(target in wanted for ref in item.traces for target in ctx.targets(key, ref)):
            continue
        fix = f'Thêm dòng "{_kw(ctx, "trace")}: <ID>" ngay dưới {item.id}'
        if rule.unless_tag:
            fix += f", hoặc gắn thẻ [{ctx.pack.syntax.tags[rule.unless_tag]}] sau ID kèm lý do"
        findings.append(make_finding(rule_id, rule.severity, key, item.id, f"{key}: {item.id} ({_short(item.title)}) không truy vết về mục {kinds} nào. {fix}."))
    return findings


def _evidence(rule_id: str, rule: RuleDef, ctx: Context) -> list[Finding]:
    """The chosen items cite a source; every cited source (of any item) could be resolved."""
    chosen = [(key, item) for key, doc in ctx.docs.items() for item in doc.items if item.kind in rule.kinds]
    chosen += [pair for pair in ctx.select(rule.also) if pair[1].kind not in rule.kinds]
    source = _kw(ctx, "source")
    findings = []
    for key, item in chosen:
        if item.sources or (rule.unless_tag and rule.unless_tag in item.tags):
            continue
        fix = f'Thêm dòng "{source}: <đường dẫn:dòng | mã ticket | URL>" ngay dưới {item.id}'
        if rule.unless_tag:
            fix += f", hoặc gắn thẻ [{ctx.pack.syntax.tags[rule.unless_tag]}] nếu thành phần này chưa có"
        findings.append(make_finding(rule_id, rule.severity, key, item.id, f"{key}: {item.id} ({_short(item.title)}) chưa trích nguồn. {fix}.", "no_source"))
    for record in ctx.evidence or []:
        if record.error and record.doc in ctx.docs:
            message = f'{record.doc}: nguồn "{record.source}" của {record.item} không lấy được ({record.error}). Sửa dòng {source} cho đúng đường dẫn, số dòng hoặc mã có thật.'
            findings.append(make_finding(rule_id, rule.severity, record.doc, record.item, message, record.source))
    return findings


def _fresh(rule_id: str, rule: RuleDef, ctx: Context) -> list[Finding]:
    """What was pinned still equals what is at the source."""
    findings = []
    for record in ctx.evidence or []:
        if record.doc in ctx.docs and _stale(record, set(rule.providers)):
            message = (
                f'{record.doc}: nguồn "{record.source}" của {record.item} đã đổi so với lúc trích. '
                f"Đọc lại đoạn đó, sửa {record.item} nếu mô tả không còn đúng, rồi lưu lại tài liệu để ghim nội dung mới."
            )
            findings.append(make_finding(rule_id, rule.severity, record.doc, record.item, message, record.source))
    return findings


def _baseline(rule_id: str, rule: RuleDef, ctx: Context) -> list[Finding]:
    """The document was written from the latest approved revision of its upstream, or nothing it uses changed since."""
    findings = []
    for key in ctx.keys_of(set(rule.docs)):
        if key not in ctx.baseline:
            continue
        for item, upstream, pinned, latest, changed in _baseline_hits(ctx.graph, key, ctx.baseline[key], ctx.approved):
            if item is None:
                message = f"{key}: viết theo {upstream} bản {pinned}, nay đã có bản duyệt {latest} và chưa rõ mục nào đổi. Đọc lại {upstream} bản {latest}, cập nhật nếu cần rồi lưu lại."
            else:
                ids = ", ".join(_written(i, upstream, key) for i in changed)
                message = f"{key}: {item} truy vết tới {ids} của {upstream}, mục này đã đổi từ bản {pinned} sang bản duyệt {latest}. Xem phần đổi, cập nhật {item} rồi lưu lại."
            findings.append(make_finding(rule_id, rule.severity, key, item, message, f"{upstream}@{latest}"))
    return findings


def _answered(rule_id: str, rule: RuleDef, ctx: Context) -> list[Finding]:
    """Items of these kinds (open questions) have an answer line."""
    answer = _kw(ctx, "answer")
    return [
        make_finding(rule_id, rule.severity, key, item.id, f'{key}: {item.id} ({_short(item.title)}) chưa có câu trả lời. Thêm dòng "{answer}: …" ngay dưới {item.id}.')
        for key, doc in ctx.docs.items()
        for item in doc.items
        if item.kind in rule.kinds and not item.answer
    ]


def _declared_change(rule_id: str, rule: RuleDef, ctx: Context) -> list[Finding]:
    """An item that cites the same place as a recorded fact must say so: it changes the fact, or it builds on it."""
    facts = [(key, item) for key, item in ctx.select(rule.against) if item.sources]
    findings = []
    for key, item in ctx.select(rule.each):
        linked = {target for ref in [*item.changes, *item.traces] for target in ctx.targets(key, ref)}
        for fact_key, fact in facts:
            if (fact_key, fact.id) in linked or (fact_key, fact.id) == (key, item.id):
                continue
            shared = next((s for s in item.sources for f in fact.sources if _same_place(s, f)), None)
            if not shared:
                continue
            ref = _written(fact.id, fact_key, key)
            message = (
                f'{key}: {item.id} trích cùng nguồn "{shared.raw}" với hiện trạng {ref} ({_short(fact.title)}) nhưng chưa nói quan hệ. '
                f'Nếu cố ý làm khác hiện trạng, thêm dòng "{_kw(ctx, "changes")}: {ref}" dưới {item.id}; nếu chỉ dựa trên nó, thêm "{_kw(ctx, "trace")}: {ref}".'
            )
            findings.append(make_finding(rule_id, rule.severity, key, item.id, message, f"{fact_key}#{fact.id}"))
    return findings


PRIMITIVES: dict[str, Primitive] = {
    "template": _template,
    "cover": _cover,
    "traces_to": _traces_to,
    "evidence": _evidence,
    "fresh": _fresh,
    "baseline": _baseline,
    "answered": _answered,
    "declared_change": _declared_change,
}


def _trace_valid(ctx: Context) -> list[Finding]:
    """An id that cannot be checked because the document that would hold it was not given is left alone."""
    findings = []
    for key, doc in ctx.docs.items():
        for item in doc.items:
            for word, refs in ((_kw(ctx, "trace"), item.traces), (_kw(ctx, "changes"), item.changes)):
                for ref in refs:
                    why = _missing(ctx, key, ref)
                    if why:
                        message = f'{key}: dòng "{word}" của {item.id} nhắc {ref} nhưng {why}. Sửa lại ID cho đúng, hoặc bỏ nó khỏi dòng {word}.'
                        findings.append(make_finding(TRACE_VALID, "error", key, item.id, message, ref))
    return findings


def _missing(ctx: Context, doc_key: str, ref: str) -> str | None:
    """Why a reference is wrong, None when it is right or cannot be judged with the documents given."""
    ticket, target = split_ref(ref)
    kind = ctx.pack.kind_of(target)
    if kind is None:
        return f"bộ chuẩn không có loại mục nào mang tiền tố của {target}"
    if ctx.targets(doc_key, ref):
        return None
    ticket = doc_ticket(doc_key) if ticket is None else ticket
    given = {doc.doctype for key, doc in ctx.docs.items() if doc_ticket(key) == ticket}
    if not given or not set(ctx.pack.doc_types_with(kind)) <= given:
        return None
    where = f"ticket {ticket}" if ticket else "các tài liệu của ticket này"
    return f"không có mục {target} nào trong {where}"


def _baseline_hits(graph: TraceGraph, doc_key: str, baseline: Baseline, approved: dict[str, Approved]) -> list[tuple[str | None, str, int, int, list[str]]]:
    """(item of this document or None when unknown, upstream key, pinned revision, latest approved, changed ids it uses)."""
    hits: list[tuple[str | None, str, int, int, list[str]]] = []
    for upstream, pinned in baseline.docs.items():
        latest = approved.get(upstream)
        if latest is None or latest.revision <= pinned:
            continue
        changed = latest.changed_since.get(pinned)
        if changed is None:
            hits.append((None, upstream, pinned, latest.revision, []))
            continue
        hits += [(item, upstream, pinned, latest.revision, ids) for item, ids in graph.affected(doc_key, upstream, changed).items()]
    return hits


def _stale(record: Evidence, providers: set[str]) -> bool:
    if record.error or record.current_hash is None or record.current_hash == record.content_hash:
        return False
    return not providers or record.category in providers or record.provider in providers


def _same_place(a: SourceRef, b: SourceRef) -> bool:
    """Same file (or page, or ticket) and overlapping lines; a source without a place in it means the whole thing."""
    if (a.kind, a.repo, a.ref) != (b.kind, b.repo, b.ref):
        return False
    if not a.locator or not b.locator:
        return True
    la, lb = a.lines(), b.lines()
    if la and lb:
        return la[0] <= lb[1] and lb[0] <= la[1]
    return a.locator == b.locator


def _doctypes(selectors: list[str]) -> set[str]:
    return {split_selector(s)[0] for s in selectors}


def _kinds(selectors: list[str]) -> str:
    """`[spec.AC, spec.BR]` → "AC/BR (spec)" for a message."""
    kinds = "/".join(dict.fromkeys(split_selector(s)[1] or "" for s in selectors))
    return f"{kinds} ({', '.join(sorted(_doctypes(selectors)))})"


def _written(item_id: str, owner: str, reader: str) -> str:
    """How an item of document `owner` is written from document `reader`: with the ticket when it is another ticket's."""
    ticket = doc_ticket(owner)
    return item_id if not ticket or ticket == doc_ticket(reader) else f"{ticket}/{item_id}"


def _kw(ctx: Context, name: str) -> str:
    return getattr(ctx.pack.syntax, name)[0]


def _short(text: str, limit: int = 60) -> str:
    text = text or "không có tiêu đề"
    return text if len(text) <= limit else text[: limit - 1] + "…"
