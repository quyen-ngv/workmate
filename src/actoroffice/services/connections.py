"""Connections: the accounts the office has on outside systems, layered office → team → project.

Every adapter the office knows is listed, connected or not: "not configured" is a normal state, and whatever depends
on a connection says what is missing instead of failing.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.adapters import registry
from actoroffice.domain.ids import utcnow
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Health, Scope

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
SCOPE_RE = re.compile(r"^(office|team:[\w-]+|project:[\w-]+)$")


class ConnectionService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self._live: dict[str, Any] = {}  # connection id → adapter instance

    # ---- storage ---------------------------------------------------------------------------------------------

    @staticmethod
    def _spec(row: dict[str, Any]) -> ConnectionSpec:
        return ConnectionSpec(
            id=row["id"], use=row["use"], scope=row["scope"], config=json.loads(row["config_json"] or "{}"), secret_ref=row["secret_ref"],
            enabled=bool(row["enabled"]), write=bool(row["can_write"]),
        )  # fmt: skip

    async def list(self) -> list[ConnectionSpec]:
        return [self._spec(r) for r in await self.office.db.fetchall("SELECT * FROM connections ORDER BY id")]

    async def get(self, connection_id: str) -> ConnectionSpec | None:
        row = await self.office.db.fetchone("SELECT * FROM connections WHERE id = ?", (connection_id,))
        return self._spec(row) if row else None

    def secret_path(self, ref: str) -> Path:
        return self.office.settings.data_dir / "secrets" / f"{ref}.json"

    def secret(self, spec: ConnectionSpec) -> dict[str, str]:
        if not spec.secret_ref:
            return {}
        try:
            data = json.loads(self.secret_path(spec.secret_ref).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        values = {str(k): str(v) for k, v in data.items() if isinstance(v, str | int | float)} if isinstance(data, dict) else {}
        for name, value in values.items():
            if name in ("token", "password", "webhook_secret", "client_secret", "private_key"):
                self.office.redact.register(value)
        return values

    async def save(self, spec: ConnectionSpec, *, secret: dict[str, str] | None = None) -> ConnectionSpec:
        """Create or change a connection. Anything invalid is refused and what was running stays as it was."""
        if not ID_RE.match(spec.id):
            raise ConnectorError("invalid", "id của kết nối chỉ gồm chữ thường, số, '-' và '_' (tối đa 40 ký tự)")
        if not SCOPE_RE.match(spec.scope):
            raise ConnectorError("invalid", "scope phải là 'office', 'team:<id>' hoặc 'project:<id>'")
        await self._check_scope(spec.scope)
        registry.validate(spec)
        if secret is not None:
            ref = spec.secret_ref or spec.id
            path = self.secret_path(ref)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(secret), encoding="utf-8")
            spec = spec.model_copy(update={"secret_ref": ref})
        now = utcnow()
        await self.office.db.execute(
            "INSERT INTO connections(id, use, scope, config_json, secret_ref, enabled, can_write, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET use = excluded.use, scope = excluded.scope, config_json = excluded.config_json, secret_ref = excluded.secret_ref, "
            "enabled = excluded.enabled, can_write = excluded.can_write, updated_at = excluded.updated_at",
            (spec.id, spec.use, spec.scope, json.dumps(spec.config, ensure_ascii=False), spec.secret_ref, int(spec.enabled), int(spec.write), now, now),
        )
        await self._drop_live(spec.id)
        self.office.bus.publish("connections.changed", id=spec.id)
        return spec

    async def _check_scope(self, scope: str) -> None:
        kind, _, ref = scope.partition(":")
        table = {"team": "teams", "project": "projects"}.get(kind)
        if table and await self.office.db.fetchone(f"SELECT 1 FROM {table} WHERE id = ?", (ref,)) is None:
            raise ConnectorError("invalid", f"không có {kind} '{ref}'")

    async def remove(self, connection_id: str) -> None:
        """Forget a connection and what was fetched through it. Its secret file stays: another connection may share it."""
        await self._drop_live(connection_id)
        await self.office.db.execute("DELETE FROM source_cache WHERE connection_id = ?", (connection_id,))
        await self.office.db.execute("DELETE FROM connections WHERE id = ?", (connection_id,))
        self.office.bus.publish("connections.changed", id=connection_id)

    def _legacy_wanted(self) -> list[tuple[Capability, ConnectionSpec, dict[str, str]]]:
        """What the configuration files from before connections hold, as the connections they become."""
        secrets = self.office.settings.data_dir / "secrets"

        def read(name: str, keys: tuple[str, ...]) -> dict[str, str] | None:
            try:
                data = json.loads((secrets / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            return {k: str(data[k]) for k in keys} if isinstance(data, dict) and all(data.get(k) for k in keys) else None

        wanted: list[tuple[Capability, ConnectionSpec, dict[str, str]]] = []
        jira = read("jira.json", ("base_url", "email", "token"))
        if jira:
            secret = {"email": jira["email"], "token": jira["token"]}
            wanted.append((Capability.TRACKER, ConnectionSpec(id="jira-main", use="jira_cloud", config={"site": jira["base_url"]}, write=True), secret))
            wanted.append((Capability.WIKI, ConnectionSpec(id="confluence-main", use="confluence_cloud", config={"site": jira["base_url"]}, write=True), secret))
        bitbucket = read("bitbucket.json", ("email", "token", "workspace"))
        if bitbucket:
            secret = {"email": bitbucket["email"], "token": bitbucket["token"]}
            wanted.append((Capability.CODE_PR, ConnectionSpec(id="bitbucket-main", use="bitbucket_cloud", config={"workspace": bitbucket["workspace"]}, write=True), secret))
        return wanted

    async def import_legacy(self, *, only_missing: bool = False) -> list[ConnectionSpec]:
        """The Atlassian configuration from before connections as connections of the office.

        `secrets/jira.json` becomes `jira-main` (jira_cloud) and `confluence-main` (confluence_cloud: same site, same
        token); `secrets/bitbucket.json` becomes `bitbucket-main` (bitbucket_cloud). Each gets its own copy of the secret
        (`secrets/<id>.json`), reaches what the old configuration reached (no project, space or repository limit) and
        may write, as the old one could. A connection whose id exists is left untouched; with `only_missing`, so is a
        capability some connection already gives. The old files stay where they are; nothing else reads them.
        Returns what was created."""
        created = []
        for capability, spec, secret in self._legacy_wanted():
            if await self.get(spec.id) is not None or (only_missing and await self._anyone_gives(capability)):
                continue
            created.append(await self.save(spec, secret=secret))
        return created

    async def _anyone_gives(self, capability: Capability) -> bool:
        for spec in await self.list():
            with contextlib.suppress(ConnectorError):
                if capability in registry.capabilities(spec.use):
                    return True
        return False

    @property
    def _legacy_marker(self) -> Path:
        return self.office.settings.data_dir / "legacy-import.json"

    async def migrate_legacy_once(self) -> list[ConnectionSpec]:
        """At office start: the old Jira / Bitbucket files become connections, once. A marker file records that it ran,
        so a connection people remove afterwards does not come back at the next start."""
        marker = self._legacy_marker
        if marker.exists() or not self._legacy_wanted():
            return []
        try:
            created = await self.import_legacy(only_missing=True)
        except ConnectorError as exc:  # an old file the adapters refuse (a site that is not an address…): say so, do not stop the office
            log.warning("old Jira / Bitbucket configuration was not turned into connections: %s", exc)
            created = []
        with contextlib.suppress(OSError):
            marker.write_text(json.dumps({"at": utcnow(), "created": [s.id for s in created]}), encoding="utf-8")
        for spec in created:
            log.warning("old configuration → connection %s (%s, scope %s, may write): check it in Cài đặt › Kết nối", spec.id, spec.use, spec.scope)
        return created

    async def _drop_live(self, connection_id: str) -> None:
        adapter = self._live.pop(connection_id, None)
        if adapter is not None:
            with contextlib.suppress(Exception):  # closing a client that is already gone
                await adapter.close()

    async def close(self) -> None:
        for connection_id in list(self._live):
            await self._drop_live(connection_id)

    # ---- use -------------------------------------------------------------------------------------------------

    async def adapter(self, connection_id: str) -> Any:
        if connection_id in self._live:
            return self._live[connection_id]
        spec = await self.get(connection_id)
        if spec is None or not spec.enabled:
            raise ConnectorError("unconfigured", f"chưa có kết nối '{connection_id}' (hoặc đang tắt)")
        self._live[connection_id] = registry.build(spec, self.secret(spec))
        return self._live[connection_id]

    async def candidates(self, capability: Capability, scope: Scope | None = None) -> list[ConnectionSpec]:
        """Enabled connections that can do `capability`, nearest first: the ticket's project, its team, the office."""
        order = {"office": 2}
        if scope is not None and scope.team_id:
            order[f"team:{scope.team_id}"] = 1
        if scope is not None and scope.project_id:
            order[f"project:{scope.project_id}"] = 0
        found = []
        for spec in await self.list():
            if not spec.enabled or spec.scope not in order:
                continue
            try:
                if capability in registry.capabilities(spec.use):
                    found.append(spec)
            except ConnectorError:
                continue
        return sorted(found, key=lambda s: (order[s.scope], s.id))

    async def resolve(self, capability: Capability, scope: Scope | None = None) -> tuple[ConnectionSpec, Any] | None:
        """The nearest connection for a capability, with its adapter. None when nothing is connected."""
        for spec in await self.candidates(capability, scope):
            try:
                return spec, await self.adapter(spec.id)
            except ConnectorError:
                continue
        return None

    async def has(self, capability: Capability, scope: Scope | None = None) -> bool:
        return bool(await self.candidates(capability, scope))

    async def health(self, connection_id: str) -> Health:
        """Ask the outside system whether the connection works, and remember the answer."""
        now = utcnow()
        try:
            adapter = await self.adapter(connection_id)
            health = await adapter.test()
        except ConnectorError as exc:
            health = Health(state="unconfigured" if exc.kind == "unconfigured" else "error", detail=str(exc))
        except Exception as exc:  # an adapter bug must show as a broken connection, not crash the page
            health = Health(state="error", detail=f"{type(exc).__name__}: {exc}"[:300])
        health = health.model_copy(update={"checked_at": now, "detail": self.office.redact(health.detail)})
        await self.office.db.execute("UPDATE connections SET health_json = ? WHERE id = ?", (health.model_dump_json(), connection_id))
        return health

    async def overview(self) -> list[dict[str, Any]]:
        """Every adapter the office knows with its connections: what the settings page shows, connected or not."""
        rows = await self.office.db.fetchall("SELECT * FROM connections ORDER BY id")
        by_use: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            spec = self._spec(row)
            health = json.loads(row["health_json"]) if row["health_json"] else Health(state="ok" if self.secret(spec) or not spec.secret_ref else "unconfigured").model_dump()
            by_use.setdefault(spec.use, []).append({**spec.model_dump(), "health": health})
        out = []
        for name in registry.names():
            try:
                info = registry.describe(name)
            except ConnectorError as exc:
                info = {"name": name, "title": name, "capabilities": [], "config_schema": {}, "secret_fields": [], "error": str(exc)}
            connections = by_use.get(name, [])
            out.append({**info, "connections": connections, "state": "connected" if any(c["enabled"] for c in connections) else "unconfigured"})
        return out
