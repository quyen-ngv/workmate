"""Outside MCP servers attached to a team's AI: the typed config lives in the team's settings, the secret values in
`<data_dir>/secrets/mcp-<team id>-<server>.json`. No API and no run event ever carries a secret value; the runtimes get
them only inside the resolved config of a run.
"""

from __future__ import annotations

import asyncio
import functools
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.team import MCP_NAME_RE, McpServer, Team, check_mcp_name, mcp_unattended, secret_looking, validation_text

if TYPE_CHECKING:
    from actoroffice.app import Office

MASKED = "•••"
TEST_TIMEOUT_S = 30
MAX_TOOL_PAGES = 20


class McpServerError(OfficeError):
    code = 422


@dataclass
class Resolved:
    """What one run gets: the servers in the shape the runtimes accept (secrets merged in), the tools that run
    unattended per server, and one plain line for every server that was left out."""

    servers: dict[str, dict[str, Any]] = field(default_factory=dict)
    allowed: dict[str, list[str]] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)


class McpServerService:
    def __init__(self, office: Office) -> None:
        self.office = office

    # ---- secrets ---------------------------------------------------------------------------------------------

    def secret_path(self, team_id: str, name: str) -> Path:
        return self.office.settings.data_dir / "secrets" / f"mcp-{team_id}-{name}.json"

    def _secrets(self, team_id: str, name: str) -> dict[str, str]:
        if not MCP_NAME_RE.match(name):
            return {}  # an old entry under a free name never had a secret file
        try:
            data = json.loads(self.secret_path(team_id, name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        values = {str(k): v for k, v in data.items() if isinstance(v, str) and v} if isinstance(data, dict) else {}
        for value in values.values():
            self._register(value)
        return values

    def _register(self, value: str) -> None:
        self.office.redact.register(value)
        scheme, _, rest = value.strip().partition(" ")
        if rest and scheme.lower() in ("bearer", "basic", "token"):
            self.office.redact.register(rest.strip())  # "Bearer <token>": the token alone must be masked too

    # ---- reading ---------------------------------------------------------------------------------------------

    def _missing(self, team: Team, name: str, server: McpServer) -> list[str]:
        stored = self._secrets(team.id, name)
        return [n for n in (server.secret_env if server.type == "stdio" else server.secret_headers) if not stored.get(n)]

    def _state(self, team: Team, name: str, server: McpServer) -> tuple[str, str]:
        """(state, why) of a server: on | off | missing_secret | invalid."""
        if server.invalid:
            return "invalid", f"cấu hình cũ không dùng được ({server.invalid})"
        if server.disabled:
            return "off", "đang tắt"
        missing = self._missing(team, name, server)
        if missing:
            return "missing_secret", f"thiếu bí mật {', '.join(missing)}"
        return "on", ""

    def list(self, team: Team) -> list[dict[str, Any]]:
        """The team's servers for a screen: no secret value, only which secret names are set."""
        out = []
        for name, server in team.settings.mcp_servers.items():
            stored = self._secrets(team.id, name)
            state, why = self._state(team, name, server)
            view = server.model_dump()
            if server.legacy_inline_secret:  # an old entry: its inline secrets are never shown
                for part in ("env", "headers"):
                    view[part] = {k: MASKED if secret_looking(k, v) else v for k, v in view[part].items()}
            view["secret_env"] = {n: bool(stored.get(n)) for n in server.secret_env}
            view["secret_headers"] = {n: bool(stored.get(n)) for n in server.secret_headers}
            out.append({"name": name, **view, "state": state, "reason": why})
        return out

    def resolved(self, team: Team) -> Resolved:
        result = Resolved()
        for name, server in team.settings.mcp_servers.items():
            state, why = self._state(team, name, server)
            if state == "off":
                continue  # switched off by a person: nothing to report
            if state != "on":
                result.skipped.append(f"MCP '{name}' bỏ qua: {why}")
                continue
            result.servers[name] = self._runtime_entry(team, name, server)
            result.allowed[name] = list(server.allowed_tools)
        return result

    def allowed(self, team: Team) -> dict[str, list[str]]:
        """Server → the globs of its tools that run without asking (what the gate of a run is handed)."""
        return self.resolved(team).allowed

    def _runtime_entry(self, team: Team, name: str, server: McpServer) -> dict[str, Any]:
        """The shape both runtimes accept today (runtimes/kiro_acp.py acp_mcp_entry, the Claude SDK's `mcp_servers`).
        Neither takes a per-server timeout, so `timeout_s` is not passed on (it bounds the "Thử" check)."""
        stored = self._secrets(team.id, name)
        if server.legacy_inline_secret:
            for part in (server.env, server.headers):
                for key, value in part.items():
                    if secret_looking(key, value):
                        self._register(value)
        if server.type == "stdio":
            env = {**server.env, **{n: stored[n] for n in server.secret_env}}
            return {"type": "stdio", "command": server.command, "args": list(server.args), "env": env}
        headers = {**server.headers, **{n: stored[n] for n in server.secret_headers}}
        return {"type": server.type, "url": server.url, "headers": headers}

    # ---- writing ---------------------------------------------------------------------------------------------

    async def save(self, team: Team, name: str, server: McpServer | dict[str, Any], secrets: dict[str, str | None] | None = None) -> Team:
        """Create or change a server. `secrets`: name → value for the names under `secret_env` / `secret_headers`;
        a missing, None or empty value keeps what is stored. Anything invalid is refused and nothing changes."""
        try:
            check_mcp_name(name)
            raw = server.model_dump() if isinstance(server, McpServer) else dict(server)
            # the flags of an old stored entry are never taken from a person: a saved server is always in the strict shape
            server = McpServer.model_validate({**raw, "legacy_inline_secret": False, "invalid": ""})
        except ValidationError as exc:
            raise McpServerError(validation_text(exc)) from None
        except ValueError as exc:
            raise McpServerError(str(exc)) from None
        names = server.secret_env if server.type == "stdio" else server.secret_headers
        given = {str(k): str(v) for k, v in (secrets or {}).items() if v}
        unknown = sorted(set(given) - set(names))
        if unknown:
            field_name = "secret_env" if server.type == "stdio" else "secret_headers"
            raise McpServerError(f"secrets: «{', '.join(unknown)}» chưa được khai tên ở {field_name}")
        stored = self._secrets(team.id, name)
        kept = {n: given.get(n) or stored.get(n, "") for n in names}
        kept = {n: v for n, v in kept.items() if v}
        path = self.secret_path(team.id, name)
        if kept:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(kept), encoding="utf-8")
            for value in kept.values():
                self._register(value)
        else:
            path.unlink(missing_ok=True)
        settings = team.settings.model_copy(update={"mcp_servers": {**team.settings.mcp_servers, name: server}})
        return await self.office.teams.update_team(team.model_copy(update={"settings": settings}))

    async def remove(self, team: Team, name: str) -> Team:
        if name not in team.settings.mcp_servers:
            raise NotFound(f"Team {team.name} không có MCP server '{name}'")
        servers = {k: v for k, v in team.settings.mcp_servers.items() if k != name}
        if MCP_NAME_RE.match(name):
            self.secret_path(team.id, name).unlink(missing_ok=True)
        settings = team.settings.model_copy(update={"mcp_servers": servers})
        return await self.office.teams.update_team(team.model_copy(update={"settings": settings}))

    # ---- trying a server -------------------------------------------------------------------------------------

    async def test(self, team: Team, name: str) -> dict[str, Any]:
        """Start / connect the server once and list its tools, split into those that would run unattended and those
        that would ask. A server that is switched off can be tried too. Never raises for a server that does not
        answer: the reason comes back as plain text."""
        server = team.settings.mcp_servers.get(name)
        if server is None:
            raise NotFound(f"Team {team.name} không có MCP server '{name}'")
        if server.invalid:
            return {"ok": False, "error": f"Cấu hình cũ không dùng được: {server.invalid}"}
        missing = self._missing(team, name, server)
        if missing:
            return {"ok": False, "error": f"Thiếu bí mật {', '.join(missing)}: nhập rồi lưu trước khi thử"}
        entry = self._runtime_entry(team, name, server)
        timeout = float(server.timeout_s or TEST_TIMEOUT_S)
        try:
            tools = await list_tools(entry, timeout)
        except Exception as exc:  # ConnectorError carries a plain Vietnamese line; anything else is named by its type
            from actoroffice.ports.connection import ConnectorError

            said = str(exc) if isinstance(exc, ConnectorError) else f"MCP: không thử được server ({type(exc).__name__})"
            return {"ok": False, "error": self.office.redact(said)}
        names = sorted(dict.fromkeys(tools))
        unattended = [t for t in names if mcp_unattended(server.allowed_tools, t)]
        return {"ok": True, "tools": names, "unattended": unattended, "asks": [t for t in names if t not in unattended]}


async def list_tools(entry: dict[str, Any], timeout_s: float) -> list[str]:
    """The tool names of one server (a resolved runtime entry), through the MCP client of adapters/generic/mcp.py:
    started, asked once, stopped. Raises ConnectorError with a plain reason."""
    from actoroffice.adapters.generic import mcp as client
    from actoroffice.ports.connection import ConnectorError

    kind = entry["type"]
    if kind == "stdio":
        config = client.McpServerConfig(type="stdio", command=entry["command"], args=entry["args"])
        opener = functools.partial(client._stdio, config, dict(entry["env"]))
        what = f"MCP server «{Path(entry['command']).name}»"
    else:
        config = client.McpServerConfig(type=kind, url=entry["url"])
        opener = functools.partial(client._http, config, dict(entry["headers"]))
        what = "MCP server"
    link = client._Link(opener, timeout_s, what)
    try:
        session = await link.get()
        names: list[str] = []
        cursor = None
        try:
            async with asyncio.timeout(timeout_s):
                for _ in range(MAX_TOOL_PAGES):
                    listed = await (session.list_tools(cursor) if cursor else session.list_tools())
                    names += [str(getattr(tool, "name", "")) for tool in getattr(listed, "tools", None) or []]
                    cursor = getattr(listed, "next_cursor", None) or getattr(listed, "nextCursor", None)
                    if not cursor:
                        break
        except TimeoutError:
            raise ConnectorError("network", f"MCP: server không trả lời danh sách tool trong {timeout_s:g} giây") from None
        except ConnectorError:
            raise
        except Exception as exc:
            raise ConnectorError("network", f"MCP: server không đưa được danh sách tool ({type(exc).__name__})") from None
        return [n for n in names if n]
    finally:
        await link.close()
