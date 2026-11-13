"""Who traces to whom, and what a change touches.

`impact` names the items that differ between two revisions; a downstream document is only disturbed when it traces to
one of them, so fixing a typo in an item nobody uses calls nobody back.

How a reference resolves: `EP-4/REQ-2` is REQ-2 of ticket EP-4. A bare `AC-1.2` is the item of the same document if it
has one with that id (an assumption `A-1` exists per document), otherwise the item of another document of the same
ticket.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import ParsedDoc


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


@dataclass(frozen=True)
class Link:
    doc: str  # document the line is written in
    item: str
    relation: str  # "trace" | "changes"
    ref: str  # as written
    ticket: str  # ticket the target is in
    target: str  # item id
    local: bool  # the target is an item of the same document


class TraceGraph:
    def __init__(self, docs: dict[str, ParsedDoc]) -> None:
        self.docs = docs
        self.links: list[Link] = []
        for key, doc in docs.items():
            own = {i.id for i in doc.items}
            for item in doc.items:
                for relation, refs in (("trace", item.traces), ("changes", item.changes)):
                    for ref in refs:
                        ticket, target = split_ref(ref)
                        self.links.append(Link(key, item.id, relation, ref, doc_ticket(key) if ticket is None else ticket, target, ticket is None and target in own))

    def links_to(self, doc_key: str, item_id: str, relations: tuple[str, ...] = ("trace", "changes")) -> list[Link]:
        """Links that land on `item_id` of `doc_key`. The item need not exist (it may have been removed)."""
        ticket = doc_ticket(doc_key)
        return [
            link
            for link in self.links
            if link.target == item_id and link.ticket == ticket and link.relation in relations and (link.doc == doc_key if link.local else True)
        ]

    def tracing_to(self, doc_key: str, item_id: str, in_doc: str | None = None) -> list[str]:
        """Items (of `in_doc`, or as `<doc key>#<item>` across all documents) that trace to item X of document A."""
        links = self.links_to(doc_key, item_id)
        if in_doc is None:
            return _unique(f"{link.doc}#{link.item}" for link in links)
        return _unique(link.item for link in links if link.doc == in_doc)

    def affected(self, doc_key: str, upstream: str, changed: set[str] | list[str]) -> dict[str, list[str]]:
        """Is document B touched by a change of `upstream`? Its items that trace to a changed item → those ids."""
        if doc_key == upstream:
            return {}
        hit: dict[str, list[str]] = {}
        for target in sorted(changed):
            for link in self.links_to(upstream, target):
                if link.doc == doc_key and target not in hit.get(link.item, []):
                    hit.setdefault(link.item, []).append(target)
        return hit


def _unique(values) -> list[str]:
    return list(dict.fromkeys(values))
