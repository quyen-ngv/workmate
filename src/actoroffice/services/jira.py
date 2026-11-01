"""Jira Cloud mirror: Jira is the source of truth for what people asked; the office keeps stages and links.

- Every `jira_poll_s` seconds, for each team with a Jira project, issues matching the team's JQL (default:
  `project = KEY AND labels = ai AND statusCategory != Done`) become office tickets running through stages.
  The template comes from a label (`ai-research`, `ai-doc`, `ai-automation`; default the team's template).
- A changed summary/description is mirrored; a changed description after the requirements were approved is
  recorded as a requirement change (the AI hears it and the BA gate reopens when the requirements change).
- When the BA approves the requirements, the approved requirements are posted back as a Jira comment.

Credentials live in `<data>/secrets/jira.json` ({"base_url", "email", "token"}); the AI never sees them.
REST API v3 (`/rest/api/3/search/jql`, `/issue/{key}`, `/issue/{key}/comment`). Descriptions use the Atlassian
Document Format (ADF): converted to plain markdown-ish text on the way in, simple paragraphs on the way out.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.team import Team

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

LABEL_TEMPLATES = {"ai-research": "research", "ai-doc": "doc", "ai-automation": "automation", "ai-code": "code"}
FIELDS = ["summary", "description", "labels", "status", "reporter", "priority", "issuetype", "updated"]


class JiraError(OfficeError):
    code = 502


def secrets_path(data_dir: Path) -> Path:
    return data_dir / "secrets" / "jira.json"


def load_credentials(data_dir: Path) -> dict[str, str] | None:
    try:
        data = json.loads(secrets_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not all(data.get(k) for k in ("base_url", "email", "token")):
        return None
    return {k: str(data[k]) for k in ("base_url", "email", "token")}


def save_credentials(data_dir: Path, base_url: str, email: str, token: str) -> None:
    path = secrets_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"base_url": base_url.rstrip("/"), "email": email, "token": token}), encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


class JiraClient:
    def __init__(self, base_url: str, email: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.http = httpx.AsyncClient(base_url=self.base_url, auth=(email, token), timeout=30, transport=transport, headers={"Accept": "application/json"})

    async def close(self) -> None:
        await self.http.aclose()

    async def _call(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            response = await self.http.request(method, url, **kwargs)
        except httpx.HTTPError as exc:
            raise JiraError(f"Jira: {type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise JiraError(f"Jira {method} {url} → {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else None

    async def search(self, jql: str, *, limit: int = 50) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        token: str | None = None
        while len(issues) < limit:
            body: dict[str, Any] = {"jql": jql, "fields": FIELDS, "maxResults": min(50, limit - len(issues))}
            if token:
                body["nextPageToken"] = token
            data = await self._call("POST", "/rest/api/3/search/jql", json=body)
            issues += data.get("issues") or []
            token = data.get("nextPageToken")
            if not token or data.get("isLast", True):
                break
        return issues

    async def issue(self, key: str) -> dict[str, Any]:
        return await self._call("GET", f"/rest/api/3/issue/{key}", params={"fields": ",".join(FIELDS)})

    async def comment(self, key: str, text: str) -> None:
        await self._call("POST", f"/rest/api/3/issue/{key}/comment", json={"body": text_to_adf(text)})

    def browse_url(self, key: str) -> str:
        return f"{self.base_url}/browse/{key}"


class JiraMirror:
    def __init__(self, office: Office, client: JiraClient | None = None) -> None:
        self.office = office
        self.client = client
        self._lock = asyncio.Lock()

    def ready(self) -> bool:
        if self.client is None:
            creds = load_credentials(self.office.settings.data_dir)
            if creds:
                self.client = JiraClient(creds["base_url"], creds["email"], creds["token"])
        return self.client is not None

    async def close(self) -> None:
        if self.client is not None:
            await self.client.close()

    async def run_forever(self) -> None:
        interval = self.office.settings.jira_poll_s
        if interval <= 0:
            return
        while True:
            try:
                if self.ready():
                    await self.sync()
            except JiraError as exc:
                log.warning("jira mirror: %s", exc)
            except Exception:
                log.exception("jira mirror failed")
            await asyncio.sleep(interval)

    async def sync(self) -> list[str]:
        """One pass over every team with a Jira project. Returns the keys of tickets created."""
        if not self.ready():
            return []
        created: list[str] = []
        async with self._lock:
            for team in await self.office.teams.list_teams():
                if not team.jira_project_key:
                    continue
                jql = team.settings.jira_jql or f"project = {team.jira_project_key} AND labels = ai AND statusCategory != Done ORDER BY created ASC"
                assert self.client is not None
                for issue in await self.client.search(jql):
                    key = await self._mirror(team, issue)
                    if key:
                        created.append(key)
        return created

    async def _mirror(self, team: Team, issue: dict[str, Any]) -> str | None:
        office = self.office
        assert self.client is not None
        jira_key = str(issue["key"])
        fields = issue.get("fields") or {}
        title = str(fields.get("summary") or jira_key)[:200]
        description = adf_to_text(fields.get("description"))
        row = await office.db.fetchone("SELECT id FROM tickets WHERE jira_key = ?", (jira_key,))
        if row:
            ticket = await office.tickets.get(row["id"])
            if ticket.title != title or ticket.description.strip() != description.strip():
                await office.db.execute("UPDATE tickets SET title = ?, description = ? WHERE id = ?", (title, description, ticket.id))
                approved = await office.db.fetchone(
                    "SELECT 1 FROM documents WHERE ticket_id = ? AND key = 'requirements' AND approved_revision IS NOT NULL", (ticket.id,)
                )
                if ticket.description.strip() != description.strip() and approved and ticket.status.value not in ("done", "cancelled"):
                    await office.tickets.change_notice(ticket.id, f"Mô tả trên Jira {jira_key} đã thay đổi:\n{description[:1500]}", by="Jira")
            return None
        labels = [str(label).lower() for label in fields.get("labels") or []]
        template = next((LABEL_TEMPLATES[label] for label in labels if label in LABEL_TEMPLATES), team.settings.default_template)
        owner = await self._owner(fields.get("reporter") or {})
        ticket = await office.tickets.create(title, description=description, created_by=f"jira:{jira_key}")
        await office.db.execute(
            "UPDATE tickets SET jira_key = ?, jira_url = ? WHERE id = ?", (jira_key, self.client.browse_url(jira_key), ticket.id)
        )
        await office.stages.start(ticket.id, template=template, by="jira", team_id=team.id, owner_user_id=owner)
        return ticket.key

    async def _owner(self, reporter: dict[str, Any]) -> str | None:
        email = str(reporter.get("emailAddress") or "").lower()
        name = str(reporter.get("displayName") or "")
        for user in await self.office.teams.list_users():
            if (email and (user.email or "").lower() == email) or (name and user.name.lower() == name.lower()):
                return user.id
        return None

    async def requirements_approved(self, ticket_id: int, approver: str) -> None:
        """Post the approved requirements back to the Jira issue (people keep reading Jira)."""
        office = self.office
        ticket = await office.tickets.get(ticket_id)
        if not ticket.jira_key or not self.ready():
            return
        doc = await office.tickets.get_document(ticket.id, "requirements")
        if doc is None:
            return
        assert self.client is not None
        text = f"✅ Requirements được {approver} duyệt (revision {doc.revision}, {ticket.key}):\n\n{doc.body}"
        try:
            await self.client.comment(ticket.jira_key, text[:30000])
        except JiraError as exc:
            log.warning("jira: cannot comment on %s: %s", ticket.jira_key, exc)
            with contextlib.suppress(NotFound):
                await office.tickets.add_comment(ticket.id, f"Không ghi được lên Jira: {exc}", author_kind="system", author_id=None)


# ---- Atlassian Document Format ----------------------------------------------------------------------------


def adf_to_text(node: Any) -> str:
    """ADF → readable text (headings as #, lists as -, code as fences). Strings pass through (Jira Server / v2)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    out: list[str] = []
    _walk(node, out, depth=0)
    text = "".join(out)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip()


def _walk(node: Any, out: list[str], depth: int) -> None:
    if isinstance(node, list):
        for child in node:
            _walk(child, out, depth)
        return
    if not isinstance(node, dict):
        return
    kind = node.get("type")
    content = node.get("content") or []
    if kind == "text":
        out.append(str(node.get("text", "")))
    elif kind == "hardBreak":
        out.append("\n")
    elif kind == "heading":
        level = int((node.get("attrs") or {}).get("level") or 2)
        out.append("#" * level + " ")
        _walk(content, out, depth)
        out.append("\n\n")
    elif kind == "paragraph":
        _walk(content, out, depth)
        out.append("\n\n" if depth == 0 else "\n")
    elif kind in ("bulletList", "orderedList"):
        for i, item in enumerate(content, 1):
            out.append("  " * depth + ("- " if kind == "bulletList" else f"{i}. "))
            _walk(item.get("content") or [], out, depth + 1)
        if depth == 0:
            out.append("\n")
    elif kind == "codeBlock":
        out.append("```\n")
        _walk(content, out, depth)
        out.append("\n```\n\n")
    elif kind == "mention":
        out.append("@" + str((node.get("attrs") or {}).get("text") or "").lstrip("@"))
    elif kind == "inlineCard":
        out.append(str((node.get("attrs") or {}).get("url") or ""))
    elif kind in ("table",):
        for row in content:
            cells = []
            for cell in row.get("content") or []:
                buf: list[str] = []
                _walk(cell.get("content") or [], buf, depth + 1)
                cells.append("".join(buf).strip().replace("\n", " "))
            out.append("| " + " | ".join(cells) + " |\n")
        out.append("\n")
    else:
        _walk(content, out, depth)


def text_to_adf(text: str) -> dict[str, Any]:
    """Plain text → ADF: one paragraph per block, lines kept with hard breaks, ``` blocks as code."""
    blocks: list[dict[str, Any]] = []
    parts = text.split("```")
    for i, part in enumerate(parts):
        if i % 2 == 1:
            code = part.split("\n", 1)[1] if "\n" in part else part
            blocks.append({"type": "codeBlock", "content": [{"type": "text", "text": code.strip("\n") or " "}]})
            continue
        for para in [p for p in part.split("\n\n") if p.strip()]:
            content: list[dict[str, Any]] = []
            for j, line in enumerate(para.strip("\n").split("\n")):
                if j:
                    content.append({"type": "hardBreak"})
                if line:
                    content.append({"type": "text", "text": line})
            blocks.append({"type": "paragraph", "content": content})
    return {"type": "doc", "version": 1, "content": blocks or [{"type": "paragraph", "content": []}]}
