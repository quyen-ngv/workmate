"""Sources of a ticket (SI-39): the tickets, Jira issues, repo files and URLs its description names, which of them
the AI read (from the run activity, never from what the AI says), skipped with a reason, or could not read.

The list is written when the ticket is created and when its description changes (`sync_tx`, one place for every
way a ticket is made). "external" (a URL outside the configured Jira/Confluence) and "out_of_scope" (another team's
ticket or Jira project) are not stored: they depend on the ticket's team, set only when its workflow starts.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from actoroffice.domain.errors import NotFound, OfficeError, PolicyDenied
from actoroffice.domain.ids import utcnow
from actoroffice.domain.sources import STATUS_LABEL, display, parse_refs
from actoroffice.domain.work import Comment, Ticket
from actoroffice.store import Tx

if TYPE_CHECKING:
    from actoroffice.app import Office

FILE_LIMIT = 512 * 1024
HEADING_BYTES = 4096
ERROR_LIMIT = 300
COMMENT_POSITION = 100_000  # people's comments come after the description's sources
FILE_READ_TOOLS = {"Read", "NotebookRead"}
TOKEN_SPLIT = re.compile(r"[\s'\"<>|;&()]+")
# A shell command that prints a file's content (R4). Only one plain command counts: a chain, pipe, redirection or
# substitution (`cat a; git add x`, `cat x > y`, `$(…)`) is not taken apart, so it never marks a file read.
FILE_READ_COMMAND = re.compile(r"^\s*(?:cat|head|tail|less|more|nl|bat|type|sed\s+-n|Get-Content|gc)\s", re.IGNORECASE)
NOT_ONE_COMMAND = re.compile(r"[;&|\n\r>`]|\$\(")


async def sync_tx(tx: Tx, ticket_id: int, description: str) -> None:
    """Split the description into sources inside the caller's transaction. Sources kept keep their status, new ones
    are unread, the ones gone are hidden (kept for history). A key is a source only when a ticket with that key
    exists or its prefix is the Jira project key of a team."""
    row = await tx.fetchone("SELECT key FROM tickets WHERE id = ?", (ticket_id,))
    own = str(row["key"]).upper() if row else ""
    jira = {
        str(r["k"]).upper()
        for r in await tx.fetchall("SELECT jira_project_key AS k FROM teams WHERE jira_project_key IS NOT NULL AND jira_project_key <> ''")
    }
    wanted: list[tuple[str, str, str]] = []
    for raw in parse_refs(description):
        kind, ref = raw.kind, raw.ref
        if kind == "code":
            if ref.upper() == own:
                continue
            hit = await tx.fetchone("SELECT key FROM tickets WHERE key = ? COLLATE NOCASE", (ref,))
            if hit:
                kind, ref = "ticket", str(hit["key"])
            elif ref.split("-", 1)[0].upper() in jira:
                kind, ref = "jira", ref.upper()
            else:
                continue  # R-12, ISO-8601, AC-1, a key with no ticket…: not a source
        if (kind, ref, raw.locator) not in wanted:
            wanted.append((kind, ref, raw.locator))
    now = utcnow()
    for position, (kind, ref, locator) in enumerate(wanted):
        await tx.execute(
            "INSERT INTO ticket_sources(ticket_id, kind, ref, locator, position, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticket_id, kind, ref, locator) DO UPDATE SET position = excluded.position, active = 1, updated_at = excluded.updated_at",
            (ticket_id, kind, ref, locator, position, now, now),
        )
    for old in await tx.fetchall("SELECT id, kind, ref, locator FROM ticket_sources WHERE ticket_id = ? AND kind <> 'comment' AND active = 1", (ticket_id,)):
        if (old["kind"], old["ref"], old["locator"]) not in wanted:
            await tx.execute("UPDATE ticket_sources SET active = 0, updated_at = ? WHERE id = ?", (now, old["id"]))
    await tx.execute("UPDATE tickets SET sources_at = ? WHERE id = ?", (now, ticket_id))


class SourceService:
    def __init__(self, office: Office) -> None:
        self.office = office

    async def sync(self, ticket_id: int) -> None:
        async with self.office.db.tx() as tx:
            row = await tx.fetchone("SELECT description FROM tickets WHERE id = ?", (ticket_id,))
            if row is not None:
                await sync_tx(tx, ticket_id, str(row["description"] or ""))

    async def ensure(self, ticket: Ticket) -> None:
        """A ticket created before sources existed gets its list on first need (its next run, the check, the web)."""
        row = await self.office.db.fetchone("SELECT sources_at FROM tickets WHERE id = ?", (ticket.id,))
        if row is not None and row["sources_at"] is None:
            await self.sync(ticket.id)

    async def add_comments(self, ticket_id: int, comments: list[Comment]) -> None:
        """People's comments the first run received in its prompt: sources already read."""
        now = utcnow()
        async with self.office.db.tx() as tx:
            for c in comments:
                await tx.execute(
                    "INSERT INTO ticket_sources(ticket_id, kind, ref, locator, position, status, done_at, created_at, updated_at) "
                    "VALUES (?, 'comment', ?, '', ?, 'read', ?, ?, ?) ON CONFLICT(ticket_id, kind, ref, locator) DO NOTHING",
                    (ticket_id, f"comment:{c.id}", COMMENT_POSITION + c.id, now, now, now),
                )

    # ---- read from the activity --------------------------------------------------------------------------

    async def refresh(self, ticket: Ticket) -> None:
        """Mark sources read (or not readable) from the finished read activities of every run of the ticket."""
        await self.ensure(ticket)
        office = self.office
        rows = await office.db.fetchall(
            "SELECT id, kind, ref, status FROM ticket_sources WHERE ticket_id = ? AND active = 1 AND kind IN ('ticket', 'jira', 'file', 'url') "
            "AND status IN ('unread', 'error')",
            (ticket.id,),
        )
        if not rows:
            return
        events = await office.db.fetchall(
            "SELECT e.activity_json FROM run_events e JOIN runs r ON r.id = e.run_id WHERE r.ticket_id = ? "
            "AND json_extract(e.activity_json, '$.kind') IN ('read', 'command', 'office') "
            "AND json_extract(e.activity_json, '$.status') IN ('ok', 'error') ORDER BY e.id",
            (ticket.id,),
        )
        acts = [json.loads(e["activity_json"]) for e in events]
        roots = await self.roots(ticket)
        now = utcnow()
        for row in rows:
            ok, error = False, None
            for act in acts:
                if not _matches(row["kind"], row["ref"], act.get("fields") or {}):
                    continue
                if act.get("status") == "ok":
                    ok = True
                    break
                error = str((act.get("fields") or {}).get("error") or act.get("title") or "lỗi")[:ERROR_LIMIT]
            if ok:
                await office.db.execute(
                    "UPDATE ticket_sources SET status = 'read', error = NULL, done_at = ?, updated_at = ? WHERE id = ?", (now, now, row["id"])
                )
            elif error is not None and row["kind"] == "file" and _exists(roots, row["ref"]):
                # a failed read of the wrong path: the file is there, so it is still unread (R6)
                if row["status"] == "error":
                    await office.db.execute(
                        "UPDATE ticket_sources SET status = 'unread', error = NULL, done_at = NULL, updated_at = ? WHERE id = ?", (now, row["id"])
                    )
            elif error is not None and (row["status"] != "error"):
                await office.db.execute(
                    "UPDATE ticket_sources SET status = 'error', error = ?, done_at = ?, updated_at = ? WHERE id = ?", (error, now, now, row["id"])
                )

    async def skip(self, ticket: Ticket, ref: str, reason: str) -> list[str]:
        """The AI says a source does not bear on the ticket. It can never mark a source read. Returns what was skipped."""
        reason = (reason or "").strip()
        if not reason:
            raise OfficeError("reason is required: say why this source does not bear on the ticket")
        await self.ensure(ticket)
        rows = await self.office.db.fetchall(
            "SELECT id, kind, ref, locator, status FROM ticket_sources WHERE ticket_id = ? AND active = 1 AND kind <> 'comment' ORDER BY position",
            (ticket.id,),
        )
        wanted = (ref or "").strip()
        exact = [r for r in rows if display(r["kind"], r["ref"], r["locator"]) == wanted]
        hits = exact or [r for r in rows if r["ref"].lower() == wanted.lower()]
        if not hits:
            valid = ", ".join(display(r["kind"], r["ref"], r["locator"]) for r in rows) or "(none)"
            raise NotFound(f"'{wanted}' is not a source of {ticket.key}. Sources: {valid}")
        now = utcnow()
        done = []
        for r in hits:
            if r["status"] == "read":
                continue
            await self.office.db.execute(
                "UPDATE ticket_sources SET status = 'skipped', reason = ?, done_at = ?, updated_at = ? WHERE id = ?", (reason[:500], now, now, r["id"])
            )
            done.append(display(r["kind"], r["ref"], r["locator"]))
        return done

    # ---- what people and the prompt see ------------------------------------------------------------------

    async def list(self, ticket: Ticket, *, refresh: bool = True) -> list[dict[str, Any]]:
        """Every active source with its shown status. Another team's ticket shows its key only: no title anywhere."""
        if refresh:
            await self.refresh(ticket)
        else:
            await self.ensure(ticket)
        office = self.office
        rows = await office.db.fetchall(
            "SELECT * FROM ticket_sources WHERE ticket_id = ? AND active = 1 ORDER BY position, id", (ticket.id,)
        )
        jira_teams = {
            str(r["k"]).upper(): r["id"]
            for r in await office.db.fetchall("SELECT id, jira_project_key AS k FROM teams WHERE jira_project_key IS NOT NULL AND jira_project_key <> ''")
        }
        hosts = self._atlassian_hosts()
        out = []
        for r in rows:
            kind, ref = r["kind"], r["ref"]
            item: dict[str, Any] = {
                "kind": kind,
                "ref": ref,
                "locator": r["locator"],
                "display": display(kind, ref, r["locator"]),
                "status": r["status"],
                "reason": r["reason"],
                "error": r["error"],
                "required": True,
                "title": None,
                "link": None,
            }
            if kind == "ticket":
                other = await office.db.fetchone("SELECT key, title, team_id FROM tickets WHERE key = ?", (ref,))
                if other is None or (other["team_id"] is not None and other["team_id"] != ticket.team_id):
                    item.update(required=False, status="out_of_scope")
                else:
                    item.update(title=other["title"], link=other["key"])
            elif kind == "jira":
                if jira_teams.get(ref.split("-", 1)[0]) != ticket.team_id:
                    item.update(required=False, status="out_of_scope")
            elif kind == "url":
                if (urlparse(ref).hostname or "").lower() not in hosts:
                    item.update(required=False, status="external", link=ref)
                else:
                    item["link"] = ref
            elif kind == "file":
                item["link"] = ref
            elif kind == "comment":
                comment = await office.db.fetchone("SELECT author_id, body, created_at FROM comments WHERE id = ? AND ticket_id = ?", (int(ref.split(":", 1)[1]), ticket.id))
                if comment is not None:
                    item["title"] = f"{comment['author_id'] or 'người'} · {str(comment['created_at'])[:16].replace('T', ' ')}: {_one_line(comment['body'], 120)}"
            item["label"] = STATUS_LABEL.get(item["status"], item["status"])
            out.append(item)
        return out

    async def unread_required(self, ticket: Ticket) -> list[str]:
        return [s["display"] for s in await self.list(ticket) if s["required"] and s["status"] == "unread"]

    async def search_text(self, ticket: Ticket) -> str:
        """Words that say what the sources are about, for finding similar work: the titles of this team's tickets,
        file names and the first heading of a markdown file, URLs. Another team's ticket adds nothing."""
        parts: list[str] = []
        roots = await self.roots(ticket)
        for s in await self.list(ticket, refresh=False):
            if s["status"] == "out_of_scope" or s["kind"] == "comment":
                continue
            if s["kind"] == "ticket" and s["title"]:
                parts.append(s["title"])
            elif s["kind"] == "file":
                parts.append(Path(s["ref"]).stem.replace("-", " ").replace("_", " "))
                if s["ref"].endswith(".md"):
                    heading = _heading(roots, s["ref"])
                    if heading:
                        parts.append(heading)
            elif s["kind"] == "url":
                parts.append(s["ref"])
        return " ".join(parts)

    async def roots(self, ticket: Ticket) -> list[Path]:
        """Where the ticket's files are: the workspace of its root ticket, then its project folder."""
        office = self.office
        root = await office.tickets.root_of(ticket.id)
        dirs = [office.settings.workspaces_dir / root.key]
        if ticket.project_id:
            try:
                project = await office.projects.get(ticket.project_id)
            except OfficeError:
                project = None
            if project is not None and project.path:
                dirs.append(Path(project.path))
        return [d.resolve() for d in dirs if d.is_dir()]

    async def file_text(self, ticket: Ticket, path: str) -> dict[str, Any]:
        """The content of a file that is a source of the ticket, from its workspace or project only."""
        wanted = path.replace("\\", "/").strip()
        row = await self.office.db.fetchone(
            "SELECT 1 FROM ticket_sources WHERE ticket_id = ? AND kind = 'file' AND active = 1 AND ref = ?", (ticket.id, wanted)
        )
        if row is None:
            raise PolicyDenied(f"{wanted} không phải nguồn của {ticket.key}")
        for root in await self.roots(ticket):
            target = (root / wanted).resolve()
            if not target.is_relative_to(root) or ".git" in target.relative_to(root).parts:
                raise PolicyDenied("đường dẫn nằm ngoài thư mục của ticket")
            if target.is_file():
                with target.open("rb") as fh:
                    data = fh.read(FILE_LIMIT + 1)
                return {"path": wanted, "text": data[:FILE_LIMIT].decode("utf-8", "replace"), "truncated": len(data) > FILE_LIMIT}
        raise NotFound(f"không tìm thấy {wanted} trong workspace hay thư mục dự án")

    def _atlassian_hosts(self) -> set[str]:
        from actoroffice.services.jira import load_credentials

        client = getattr(self.office.jira, "client", None)
        base = getattr(client, "base_url", None) or (load_credentials(self.office.settings.data_dir) or {}).get("base_url")
        host = (urlparse(base).hostname or "").lower() if base else ""
        return {host} if host else set()


def _matches(kind: str, ref: str, fields: dict[str, Any]) -> bool:
    tool = str(fields.get("tool") or "")
    if kind == "ticket":
        # doc_get without `ticket` reads the current ticket; ticket_get before SI-39 kept its key in `path` only
        target = fields.get("ticket") or (fields.get("path") if tool == "ticket_get" else None)
        return tool in ("ticket_get", "doc_get") and str(target or "").strip().upper() == ref.upper()
    if kind == "jira":
        return tool == "jira_issue" and str(fields.get("key") or fields.get("path") or "").strip().upper() == ref.upper()
    if kind == "url":
        if tool == "wiki_page":
            page = str(fields.get("id") or "").strip()
            return bool(page) and re.search(rf"(?<!\d){re.escape(page)}(?!\d)", ref) is not None
        if tool == "jira_issue":
            key = str(fields.get("key") or "").strip().upper()
            return bool(key) and re.search(rf"(?<![\w-]){re.escape(key)}(?![\w-])", ref.upper()) is not None
        return False
    if kind == "file":
        paths: list[str] = []
        if tool in FILE_READ_TOOLS:
            paths.append(str(fields.get("path") or ""))
        command = str(fields.get("command") or "")
        if FILE_READ_COMMAND.match(command) and not NOT_ONE_COMMAND.search(command):  # not `ls` / `git add` / `rm`
            paths += TOKEN_SPLIT.split(command)
        return any(_same_file(p, ref) for p in paths if p)
    return False


def _same_file(path: str, ref: str) -> bool:
    p = path.replace("\\", "/").strip()
    return p == ref or p.endswith("/" + ref)


def _exists(roots: list[Path], ref: str) -> bool:
    for root in roots:
        target = (root / ref).resolve()
        if target.is_relative_to(root) and target.is_file():
            return True
    return False


def _heading(roots: list[Path], ref: str) -> str:
    for root in roots:
        target = (root / ref).resolve()
        if not target.is_relative_to(root) or not target.is_file():
            continue
        try:
            with target.open("rb") as fh:
                head = fh.read(HEADING_BYTES).decode("utf-8", "replace")
        except OSError:
            continue
        for line in head.splitlines():
            if line.startswith("# "):
                return line[2:].strip()
    return ""


def _one_line(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text[:limit] + ("…" if len(text) > limit else "")
