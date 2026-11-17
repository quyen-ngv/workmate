"""People, teams, memberships and ticket participants; who holds which role for a ticket.

One team. The office works as a single team: the first team (oldest) is THE team, created on first need ("Team") when
there is none. Anything that names no team (a ticket, a repository, a chat, a person's roles) belongs to it, so a
role, a gate or a knowledge item never goes missing because nobody picked a team. Teams made earlier stay readable.
"""

from __future__ import annotations

import json
from typing import Any

from actoroffice.core.events import EventBus
from actoroffice.domain.errors import Conflict, NotFound, OfficeError
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.stage import GatePolicy, StageTemplate, default_gates, default_templates
from actoroffice.domain.team import LOCAL_OWNER_ID, Member, Role, Team, TeamSettings, User
from actoroffice.domain.work import Ticket
from actoroffice.services.docflow import pack_gates, pack_templates
from actoroffice.store import Database

ROLES = {r.value for r in Role}
DEFAULT_TEAM_NAME = "Team"


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
        self.pack_of: Any = None  # ticket → the standard pack it follows (set by the office: DocService.pack_of)
        self._default: str | None = None  # id of THE team, once known

    # ---- the team ---------------------------------------------------------------------------------

    async def default_team_id(self, *, create: bool = False) -> str | None:
        """The team everything belongs to: the oldest one. None when there is none yet, unless `create`."""
        if self._default and await self.db.fetchone("SELECT 1 FROM teams WHERE id = ?", (self._default,)):
            return self._default
        row = await self.db.fetchone("SELECT id FROM teams ORDER BY created_at, rowid LIMIT 1")
        if row is None and create:
            return (await self.create_team(DEFAULT_TEAM_NAME)).id
        self._default = str(row["id"]) if row else None
        return self._default

    async def team_of(self, ticket: Ticket | None) -> str | None:
        """The team a ticket belongs to: its own, else THE team."""
        return (ticket.team_id if ticket is not None and ticket.team_id else None) or await self.default_team_id()

    async def adopt_orphans(self) -> int:
        """Tickets and repositories saved without a team belong to THE team (when there is one). Returns how many tickets."""
        team_id = await self.default_team_id()
        if team_id is None:
            return 0
        cur = await self.db.execute("UPDATE tickets SET team_id = ? WHERE team_id IS NULL", (team_id,))
        # people registered with roles before the team existed are in it with those roles
        for row in await self.db.fetchall(
            "SELECT id, default_roles_json FROM users WHERE id <> ? AND default_roles_json NOT IN ('', '[]') "
            "AND id NOT IN (SELECT user_id FROM memberships WHERE team_id = ?)", (LOCAL_OWNER_ID, team_id),
        ):
            await self._join(str(row["id"]), json.loads(row["default_roles_json"]))
        await self.db.execute(
            "UPDATE projects SET config_json = json_set(config_json, '$.team_id', ?) "
            "WHERE json_extract(config_json, '$.team_id') IS NULL AND COALESCE(json_extract(config_json, '$.is_default'), 0) = 0",
            (team_id,),
        )
        return int(cur.rowcount or 0)

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
        await self._join(user.id, user.default_roles)
        self.bus.publish("people.changed")
        return user

    async def _join(self, user_id: str, roles: list[str]) -> None:
        """A person's roles are also their roles in THE team (when there is one), so a gate finds them."""
        team_id = await self.default_team_id(create=True) if roles else None
        if team_id is not None:
            await self.db.execute(
                "INSERT INTO memberships(team_id, user_id, roles_json) VALUES (?, ?, ?) "
                "ON CONFLICT(team_id, user_id) DO UPDATE SET roles_json = excluded.roles_json",
                (team_id, user_id, json.dumps([r for r in roles if r != Role.ADMIN.value] or roles)),
            )

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
        if roles is not None:
            await self._join(user_id, updated.default_roles)
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
        self._default = None  # the oldest team is THE team: look again
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
        team_id = await self.team_of(ticket)
        if team_id:
            row = await self.db.fetchone("SELECT roles_json FROM memberships WHERE team_id = ? AND user_id = ?", (team_id, user_id))
            if row:
                roles |= set(json.loads(row["roles_json"]))
        if Role.ADMIN.value in roles:
            roles |= ROLES
        return roles

    async def can_act(self, user_id: str, ticket: Ticket | None, wanted: list[str]) -> bool:
        if not wanted:
            return True
        return bool(await self.roles_for(user_id, ticket) & set(wanted))

    async def has_role_anywhere(self, user_id: str, wanted: list[str]) -> bool:
        """Whether the person holds one of `wanted` by default or in any team (decisions not tied to a ticket)."""
        roles = await self.roles_for(user_id, None)
        rows = await self.db.fetchall("SELECT roles_json FROM memberships WHERE user_id = ?", (user_id,))
        for row in rows:
            roles |= set(json.loads(row["roles_json"]))
        return bool(roles & set(wanted))

    async def gate_policies(self) -> dict[str, tuple[GatePolicy, bool]]:
        """Every gate of the office and who approves it now, for THE team: name → (policy, set by the team)."""
        team_id = await self.default_team_id()
        own = (await self.get_team(team_id)).settings.gates if team_id else {}
        names = [*default_gates(), *(g for g in pack_gates(self.pack_of(None) if self.pack_of else None) if g not in default_gates())]
        return {name: (own[name], True) if name in own else (await self.gate_policy(None, name), False) for name in names}

    async def set_gate(self, gate: str, policy: GatePolicy | None) -> None:
        """Set who approves a gate for THE team; None goes back to the default."""
        known = await self.gate_policies()
        if gate not in known:
            raise TeamError(f"Không có bước duyệt '{gate}'. Có: {', '.join(known)}")
        if policy is not None:
            bad = [r for r in [*policy.roles, *policy.submit_by, *policy.accept_findings] if r not in ROLES]
            if bad:
                raise TeamError(f"Vai không hợp lệ: {', '.join(bad)} (chọn trong: {', '.join(sorted(ROLES))})")
            if not policy.roles and not policy.fallback and not policy.external:
                raise TeamError("Cần ít nhất một vai được duyệt, hoặc người dự phòng")
        team = await self.get_team(await self.default_team_id(create=True) or "")
        gates = dict(team.settings.gates)
        if policy is None:
            gates.pop(gate, None)
        else:
            gates[gate] = policy.model_copy(update={"external": known[gate][0].external})  # how a gate resolves is not a choice
        await self.update_team(team.model_copy(update={"settings": team.settings.model_copy(update={"gates": gates})}))

    async def approvers(self, ticket: Ticket, policy: GatePolicy) -> list[User]:
        """Who is asked at a gate: participants holding a role, else team members holding it, else the fallback."""
        wanted = set(policy.roles)
        found: dict[str, User] = {}
        if Role.OWNER.value in wanted and ticket.owner_user_id:
            found[ticket.owner_user_id] = await self.get_user(ticket.owner_user_id)
        for member in await self.participants(ticket.id):
            if wanted & set(member.roles):
                found[member.user.id] = member.user
        team_id = await self.team_of(ticket)
        if not found and team_id:
            for member in await self.members(team_id):
                if wanted & set(member.roles):
                    found[member.user.id] = member.user
        if not found and policy.fallback == Role.OWNER.value and ticket.owner_user_id:
            found[ticket.owner_user_id] = await self.get_user(ticket.owner_user_id)
        return list(found.values())

    async def template(self, ticket: Ticket) -> StageTemplate | None:
        name = ticket.stage_template
        if not name:
            return None
        team_id = await self.team_of(ticket)
        if team_id:
            team = await self.get_team(team_id)
            if name in team.settings.stage_templates:
                return team.settings.stage_templates[name]
        return default_templates().get(name) or pack_templates(self.pack_of(ticket) if self.pack_of else None).get(name)

    async def gate_policy(self, ticket: Ticket | None, gate: str) -> GatePolicy:
        team_id = await self.team_of(ticket)
        if team_id:
            team = await self.get_team(team_id)
            if gate in team.settings.gates:
                return team.settings.gates[gate]
        return default_gates().get(gate) or pack_gates(self.pack_of(ticket) if self.pack_of else None).get(gate) or GatePolicy(roles=["owner"])


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
        # "stored": entries of `mcp_servers` written in the old free shape still load (domain/team.py McpServer.from_stored)
        settings=TeamSettings.model_validate_json(row.get("settings_json") or "{}", context={"stored": True}),
        created_at=row.get("created_at") or "",
    )
