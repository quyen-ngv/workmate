"""People, teams, memberships and ticket participants; who holds which role for a ticket."""

from __future__ import annotations

import json
from typing import Any

from actoroffice.core.events import EventBus
from actoroffice.domain.errors import Conflict, NotFound, OfficeError
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.stage import GatePolicy, StageTemplate, default_gates, default_templates
from actoroffice.domain.team import LOCAL_OWNER_ID, Member, Role, Team, TeamSettings, User
from actoroffice.domain.work import Ticket
from actoroffice.store import Database

ROLES = {r.value for r in Role}


class TeamError(OfficeError):
    code = 422


def _roles(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for value in values or []:
        role = str(value).strip().lower()
        if role not in ROLES:
            raise TeamError(f"Vai '{value}' không hợp lệ (chọn trong: {', '.join(sorted(ROLES))})")
        if role not in out:
            out.append(role)
    return out


class TeamService:
    def __init__(self, db: Database, bus: EventBus) -> None:
        self.db = db
        self.bus = bus

    # ---- users ------------------------------------------------------------------------------------

    async def ensure_local_owner(self, name: str = "Owner") -> User:
        """The person running the office (web login, CLI): admin everywhere."""
        row = await self.db.fetchone("SELECT * FROM users WHERE id = ?", (LOCAL_OWNER_ID,))
        if row:
            return _user(row)
        await self.db.execute(
            "INSERT INTO users(id, name, default_roles_json, created_at) VALUES (?, ?, ?, ?)",
            (LOCAL_OWNER_ID, name, json.dumps([Role.ADMIN.value]), utcnow()),
        )
        return await self.get_user(LOCAL_OWNER_ID)

    async def create_user(
        self, name: str, *, roles: list[str] | None = None, telegram_id: int | None = None, email: str | None = None
    ) -> User:
        name = name.strip()
        if not name:
            raise TeamError("Cần tên người dùng")
        if await self.db.fetchone("SELECT 1 FROM users WHERE name = ? COLLATE NOCASE", (name,)):
            raise Conflict(f"Người dùng '{name}' đã tồn tại")
        if telegram_id is not None and await self.db.fetchone("SELECT 1 FROM users WHERE telegram_id = ?", (telegram_id,)):
            raise Conflict(f"Telegram id {telegram_id} đã gắn với người khác")
        user = User(id=new_id("usr"), name=name, email=email, telegram_id=telegram_id, default_roles=_roles(roles), created_at=utcnow())
        await self.db.execute(
            "INSERT INTO users(id, name, email, telegram_id, default_roles_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user.id, user.name, email, telegram_id, json.dumps(user.default_roles), user.created_at),
        )
        self.bus.publish("people.changed")
        return user

    async def update_user(
        self,
        user_id: str,
        *,
        name: str | None = None,
        roles: list[str] | None = None,
        telegram_id: int | None = None,
        email: str | None = None,
        active: bool | None = None,
    ) -> User:
        user = await self.get_user(user_id)
        updated = user.model_copy(
            update={
                "name": name.strip() if name else user.name,
                "default_roles": _roles(roles) if roles is not None else user.default_roles,
                "telegram_id": telegram_id if telegram_id is not None else user.telegram_id,
                "email": email if email is not None else user.email,
                "active": user.active if active is None else active,
            }
        )
        await self.db.execute(
            "UPDATE users SET name = ?, email = ?, telegram_id = ?, default_roles_json = ?, active = ? WHERE id = ?",
            (updated.name, updated.email, updated.telegram_id, json.dumps(updated.default_roles), 1 if updated.active else 0, user_id),
        )
        self.bus.publish("people.changed")
        return updated

    async def get_user(self, user_id: str) -> User:
        row = await self.db.fetchone("SELECT * FROM users WHERE id = ?", (user_id,))
        if not row:
            raise NotFound(f"Người dùng {user_id} không tồn tại")
        return _user(row)

    async def find_user(self, ref: str) -> User | None:
        ref = ref.strip().lstrip("@")
        row = await self.db.fetchone("SELECT * FROM users WHERE id = ? OR name = ? COLLATE NOCASE", (ref, ref))
        return _user(row) if row else None

    async def user_by_telegram(self, telegram_id: int) -> User | None:
        row = await self.db.fetchone("SELECT * FROM users WHERE telegram_id = ? AND active = 1", (telegram_id,))
        return _user(row) if row else None

    async def list_users(self) -> list[User]:
        return [_user(r) for r in await self.db.fetchall("SELECT * FROM users ORDER BY name COLLATE NOCASE")]

    async def has_people(self) -> bool:
        """True once anyone besides the local owner is registered (multi-user mode)."""
        row = await self.db.fetchone("SELECT 1 FROM users WHERE id <> ? LIMIT 1", (LOCAL_OWNER_ID,))
        return row is not None

    # ---- teams ------------------------------------------------------------------------------------

    async def create_team(
        self, name: str, *, jira_project_key: str | None = None, confluence_space: str | None = None, settings: TeamSettings | None = None
    ) -> Team:
        name = name.strip()
        if not name:
            raise TeamError("Cần tên team")
        if await self.db.fetchone("SELECT 1 FROM teams WHERE name = ?", (name,)):
            raise Conflict(f"Team '{name}' đã tồn tại")
        team = Team(
            id=new_id("team"),
            name=name,
            jira_project_key=(jira_project_key or "").strip().upper() or None,
            confluence_space=confluence_space,
            settings=settings or TeamSettings(),
            created_at=utcnow(),
        )
        await self.db.execute(
            "INSERT INTO teams(id, name, jira_project_key, confluence_space, settings_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (team.id, team.name, team.jira_project_key, team.confluence_space, team.settings.model_dump_json(), team.created_at),
        )
        self.bus.publish("people.changed")
        return team

    async def update_team(self, team: Team) -> Team:
        await self.db.execute(
            "UPDATE teams SET name = ?, jira_project_key = ?, confluence_space = ?, confluence_parent_id = ?, settings_json = ? WHERE id = ?",
            (team.name, team.jira_project_key, team.confluence_space, team.confluence_parent_id, team.settings.model_dump_json(), team.id),
        )
        self.bus.publish("people.changed")
        return await self.get_team(team.id)

    async def get_team(self, ref: str) -> Team:
        row = await self.db.fetchone("SELECT * FROM teams WHERE id = ? OR name = ? COLLATE NOCASE", (ref, ref))
        if not row:
            raise NotFound(f"Team {ref} không tồn tại")
        return _team(row)

    async def list_teams(self) -> list[Team]:
        return [_team(r) for r in await self.db.fetchall("SELECT * FROM teams ORDER BY name COLLATE NOCASE")]

    async def add_member(self, team_id: str, user_id: str, roles: list[str]) -> None:
        await self.get_team(team_id)
        await self.get_user(user_id)
        await self.db.execute(
            "INSERT INTO memberships(team_id, user_id, roles_json) VALUES (?, ?, ?) "
            "ON CONFLICT(team_id, user_id) DO UPDATE SET roles_json = excluded.roles_json",
            (team_id, user_id, json.dumps(_roles(roles))),
        )
        self.bus.publish("people.changed")

    async def remove_member(self, team_id: str, user_id: str) -> None:
        await self.db.execute("DELETE FROM memberships WHERE team_id = ? AND user_id = ?", (team_id, user_id))
        self.bus.publish("people.changed")

    async def members(self, team_id: str) -> list[Member]:
        rows = await self.db.fetchall(
            "SELECT u.*, m.roles_json AS member_roles FROM memberships m JOIN users u ON u.id = m.user_id "
            "WHERE m.team_id = ? AND u.active = 1 ORDER BY u.name COLLATE NOCASE",
            (team_id,),
        )
        return [Member(user=_user(r), roles=json.loads(r["member_roles"])) for r in rows]

    async def teams_of(self, user_id: str) -> list[Team]:
        rows = await self.db.fetchall(
            "SELECT t.* FROM teams t JOIN memberships m ON m.team_id = t.id WHERE m.user_id = ? ORDER BY t.name", (user_id,)
        )
        return [_team(r) for r in rows]

    # ---- ticket participants ----------------------------------------------------------------------

    async def set_participant(self, ticket_id: int, user_id: str, roles: list[str]) -> None:
        await self.get_user(user_id)
        await self.db.execute(
            "INSERT INTO ticket_participants(ticket_id, user_id, roles_json) VALUES (?, ?, ?) "
            "ON CONFLICT(ticket_id, user_id) DO UPDATE SET roles_json = excluded.roles_json",
            (ticket_id, user_id, json.dumps(_roles(roles))),
        )
        self.bus.publish("ticket.people", ticket_id=ticket_id)

    async def remove_participant(self, ticket_id: int, user_id: str) -> None:
        await self.db.execute("DELETE FROM ticket_participants WHERE ticket_id = ? AND user_id = ?", (ticket_id, user_id))
        self.bus.publish("ticket.people", ticket_id=ticket_id)

    async def participants(self, ticket_id: int) -> list[Member]:
        rows = await self.db.fetchall(
            "SELECT u.*, p.roles_json AS member_roles FROM ticket_participants p JOIN users u ON u.id = p.user_id "
            "WHERE p.ticket_id = ? AND u.active = 1 ORDER BY u.name COLLATE NOCASE",
            (ticket_id,),
        )
        return [Member(user=_user(r), roles=json.loads(r["member_roles"])) for r in rows]

    # ---- roles, templates, gates ------------------------------------------------------------------

    async def roles_for(self, user_id: str, ticket: Ticket | None) -> set[str]:
        """Every role this person holds for the ticket: its own participant roles, the team's roles, their
        default roles, "owner" when they asked for it. Admins hold every role."""
        try:
            user = await self.get_user(user_id)
        except NotFound:
            return set()
        roles = set(user.default_roles)
        if ticket is not None:
            if ticket.owner_user_id == user_id:
                roles.add(Role.OWNER.value)
            row = await self.db.fetchone(
                "SELECT roles_json FROM ticket_participants WHERE ticket_id = ? AND user_id = ?", (ticket.id, user_id)
            )
            if row:
                roles |= set(json.loads(row["roles_json"]))
            if ticket.team_id:
                row = await self.db.fetchone(
                    "SELECT roles_json FROM memberships WHERE team_id = ? AND user_id = ?", (ticket.team_id, user_id)
                )
                if row:
                    roles |= set(json.loads(row["roles_json"]))
        if Role.ADMIN.value in roles:
            roles |= ROLES
        return roles

    async def can_act(self, user_id: str, ticket: Ticket | None, wanted: list[str]) -> bool:
        if not wanted:
            return True
        return bool(await self.roles_for(user_id, ticket) & set(wanted))

    async def approvers(self, ticket: Ticket, policy: GatePolicy) -> list[User]:
        """Who is asked at a gate: participants holding a role, else team members holding it, else the fallback."""
        wanted = set(policy.roles)
        found: dict[str, User] = {}
        if Role.OWNER.value in wanted and ticket.owner_user_id:
            found[ticket.owner_user_id] = await self.get_user(ticket.owner_user_id)
        for member in await self.participants(ticket.id):
            if wanted & set(member.roles):
                found[member.user.id] = member.user
        if not found and ticket.team_id:
            for member in await self.members(ticket.team_id):
                if wanted & set(member.roles):
                    found[member.user.id] = member.user
        if not found and policy.fallback == Role.OWNER.value and ticket.owner_user_id:
            found[ticket.owner_user_id] = await self.get_user(ticket.owner_user_id)
        return list(found.values())

    async def template(self, ticket: Ticket) -> StageTemplate | None:
        name = ticket.stage_template
        if not name:
            return None
        if ticket.team_id:
            team = await self.get_team(ticket.team_id)
            if name in team.settings.stage_templates:
                return team.settings.stage_templates[name]
        return default_templates().get(name)

    async def gate_policy(self, ticket: Ticket, gate: str) -> GatePolicy:
        if ticket.team_id:
            team = await self.get_team(ticket.team_id)
            if gate in team.settings.gates:
                return team.settings.gates[gate]
        return default_gates().get(gate) or GatePolicy(roles=["owner"])


def _user(row: dict[str, Any]) -> User:
    return User(
        id=row["id"],
        name=row["name"],
        email=row.get("email"),
        telegram_id=row.get("telegram_id"),
        default_roles=json.loads(row.get("default_roles_json") or "[]"),
        active=bool(row.get("active", 1)),
        created_at=row.get("created_at") or "",
    )


def _team(row: dict[str, Any]) -> Team:
    return Team(
        id=row["id"],
        name=row["name"],
        jira_project_key=row.get("jira_project_key"),
        confluence_space=row.get("confluence_space"),
        confluence_parent_id=row.get("confluence_parent_id"),
        settings=TeamSettings.model_validate_json(row.get("settings_json") or "{}"),
        created_at=row.get("created_at") or "",
    )
