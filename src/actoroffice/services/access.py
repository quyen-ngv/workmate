"""Access envelope: how a ticket's runs reach shared environments without the AI holding credentials.

- Resources (a service, a database, a broker, a log store) are registered per team with a maximum level and a
  secret name; secret values stay in <data>/secrets/resources.json.
- The AI asks with `env_access(resource, level, reason, ttl)`; a Dev/Lead approves the exact request (or approves
  it together with the plan). A grant lasts `ttl` hours, is revoked automatically and is audited.
- HTTP resources get a local reverse proxy per grant: the run's env has `AO_<NAME>_URL=http://127.0.0.1:<port>`;
  the proxy adds the credentials, allows only the granted methods/paths and logs every request.
- Databases: tools use the read credential server-side; a write grant may put a connection string in the run's
  env (registered with the redactor so it never shows up in logs or chat).
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from actoroffice.core.http import ssl_context
from actoroffice.domain.access import LEVEL_ORDER, LEVEL_VI, AccessLevel, Grant, Resource, ResourceKind
from actoroffice.domain.errors import Conflict, NotFound, OfficeError, PolicyDenied
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.team import LOCAL_OWNER_ID
from actoroffice.domain.work import Interaction, InteractionKind

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)
NAME = re.compile(r"^[a-z0-9][a-z0-9\-]{1,40}$")
READ_METHODS = {"GET", "HEAD", "OPTIONS"}
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}
MAX_TTL_H = 24
APPROVER_ROLES = ["dev", "lead"]


class AccessError(OfficeError):
    code = 422


def secrets_file(data_dir: Path) -> Path:
    return data_dir / "secrets" / "resources.json"


def load_secret(data_dir: Path, name: str | None) -> Any:
    if not name:
        return None
    try:
        return json.loads(secrets_file(data_dir).read_text(encoding="utf-8")).get(name)
    except (OSError, ValueError):
        return None


def save_secret(data_dir: Path, name: str, value: Any) -> None:
    path = secrets_file(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {}
    with contextlib.suppress(OSError, ValueError):
        data = json.loads(path.read_text(encoding="utf-8"))
    data[name] = value
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


class HttpEnvelope:
    """A local reverse proxy for one grant: adds credentials, enforces the level, logs each request."""

    def __init__(self, office: Office, grant: Grant, resource: Resource, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.office = office
        self.grant = grant
        self.resource = resource
        base = str(resource.config.get("base_url") or "").rstrip("/")
        if not base.startswith(("http://", "https://")):
            raise AccessError(f"{resource.name}: config.base_url phải là http(s) URL")
        self.base = base
        secret = load_secret(office.settings.data_dir, resource.config.get("secret")) or {}
        self.headers = {str(k): str(v) for k, v in (secret.get("headers") or {}).items()} if isinstance(secret, dict) else {}
        for value in self.headers.values():
            office.redact.register(value)
        self.client = httpx.AsyncClient(timeout=60, transport=transport, follow_redirects=False, verify=True if transport else ssl_context())
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.log: list[str] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = int(self.server.sockets[0].getsockname()[1])
        return self.port

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            with contextlib.suppress(Exception):
                await self.server.wait_closed()
        await self.client.aclose()

    def allowed(self, method: str, path: str) -> str | None:
        """None when allowed, else the reason."""
        level = self.grant.level
        if method in READ_METHODS:
            return None
        if level == AccessLevel.READ:
            return f"grant {self.grant.id} chỉ đọc ({self.resource.name})"
        if level == AccessLevel.WRITE_SANDBOXED:
            allow = self.resource.config.get("sandbox_paths") or []
            if not any(fnmatch.fnmatch(path.split("?")[0], p) for p in allow):
                return f"chỉ được ghi vào {', '.join(allow) or '(chưa khai báo sandbox_paths)'}"
        return None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            method, target, _version = lines[0].split(" ", 2)
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip()] = v.strip()
            length = int(headers.get("Content-Length") or headers.get("content-length") or 0)
            body = await reader.readexactly(length) if length else b""
            path = target if target.startswith("/") else urlsplit(target).path + (("?" + urlsplit(target).query) if urlsplit(target).query else "")
            reason = self.allowed(method.upper(), path)
            if datetime.fromisoformat(self.grant.expires_at) < datetime.now(UTC):
                reason = "grant đã hết hạn"
            if reason:
                await self._reply(writer, 403, {"Content-Type": "application/json"}, json.dumps({"error": reason}, ensure_ascii=False).encode())
                await self._note(method, path, 403, reason)
                return
            out_headers = {k: v for k, v in headers.items() if k.lower() not in HOP}
            out_headers.update(self.headers)
            response = await self.client.request(method, self.base + path, headers=out_headers, content=body)
            keep = {k: v for k, v in response.headers.items() if k.lower() not in HOP and k.lower() != "content-encoding"}
            await self._reply(writer, response.status_code, keep, response.content)
            await self._note(method, path, response.status_code)
        except (asyncio.IncompleteReadError, ConnectionError, ValueError, httpx.HTTPError) as exc:
            with contextlib.suppress(Exception):
                await self._reply(writer, 502, {"Content-Type": "text/plain"}, f"proxy error: {type(exc).__name__}".encode())
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _reply(self, writer: asyncio.StreamWriter, status: int, headers: dict[str, str], body: bytes) -> None:
        reason = {200: "OK", 201: "Created", 204: "No Content", 403: "Forbidden", 404: "Not Found", 502: "Bad Gateway"}.get(status, "Status")
        head = [f"HTTP/1.1 {status} {reason}"] + [f"{k}: {v}" for k, v in headers.items()] + [f"Content-Length: {len(body)}", "Connection: close", "", ""]
        writer.write("\r\n".join(head).encode("latin-1") + body)
        await writer.drain()

    async def _note(self, method: str, path: str, status: int, reason: str = "") -> None:
        line = f"{utcnow()} {method} {path} → {status}" + (f" ({reason})" if reason else "")
        self.log.append(line)
        del self.log[:-500]
        await self.office.audit.record(
            f"access:{self.resource.name}", "denied" if status == 403 and reason else "proxied", ticket_id=self.grant.ticket_id,
            target=f"{method} {path}"[:300], detail={"status": status, "grant": self.grant.id, "reason": reason},
        )


class AccessService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self.envelopes: dict[str, HttpEnvelope] = {}
        self.transport: httpx.AsyncBaseTransport | None = None  # tests plug a fake upstream in

    # ---- resources -----------------------------------------------------------------------------------------

    async def add_resource(
        self,
        name: str,
        kind: str,
        *,
        team_id: str | None = None,
        project_id: str | None = None,
        env: str = "dev",
        config: dict[str, Any] | None = None,
        max_level: str = "read",
    ) -> Resource:
        name = name.strip().lower()
        if not NAME.match(name):
            raise AccessError("Tên resource: chữ thường, số, '-' (vd. payments-api)")
        if env.strip().lower() in ("prod", "production", "prd"):
            raise PolicyDenied("Môi trường production không bao giờ được nối cho AI")
        resource = Resource(
            id=new_id("res"), team_id=team_id, project_id=project_id, kind=ResourceKind(kind), name=name, env=env.strip().lower(),
            config=config or {}, max_level=AccessLevel(max_level), created_at=utcnow(),
        )
        if resource.kind == ResourceKind.HTTP and not str(resource.config.get("base_url", "")).startswith(("http://", "https://")):
            raise AccessError("Resource http cần config.base_url")
        host = urlsplit(str(resource.config.get("base_url") or "")).hostname or ""
        if re.search(r"(^|[.\-])(prod|production|prd)([.\-]|$)", host):
            raise PolicyDenied(f"{host} trông như production: không được nối cho AI")
        try:
            await self.office.db.execute(
                "INSERT INTO resources(id, team_id, project_id, kind, name, env, config_json, max_level, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (resource.id, team_id, project_id, resource.kind.value, name, resource.env, json.dumps(resource.config), resource.max_level.value, resource.created_at),
            )
        except Exception as exc:
            if "UNIQUE" in str(exc):
                raise Conflict(f"Resource '{name}' đã có") from exc
            raise
        return resource

    async def resources_for(self, team_id: str | None) -> list[Resource]:
        rows = await self.office.db.fetchall("SELECT * FROM resources WHERE team_id IS NULL OR team_id = ? ORDER BY name", (team_id,))
        return [_resource(r) for r in rows]

    async def resource(self, ref: str, team_id: str | None = None) -> Resource:
        if ref.startswith("res_"):
            row = await self.office.db.fetchone("SELECT * FROM resources WHERE id = ?", (ref,))
            if not row:
                raise NotFound(f"Không có resource '{ref}'")
            return _resource(row)
        row = await self.office.db.fetchone(
            "SELECT * FROM resources WHERE (id = ? OR name = ?) AND (team_id IS NULL OR team_id IS ?) ORDER BY team_id IS NULL LIMIT 1",
            (ref, ref.strip().lower(), team_id),
        )
        if not row:
            raise NotFound(f"Không có resource '{ref}'")
        return _resource(row)

    # ---- requests and grants -------------------------------------------------------------------------------

    async def request(self, ticket_id: int, resource_ref: str, level: str, reason: str, ttl_h: float, *, actor_id: str, run_id: str | None) -> Interaction:
        office = self.office
        ticket = await office.tickets.get(ticket_id)
        resource = await self.resource(resource_ref, ticket.team_id)
        wanted = AccessLevel(level)
        if LEVEL_ORDER[wanted] > LEVEL_ORDER[resource.max_level]:
            raise PolicyDenied(f"{resource.name} chỉ cho tối đa '{resource.max_level.value}'")
        ttl = max(0.25, min(float(ttl_h or 4), MAX_TTL_H))
        from actoroffice.domain.stage import GatePolicy

        approvers = await office.teams.approvers(ticket, GatePolicy(roles=APPROVER_ROLES))
        return await office.interactions.create(
            InteractionKind.ENV_ACCESS,
            {
                "ticket": ticket.key,
                "resource": resource.name,
                "resource_id": resource.id,
                "kind": resource.kind.value,
                "env": resource.env,
                "level": wanted.value,
                "level_label": LEVEL_VI[wanted],
                "ttl_h": ttl,
                "reason": reason.strip()[:600],
                "roles": APPROVER_ROLES,
                "roles_label": "Dev / Lead",
                "approver_ids": [u.id for u in approvers],
                "approver_names": [u.name for u in approvers],
                "stage": ticket.stage,
            },
            actor_id=actor_id,
            ticket_id=ticket.id,
            run_id=run_id,
        )

    async def answer(self, item: Interaction, response: dict[str, Any], *, by: str) -> Interaction:
        office = self.office
        ticket = await office.tickets.get(item.ticket_id or 0)
        if by != LOCAL_OWNER_ID and not await office.teams.can_act(by, ticket, APPROVER_ROLES):
            raise PolicyDenied("Cần vai Dev hoặc Lead để cấp quyền môi trường")
        approved = bool(response.get("approved"))
        if approved:
            grant = await self.grant(ticket.id, item.payload["resource_id"], item.payload["level"], float(item.payload["ttl_h"]), by=by, reason=item.payload.get("reason", ""))
            response = {**response, "grant_id": grant.id, "expires_at": grant.expires_at, "url": grant.envelope.get("url")}
        return await office.interactions.resolve(item.id, response, by=by)

    async def grant(self, ticket_id: int, resource_id: str, level: str, ttl_h: float, *, by: str, reason: str = "") -> Grant:
        resource = await self.resource(resource_id)
        now = datetime.now(UTC)
        grant = Grant(
            id=new_id("grant"), ticket_id=ticket_id, resource_id=resource.id, level=AccessLevel(level), reason=reason, granted_by=by,
            created_at=now.isoformat(timespec="seconds"), expires_at=(now + timedelta(hours=ttl_h)).isoformat(timespec="seconds"),
        )
        grant.envelope = await self._open(grant, resource)
        await self.office.db.execute(
            "INSERT INTO grants(id, ticket_id, resource_id, level, reason, granted_by, created_at, expires_at, envelope_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (grant.id, ticket_id, resource.id, grant.level.value, reason, by, grant.created_at, grant.expires_at, json.dumps(grant.envelope)),
        )
        await self.office.audit.record("access:grant", grant.level.value, ticket_id=ticket_id, target=resource.name, detail={"by": by, "expires_at": grant.expires_at})
        self.office.bus.publish("access.changed", ticket_id=ticket_id)
        return grant

    async def _open(self, grant: Grant, resource: Resource) -> dict[str, Any]:
        if resource.kind == ResourceKind.HTTP:
            envelope = HttpEnvelope(self.office, grant, resource, transport=self.transport)
            port = await envelope.start()
            self.envelopes[grant.id] = envelope
            return {"port": port, "url": f"http://127.0.0.1:{port}", "env": {f"{resource.env_name}_URL": f"http://127.0.0.1:{port}"}}
        if resource.kind == ResourceKind.DB and grant.level != AccessLevel.READ:
            secret = load_secret(self.office.settings.data_dir, resource.config.get("secret")) or {}
            dsn = (secret.get("dsn") or {}).get(grant.level.value) if isinstance(secret, dict) and isinstance(secret.get("dsn"), dict) else None
            if dsn:
                self.office.redact.register(str(dsn))
                return {"env_secret": f"{resource.env_name}_DSN"}
        return {}

    async def active(self, ticket_id: int) -> list[tuple[Grant, Resource]]:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        rows = await self.office.db.fetchall(
            "SELECT * FROM grants WHERE ticket_id = ? AND revoked_at IS NULL AND expires_at > ? ORDER BY created_at", (ticket_id, now)
        )
        out = []
        for row in rows:
            grant = _grant(row)
            with contextlib.suppress(NotFound):
                out.append((grant, await self.resource(grant.resource_id)))
        return out

    async def env_for(self, ticket_id: int) -> dict[str, str]:
        """Environment for the ticket's runs: proxy URLs (no credentials) and granted connection strings."""
        env: dict[str, str] = {}
        for grant, resource in await self.active(ticket_id):
            if grant.id not in self.envelopes and resource.kind == ResourceKind.HTTP:
                grant.envelope = await self._open(grant, resource)  # after a restart
                await self.office.db.execute("UPDATE grants SET envelope_json = ? WHERE id = ?", (json.dumps(grant.envelope), grant.id))
            env.update(grant.envelope.get("env") or {})
            if grant.envelope.get("env_secret"):
                secret = load_secret(self.office.settings.data_dir, resource.config.get("secret")) or {}
                dsn = (secret.get("dsn") or {}).get(grant.level.value)
                if dsn:
                    env[grant.envelope["env_secret"]] = str(dsn)
        return env

    async def revoke(self, grant_id: str, *, by: str) -> None:
        await self.office.db.execute("UPDATE grants SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (utcnow(), grant_id))
        envelope = self.envelopes.pop(grant_id, None)
        if envelope is not None:
            await envelope.stop()
        await self.office.audit.record("access:revoke", "revoked", target=grant_id, detail={"by": by})
        self.office.bus.publish("access.changed")

    async def expire(self) -> int:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        rows = await self.office.db.fetchall("SELECT id FROM grants WHERE revoked_at IS NULL AND expires_at <= ?", (now,))
        for row in rows:
            await self.revoke(row["id"], by="expiry")
        return len(rows)

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(60)
            with contextlib.suppress(Exception):
                await self.expire()

    async def close(self) -> None:
        for envelope in list(self.envelopes.values()):
            await envelope.stop()
        self.envelopes.clear()

    async def approve_with_plan(self, ticket_id: int, *, by: str) -> list[str]:
        """Access the plan asked for is approved together with the plan (one click for the Dev)."""
        granted = []
        for item in await self.office.interactions.pending(ticket_id=ticket_id):
            if item.kind == InteractionKind.ENV_ACCESS:
                with contextlib.suppress(OfficeError):
                    await self.answer(item, {"approved": True, "with_plan": True}, by=by)
                    granted.append(item.payload["resource"])
        return granted


def _resource(row: dict[str, Any]) -> Resource:
    return Resource(
        id=row["id"], team_id=row["team_id"], project_id=row["project_id"], kind=ResourceKind(row["kind"]), name=row["name"], env=row["env"],
        config=json.loads(row["config_json"] or "{}"), max_level=AccessLevel(row["max_level"]), created_at=row["created_at"],
    )


def _grant(row: dict[str, Any]) -> Grant:
    return Grant(
        id=row["id"], ticket_id=row["ticket_id"], resource_id=row["resource_id"], level=AccessLevel(row["level"]), reason=row["reason"],
        granted_by=row["granted_by"], created_at=row["created_at"], expires_at=row["expires_at"], revoked_at=row["revoked_at"],
        envelope=json.loads(row["envelope_json"] or "{}"),
    )
