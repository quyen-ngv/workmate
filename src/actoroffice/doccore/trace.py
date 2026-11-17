"""Who traces to whom, and what a change touches.

`impact` names the items that differ between two revisions; a downstream document is only disturbed when it traces to
one of them, so fixing a typo in an item nobody uses calls nobody back.

How a reference resolves: `EP-4/REQ-2` is REQ-2 of ticket EP-4. A bare `AC-1.2` is the item of the same document if it
has one with that id (an assumption `A-1` exists per document), otherwise the item of another document of the same
ticket. When the graph knows the pack, a bare id of a kind that no document of the ticket can hold (`REQ-1` written in
a task's design, where requirements live on the epic) is the item of the one other ticket in the set that has it.

Besides `Truy vết:` and `Thay đổi:` the graph holds the typed links of the pack's `syntax.relations` (`Phụ thuộc:`,
`Liên quan:`, `Thay thế:`). Such a link lands on an item, or on a ticket as a whole (`AO-14`): then its `target` is
empty. A link written outside any item is the document's own: its `item` is empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .model import ParsedDoc

if TYPE_CHECKING:
    from .pack import StandardPack


def impact(old: ParsedDoc | None, new: ParsedDoc) -> set[str]:
    """Ids added, removed, or whose hash changed. No old revision: everything is new."""
    before = {i.id: i.hash for i in old.items} if old else {}
    after = {i.id: i.hash for i in new.items}
    return {i for i in before.keys() | after.keys() if before.get(i) != after.get(i)}


def doc_ticket(doc_key: str) -> str:
    """`EP-4/analysis` → `EP-4`; `spec` → `` (no ticket: every such document is in the same, unnamed one)."""
    return doc_key.rpartition("/")[0]


def split_ref(ref: str) -> tuple[str | None, str]:
    """`EP-4/REQ-2` → ("EP-4", "REQ-2"); `AC-1.2` → (None, "AC-1.2")."""
    ticket, sep, item = ref.rpartition("/")
    return (ticket if sep else None), item


def link_target(pack: StandardPack | None, ref: str) -> tuple[str, str]:
    """What a typed link written as `ref` points at: (ticket key or "", item id or ""). `AO-14/C-2` → ("AO-14", "C-2");
    a bare id whose prefix is an item kind of the pack is an item (`T-1` → ("", "T-1")), any other is a ticket
    (`AO-14` → ("AO-14", ""))."""
    ticket, sep, target = ref.rpartition("/")
    if sep:
        return ticket, target
    return ("", ref) if pack is None or pack.kind_of(ref) else (ref, "")


def holders_elsewhere(docs: dict[str, ParsedDoc], pack: StandardPack | None, doc_key: str, target: str, far: frozenset[str] = frozenset()) -> list[str] | None:
    """Where a bare id written in `doc_key` has to be looked for when its own ticket cannot hold it: the documents of
    other tickets whose type holds that kind. None when the ticket's own documents are the place to look (the kind
    is theirs, the pack is unknown, or the id is of no kind). `far`: documents reached only by naming their ticket
    (`AO-14/C-2`): those of tickets it is merely linked to, never what a bare id means."""
    kind = pack.kind_of(target) if pack else None
    if kind is None:
        return None
    own, types = doc_ticket(doc_key), set(pack.doc_types_with(kind))
    if any(doc_ticket(key) == own and doc.doctype in types for key, doc in docs.items()):
        return None
    return [key for key, doc in docs.items() if doc.doctype in types and doc_ticket(key) != own and (key not in far or doc_key in far)]


def home_ticket(docs: dict[str, ParsedDoc], pack: StandardPack | None, doc_key: str, target: str, far: frozenset[str] = frozenset()) -> str:
    """The ticket a bare id written in `doc_key` points into: its own, unless exactly one other ticket holds the item."""
    elsewhere = holders_elsewhere(docs, pack, doc_key, target, far)
    tickets = {doc_ticket(key) for key in elsewhere or [] if docs[key].item(target)}
    return tickets.pop() if len(tickets) == 1 else doc_ticket(doc_key)


@dataclass(frozen=True)
class Link:
    doc: str  # document the line is written in
    item: str
    relation: str  # "trace" | "changes" | a name of the pack's `syntax.relations`
    ref: str  # as written
    ticket: str  # ticket the target is in
    target: str  # item id; "" when a typed link names a ticket as a whole
    local: bool  # the target is an item of the same document


class TraceGraph:
    def __init__(self, docs: dict[str, ParsedDoc], pack: StandardPack | None = None, far: frozenset[str] = frozenset()) -> None:
        self.docs = docs
        self.links: list[Link] = []
        for key, doc in docs.items():
            own = {i.id for i in doc.items}
            for item in doc.items:
                for relation, refs in (("trace", item.traces), ("changes", item.changes)):
                    for ref in refs:
                        ticket, target = split_ref(ref)
                        local = ticket is None and target in own
                        if ticket is None:
                            ticket = doc_ticket(key) if local else home_ticket(docs, pack, key, target, far)
                        self.links.append(Link(key, item.id, relation, ref, ticket, target, local))
            for holder, relations in [(item.id, item.relations) for item in doc.items] + [("", doc.relations)]:
                for relation, refs in relations.items():
                    for ref in refs:
                        ticket, target = link_target(pack, ref)
                        if not target:  # a ticket as a whole
                            self.links.append(Link(key, holder, relation, ref, ticket, "", False))
                            continue
                        local = not ticket and target in own
                        if not ticket:
                            ticket = doc_ticket(key) if local else home_ticket(docs, pack, key, target, far)
                        self.links.append(Link(key, holder, relation, ref, ticket, target, local))

    def links_to(self, doc_key: str, item_id: str, relations: tuple[str, ...] | None = ("trace", "changes")) -> list[Link]:
        """Links that land on `item_id` of `doc_key`. The item need not exist (it may have been removed).
        `relations=None`: of every kind, the typed links of the pack included."""
        ticket = doc_ticket(doc_key)
        return [
            link
            for link in self.links
            if link.target == item_id and link.ticket == ticket and (relations is None or link.relation in relations) and (link.doc == doc_key if link.local else True)
        ]

    def typed(self, relation: str | None = None) -> list[Link]:
        """The typed links (not `Truy vết:` / `Thay đổi:`), of one relation or of all."""
        return [link for link in self.links if link.relation not in ("trace", "changes") and (relation is None or link.relation == relation)]

    def tracing_to(self, doc_key: str, item_id: str, in_doc: str | None = None) -> list[str]:
        """Items (of `in_doc`, or as `<doc key>#<item>` across all documents) that trace to item X of document A."""
        links = self.links_to(doc_key, item_id)
        if in_doc is None:
            return _unique(f"{link.doc}#{link.item}" for link in links)
        return _unique(link.item for link in links if link.doc == in_doc)

    def affected(self, doc_key: str, upstream: str, changed: set[str] | list[str]) -> dict[str, list[str]]:
        """Is document B touched by a change of `upstream`? Its items that trace to a changed item, or are linked to
        it by a typed link (`Phụ thuộc: AO-14/C-2`) → those ids. A link of the document as a whole counts under ""."""
        if doc_key == upstream:
            return {}
        hit: dict[str, list[str]] = {}
        for target in sorted(changed):
            for link in self.links_to(upstream, target, None):
                if link.doc == doc_key and target not in hit.get(link.item, []):
                    hit.setdefault(link.item, []).append(target)
        return hit


def _unique(values) -> list[str]:
    return list(dict.fromkeys(values))
