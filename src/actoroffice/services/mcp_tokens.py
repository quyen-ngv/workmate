"""Personal tokens for the office as an MCP server (web/mcp_api.py): one per IDE of a person.

A token is shown once, when it is created. The office keeps its SHA-256 only, so a copy of the database gives no
working token. A token answers for its user, never for more: what a call may read is decided from the user's teams at
the time of the call.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.team import Role, User

if TYPE_CHECKING:
    from actoroffice.app import Office

PREFIX = "aomcp_"
SELECT = "SELECT id, user_id, name, team_id, created_at, last_used_at, revoked_at FROM mcp_tokens"  # never the hash


class McpTokenError(OfficeError):
    code = 422


class McpToken(BaseModel):
    """A token as the office knows it: everything but the value."""

    id: str
    user_id: str
    name: str = ""
    team_id: str | None = None
    created_at: str = ""
    last_used_at: str | None = None
    revoked_at: str | None = None


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _token(row: dict[str, Any]) -> McpToken:
    return McpToken(**{k: row.get(k) for k in McpToken.model_fields})


class McpTokenService:
    def __init__(self, office: Office) -> None:
        self.office = office

    async def in_team(self, user: User, team_id: str) -> bool:
        """Whether the person may read what belongs to a team: a member of it, or an admin of the office."""
        if Role.ADMIN.value in user.default_roles:
            return True
        return any(t.id == team_id for t in await self.office.teams.teams_of(user.id))

    async def create(self, user_id: str, name: str = "", team_id: str | None = None) -> tuple[McpToken, str]:
        """A new token for a person. The value is returned here and nowhere else."""
        office = self.office
        user = await office.teams.get_user(user_id)
        if not user.active:
            raise McpTokenError(f"Người dùng {user.name} đã bị khoá")
        if team_id:
            team = await office.teams.get_team(team_id)
            if not await self.in_team(user, team.id):
                raise McpTokenError(f"{user.name} không phải thành viên của team {team.name}")
            team_id = team.id
        value = PREFIX + secrets.token_urlsafe(32)
        office.redact.register(value)
        token = McpToken(id=new_id("mcp"), user_id=user.id, name=name.strip()[:80] or "IDE", team_id=team_id or None, created_at=utcnow())
        await office.db.execute(
            "INSERT INTO mcp_tokens(id, user_id, name, token_hash, team_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (token.id, token.user_id, token.name, _hash(value), token.team_id, token.created_at),
        )
        await office.audit.record("mcp:token", "created", target=token.id, detail={"user": user.id, "name": token.name, "team": token.team_id})
        return token, value

    async def list(self, user_id: str | None = None) -> list[McpToken]:
        if user_id is None:
            rows = await self.office.db.fetchall(SELECT + " ORDER BY created_at DESC, id")
        else:
            rows = await self.office.db.fetchall(SELECT + " WHERE user_id = ? ORDER BY created_at DESC, id", (user_id,))
        return [_token(r) for r in rows]

    async def revoke(self, token_id: str) -> McpToken:
        """Stop a token for good. Revoking it again changes nothing."""
        office = self.office
        row = await office.db.fetchone(SELECT + " WHERE id = ?", (token_id,))
        if row is None:
            raise NotFound(f"Không có token {token_id}")
        if row["revoked_at"] is None:
            row["revoked_at"] = utcnow()
            await office.db.execute("UPDATE mcp_tokens SET revoked_at = ? WHERE id = ?", (row["revoked_at"], token_id))
            await office.audit.record("mcp:token", "revoked", target=token_id, detail={"user": row["user_id"]})
        return _token(row)

    async def resolve(self, token: str) -> tuple[User, str | None] | None:
        """Whose token this is and the team it is tied to. None for a value the office never issued, a revoked token,
        or one of a person who is no longer active. The value is looked up by its hash and never compared as text."""
        if not token or not token.startswith(PREFIX):
            return None
        office = self.office
        digest = _hash(token)
        row = await office.db.fetchone("SELECT id, user_id, team_id, token_hash, revoked_at FROM mcp_tokens WHERE token_hash = ?", (digest,))
        if row is None or not hmac.compare_digest(str(row["token_hash"]), digest) or row["revoked_at"] is not None:
            return None
        try:
            user = await office.teams.get_user(row["user_id"])
        except NotFound:
            return None
        if not user.active:
            return None
        office.redact.register(token)  # after a restart the office no longer knows the value: keep it out of logs again
        await office.db.execute("UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?", (utcnow(), row["id"]))
        return user, row["team_id"]
