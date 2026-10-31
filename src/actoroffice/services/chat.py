"""Chat: DMs and groups (the user is always a member), @mentions and who gets woken.

Wake rules:
- DM: every user message wakes the actor.
- Group: only @mentioned actors (or @all) are woken; mentioning someone who is not in the group adds them.
- Actor → actor mentions wake too, but at most `max_unattended` actor messages in a row without the user;
  after that the conversation pauses until the user writes again (no endless agent chatter).
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Any

from actoroffice.core.events import EventBus
from actoroffice.domain.chat import Conversation, ConversationKind, Member, Message, MessageKind
from actoroffice.domain.errors import NotFound
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.org import Actor
from actoroffice.domain.run import WakeSource
from actoroffice.store import Database

from .org import OrgService
from .wakes import WakeService

MENTION = re.compile(r"(?<![\w@])@([\w.\-]+)", re.UNICODE)
USER_ID = "user"


def handle(name: str) -> str:
    """'Minh Anh' → 'minhanh'; accents removed so '@Hung' matches 'Hùng'."""
    text = unicodedata.normalize("NFD", name.lower()).replace("đ", "d")
    return "".join(c for c in text if c.isalnum() and not unicodedata.combining(c))


class ChatService:
    def __init__(self, db: Database, bus: EventBus, org: OrgService, wakes: WakeService, *, max_unattended: int = 8) -> None:
        self.db = db
        self.bus = bus
        self.org = org
        self.wakes = wakes
        self.max_unattended = max_unattended
        self.default_project_id: str | None = None  # set by the office once the default project exists
        self._room_locks: dict[int, asyncio.Lock] = {}

    # ---- conversations ----------------------------------------------------------------------

    async def dm(self, actor_id: str, project_id: str | None = None) -> Conversation:
        """One direct chat per (actor, project)."""
        project_id = project_id or self.default_project_id
        row = await self.db.fetchone(
            "SELECT c.id FROM conversations c JOIN participants p ON p.conversation_id = c.id "
            "WHERE c.kind = 'dm' AND p.member_kind = 'actor' AND p.member_id = ? AND c.project_id IS ?",
            (actor_id, project_id),
        )
        if row:
            return await self.get(row["id"])
        actor = await self.org.get_actor(actor_id)
        return await self._create(ConversationKind.DM, actor.name, [actor_id], created_by=USER_ID, project_id=project_id)

    async def create_group(
        self,
        title: str,
        actor_ids: list[str],
        *,
        created_by: str = USER_ID,
        ticket_id: int | None = None,
        project_id: str | None = None,
    ) -> Conversation:
        conv = await self._create(
            ConversationKind.GROUP,
            title.strip() or "Nhóm",
            list(dict.fromkeys(actor_ids)),
            created_by=created_by,
            ticket_id=ticket_id,
            project_id=project_id or self.default_project_id,
        )
        names = [(await self.org.get_actor(a)).name for a in conv.actor_ids]
        creator = "Bạn" if created_by in (USER_ID, "Bạn") else created_by
        await self.post_system(conv.id, f"{creator} đã tạo nhóm. Thành viên: bạn, {', '.join(names)}")
        return conv

    async def _create(
        self,
        kind: ConversationKind,
        title: str,
        actor_ids: list[str],
        *,
        created_by: str,
        ticket_id: int | None = None,
        project_id: str | None = None,
    ) -> Conversation:
        conv_id = new_id("cv")
        now = utcnow()
        async with self.db.tx() as tx:
            await tx.execute(
                "INSERT INTO conversations(id, kind, title, project_id, ticket_id, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (conv_id, kind.value, title, project_id, ticket_id, created_by, now, now),
            )
            await tx.execute("INSERT INTO participants VALUES (?, 'user', ?)", (conv_id, USER_ID))
            for actor_id in actor_ids:
                await tx.execute("INSERT OR IGNORE INTO participants VALUES (?, 'actor', ?)", (conv_id, actor_id))
        conv = await self.get(conv_id)
        self.bus.publish("chat.conversation", conversation=conv.model_dump())
        return conv

    async def ticket_room(self, root: Any, actor_ids: list[str], *, by: str, create: bool = True) -> Conversation | None:
        """The group chat of a ticket tree (one per feature): where the team talks and reports; the user is a member."""
        async with self._room_locks.setdefault(root.id, asyncio.Lock()):
            return await self._ticket_room(root, actor_ids, by=by, create=create)

    async def _ticket_room(self, root: Any, actor_ids: list[str], *, by: str, create: bool) -> Conversation | None:
        row = await self.db.fetchone(
            "SELECT id FROM conversations WHERE ticket_id = ? AND kind = 'group' AND archived = 0 ORDER BY created_at LIMIT 1",
            (root.id,),
        )
        if row:
            conv = await self.get(row["id"])
            missing = [a for a in dict.fromkeys(actor_ids) if a and a not in conv.actor_ids]
            if missing:
                await self.add_members(conv.id, missing, by=by)
                conv = await self.get(conv.id)
            return conv
        if not create:
            return None
        ids = [a for a in dict.fromkeys(actor_ids) if a]
        return await self.create_group(f"Họp {root.key}: {root.title}"[:80], ids, created_by=by, ticket_id=root.id, project_id=root.project_id)

    async def add_members(self, conversation_id: str, actor_ids: list[str], *, by: str) -> None:
        conv = await self.get(conversation_id)
        new = [a for a in actor_ids if a not in conv.actor_ids]
        if not new:
            return
        for actor_id in new:
            await self.db.execute("INSERT OR IGNORE INTO participants VALUES (?, 'actor', ?)", (conversation_id, actor_id))
        names = [(await self.org.get_actor(a)).name for a in new]
        await self.post_system(conversation_id, f"{by} đã thêm {', '.join(names)} vào nhóm")
        self.bus.publish("chat.conversation", conversation=(await self.get(conversation_id)).model_dump())

    async def get(self, conversation_id: str) -> Conversation:
        row = await self.db.fetchone("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        if not row:
            raise NotFound(f"Conversation {conversation_id} not found")
        members = await self.db.fetchall(
            "SELECT member_kind, member_id FROM participants WHERE conversation_id = ?", (conversation_id,)
        )
        return _conversation(row, members)

    async def list(self, project_id: str | None = None) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT c.*, (SELECT MAX(id) FROM messages m WHERE m.conversation_id = c.id) AS last_message_id "
            "FROM conversations c WHERE c.archived = 0" + (" AND c.project_id = ?" if project_id else "")
            + " ORDER BY COALESCE(c.updated_at, c.created_at) DESC",
            (project_id,) if project_id else (),
        )
        result = []
        for row in rows:
            members = await self.db.fetchall("SELECT member_kind, member_id FROM participants WHERE conversation_id = ?", (row["id"],))
            conv = _conversation(row, members)
            result.append({**conv.model_dump(), "last_message_id": row["last_message_id"]})
        return result

    # ---- messages ---------------------------------------------------------------------------

    async def messages(self, conversation_id: str, *, after_id: int = 0, limit: int = 200) -> list[Message]:
        if after_id:
            rows = await self.db.fetchall(
                "SELECT * FROM messages WHERE conversation_id = ? AND id > ? ORDER BY id LIMIT ?", (conversation_id, after_id, limit)
            )
        else:  # latest `limit` messages, oldest first
            rows = await self.db.fetchall(
                "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id",
                (conversation_id, limit),
            )
        return [_message(r) for r in rows]

    async def post_user(self, conversation_id: str, body: str, *, user_id: str = USER_ID, wake: bool = True) -> Message:
        """A person writes. `user_id` is "user" for the local owner, or a registered person's id (team mode)."""
        conv = await self.get(conversation_id)
        mentioned = await self._mentions(conv, body, author_actor=None)
        if conv.kind == ConversationKind.GROUP and wake:
            await self._add_mentioned_outsiders(conv, mentioned, by="Bạn")
        msg = await self._insert(conv.id, "user", user_id, body, mentions=mentioned)
        if not wake:
            return msg
        targets = conv.actor_ids if conv.kind == ConversationKind.DM else mentioned
        for actor_id in targets:
            if conv.kind == ConversationKind.GROUP and await self._waiting_here(actor_id, conv.id):
                continue
            await self.wakes.enqueue(actor_id, WakeSource.MENTION, conversation_id=conv.id, payload={"message_id": msg.id})
        return msg

    async def post_actor(
        self, conversation_id: str, actor: Actor, body: str, *, meta: dict[str, Any] | None = None, wake: bool = True
    ) -> Message:
        conv = await self.get(conversation_id)
        mentioned = [a for a in await self._mentions(conv, body, author_actor=actor.id) if a != actor.id] if wake else []
        if conv.kind == ConversationKind.GROUP:
            await self._add_mentioned_outsiders(conv, mentioned, by=actor.name)
        else:
            mentioned = []  # in a DM other actors are not present; use a group to involve them
        msg = await self._insert(conv.id, "actor", actor.id, body, mentions=mentioned, meta=meta)
        if mentioned:
            if await self._unattended_streak(conv.id) > self.max_unattended:
                await self.post_system(
                    conv.id, "Các actor đã trao đổi nhiều lượt liên tiếp. Tạm dừng; hãy nhắn để họ tiếp tục."
                )
            else:
                for actor_id in mentioned:
                    # Someone already replying here (or about to) will read this message anyway;
                    # waking them again only produces a second, redundant reply.
                    if await self._engaged(actor_id, conv.id):
                        continue
                    await self.wakes.enqueue(actor_id, WakeSource.MENTION, conversation_id=conv.id, payload={"message_id": msg.id})
        return msg

    async def _engaged(self, actor_id: str, conversation_id: str) -> bool:
        # An actor waiting on its own question here is woken through its ticket when the answer arrives.
        row = await self.db.fetchone(
            "SELECT (SELECT COUNT(*) FROM runs WHERE actor_id = ? AND conversation_id = ? AND status = 'running') + "
            "(SELECT COUNT(*) FROM wake_requests WHERE actor_id = ? AND conversation_id = ? AND status = 'pending') + "
            "(SELECT COUNT(*) FROM interactions WHERE actor_id = ? AND kind = 'ask_colleague' AND status = 'pending' "
            " AND json_extract(payload_json, '$.conversation_id') = ?) AS n",
            (actor_id, conversation_id, actor_id, conversation_id, actor_id, conversation_id),
        )
        return bool(row and row["n"])

    async def _waiting_here(self, actor_id: str, conversation_id: str) -> bool:
        row = await self.db.fetchone(
            "SELECT 1 FROM interactions WHERE actor_id = ? AND kind = 'ask_colleague' AND status = 'pending' "
            "AND json_extract(payload_json, '$.conversation_id') = ?",
            (actor_id, conversation_id),
        )
        return row is not None

    async def post_system(self, conversation_id: str, body: str, *, kind: MessageKind = MessageKind.SYSTEM, meta: dict[str, Any] | None = None) -> Message:
        return await self._insert(conversation_id, "system", None, body, kind=kind, meta=meta)

    async def last_actor_message_id(self, conversation_id: str, actor_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT MAX(id) AS id FROM messages WHERE conversation_id = ? AND author_kind = 'actor' AND author_id = ?",
            (conversation_id, actor_id),
        )
        return int(row["id"] or 0) if row else 0

    async def _insert(
        self,
        conversation_id: str,
        author_kind: str,
        author_id: str | None,
        body: str,
        *,
        kind: MessageKind = MessageKind.TEXT,
        mentions: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Message:
        now = utcnow()
        cur = await self.db.execute(
            "INSERT INTO messages(conversation_id, author_kind, author_id, body, mentions_json, kind, meta_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                conversation_id,
                author_kind,
                author_id,
                body,
                json.dumps(mentions or []),
                kind.value,
                json.dumps(meta or {}, ensure_ascii=False),
                now,
            ),
        )
        await self.db.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (now, conversation_id))
        msg = Message(
            id=int(cur.lastrowid or 0),
            conversation_id=conversation_id,
            author_kind=author_kind,
            author_id=author_id,
            body=body,
            kind=kind,
            mentions=mentions or [],
            meta=meta or {},
            created_at=now,
        )
        self.bus.publish("chat.message", message=msg.model_dump())
        return msg

    async def _mentions(self, conv: Conversation, body: str, *, author_actor: str | None) -> list[str]:
        handles = {m.group(1).lower() for m in MENTION.finditer(body)}
        if not handles:
            return []
        actors = await self.org.list_actors()
        by_handle = {handle(a.name): a.id for a in actors}
        found: list[str] = []
        if "all" in handles or "moinguoi" in handles:
            found += [a for a in conv.actor_ids if a != author_actor]
        for h in handles:
            actor_id = by_handle.get(handle(h))
            if actor_id and actor_id not in found:
                found.append(actor_id)
        return found

    async def _add_mentioned_outsiders(self, conv: Conversation, mentioned: list[str], *, by: str) -> None:
        outsiders = [a for a in mentioned if a not in conv.actor_ids]
        if outsiders:
            await self.add_members(conv.id, outsiders, by=by)

    async def _unattended_streak(self, conversation_id: str) -> int:
        row = await self.db.fetchone(
            "SELECT COUNT(*) AS n FROM messages WHERE conversation_id = ? AND author_kind = 'actor' AND id > "
            "COALESCE((SELECT MAX(id) FROM messages WHERE conversation_id = ? AND author_kind = 'user'), 0)",
            (conversation_id, conversation_id),
        )
        return int(row["n"]) if row else 0


def _conversation(row: dict[str, Any], members: list[dict[str, Any]]) -> Conversation:
    return Conversation(
        id=row["id"],
        kind=ConversationKind(row["kind"]),
        title=row["title"],
        project_id=row.get("project_id"),
        ticket_id=row["ticket_id"],
        created_by=row["created_by"],
        archived=bool(row["archived"]),
        members=[Member(kind=m["member_kind"], id=m["member_id"]) for m in members],
        created_at=row["created_at"],
        updated_at=row.get("updated_at"),
    )


def _message(row: dict[str, Any]) -> Message:
    return Message(
        id=row["id"],
        conversation_id=row["conversation_id"],
        author_kind=row["author_kind"],
        author_id=row["author_id"],
        body=row["body"],
        kind=MessageKind(row["kind"]),
        mentions=json.loads(row["mentions_json"]),
        meta=json.loads(row["meta_json"]),
        provenance=row["provenance"],
        created_at=row["created_at"],
    )
