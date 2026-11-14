"""Events pushed by code hosts: `POST /hooks/<connection id>`.

The connection's adapter checks the signature; a delivery that does not pass is dropped and audited, nothing else. A
push to a repository's main branch brings its index up to date; a pull-request event makes the pull-request watcher
look now instead of at its next poll. An office behind a firewall gets no deliveries and keeps polling as before.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from actoroffice.domain.ids import utcnow
from actoroffice.ports.code import WebhookEvent
from actoroffice.ports.connection import ConnectorError

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

MAX_BODY = 5 * 1024 * 1024
KEEP_EVENTS = 500


def _same_repo(repo_url: str | None, slug: str) -> bool:
    """`https://github.com/acme/pay.git` and `git@bitbucket.org:acme/pay` are the repository `acme/pay`."""
    if not repo_url or not slug:
        return False
    tail = repo_url.strip().lower().removesuffix(".git").replace(":", "/").rstrip("/")
    return tail.endswith("/" + slug.lower().strip("/"))


class WebhookService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self._tasks: set[asyncio.Task[Any]] = set()

    async def receive(self, connection_id: str, headers: dict[str, str], body: bytes) -> dict[str, Any] | None:
        """One delivery. None when it must be refused (unknown connection, no webhook support, bad signature)."""
        office = self.office
        if len(body) > MAX_BODY:
            return None
        try:
            adapter = await office.connections.adapter(connection_id)
        except ConnectorError:
            await office.audit.record("webhook", "refused", target=connection_id, detail={"reason": "no such connection"})
            return None
        verify = getattr(adapter, "verify_webhook", None)
        event: WebhookEvent | None = verify(headers, body) if verify is not None else None
        if event is None:
            await office.audit.record("webhook", "refused", target=connection_id, detail={"reason": "signature"})
            return None
        summary = {"repo": event.repo, "ref": event.ref, "commit": event.commit, "pr_id": event.pr_id}
        cur = await office.db.execute(
            "INSERT INTO webhook_events(connection_id, kind, payload_json, received_at) VALUES (?, ?, ?, ?)", (connection_id, event.kind, json.dumps(summary), utcnow())
        )
        event_id = int(cur.lastrowid or 0)
        await office.db.execute("DELETE FROM webhook_events WHERE id <= ?", (event_id - KEEP_EVENTS,))
        task = asyncio.create_task(self._handle(event_id, connection_id, event), name=f"webhook-{event_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return {"id": event_id, "kind": event.kind, **summary}

    async def _handle(self, event_id: int, connection_id: str, event: WebhookEvent) -> str:
        office = self.office
        try:
            if event.kind == "push":
                result = await self._on_push(connection_id, event)
            elif event.kind.startswith("pr_"):
                done = await office.prs.poll()
                result = "; ".join(done) or "pull requests checked"
            else:
                result = "ignored"
        except Exception as exc:  # one bad delivery must not take the office down
            log.exception("webhook %s failed", event_id)
            result = f"error: {type(exc).__name__}: {exc}"[:300]
        await office.db.execute("UPDATE webhook_events SET handled_at = ?, result = ? WHERE id = ?", (utcnow(), office.redact(result), event_id))
        office.bus.publish("webhook.handled", id=event_id, kind=event.kind, repo=event.repo, result=result)
        return result

    async def _on_push(self, connection_id: str, event: WebhookEvent) -> str:
        """Index again every repository of the office that is this pushed repository, when its main branch moved."""
        office = self.office
        touched = []
        for repo in await office.codeintel.all_repos():
            project = await office.projects.get(repo.project_id) if repo.project_id else None
            mine = (repo.remote is not None and repo.remote.connection == connection_id and repo.remote.slug.lower() == event.repo.lower()) or (
                project is not None and _same_repo(project.repo_url, event.repo)
            )
            if not mine or (event.ref and event.ref != repo.branch):
                continue
            state = await office.codeintel.index(repo)
            touched.append(f"{repo.slug} → {(state.indexed_commit or '')[:10] or state.state}")
        return "indexed " + ", ".join(touched) if touched else "no repository of the office follows this branch"

    async def wait(self) -> None:
        """Until everything received so far is handled (tests)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = await self.office.db.fetchall("SELECT * FROM webhook_events ORDER BY id DESC LIMIT ?", (limit,))
        return [{**dict(r), "payload": json.loads(r["payload_json"] or "{}")} for r in rows]
