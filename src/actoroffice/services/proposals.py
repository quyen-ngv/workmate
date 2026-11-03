"""Ticket proposals (SI-24): the AI proposes a list of tickets on one card; an owner or lead approves it once.

- `propose()` checks the whole list and creates one `propose_tickets` card. Nothing else is written: no ticket,
  no blocker, no stage, until a person approves.
- `answer()` resolves the card first (atomic: a double click gets a 409), then creates the tickets in list order.
  "Run now" items start their suggested workflow (blocked while a blocker is open), the others stay in `backlog`
  with the suggested workflow kept in `tickets.stage_template` (a ticket with `stage IS NULL` never reads it
  otherwise). Dependencies become `blocked_by`; a dependency on an item that was not chosen is dropped and noted.
- `start_backlog()` is the "Bắt đầu" button of the web team's Backlog list.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from actoroffice.domain.chat import MessageKind
from actoroffice.domain.errors import Conflict, NotFound, OfficeError, PolicyDenied
from actoroffice.domain.ids import utcnow
from actoroffice.domain.team import LOCAL_OWNER_ID
from actoroffice.domain.work import Interaction, InteractionKind, Priority, Ticket, TicketStatus

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)
S = TicketStatus

MAX_ITEMS = 20
WORKFLOWS = ("code", "quick")
APPROVER_ROLES = ["owner", "lead"]
BACKLOG_LIMIT = 50
_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9]*-\d+$")
_DROP_WORD = re.compile(r"(?i)\b(bỏ|bo|drop|skip|xóa|xoá)\b")


class ProposalError(OfficeError):
    code = 422


def parse_drop(text: str) -> list[int] | None:
    """The item numbers of a reply like "bỏ 2, 5"; None when the text is not such a reply."""
    if not _DROP_WORD.search(text):
        return None
    numbers = sorted({int(n) for n in re.findall(r"\d+", text)})
    return numbers or None


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


class ProposalService:
    def __init__(self, office: Office) -> None:
        self.office = office

    # ---- propose ------------------------------------------------------------------------------------

    async def validate(self, items: Any, *, default_project_id: str | None) -> list[dict[str, Any]]:
        """The proposals as they go on the card, numbered from 1. Any error names the item and the field."""
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_ITEMS:
            count = len(items) if isinstance(items, list) else 0
            raise ProposalError(f"Cần 1..{MAX_ITEMS} mục, nhận {count}")
        result: list[dict[str, Any]] = []
        for n, raw in enumerate(items, 1):
            if not isinstance(raw, dict):
                raise ProposalError(f"mục {n}: phải là một object")
            for field in ("title", "why", "description"):
                if not _text(raw.get(field)):
                    raise ProposalError(f"mục {n}: thiếu trường {field}")
            workflow = _text(raw.get("workflow")) or "code"
            if workflow not in WORKFLOWS:
                raise ProposalError(f"mục {n}: trường workflow phải là code hoặc quick, nhận '{workflow}'")
            try:
                priority = Priority(_text(raw.get("priority")) or "medium").value
            except ValueError as exc:
                raise ProposalError(f"mục {n}: trường priority không hợp lệ") from exc
            project_id = default_project_id
            project_name = ""
            if ref := _text(raw.get("project")):
                project = await self.office.projects.find(ref)
                if project is None:
                    raise ProposalError(f"mục {n}: trường project: không có dự án '{ref}'")
                project_id, project_name = project.id, project.name
            elif project_id:
                project_name = (await self.office.projects.get(project_id)).name
            depends_on: list[int] = []
            blocked_by: list[str] = []
            for dep in raw.get("depends_on") or []:
                ref = _text(dep)
                if ref.isdigit():
                    k = int(ref)
                    if not 1 <= k < n:
                        raise ProposalError(f"mục {n}: trường depends_on: mục {k} không đứng trước mục {n}")
                    depends_on.append(k)
                elif _KEY.match(ref):
                    try:
                        blocked_by.append((await self.office.tickets.get(ref)).key)
                    except NotFound as exc:
                        raise ProposalError(f"mục {n}: trường depends_on: không có ticket {ref}") from exc
                else:
                    raise ProposalError(f"mục {n}: trường depends_on: '{ref}' không phải số mục hay key ticket")
            result.append(
                {
                    "n": n,
                    "title": _text(raw["title"])[:200],
                    "why": _text(raw["why"]),
                    "description": _text(raw["description"]),
                    "workflow": workflow,
                    "project_id": project_id,
                    "project": project_name,
                    "priority": priority,
                    "depends_on": sorted(set(depends_on)),
                    "blocked_by": sorted(set(blocked_by)),
                    "run_now": n == 1,  # only the first runs at once: same-project tickets in parallel conflict
                }
            )
        return result

    async def propose(
        self, items: Any, *, actor_id: str, actor_name: str, run_id: str | None, ticket_id: int | None, conversation_id: str | None
    ) -> tuple[Interaction, list[str]]:
        """One pending card with every proposal; an older pending card of the same actor and place is withdrawn."""
        office = self.office
        source = await office.tickets.get(ticket_id) if ticket_id is not None else None
        default_project = None
        if source is not None:
            default_project = source.project_id
        elif conversation_id:
            default_project = (await office.chat.get(conversation_id)).project_id
        checked = await self.validate(items, default_project_id=default_project or office.chat.default_project_id)
        withdrawn: list[str] = []
        for old in await office.interactions.pending():
            same_place = old.ticket_id == ticket_id and old.payload.get("conversation_id") == (None if source else conversation_id)
            if old.kind == InteractionKind.PROPOSE_TICKETS and old.actor_id == actor_id and same_place and await office.interactions.cancel(old.id):
                withdrawn.append(old.id)
        item = await office.interactions.create(
            InteractionKind.PROPOSE_TICKETS,
            {
                "items": checked,
                "proposed_by": actor_name,
                "source_ticket": source.key if source else None,
                # a ticket's card goes to the ticket's own room (notices), a chat's card to that chat
                "conversation_id": None if source else conversation_id,
                "team_id": source.team_id if source else None,
            },
            actor_id=actor_id,
            ticket_id=source.id if source else None,
            run_id=run_id,
        )
        await office.audit.record(
            "tickets:propose",
            "proposed",
            actor_id=actor_id,
            run_id=run_id,
            ticket_id=source.id if source else None,
            target=item.id,
            detail={"by": actor_name, "source": source.key if source else f"chat:{conversation_id}", "count": len(checked)},
        )
        return item, withdrawn

    # ---- answer -------------------------------------------------------------------------------------

    async def can_approve(self, by: str, source: Ticket | None) -> bool:
        if by == LOCAL_OWNER_ID:
            return True
        if source is not None:
            return await self.office.teams.can_act(by, source, APPROVER_ROLES)
        return await self.office.teams.has_role_anywhere(by, APPROVER_ROLES)

    async def answer(self, item: Interaction, response: dict[str, Any], *, by: str) -> Interaction:
        office = self.office
        p = item.payload
        source = await office.tickets.get(item.ticket_id) if item.ticket_id else None
        if not await self.can_approve(by, source):
            raise PolicyDenied("Chỉ owner hoặc lead được duyệt đề xuất ticket")
        chosen, dropped = await self._chosen(p.get("items") or [], response) if response.get("approved") else ([], [])
        if not chosen:
            resolved = await office.interactions.resolve(item.id, {"approved": False}, by=by)
            await office.audit.record("tickets:propose", "dropped", ticket_id=item.ticket_id, target=item.id, detail={"by": by})
            await self._announce(item, f"🗑 Đã bỏ đề xuất {len(p.get('items') or [])} ticket của {p.get('proposed_by')}.")
            return resolved
        await office.interactions.resolve(item.id, {"approved": True}, by=by)
        created, errors = await self._create(item, chosen, dropped)
        result = {"approved": True, "created": [t.key for t in created.values()], "dropped": dropped, "errors": errors}
        resolved = await office.interactions.set_response(item.id, result)
        await office.audit.record(
            "tickets:propose", "approved", ticket_id=item.ticket_id, target=item.id, detail={"by": by, "created": result["created"], "dropped": dropped}
        )
        lines = [f"✅ Đã tạo {len(created)} ticket từ đề xuất của {p.get('proposed_by')}:"]
        for it in chosen:
            if ticket := created.get(it["n"]):
                lines.append(f"- {ticket.key} · {ticket.title} ({'chạy ngay' if it['run_now'] else 'backlog'})")
        if dropped:
            lines.append(f"Bỏ mục: {', '.join(str(n) for n in dropped)}")
        lines += [f"⚠️ {e}" for e in errors]
        await self._announce(item, "\n".join(lines), keys=result["created"])
        return resolved

    async def _chosen(self, items: list[dict[str, Any]], response: dict[str, Any]) -> tuple[list[dict[str, Any]], list[int]]:
        """The items to create, with the person's edits (web) or minus the dropped numbers (Telegram)."""
        edits = {int(e["n"]): e for e in response.get("items") or [] if isinstance(e, dict) and str(e.get("n", "")).isdigit()}
        drop = {int(n) for n in response.get("drop") or []}
        chosen, dropped = [], []
        for it in items:
            n = int(it["n"])
            edit = edits.get(n, {})
            if n in drop or edit.get("selected") is False:
                dropped.append(n)
                continue
            merged = {**it}
            for field in ("title", "description"):
                if field in edit:
                    merged[field] = _text(edit[field])
                    if not merged[field]:
                        raise ProposalError(f"mục {n}: {field} không được trống")
            if "workflow" in edit:
                merged["workflow"] = _text(edit["workflow"])
                if merged["workflow"] not in WORKFLOWS:
                    raise ProposalError(f"mục {n}: quy trình phải là code hoặc quick")
            if edit.get("project_id") and edit["project_id"] != it.get("project_id"):
                merged["project_id"] = (await self.office.projects.get(str(edit["project_id"]))).id
            if "run_now" in edit:
                merged["run_now"] = bool(edit["run_now"])
            merged["title"] = merged["title"][:200]
            chosen.append(merged)
        return chosen, dropped

    async def _create(self, item: Interaction, chosen: list[dict[str, Any]], dropped: list[int]) -> tuple[dict[int, Ticket], list[str]]:
        """Create every ticket first, in `backlog` (in order, so keys grow with the list), then start the "run now"
        ones. The card is already resolved: an item that fails is reported in `errors`, never stops the others, so
        the response, the audit and the notice always list what was created."""
        p = item.payload
        titles = {int(it["n"]): it["title"] for it in p.get("items") or []}
        origin = p.get("source_ticket") or "chat"
        created: dict[int, Ticket] = {}
        errors: list[str] = []
        for it in chosen:
            n = int(it["n"])
            try:
                created[n] = await self._create_one(it, p, origin, titles, dropped, created, errors)
            except Exception as exc:  # e.g. the project was removed after the proposal
                log.exception("could not create proposed ticket %s of %s", n, item.id)
                errors.append(f"mục {n}: không tạo được ({exc})")
        for it in chosen:
            ticket = created.get(int(it["n"]))
            if ticket is None or not it["run_now"]:
                continue
            try:
                created[int(it["n"])] = await self._start(ticket.id, it["workflow"], by="proposal")
            except Exception as exc:
                log.warning("could not start %s: %s", ticket.key, exc)
                errors.append(f"{ticket.key}: chưa chạy được quy trình, vẫn ở Backlog ({exc})")
        return created, errors

    async def _create_one(
        self, it: dict[str, Any], p: dict[str, Any], origin: str, titles: dict[int, str], dropped: list[int], created: dict[int, Ticket], errors: list[str]
    ) -> Ticket:
        office = self.office
        blockers = [created[d].id for d in it["depends_on"] if d in created]
        for d in it["depends_on"]:
            if d not in created and d not in dropped:
                errors.append(f"mục {it['n']}: mục {d} không tạo được, bỏ phụ thuộc")
        for key in it.get("blocked_by") or []:
            try:
                blockers.append((await office.tickets.get(key)).id)
            except NotFound:
                errors.append(f"mục {it['n']}: ticket {key} không còn, bỏ phụ thuộc")
        notes = [f'Bỏ phụ thuộc vào "{titles.get(d, d)}" (không được chọn).' for d in it["depends_on"] if d in dropped]
        body = "\n\n".join(
            x for x in (it["description"], f"**Vì sao:** {it['why']}" if it.get("why") else "", f"_Đề xuất từ {origin} bởi {p.get('proposed_by')}._", "\n".join(notes)) if x
        )
        team_id = p.get("team_id")
        if not team_id and it.get("project_id"):
            team_id = (await office.projects.get(it["project_id"])).team_id
        ticket = await office.tickets.create(
            it["title"],
            description=body,
            type="task",
            project_id=it.get("project_id"),
            priority=Priority(it.get("priority") or "medium"),
            created_by=str(p.get("proposed_by") or "user"),
            status=S.BACKLOG,
            blocked_by=blockers,
        )
        # the suggested workflow waits in stage_template until it starts (stage stays NULL until then)
        await office.db.execute("UPDATE tickets SET stage_template = ?, team_id = ? WHERE id = ?", (it["workflow"], team_id, ticket.id))
        return await office.tickets.get(ticket.id)

    async def _start(self, ticket_id: int, workflow: str, *, by: str) -> Ticket:
        """Take a backlog ticket (atomic: two clicks cannot both start it) and start its workflow; an open blocker
        makes it wait in `blocked`. When the start fails, the ticket goes back to the Backlog list to be retried."""
        office = self.office
        cur = await office.db.execute(
            "UPDATE tickets SET status = 'todo', updated_at = ? WHERE id = ? AND status = 'backlog' AND stage IS NULL", (utcnow(), ticket_id)
        )
        if cur.rowcount != 1:
            raise Conflict(f"{(await office.tickets.get(ticket_id)).key} không nằm trong Backlog")
        try:
            if await office.tickets.open_blockers(ticket_id):
                await office.tickets.transition(ticket_id, S.BLOCKED, by=by)
            return await office.stages.start(ticket_id, template=workflow, by=by)
        except Exception:
            await office.db.execute("UPDATE tickets SET status = 'backlog', updated_at = ? WHERE id = ? AND stage IS NULL", (utcnow(), ticket_id))
            office.bus.publish("ticket.updated", ticket=(await office.tickets.get(ticket_id)).model_dump())
            raise

    async def _announce(self, item: Interaction, text: str, *, keys: list[str] | None = None) -> None:
        office = self.office
        if item.ticket_id:
            await office.tickets.add_comment(item.ticket_id, text, author_kind="system", author_id=None)
        elif conv := item.payload.get("conversation_id"):
            await office.chat.post_system(conv, text, kind=MessageKind.SYSTEM, meta={"ticket_keys": keys or []})

    # ---- backlog ------------------------------------------------------------------------------------

    async def backlog(self, limit: int = BACKLOG_LIMIT) -> tuple[int, list[dict[str, Any]]]:
        """Tickets waiting in `backlog` that have not entered a workflow yet, oldest first."""
        office = self.office
        count = await office.db.fetchone("SELECT COUNT(*) AS n FROM tickets WHERE status = 'backlog' AND stage IS NULL")
        rows = await office.db.fetchall(
            "SELECT id, key, title, team_id, stage_template, project_id, created_at FROM tickets "
            "WHERE status = 'backlog' AND stage IS NULL ORDER BY id LIMIT ?",
            (limit,),
        )
        items = []
        for r in rows:
            items.append(
                {
                    "id": r["id"], "ticket": r["key"], "title": r["title"], "team_id": r["team_id"],
                    "template": r["stage_template"] or "code", "project_id": r["project_id"], "created_at": r["created_at"],
                    "blockers": await office.tickets.open_blockers(r["id"]),
                }
            )
        return (count["n"] if count else 0), items

    async def start_backlog(self, ref: int | str, *, by: str) -> Ticket:
        """The "Bắt đầu" button: run the suggested workflow (code when none); still blocked → `blocked` (A-3)."""
        office = self.office
        ticket = await office.tickets.get(ref)
        workflow = ticket.stage_template or "code"
        ticket = await self._start(ticket.id, workflow, by=by)
        await office.audit.record("tickets:backlog", "started", ticket_id=ticket.id, target=ticket.key, detail={"by": by, "workflow": workflow})
        return ticket
