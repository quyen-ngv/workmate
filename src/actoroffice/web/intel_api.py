"""JSON API for connections (accounts on outside systems), the code index and the service map."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from actoroffice.app import Office
from actoroffice.codeintel import services as service_map
from actoroffice.domain.errors import OfficeError
from actoroffice.ports.connection import ConnectionSpec, ConnectorError

ERROR_STATUS = {"auth": 401, "forbidden": 403, "not_found": 404, "rate_limited": 429, "network": 502, "invalid": 422, "unconfigured": 409}


class ConnectionBody(BaseModel):
    id: str
    use: str
    scope: str = "office"
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    write: bool = False
    secret_ref: str | None = None
    # Only ever sent to the office, never returned: stored in <data_dir>/secrets/. None = keep what is there.
    secret: dict[str, str] | None = None


def register(app: FastAPI, office: Office) -> None:
    def fail(exc: ConnectorError) -> HTTPException:
        return HTTPException(ERROR_STATUS.get(exc.kind, 400), str(exc))

    # ---- connections -------------------------------------------------------------------------------------------

    @app.get("/api/connections")
    async def connections() -> list[dict[str, Any]]:
        """Every adapter the office knows, connected or not, with its connections. No secret is ever in the answer."""
        return await office.connections.overview()

    @app.post("/api/connections")
    async def save_connection(body: ConnectionBody) -> dict[str, Any]:
        spec = ConnectionSpec(id=body.id.strip(), use=body.use, scope=body.scope, config=body.config, enabled=body.enabled, write=body.write, secret_ref=body.secret_ref)
        previous = await office.connections.get(spec.id)
        if spec.secret_ref is None and previous is not None:
            spec = spec.model_copy(update={"secret_ref": previous.secret_ref})
        secret = {k: v for k, v in (body.secret or {}).items() if v} or None
        try:
            saved = await office.connections.save(spec, secret=secret)
        except ConnectorError as exc:
            raise fail(exc) from exc
        return saved.model_dump()

    @app.post("/api/connections/{connection_id}/test")
    async def test_connection(connection_id: str) -> dict[str, Any]:
        if await office.connections.get(connection_id) is None:
            raise HTTPException(404, f"không có kết nối '{connection_id}'")
        return (await office.connections.health(connection_id)).model_dump()

    @app.delete("/api/connections/{connection_id}")
    async def remove_connection(connection_id: str) -> dict[str, Any]:
        await office.connections.remove(connection_id)
        return {"removed": connection_id}

    # ---- code index --------------------------------------------------------------------------------------------

    async def repos_of(team: str) -> list[Any]:
        team_id = None if team in ("", "-", "all") else (await office.teams.get_team(team)).id
        return await office.codeintel.repos(team_id=team_id)

    @app.get("/api/index")
    async def index_state(team: str = "") -> list[dict[str, Any]]:
        return [s.model_dump() for s in await office.codeintel.status(await repos_of(team))]

    @app.post("/api/index/refresh")
    async def index_refresh(team: str = "") -> list[dict[str, Any]]:
        """Bring the index of the team's repositories up to their main branch (only changed files are read again)."""
        return [s.model_dump() for s in await office.codeintel.ensure(await repos_of(team))]

    # ---- service map -------------------------------------------------------------------------------------------

    @app.get("/api/map")
    async def service_graph(team: str = "", focus: str = "", depth: int = 1, tables: str = "shared", refresh: bool = True) -> dict[str, Any]:
        """The services of a team's repositories and how they reach each other. Every edge carries the line of code
        that shows it (`cite`) and how it was found (`confidence`)."""
        if tables not in ("shared", "all", "none"):
            raise HTTPException(422, "tables: shared | all | none")
        repos = await repos_of(team)
        states = await (office.codeintel.ensure(repos) if refresh else office.codeintel.status(repos))
        try:
            graph = await office.codeintel.map(repos, focus=focus or None, depth=max(1, min(depth, 3)), tables=tables)
        except OfficeError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {
            "nodes": [n.model_dump() for n in graph.nodes],
            "edges": [e.model_dump() for e in graph.edges],
            "commit": graph.commit,
            "index": [s.model_dump() for s in states],
            "mermaid": service_map.to_mermaid(graph),
        }

    @app.get("/api/map/overlay")
    async def overlay(ticket: str) -> dict[str, Any]:
        """What a ticket's documents touch on the map: the services of the code its `Nguồn:` lines cite, and the
        endpoints, topics and tables reached from there (code_impact)."""
        t = await office.tickets.get(ticket)
        repos = await office.codeintel.for_ticket(t)
        await office.codeintel.ensure(repos)
        rows = await office.db.fetchall(
            "SELECT DISTINCT ref, locator FROM doc_evidence WHERE ticket_id = ? AND category = 'code' AND error IS NULL", (t.id,)
        )
        targets = sorted({f"{str(r['ref']).split(':', 1)[-1]}{r['locator'] if str(r['locator']).startswith(':') else ''}" for r in rows})
        impact = office.codeintel.query.impact(targets, [r.slug for r in repos]) if targets else None
        services: set[str] = set()
        for repo in repos:
            declared = service_map.Declared()
            defs = service_map.detect(repo.slug, office.codeintel.query.files(repo.slug), declared)
            for target in targets:
                services.add(service_map.service_of(target.split(":", 1)[0], defs))
        return {
            "ticket": t.key,
            "targets": targets,
            "services": sorted(s for s in services if s),
            "endpoints": [n.name for n in impact.endpoints] if impact else [],
            "topics": [n.name for n in impact.topics] if impact else [],
            "tables": [n.name for n in impact.tables] if impact else [],
        }
