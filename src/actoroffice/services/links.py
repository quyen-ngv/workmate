"""Typed links between tickets: which story needs which, which ones touch the same thing, which one replaces which.

Where a link comes from:
- the documents (`origin = "doc"`): a line `Phụ thuộc: …` / `Liên quan: …` / `Thay thế: …` under an item, outside any
  item, or in a section named so (the words are the pack's `syntax.relations`). What the line names is a ticket
  (`AO-14`), an item of another ticket (`AO-14/C-2`), or an item of a breakdown that became a ticket (`T-1`). The
  links a breakdown writes between its tasks become links between the tickets those tasks became. Such rows are
  rewritten from the documents whenever they are saved, so they cannot drift from the text.
- people (`origin = "manual"`): added on the ticket's page.

What a link does is not decided here: `DocService` checks a ticket's documents together with the documents of what it
depends on (so `AO-14/C-2` is a checked reference and a change of C-2 calls the dependent document back), `DocContext`
puts the linked tickets in the stage prompt, and the document repository writes them as links between files.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from actoroffice.doccore import parse
from actoroffice.doccore.trace import link_target
from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.ids import utcnow
from actoroffice.domain.work import Ticket

if TYPE_CHECKING:
    from actoroffice.app import Office

DEPENDS = "depends_on"
BREAKDOWN = "breakdown"  # the document whose items become tickets (services/docs.py `spawn`)
# How a link reads from the other end, for the relations the built-in pack knows. Any other relation reads "← <word>".
INVERSE_VI = {"depends_on": "Được phụ thuộc bởi", "relates": "Liên quan", "supersedes": "Bị thay thế bởi"}
SYMMETRIC = ("relates",)


class LinkError(OfficeError):
    code = 422


@dataclass(frozen=True)
class TicketLink:
    relation: str
    outgoing: bool  # written on this ticket (True) or on the other one
    other: Ticket
    origin: str
    to_item: str = ""
    detail: str = ""

    def label(self, words: dict[str, list[str]]) -> str:
        word = (words.get(self.relation) or [self.relation])[0]
        return word if self.outgoing or self.relation in SYMMETRIC else INVERSE_VI.get(self.relation, f"← {word}")


class LinkService:
    def __init__(self, office: Office) -> None:
        self.office = office

    # ---- reading ---------------------------------------------------------------------------------------------

    async def of(self, ticket: Ticket) -> list[TicketLink]:
        """Every link of a ticket, the ones written on it first. A link to a ticket that no longer exists is left out."""
        office = self.office
        rows = await office.db.fetchall(
            "SELECT * FROM ticket_links WHERE from_id = ? OR to_id = ? ORDER BY CASE WHEN from_id = ? THEN 0 ELSE 1 END, relation, id", (ticket.id, ticket.id, ticket.id)
        )
        out: list[TicketLink] = []
        seen: set[tuple[str, bool, int, str]] = set()
        for r in rows:
            outgoing = int(r["from_id"]) == ticket.id
            other_id = int(r["to_id"] if outgoing else r["from_id"])
            key = (str(r["relation"]), outgoing, other_id, str(r["to_item"]))
            if key in seen or other_id == ticket.id:
                continue
            seen.add(key)
            try:
                other = await office.tickets.get(other_id)
            except NotFound:
                continue
            out.append(TicketLink(str(r["relation"]), outgoing, other, str(r["origin"]), str(r["to_item"]), str(r["detail"])))
        return out

    async def upstream(self, ticket: Ticket) -> list[Ticket]:
        """Tickets whose documents this ticket's are written against: what it depends on, and every ticket one of whose
        items it names. Their documents join the set its checks look at."""
        rows = await self.office.db.fetchall(
            "SELECT DISTINCT to_id FROM ticket_links WHERE from_id = ? AND (relation = ? OR to_item <> '') ORDER BY to_id", (ticket.id, DEPENDS)
        )
        out = []
        for r in rows:
            try:
                out.append(await self.office.tickets.get(int(r["to_id"])))
            except NotFound:
                continue
        return [t for t in out if t.id != ticket.id]

    async def dependents(self, ticket_id: int) -> list[int]:
        """Ids of the tickets whose documents are written against this one's."""
        rows = await self.office.db.fetchall(
            "SELECT DISTINCT from_id FROM ticket_links WHERE to_id = ? AND (relation = ? OR to_item <> '') AND from_id <> ?", (ticket_id, DEPENDS, ticket_id)
        )
        return [int(r["from_id"]) for r in rows]

    async def view(self, ticket: Ticket) -> list[dict[str, Any]]:
        """The links of a ticket as the web and the tools show them."""
        words = self.office.docs.pack_of(ticket).syntax.relations
        return [
            {
                "relation": link.relation, "label": link.label(words), "outgoing": link.outgoing, "origin": link.origin, "item": link.to_item, "detail": link.detail,
                "ticket": link.other.key, "title": link.other.title, "status": link.other.status.value, "stage": link.other.stage,
            }
            for link in await self.of(ticket)
        ]  # fmt: skip

    # ---- links people add ------------------------------------------------------------------------------------

    async def add(self, ticket: Ticket, other: Ticket, relation: str, *, by: str) -> None:
        relations = self.office.docs.pack_of(ticket).syntax.relations
        if relation not in relations:
            raise LinkError(f"Quan hệ '{relation}' không có. Chọn: {', '.join(relations)}")
        if other.id == ticket.id:
            raise LinkError("Một ticket không liên kết với chính nó")
        if relation == DEPENDS and await self._reaches(other.id, ticket.id):
            raise LinkError(f"{other.key} đã phụ thuộc (trực tiếp hoặc gián tiếp) vào {ticket.key}: thêm liên kết này sẽ thành một vòng")
        exists = await self.office.db.fetchone(
            "SELECT 1 FROM ticket_links WHERE from_id = ? AND to_id = ? AND relation = ? AND origin = 'manual'", (ticket.id, other.id, relation)
        )
        if exists is None:
            await self.office.db.execute(
                "INSERT INTO ticket_links(from_id, to_id, relation, origin, created_by, created_at) VALUES (?, ?, ?, 'manual', ?, ?)",
                (ticket.id, other.id, relation, by, utcnow()),
            )
        self._changed(ticket.id, other.id)

    async def remove(self, ticket: Ticket, other: Ticket, relation: str) -> None:
        """Remove a link people added. One read from a document goes away when the line is removed from the document."""
        await self.office.db.execute(
            "DELETE FROM ticket_links WHERE origin = 'manual' AND relation = ? AND ((from_id = ? AND to_id = ?) OR (from_id = ? AND to_id = ?))",
            (relation, ticket.id, other.id, other.id, ticket.id),
        )
        self._changed(ticket.id, other.id)

    async def _reaches(self, start: int, goal: int) -> bool:
        """Does `start` depend on `goal`, through any number of tickets?"""
        seen, queue = {start}, [start]
        while queue:
            rows = await self.office.db.fetchall("SELECT to_id FROM ticket_links WHERE from_id = ? AND relation = ?", (queue.pop(), DEPENDS))
            for r in rows:
                nxt = int(r["to_id"])
                if nxt == goal:
                    return True
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return False

    def _changed(self, *ticket_ids: int) -> None:
        for ticket_id in dict.fromkeys(ticket_ids):
            self.office.bus.publish("ticket.links", ticket_id=ticket_id)

    # ---- links the documents write ---------------------------------------------------------------------------

    async def written(self, ticket: Ticket) -> tuple[list[tuple[int, str, str, str]], list[tuple[str, str, str]]]:
        """What the ticket's documents say it is linked to: ((other ticket id, relation, item of it or "", where it is
        written), …) and the references that name a ticket that does not exist ((document key, item or "", reference), …).

        Read from the current text of its own documents, and, for a ticket that a breakdown item became, from that
        item's lines in the parent's breakdown (`T-2` … `Phụ thuộc: T-1` → the ticket T-1 became)."""
        office = self.office
        pack = office.docs.pack_of(ticket)
        found: dict[tuple[int, str, str], str] = {}
        unknown: list[tuple[str, str, str]] = []

        async def resolve(owner: Ticket, ref: str) -> tuple[int | None, str, bool]:
            """(ticket id, item of it, whether the reference names a ticket at all)."""
            key, target = link_target(pack, ref)
            if key:
                try:
                    return (await office.tickets.get(key)).id, target, True
                except NotFound:
                    return None, target, True
            # a bare item: a ticket link only when it is an item that became a ticket (a task of the breakdown)
            holder = owner.id if await self._spawned(owner.id, target) is not None or owner.parent_id is None else owner.parent_id
            return await self._spawned(holder, target), "", False

        async def take(owner: Ticket, doc_key: str, holder: str, relations: dict[str, list[str]], where: str) -> None:
            for relation, refs in relations.items():
                for ref in refs:
                    other_id, target, names_ticket = await resolve(owner, ref)
                    if other_id is None:
                        if names_ticket:
                            unknown.append((doc_key, holder, ref))
                        continue
                    if other_id != ticket.id:
                        found.setdefault((other_id, relation, target), where)

        for doc in await office.tickets.list_documents(ticket.id):
            doctype = office.docs.doctype_of(doc.key, pack)
            if doctype is None or "." in doc.key:
                continue  # the spec of a task that is not a ticket yet is that task's, not the epic's
            parsed = parse(doc.body, doctype, pack)
            if doc.key != BREAKDOWN:  # the items of a breakdown link its tasks to each other, not the epic to them
                for item in parsed.items:
                    await take(ticket, doc.key, item.id, item.relations, f"{doc.key} {item.id}")
            await take(ticket, doc.key, "", parsed.relations, doc.key)
        row = await office.db.fetchone("SELECT parent_id, item_id FROM doc_spawned WHERE child_id = ?", (ticket.id,))
        if row is not None:
            parent = await office.tickets.get(int(row["parent_id"]))
            breakdown = await office.tickets.get_document(parent.id, BREAKDOWN)
            if breakdown is not None:
                body = await office.docs._approved_body(breakdown)
                item = parse(body, BREAKDOWN, office.docs.pack_of(parent)).item(str(row["item_id"]))
                if item is not None:
                    await take(parent, BREAKDOWN, item.id, item.relations, f"{parent.key}/{BREAKDOWN} {item.id}")
        return [(other, relation, target, where) for (other, relation, target), where in found.items()], unknown

    async def _spawned(self, parent_id: int, item_id: str) -> int | None:
        row = await self.office.db.fetchone("SELECT child_id FROM doc_spawned WHERE parent_id = ? AND item_id = ? AND child_id IS NOT NULL", (parent_id, item_id))
        return int(row["child_id"]) if row is not None else None

    async def sync(self, ticket: Ticket) -> bool:
        """Make the stored links of a ticket that come from documents say what the documents say now. True when they changed."""
        office = self.office
        wanted, _ = await self.written(ticket)
        have = await office.db.fetchall("SELECT to_id, relation, to_item, detail FROM ticket_links WHERE from_id = ? AND origin = 'doc'", (ticket.id,))
        before = sorted((int(r["to_id"]), str(r["relation"]), str(r["to_item"]), str(r["detail"])) for r in have)
        if before == sorted(wanted):
            return False
        now = utcnow()
        async with office.db.tx() as tx:
            await tx.execute("DELETE FROM ticket_links WHERE from_id = ? AND origin = 'doc'", (ticket.id,))
            for other_id, relation, target, where in wanted:
                await tx.execute(
                    "INSERT INTO ticket_links(from_id, to_id, relation, origin, to_item, detail, created_at) VALUES (?, ?, ?, 'doc', ?, ?, ?)",
                    (ticket.id, other_id, relation, target, where, now),
                )
        self._changed(ticket.id, *{o for o, *_ in wanted}, *{o for o, *_ in before})
        return True

    async def sync_family(self, ticket: Ticket) -> None:
        """A document of `ticket` was saved, or its tasks became tickets: its own links and those of its children (their
        links are written in its breakdown)."""
        await self.sync(ticket)
        for r in await self.office.db.fetchall("SELECT child_id FROM doc_spawned WHERE parent_id = ? AND child_id IS NOT NULL", (ticket.id,)):
            try:
                await self.sync(await self.office.tickets.get(int(r["child_id"])))
            except NotFound:
                continue
