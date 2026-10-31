"""Positions and actors."""

from __future__ import annotations

import json
import re

from actoroffice.domain.errors import Conflict, NotFound, OfficeError
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.org import Actor, ActorStatus, Position
from actoroffice.store import Database


# Default names offered when hiring (one word each: the name is also the @mention handle).
DEFAULT_NAMES = (
    "Emma", "Liam", "Olivia", "Noah", "Ava", "Ethan", "Mia", "Lucas", "Sophia", "Mason", "Chloe", "Jack", "Grace",
    "Leo", "Ruby", "Owen", "Zoe", "Henry", "Lily", "Ryan", "Nora", "Adam", "Ella", "Daniel", "Alice", "Sam",
)
NAME_RE = re.compile(r"^[^\W\d][\w\-]{0,31}$", re.UNICODE)


class OrgService:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def create_position(self, position: Position | None = None, **fields: object) -> Position:
        pos = position or Position(id=new_id("pos"), **fields)  # type: ignore[arg-type]
        if await self.db.fetchone("SELECT 1 FROM positions WHERE name = ?", (pos.name,)):
            raise Conflict(f"Position '{pos.name}' already exists")
        await self.db.execute(
            "INSERT INTO positions(id, name, title, config_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (pos.id, pos.name, pos.title, pos.model_dump_json(), utcnow()),
        )
        return pos

    async def get_position(self, position_id: str) -> Position:
        row = await self.db.fetchone("SELECT config_json FROM positions WHERE id = ?", (position_id,))
        if not row:
            raise NotFound(f"Position {position_id} not found")
        return Position.model_validate_json(row["config_json"])

    async def find_position(self, name: str) -> Position | None:
        row = await self.db.fetchone("SELECT config_json FROM positions WHERE name = ?", (name,))
        return Position.model_validate_json(row["config_json"]) if row else None

    async def list_positions(self) -> list[Position]:
        rows = await self.db.fetchall("SELECT config_json FROM positions ORDER BY name")
        return [Position.model_validate_json(r["config_json"]) for r in rows]

    async def update_position(self, position: Position) -> Position:
        clash = await self.db.fetchone("SELECT id FROM positions WHERE name = ? AND id <> ?", (position.name, position.id))
        if clash:
            raise Conflict(f"Position '{position.name}' already exists")
        await self.db.execute(
            "UPDATE positions SET name = ?, title = ?, config_json = ? WHERE id = ?",
            (position.name, position.title, position.model_dump_json(), position.id),
        )
        return position

    async def hire(self, position_id: str, name: str, *, emoji: str = "", specialty: str = "", model: str | None = None) -> Actor:
        await self.get_position(position_id)
        if await self.find_actor(name):
            raise Conflict(f"Actor '{name}' already exists")
        actor = Actor(id=new_id("act"), position_id=position_id, name=name, emoji=emoji, specialty=specialty, model=model)
        await self.db.execute(
            "INSERT INTO actors(id, position_id, name, config_json, status, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (actor.id, actor.position_id, actor.name, actor.model_dump_json(), actor.status.value, utcnow()),
        )
        return actor

    async def suggest_name(self) -> str:
        taken = {a.name.lower() for a in await self.list_actors()}
        for name in DEFAULT_NAMES:
            if name.lower() not in taken:
                return name
        n = 2
        while f"{DEFAULT_NAMES[0]}{n}".lower() in taken:
            n += 1
        return f"{DEFAULT_NAMES[0]}{n}"

    async def rename_actor(self, actor_id: str, name: str) -> Actor:
        """Only the actor row; Office.rename_actor also updates everything that refers to the old name."""
        name = name.strip()
        if not NAME_RE.match(name):
            raise OfficeError("Tên là một từ (chữ, số, '-' hoặc '_'), không dấu cách, vì dùng để @gọi")
        other = await self.find_actor(name)
        if other and other.id != actor_id:
            raise Conflict(f"Đã có nhân viên tên {other.name}")
        renamed = (await self.get_actor(actor_id)).model_copy(update={"name": name})
        await self.db.execute("UPDATE actors SET name = ?, config_json = ? WHERE id = ?", (name, renamed.model_dump_json(), actor_id))
        return renamed

    async def get_actor(self, actor_id: str) -> Actor:
        row = await self.db.fetchone("SELECT config_json, status FROM actors WHERE id = ?", (actor_id,))
        if not row:
            raise NotFound(f"Actor {actor_id} not found")
        return _actor(row)

    async def find_actor(self, name: str) -> Actor | None:
        row = await self.db.fetchone("SELECT config_json, status FROM actors WHERE name = ? COLLATE NOCASE", (name,))
        return _actor(row) if row else None

    async def list_actors(self) -> list[Actor]:
        rows = await self.db.fetchall("SELECT config_json, status FROM actors ORDER BY name")
        return [_actor(r) for r in rows]

    async def set_actor_status(self, actor_id: str, status: ActorStatus) -> None:
        await self.db.execute("UPDATE actors SET status = ? WHERE id = ?", (status.value, actor_id))

    async def update_actor(self, actor_id: str, **changes: object) -> Actor:
        """Profile and permission edits (emoji, specialty, model, permission_mode, allow_rules)."""
        actor = await self.get_actor(actor_id)
        allowed = {"emoji", "specialty", "model", "permission_mode", "allow_rules", "position_id"}
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"cannot change {sorted(unknown)}")
        if "position_id" in changes:
            await self.get_position(str(changes["position_id"]))
        updated = Actor.model_validate({**actor.model_dump(), **changes})
        updated.allow_rules = list(dict.fromkeys(r.strip() for r in updated.allow_rules if r.strip()))
        await self.db.execute(
            "UPDATE actors SET position_id = ?, config_json = ? WHERE id = ?",
            (updated.position_id, updated.model_dump_json(), actor_id),
        )
        return updated

    async def add_allow_rule(self, actor_id: str, rule: str) -> Actor:
        actor = await self.get_actor(actor_id)
        return await self.update_actor(actor_id, allow_rules=[*actor.allow_rules, rule])


def _actor(row: dict) -> Actor:
    data = json.loads(row["config_json"])
    data["status"] = row["status"]
    return Actor.model_validate(data)
