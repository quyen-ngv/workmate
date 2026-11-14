"""Documents from outside systems, through whatever is connected (Jira, Confluence, a plugin…).

One way in for everything that reads such a document: the AI's `source_get` / `source_search`, the "read" mark of a
ticket's sources, and the evidence a document pins. A document is kept by version in `source_cache`, so an unchanged
one costs one request at most (the adapter is still asked: the version is its answer). Nothing connected is a normal
state: the answer says what is missing.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from actoroffice.domain.ids import utcnow
from actoroffice.domain.work import Ticket
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Scope
from actoroffice.ports.docs import DocContent, DocRef

if TYPE_CHECKING:
    from actoroffice.app import Office

NOT_CONNECTED = "Chưa có kết nối nào đọc được tài liệu này. Thêm Jira / Confluence (hoặc hệ thống khác) trong Cài đặt › Kết nối."


@dataclass
class Fetched:
    connection: str
    use: str
    ref: DocRef
    content: DocContent

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.content.text.encode("utf-8")).hexdigest()


class SourceHub:
    def __init__(self, office: Office) -> None:
        self.office = office

    def scope(self, ticket: Ticket | None) -> Scope:
        return Scope(team_id=ticket.team_id, project_id=ticket.project_id, ticket_key=ticket.key) if ticket is not None else Scope()

    async def _sources(self, scope: Scope) -> list[tuple[ConnectionSpec, Any]]:
        out = []
        for spec in await self.office.connections.candidates(Capability.DOCS, scope):
            try:
                out.append((spec, await self.office.connections.adapter(spec.id)))
            except ConnectorError:
                continue  # no secret yet: as good as not connected
        return out

    async def connected(self, ticket: Ticket | None) -> bool:
        return bool(await self._sources(self.scope(ticket)))

    async def recognize(self, ticket: Ticket | None, text: str) -> list[tuple[ConnectionSpec, Any, DocRef]]:
        """Which connected system says a reference is one of its documents, nearest connection first."""
        scope = self.scope(ticket)
        found = []
        for spec, adapter in await self._sources(scope):
            for ref in adapter.recognize(text):
                found.append((spec, adapter, ref.model_copy(update={"connection": spec.id})))
        return found

    async def get(self, ticket: Ticket | None, ref: str) -> Fetched:
        """A document by what people write: an issue key, a page URL, a page id. Raises ConnectorError: `unconfigured`
        when nothing is connected, `not_found` when no connection knows the reference, `forbidden` outside its scope."""
        scope = self.scope(ticket)
        sources = await self._sources(scope)
        if not sources:
            raise ConnectorError("unconfigured", NOT_CONNECTED)
        text = ref.strip()
        last: ConnectorError | None = None
        for spec, adapter, doc_ref in await self.recognize(ticket, text):
            if not adapter.in_scope(doc_ref, scope):
                last = ConnectorError("forbidden", f"{text} nằm ngoài phạm vi của kết nối '{spec.id}'")
                continue
            try:
                content = await adapter.fetch(doc_ref, scope)
            except ConnectorError as exc:
                last = exc
                continue
            await self._remember(spec.id, doc_ref, content)
            await self.office.audit.record("source:get", "read", ticket_id=ticket.id if ticket else None, target=f"{spec.id}:{doc_ref.ref}", detail={"version": content.version})
            return Fetched(spec.id, spec.use, doc_ref, content)
        if last is not None:
            raise last
        names = ", ".join(spec.id for spec, _ in sources)
        raise ConnectorError("not_found", f"Không kết nối nào ({names}) nhận ra '{text}' là tài liệu của nó.")

    async def _remember(self, connection_id: str, ref: DocRef, content: DocContent) -> None:
        meta = {"url": content.url, "notes": content.notes, "links": [link.model_dump() for link in content.links], "truncated": content.truncated}
        await self.office.db.execute(
            "INSERT INTO source_cache(connection_id, ref, version, title, body, meta_json, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(connection_id, ref) DO UPDATE SET version = excluded.version, title = excluded.title, body = excluded.body, "
            "meta_json = excluded.meta_json, fetched_at = excluded.fetched_at",
            (connection_id, ref.ref, content.version, content.title, content.text, json.dumps(meta, ensure_ascii=False), utcnow()),
        )

    async def search(self, ticket: Ticket | None, query: str, limit: int = 10) -> list[dict[str, Any]]:
        scope = self.scope(ticket)
        sources = await self._sources(scope)
        if not sources:
            raise ConnectorError("unconfigured", NOT_CONNECTED)
        hits: list[dict[str, Any]] = []
        errors: list[str] = []
        for spec, adapter in sources:
            find = getattr(adapter, "search_docs", None) or adapter.search
            try:
                found = await find(query, scope, limit)
            except ConnectorError as exc:
                errors.append(f"{spec.id}: {exc}")
                continue
            hits += [{"connection": spec.id, "ref": h.ref.ref, "title": h.title, "snippet": h.snippet, "url": h.url} for h in found]
        if not hits and errors:
            raise ConnectorError("network", "; ".join(errors))
        return hits[:limit]

    async def mark_read(self, ticket: Ticket, fetched: Fetched, asked: str) -> list[str]:
        """The ticket's sources that this document is: an issue key, or a URL the same connection recognises as the
        same document. Returns what was marked."""
        office = self.office
        rows = await office.db.fetchall(
            "SELECT id, kind, ref, locator FROM ticket_sources WHERE ticket_id = ? AND active = 1 AND kind IN ('jira', 'url') AND status IN ('unread', 'error')", (ticket.id,)
        )
        adapter = await office.connections.adapter(fetched.connection)
        same = {fetched.ref.ref.lower(), asked.strip().lower()}
        marked = []
        now = utcnow()
        for row in rows:
            ref = str(row["ref"])
            refs = {r.ref.lower() for r in adapter.recognize(ref)} | {ref.lower()}
            if refs & same:
                await office.db.execute("UPDATE ticket_sources SET status = 'read', error = NULL, done_at = ?, updated_at = ? WHERE id = ?", (now, now, row["id"]))
                marked.append(ref)
        return marked
