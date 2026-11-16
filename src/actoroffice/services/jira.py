"""Jira Cloud mirror: Jira is the source of truth for what people asked; the office keeps stages and links.

- Every `jira_poll_s` seconds, for each team with a Jira project, issues matching the team's JQL (default:
  `project = KEY AND labels = ai AND statusCategory != Done`) become office tickets running through stages.
  The template comes from a label (`ai-research`, `ai-doc`, `ai-automation`; default the team's template).
- A changed summary/description is mirrored; a changed description after the requirements were approved is
  recorded as a requirement change (the AI hears it and the BA gate reopens when the requirements change).
- When the BA approves the requirements, the approved requirements are posted back as a Jira comment.

The mirror talks to Jira through a tracker connection (`Capability.TRACKER`, see services/connections.py): the
team's own, else the office's. No tracker connected: nothing is mirrored and nothing is posted back.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from typing import TYPE_CHECKING, Any

from actoroffice.adapters import registry
from actoroffice.domain.errors import NotFound
from actoroffice.domain.team import Team
from actoroffice.ports.connection import Capability, ConnectorError, Scope
from actoroffice.ports.docs import Issue

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

LABEL_TEMPLATES = {"ai-research": "research", "ai-doc": "doc", "ai-automation": "automation", "ai-code": "code"}
SEARCH_LIMIT = 50
_MARKUP = re.compile(r"[\s*_~`>#|\-]+")


def _same_text(a: str, b: str) -> bool:
    """The same words, whatever the markdown around them: a description stored by an older version (plain text) and
    the same description read today (markdown) are not a change people made."""
    return _MARKUP.sub("", a) == _MARKUP.sub("", b)


class JiraMirror:
    def __init__(self, office: Office) -> None:
        self.office = office
        self._lock = asyncio.Lock()

    async def ready(self) -> bool:
        """Is any tracker connected (whatever its scope)? Asked each cycle: a connection added later starts the
        mirror without a restart."""
        for spec in await self.office.connections.list():
            with contextlib.suppress(ConnectorError):  # an adapter that no longer loads
                if spec.enabled and Capability.TRACKER in registry.capabilities(spec.use):
                    return True
        return False

    async def run_forever(self) -> None:
        interval = self.office.settings.jira_poll_s
        if interval <= 0:
            return
        while True:
            try:
                if await self.ready():
                    await self.sync()
            except ConnectorError as exc:
                log.warning("jira mirror: %s", exc)
            except Exception:
                log.exception("jira mirror failed")
            await asyncio.sleep(interval)

    async def sync(self) -> list[str]:
        """One pass over every team with a Jira project, each through the tracker connection nearest to it (the team's
        own, else the office's). Returns the keys of tickets created."""
        created: list[str] = []
        async with self._lock:
            for team in await self.office.teams.list_teams():
                if not team.jira_project_key:
                    continue
                found = await self.office.connections.resolve(Capability.TRACKER, Scope(team_id=team.id))
                if found is None:
                    continue
                jql = team.settings.jira_jql or f"project = {team.jira_project_key} AND labels = ai AND statusCategory != Done ORDER BY created ASC"
                for issue in await found[1].search(jql, SEARCH_LIMIT):
                    key = await self._mirror(team, issue)
                    if key:
                        created.append(key)
        return created

    async def _mirror(self, team: Team, issue: Issue) -> str | None:
        office = self.office
        jira_key = issue.key
        title = (issue.title or jira_key)[:200]
        description = issue.description
        row = await office.db.fetchone("SELECT id FROM tickets WHERE jira_key = ?", (jira_key,))
        if row:
            ticket = await office.tickets.get(row["id"])
            if ticket.title != title or ticket.description.strip() != description.strip():
                await office.db.execute("UPDATE tickets SET title = ?, description = ? WHERE id = ?", (title, description, ticket.id))
                changed = not _same_text(ticket.description, description)
                if changed:
                    await office.sources.sync(ticket.id)  # kept sources keep their status, new ones are unread (SI-39)
                approved = await office.db.fetchone(
                    "SELECT 1 FROM documents WHERE ticket_id = ? AND key = 'requirements' AND approved_revision IS NOT NULL", (ticket.id,)
                )
                if changed and approved and ticket.status.value not in ("done", "cancelled"):
                    await office.tickets.change_notice(ticket.id, f"Mô tả trên Jira {jira_key} đã thay đổi:\n{description[:1500]}", by="Jira")
            return None
        labels = [label.lower() for label in issue.labels]
        template = next((LABEL_TEMPLATES[label] for label in labels if label in LABEL_TEMPLATES), team.settings.default_template)
        owner = await self._owner(issue.reporter_email, issue.reporter_name)
        ticket = await office.tickets.create(title, description=description, created_by=f"jira:{jira_key}")
        await office.db.execute("UPDATE tickets SET jira_key = ?, jira_url = ? WHERE id = ?", (jira_key, issue.url, ticket.id))
        await office.stages.start(ticket.id, template=template, by="jira", team_id=team.id, owner_user_id=owner)
        return ticket.key

    async def _owner(self, email: str, name: str) -> str | None:
        email = email.lower()
        for user in await self.office.teams.list_users():
            if (email and (user.email or "").lower() == email) or (name and user.name.lower() == name.lower()):
                return user.id
        return None

    async def _writer(self, scope: Scope) -> Any:
        """The nearest tracker connection that may comment; None when there is none."""
        for spec in await self.office.connections.candidates(Capability.TRACKER, scope):
            if spec.write:
                with contextlib.suppress(ConnectorError):  # no secret yet: as good as not connected
                    return await self.office.connections.adapter(spec.id)
        return None

    async def requirements_approved(self, ticket_id: int, approver: str) -> None:
        """Post the approved requirements back to the Jira issue (people keep reading Jira). Nothing is posted when no
        tracker connection may write."""
        office = self.office
        ticket = await office.tickets.get(ticket_id)
        if not ticket.jira_key:
            return
        tracker = await self._writer(Scope(team_id=ticket.team_id, project_id=ticket.project_id, ticket_key=ticket.key))
        if tracker is None:
            return
        doc = await office.tickets.get_document(ticket.id, "requirements")
        if doc is None:
            return
        text = f"✅ Requirements được {approver} duyệt (revision {doc.revision}, {ticket.key}):\n\n{doc.body}"
        try:
            await tracker.comment(ticket.jira_key, text[:30000])
        except ConnectorError as exc:
            log.warning("jira: cannot comment on %s: %s", ticket.jira_key, exc)
            with contextlib.suppress(NotFound):
                await office.tickets.add_comment(ticket.id, f"Không ghi được lên Jira: {exc}", author_kind="system", author_id=None)

