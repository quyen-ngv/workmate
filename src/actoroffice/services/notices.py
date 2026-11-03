"""Mirrors work events into chat: progress, reports and pending decisions show up where the user talks.

Target conversation: the ticket's origin conversation (or the proposal's), otherwise the actor's DM in
the ticket's project.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from actoroffice.domain.chat import MessageKind
from actoroffice.domain.errors import OfficeError
from actoroffice.services.chat import MENTION, handle

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

STATUS_TEXT = {
    "in_progress": "bắt đầu làm",
    "in_review": "đang chờ bạn",
    "blocked": "bị chặn",
    "done": "đã xong",
    "cancelled": "đã huỷ",
}
KIND_TEXT = {"lesson": "bài học", "fact": "thông tin", "preference": "sở thích", "decision": "quyết định"}


class ChatNotifier:
    def __init__(self, office: Office) -> None:
        self.office = office

    async def run(self, ready: asyncio.Event | None = None) -> None:
        async with self.office.bus.subscribe() as queue:
            if ready:
                ready.set()
            while True:
                event = await queue.get()
                try:
                    await self.handle(event.kind, event.data)
                except OfficeError:
                    log.exception("could not mirror %s into chat", event.kind)
                except Exception:
                    log.exception("notifier failed on %s", event.kind)

    async def handle(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "ticket.updated" and data.get("previous") and data["previous"] != data["ticket"]["status"]:
            await self._ticket_status(data)
        elif kind == "ticket.created":
            ticket = data["ticket"]
            if ticket.get("parent_id") and (ticket.get("origin_conversation_id") or ticket.get("assignee_actor_id")):
                conv_id = await self._ticket_conv(ticket, team=True)
                if conv_id:
                    await self._post_ticket(conv_id, ticket, "created")
        elif kind == "interaction.created":
            if data["interaction"]["kind"] != "ask_colleague":  # the question itself is already in the group
                await self._interaction(data["interaction"])
        elif kind == "chat.message":
            await self._colleague_answer(data["message"])
        elif kind == "ticket.change":
            await self._change(data)
        elif kind == "memory.saved":
            await self._memory(data)
        elif kind == "skill.saved" and data.get("author") not in ("user", "Bạn"):
            await self._skill(data)

    # ---- tickets ------------------------------------------------------------------------------

    async def _ticket_status(self, data: dict[str, Any]) -> None:
        ticket = data["ticket"]
        status = ticket["status"]
        if status not in STATUS_TEXT:
            return
        conv_id = await self._ticket_conv(ticket)
        if not conv_id:
            return
        by_actor = data.get("by") not in (None, "user", "office")
        if status == "done" and by_actor and ticket.get("assignee_actor_id"):
            await self._report(conv_id, ticket, data.get("comment") or "")
        else:
            await self._post_ticket(conv_id, ticket, status, note=data.get("comment"))

    async def _report(self, conv_id: str, ticket: dict[str, Any], summary: str) -> None:
        """The assignee reports finished work in the conversation where it was asked for."""
        office = self.office
        actor = await office.org.get_actor(ticket["assignee_actor_id"])
        products = await office.tickets.work_products(ticket["id"])
        lines = [f"✅ **{ticket['key']} · {ticket['title']}** — đã xong", "", summary.strip() or "(không có tóm tắt)"]
        if products:
            lines += ["", "**Kết quả bàn giao:**"]
            for p in products[:10]:
                note = (p.get("metadata") or {}).get("note") or ""
                lines.append(f"- {p['kind']}: `{p['ref']}`" + (f" — {note}" if note else ""))
        await office.chat.post_actor(
            conv_id,
            actor,
            "\n".join(lines),
            meta={"ticket_key": ticket["key"], "status": "done", "report": True},
            wake=False,  # a report never wakes anyone
        )

    async def _post_ticket(self, conv_id: str, ticket: dict[str, Any], event: str, note: str | None = None) -> None:
        names = {a.id: a.name for a in await self.office.org.list_actors()}
        who = names.get(ticket.get("assignee_actor_id") or "", "")
        verb = "được tạo" if event == "created" else STATUS_TEXT.get(event, event)
        body = f"{ticket['key']} · {ticket['title']} — {who} {verb}".strip()
        await self.office.chat.post_system(
            conv_id,
            body,
            kind=MessageKind.TICKET,
            meta={"ticket_key": ticket["key"], "status": ticket["status"], "event": event, "note": note or ""},
        )

    # ---- decisions, memory, skills -------------------------------------------------------------

    async def _interaction(self, item: dict[str, Any]) -> None:
        payload = item["payload"]
        origin = payload.get("conversation_id")
        project_id = None
        if item.get("ticket_id") and not origin:
            ticket = await self.office.tickets.get(item["ticket_id"])
            conv_id = await self._ticket_conv(ticket.model_dump())
        else:
            conv_id = await self._target(origin, item.get("actor_id"), project_id)
        if conv_id:
            await self.office.chat.post_system(
                conv_id, _summary(item), kind=MessageKind.INTERACTION, meta={"interaction_id": item["id"]}
            )

    async def _colleague_answer(self, message: dict[str, Any]) -> None:
        """A reply in a meeting group answers the questions asked there (by that colleague, or by the user)."""
        if message["kind"] != MessageKind.TEXT.value or message["author_kind"] not in ("actor", "user"):
            return
        meta = message.get("meta") or {}
        if meta.get("report") or meta.get("automation_id") or meta.get("interaction_id"):
            return
        questions = await self.office.interactions.colleague_questions(message["conversation_id"])
        by_user = message["author_kind"] == "user"
        run = await self.office.runs.get(meta["run_id"]) if meta.get("run_id") else None
        for q in questions:
            if by_user:
                # The user answers a question only when they address the asker or the colleague
                # (a plain "tiếp tục" in the room is not an answer to everything pending there).
                names = {handle(q.payload.get("asked_by", "")), handle(q.payload.get("to", ""))} - {""}
                if not ({handle(h) for h in MENTION.findall(message["body"])} & names):
                    continue
            elif q.payload.get("to_actor_id") != message["author_id"]:
                continue
            elif run is not None and run.started_at and run.started_at < q.created_at:
                continue  # written by a turn that started before the question existed: not an answer to it
            who = "user" if by_user else q.payload.get("to")
            try:
                await self.office.interactions.resolve(q.id, {"answer": message["body"], "answered_by": who}, by=who or "user")
            except OfficeError:
                continue  # answered meanwhile

    async def _change(self, data: dict[str, Any]) -> None:
        office = self.office
        source = await office.tickets.get(data["source"])
        root = await office.tickets.root_of(source.id)
        result = data.get("result") or {}
        touched = ", ".join(result.get("notified", []) + result.get("reopened", [])) or "không có ticket nào đang mở"
        text = f"⚠️ {data['by']} ghi nhận thay đổi yêu cầu ({source.key}): {data['summary']}\n→ đã báo: {touched}"
        if result.get("reopened"):
            text += f"\n→ mở lại: {', '.join(result['reopened'])}"
        targets = {root.origin_conversation_id} if root.origin_conversation_id else set()
        row = await office.db.fetchone("SELECT id FROM conversations WHERE ticket_id = ? AND kind = 'group' AND archived = 0", (root.id,))
        if row:
            targets.add(row["id"])
        for conv_id in targets:
            await office.chat.post_system(conv_id, text, meta={"ticket_key": source.key, "change": True})

    async def _memory(self, data: dict[str, Any]) -> None:
        memory = data["memory"]
        if memory.get("created_by") in ("user", "Bạn", "compaction"):
            return  # added by hand or rewritten by a compaction: no need to announce each one
        actor = await self.office.org.get_actor(data["actor_id"])
        conv_id, project_id = data.get("conversation_id"), None
        if not conv_id and memory.get("source_ticket_id"):
            ticket = await self.office.tickets.get(memory["source_ticket_id"])
            conv_id, project_id = ticket.origin_conversation_id, ticket.project_id
        conv_id = await self._target(conv_id, actor.id, project_id)
        if conv_id:
            kind = KIND_TEXT.get(memory["kind"], memory["kind"])
            await self.office.chat.post_system(
                conv_id,
                f"📌 {actor.name} ghi nhớ ({kind}): {memory['content']}",
                kind=MessageKind.MEMORY,
                meta={"memory_id": memory["id"], "actor_id": actor.id},
            )

    async def _skill(self, data: dict[str, Any]) -> None:
        skill = data["skill"]
        actor = await self.office.org.find_actor(str(data.get("author") or ""))
        if actor is None:
            return
        conv_id, project_id = data.get("conversation_id"), None
        if not conv_id and data.get("ticket_id"):
            ticket = await self.office.tickets.get(data["ticket_id"])
            conv_id, project_id = ticket.origin_conversation_id, ticket.project_id
        conv_id = await self._target(conv_id, actor.id, project_id)
        if conv_id:
            await self.office.chat.post_system(
                conv_id, f"📚 {actor.name} đã lưu skill '{skill['ref']['name']}': {skill['description']}"
            )

    async def _ticket_conv(self, ticket: dict[str, Any], *, team: bool = False) -> str | None:
        """Sub-ticket news goes to the feature's team room (created on first need when `team`), so the person who
        asked for the feature only gets the final report in their chat; root-ticket news goes to that chat."""
        office = self.office
        if ticket.get("parent_id"):
            root = await office.tickets.root_of(ticket["id"])
            members = [root.assignee_actor_id, ticket.get("assignee_actor_id")]
            room = await office.chat.ticket_room(root, [m for m in members if m], by="office", create=team)
            if room is not None:
                return room.id
        return await self._target(ticket.get("origin_conversation_id"), ticket.get("assignee_actor_id"), ticket.get("project_id"))

    async def _target(self, conversation_id: str | None, actor_id: str | None, project_id: str | None) -> str | None:
        if conversation_id:
            return conversation_id
        if actor_id:
            return (await self.office.chat.dm(actor_id, project_id)).id
        return None


def _summary(item: dict[str, Any]) -> str:
    p = item["payload"]
    kind = item["kind"]
    if kind == "approve_tool":
        return f"{p.get('actor')} xin phép chạy {p.get('tool')}: {p.get('target')}"
    if kind == "question":
        return f"{p.get('asked_by')} hỏi: {p.get('question')}"
    if kind == "confirm_ticket":
        return f"{p.get('proposed_by')} đề xuất ticket: {p.get('title')} → {p.get('assignee')}"
    if kind == "approve_access":
        return f"{p.get('actor')} xin quyền sửa thư mục dự án {p.get('project')}: {p.get('reason')}"
    if kind == "approve_automation":
        return f"{p.get('owner')} xin duyệt automation {p.get('name')} ({p.get('schedule_text')}): {p.get('description')}"
    if kind == "propose_tickets":
        titles = "; ".join(str(i.get("title")) for i in p.get("items") or [])
        return f"{p.get('proposed_by')} đề xuất {len(p.get('items') or [])} ticket: {titles}"
    return kind
