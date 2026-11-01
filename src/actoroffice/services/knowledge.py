"""Team knowledge base: what the team learns ticket after ticket, typed, sourced and verified by people.

Kinds
- fact:      business rules and terms ("VIP orders: refunds within 60 days")
- code_note: where and how something is done in code, anchored to file@commit (goes stale when the file changes)
- decision:  what was chosen, why, by whom (so the debate is not repeated)
- contract:  an API / topic / queue: spec location, auth kind (never a secret), owner, masked samples, quirks
- flow:      a business flow across services: steps, data written, events, the state before each step

Life cycle: draft (AI noted it) → verified (a person confirmed it, usually in one batch at ticket close) →
stale (anchored code changed after a merge; re-check before relying on it). Rejected items stay for history.
Published one-way to Confluence (one page per kind per team).
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.ids import utcnow

if TYPE_CHECKING:
    from actoroffice.app import Office

KINDS = ("fact", "code_note", "decision", "contract", "flow")
KIND_VI = {"fact": "Nghiệp vụ & thuật ngữ", "code_note": "Ghi chú code", "decision": "Quyết định", "contract": "Hợp đồng tích hợp", "flow": "Luồng nghiệp vụ"}
MAX_CONTENT = 4000


class KbError(OfficeError):
    code = 422


class KbItem(BaseModel):
    id: int
    team_id: str | None = None
    project_id: str | None = None
    kind: str
    title: str
    content: str
    data: dict[str, Any] = Field(default_factory=dict)
    status: str = "draft"
    sources: list[str] = Field(default_factory=list)
    anchor: dict[str, Any] | None = None
    ticket_id: int | None = None
    created_by: str
    verified_by: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def line(self) -> str:
        label = {"verified": "✓", "draft": "chưa xác nhận", "stale": "có thể đã cũ"}.get(self.status, self.status)
        src = f" (nguồn: {'; '.join(self.sources[:3])})" if self.sources else ""
        return f"[{self.kind} #{self.id}, {label}] {self.title}: {self.content[:600]}{src}"


class KnowledgeService:
    def __init__(self, office: Office) -> None:
        self.office = office

    async def note(
        self,
        *,
        kind: str,
        title: str,
        content: str,
        by: str,
        team_id: str | None = None,
        project_id: str | None = None,
        ticket_id: int | None = None,
        sources: list[str] | None = None,
        anchor: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> KbItem:
        if kind not in KINDS:
            raise KbError(f"kind: {', '.join(KINDS)}")
        title = self.office.redact(title.strip())[:200]
        content = self.office.redact(content.strip())[:MAX_CONTENT]
        if not title or not content:
            raise KbError("title và content không được trống")
        if kind == "code_note" and not (anchor and (anchor.get("path") or anchor.get("paths"))):
            raise KbError("code_note cần anchor.path (file mà ghi chú nói về)")
        data = json.loads(self.office.redact(json.dumps(data or {}, ensure_ascii=False)))
        now = utcnow()
        existing = await self.office.db.fetchone(
            "SELECT id, content FROM kb_items WHERE team_id IS ? AND kind = ? AND lower(title) = lower(?) AND status <> 'rejected'",
            (team_id, kind, title),
        )
        async with self.office.db.tx() as tx:
            if existing:
                item_id = int(existing["id"])
                changed = existing["content"].strip() != content
                await tx.execute(
                    "UPDATE kb_items SET content = ?, data_json = ?, sources_json = ?, anchor_json = COALESCE(?, anchor_json), "
                    "status = CASE WHEN ? THEN 'draft' ELSE status END, ticket_id = COALESCE(?, ticket_id), updated_at = ? WHERE id = ?",
                    (content, json.dumps(data, ensure_ascii=False), json.dumps(sources or []), json.dumps(anchor) if anchor else None, changed, ticket_id, now, item_id),
                )
                await tx.execute("DELETE FROM kb_fts WHERE rowid = ?", (item_id,))
            else:
                cur = await tx.execute(
                    "INSERT INTO kb_items(team_id, project_id, kind, title, content, data_json, sources_json, anchor_json, ticket_id, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (team_id, project_id, kind, title, content, json.dumps(data, ensure_ascii=False), json.dumps(sources or []),
                     json.dumps(anchor) if anchor else None, ticket_id, by, now, now),
                )
                item_id = int(cur.lastrowid or 0)
            await tx.execute("INSERT INTO kb_fts(rowid, title, content) VALUES (?, ?, ?)", (item_id, title, content))
        item = await self.get(item_id)
        self.office.bus.publish("kb.changed", item=item.model_dump())
        return item

    async def get(self, item_id: int) -> KbItem:
        row = await self.office.db.fetchone("SELECT * FROM kb_items WHERE id = ?", (item_id,))
        if not row:
            raise NotFound(f"KB #{item_id} không tồn tại")
        return _item(row)

    async def search(self, query: str, *, team_id: str | None, kinds: list[str] | None = None, limit: int = 10, include_drafts: bool = True) -> list[KbItem]:
        terms = " OR ".join(f'"{t}"' for t in re.findall(r"\w{2,}", query)[:16])
        if not terms:
            return []
        where = ["kb_fts MATCH ?", "(k.team_id IS NULL OR k.team_id IS ?)", "k.status IN (" + ("'verified','draft','stale'" if include_drafts else "'verified'") + ")"]
        params: list[Any] = [terms, team_id]
        if kinds:
            where.append(f"k.kind IN ({','.join('?' * len(kinds))})")
            params += kinds
        rows = await self.office.db.fetchall(
            "SELECT k.* FROM kb_fts f JOIN kb_items k ON k.id = f.rowid WHERE " + " AND ".join(where)
            + " ORDER BY CASE k.status WHEN 'verified' THEN 0 WHEN 'draft' THEN 1 ELSE 2 END, bm25(kb_fts) LIMIT ?",
            (*params, limit),
        )
        return [_item(r) for r in rows]

    async def list(self, *, team_id: str | None = None, status: str | None = None, kind: str | None = None, ticket_id: int | None = None) -> list[KbItem]:
        where, params = [], []
        for col, val in (("team_id", team_id), ("status", status), ("kind", kind), ("ticket_id", ticket_id)):
            if val is not None:
                where.append(f"{col} = ?")
                params.append(val)
        order = " ORDER BY id" if ticket_id is not None else " ORDER BY kind, id"  # a ticket's items: in the order they were noted
        rows = await self.office.db.fetchall("SELECT * FROM kb_items" + (f" WHERE {' AND '.join(where)}" if where else "") + order, params)
        return [_item(r) for r in rows]

    async def set_status(self, item_id: int, status: str, *, by: str) -> KbItem:
        if status not in ("draft", "verified", "stale", "rejected"):
            raise KbError("status: draft | verified | stale | rejected")
        await self.office.db.execute(
            "UPDATE kb_items SET status = ?, verified_by = CASE WHEN ? = 'verified' THEN ? ELSE verified_by END, updated_at = ? WHERE id = ?",
            (status, status, by, utcnow(), item_id),
        )
        item = await self.get(item_id)
        self.office.bus.publish("kb.changed", item=item.model_dump())
        return item

    async def for_ticket(self, ticket: Any, *, limit: int = 12) -> list[KbItem]:
        return await self.search(f"{ticket.title} {ticket.description[:400]}", team_id=ticket.team_id, limit=limit)

    async def review_close(self, ticket_id: int, *, approved: bool, note: str, by: str) -> dict[str, list[int]]:
        """The knowledge gate at ticket close: accept the ticket's draft items except those named in the note
        ("bỏ 2, 5" / "reject 2 5"); a rejection rejects them all."""
        drafts = [i for i in await self.list(ticket_id=ticket_id) if i.status == "draft"]
        dropped = {int(n) for n in re.findall(r"\d+", note)} if approved and re.search(r"(?i)\b(bỏ|bo|reject|sai|xóa|xoá)\b", note) else set()
        result: dict[str, list[int]] = {"verified": [], "rejected": []}
        for n, item in enumerate(drafts, 1):
            keep = approved and n not in dropped  # numbers refer to the card's list
            await self.set_status(item.id, "verified" if keep else "rejected", by=by)
            result["verified" if keep else "rejected"].append(item.id)
        return result

    async def mark_stale(self, *, team_id: str | None, changed_files: list[str]) -> list[int]:
        """Code notes and flows anchored to files that changed are no longer trusted until re-checked."""
        if not changed_files:
            return []
        changed = {f.replace("\\", "/").lstrip("./") for f in changed_files}
        stale = []
        for item in await self.list(team_id=team_id):
            if item.status not in ("verified", "draft") or not item.anchor:
                continue
            paths = [item.anchor.get("path")] if item.anchor.get("path") else list(item.anchor.get("paths") or [])
            if any(str(p).replace("\\", "/").lstrip("./") in changed for p in paths if p):
                await self.set_status(item.id, "stale", by="code-change")
                stale.append(item.id)
        return stale

    # ---- Confluence ---------------------------------------------------------------------------------------

    async def publish(self, ticket_id: int) -> list[str]:
        """One-way publish: the ticket's dossier page and the team's KB pages (one per kind). Never blocks closing."""
        from actoroffice.services.atlassian import markdown_to_storage

        office = self.office
        ticket = await office.tickets.get(ticket_id)
        team = await office.teams.get_team(ticket.team_id) if ticket.team_id else None
        wiki = office.atlassian.wiki()
        if wiki is None or team is None or not team.confluence_space:
            return []
        urls: list[str] = []
        dossier = await office.tickets.get_document(ticket.id, "dossier")
        body = (dossier.body if dossier else "") + "\n\n" + "\n".join(f"- {k}: {v}" for k, v in (("Jira", ticket.jira_url), ("PR", ticket.pr_url)) if v)
        page = await wiki.upsert_page(team.confluence_space, f"{ticket.key} · {ticket.title}"[:250], markdown_to_storage(body), parent_id=team.confluence_parent_id)
        urls.append(page["url"])
        await office.db.execute("UPDATE tickets SET confluence_url = ? WHERE id = ?", (page["url"], ticket.id))
        for kind in KINDS:
            items = [i for i in await self.list(team_id=team.id, kind=kind) if i.status in ("verified", "stale")]
            if not items:
                continue
            lines = [f"# {KIND_VI[kind]} — {team.name}", "", f"_Cập nhật tự động {datetime.now(UTC):%Y-%m-%d}. Mục có (cũ?) cần kiểm tra lại._", ""]
            for item in items:
                mark = " (cũ?)" if item.status == "stale" else ""
                lines += [f"## {item.title}{mark}", item.content, ""]
                if item.sources:
                    lines.append("Nguồn: " + "; ".join(item.sources))
                if item.data:
                    lines += ["```", json.dumps(item.data, ensure_ascii=False, indent=1)[:4000], "```"]
                lines.append("")
            page = await wiki.upsert_page(team.confluence_space, f"KB · {team.name} · {KIND_VI[kind]}", markdown_to_storage("\n".join(lines)), parent_id=team.confluence_parent_id)
            urls.append(page["url"])
        return urls


def _item(row: dict[str, Any]) -> KbItem:
    return KbItem(
        id=row["id"], team_id=row["team_id"], project_id=row["project_id"], kind=row["kind"], title=row["title"], content=row["content"],
        data=json.loads(row["data_json"] or "{}"), status=row["status"], sources=json.loads(row["sources_json"] or "[]"),
        anchor=json.loads(row["anchor_json"]) if row["anchor_json"] else None, ticket_id=row["ticket_id"], created_by=row["created_by"],
        verified_by=row["verified_by"], created_at=row["created_at"], updated_at=row["updated_at"],
    )


async def safe_publish(office: Office, ticket_id: int) -> None:
    """Publishing must never block closing: failures become a comment."""
    try:
        urls = await office.knowledge.publish(ticket_id)
    except Exception as exc:  # network, permissions, missing space…
        with contextlib.suppress(Exception):
            await office.tickets.add_comment(ticket_id, f"Không đăng được lên Confluence: {exc}", author_kind="system", author_id=None)
        return
    if urls:
        await office.tickets.add_comment(ticket_id, "📘 Đã đăng lên Confluence:\n" + "\n".join(urls), author_kind="system", author_id=None)
