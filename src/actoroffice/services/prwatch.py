"""Watches the pull requests of tickets waiting at the merge gate: a merged PR moves the ticket on; new review
comments wake the AI (it reads them with pr_comments, fixes and replies). Polling, so no public webhook is needed."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.run import WakeSource

from .atlassian import pr_host

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)


class PullRequestWatcher:
    def __init__(self, office: Office, interval_s: float = 120) -> None:
        self.office = office
        self.interval_s = interval_s
        self.seen: dict[int, set[int]] = {}  # ticket id → review comment ids already passed on

    async def run_forever(self) -> None:
        while True:
            await asyncio.sleep(self.interval_s)
            with contextlib.suppress(Exception):
                await self.poll()

    async def poll(self) -> list[str]:
        """One pass. Returns what happened, e.g. ["AO-3 merged", "AO-4: 2 new comments"]."""
        office = self.office
        events: list[str] = []
        rows = await office.db.fetchall("SELECT id FROM tickets WHERE stage = 'pr' AND pr_url IS NOT NULL AND status NOT IN ('done', 'cancelled')")
        for row in rows:
            ticket = await office.tickets.get(row["id"])
            # the nearest code-host connection that owns the URL (Bitbucket, GitHub…), else the old Bitbucket client
            found = await pr_host(office, office.sourcehub.scope(ticket), ticket.pr_url or "")
            if found is None:
                continue
            host, parsed = found[0], found[1:]
            who = getattr(host, "label", "bitbucket")
            try:
                info = await host.pr_get(*parsed)
                comments = await host.pr_comments(*parsed)
            except OfficeError as exc:
                log.warning("pull request %s: %s", ticket.pr_url, exc)
                continue
            if info.get("state") == "MERGED":
                await office.stages.external_passed(ticket.id, "merge", by=who, note=f"PR #{parsed[1]} đã merge")
                events.append(f"{ticket.key} merged")
                continue
            seen = self.seen.setdefault(ticket.id, await self._known(ticket.id))
            fresh = [c for c in comments if c["id"] not in seen and c.get("by") and not c.get("resolved")]
            if not fresh:
                continue
            seen.update(c["id"] for c in fresh)
            lines = "\n".join(f"- #{c['id']} {c['by']}" + (f" {c['file']}:{c['line']}" if c.get("file") else "") + f": {c['text'][:300]}" for c in fresh)
            await office.tickets.add_comment(
                ticket.id, f"💬 Review mới trên PR #{parsed[1]} ({len(fresh)}):\n{lines}", author_kind="review", author_id=who
            )
            await office.db.execute(
                "INSERT INTO work_products(ticket_id, kind, ref, metadata_json, created_at) VALUES (?, 'review_seen', ?, '{}', datetime('now'))",
                (ticket.id, ",".join(str(c["id"]) for c in fresh)),
            )
            if ticket.assignee_actor_id:
                await office.wakes.enqueue(ticket.assignee_actor_id, WakeSource.COMMENT, ticket_id=ticket.id, payload={"review": len(fresh)})
                office.scheduler.kick()
            events.append(f"{ticket.key}: {len(fresh)} new comments")
        return events

    async def _known(self, ticket_id: int) -> set[int]:
        rows = await self.office.db.fetchall("SELECT ref FROM work_products WHERE ticket_id = ? AND kind = 'review_seen'", (ticket_id,))
        return {int(x) for r in rows for x in str(r["ref"]).split(",") if x.strip().isdigit()}
