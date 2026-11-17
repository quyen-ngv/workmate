"""The documents repository: a git folder that holds every document of the office as a file, kept in step both ways.

Why: the documents live in the office's database, where a gate pins the exact revision it approved. That is right for
the workflow and poor for everything else people and tools do with documents: browse them as a tree, grep them, read
them in an IDE next to the code, review a change as a diff, let another AI tool read them. So the office mirrors them
into a folder (`docs_repo` in the settings; layout in `actoroffice/docrepo/layout.py`) and commits it.

Office → folder. A document is written to its file the moment it is saved (the service listens to the office's
events), so a search of the folder sees it at once. Commits are another matter: they wait until the folder has been
quiet for `docs_commit_s` seconds, so a run that saves a document ten times makes one commit, not ten.

Folder → office. A file whose content is not what the office last wrote was changed outside: in an editor, by another
tool, or by a commit somebody pushed. The folder is looked over every `docs_scan_s` seconds (file sizes and times
first, content only where those moved) and before every commit:
- a document file → a new revision of that document (author `git:<name>`), through the same door as any other save:
  what it is written from is recorded, and an approved document that changed has to be approved again;
- a new `<name>.md` in a ticket's folder → a new document of that ticket;
- a knowledge file → the item's title and content; a new file in a knowledge folder → a new item;
- anything else (`knowledge/library/…`) → indexed for search, never rewritten;
- a file the office generates (a ticket's README, the catalogues, AGENTS.md) → written again.
When the office saved a newer revision since the file was written, the two are merged line by line (`git merge-file`);
where both changed the same lines the file wins, and nothing is lost: every revision stays in the database.

A remote. With `docs_remote` (and `docs_connection` for the credential) or a remote named `origin`, the folder is
pushed after each commit and fetched every `docs_pull_s` seconds, at once when the code host announces a push
(`services/webhooks.py`). What came in is merged (the remote side wins a conflict), then read like any outside change.

Search. Every text file of the folder is cut at its headings into a full-text index (`docrepo_fts`, accents ignored):
`search` ranks, `grep` matches exactly, `read` returns a file or one section of it. The index is rebuilt from the
files; it holds nothing of its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.codeintel.sync import auth_env
from actoroffice.core import http as core_http
from actoroffice.docrepo import layout
from actoroffice.docrepo.chunks import chunks
from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.ids import utcnow
from actoroffice.domain.work import Ticket
from actoroffice.ports.connection import ConnectorError

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

BOT_NAME, BOT_EMAIL = "ActorOffice", "actoroffice@localhost"
SKIP_DIRS = {".git", "node_modules", "__pycache__"}
MAX_FILE = 2_000_000  # bytes: a larger text file is left alone (not indexed, not imported)
GREP_LINE = 300
GENERATED_WEIGHT = 0.35  # a hit in a README or AGENTS.md the office writes counts for this much of a hit in a document


class DocRepoError(OfficeError):
    code = 400


def sha_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class DocRepoService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self._tickets: set[int] = set()  # tickets whose files must be written again
        self._kb: set[int] = set()
        self._indexes = False  # the catalogues must be written again
        self._notes: list[str] = []  # what the next commit says
        self._lock = asyncio.Lock()  # one pass over the folder at a time
        self._ready = False
        self._urgent = False  # something was approved: commit without waiting for quiet
        self._failed: set[str] = set()  # files whose outside edit could not be taken in: not written over until it is
        self.last_error = ""
        self.last_sync = ""

    # ---- where ---------------------------------------------------------------------------------------------------

    @property
    def root(self) -> Path | None:
        return self.office.settings.docs_repo_path

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def _need(self) -> Path:
        if self.root is None:
            raise DocRepoError("Kho tài liệu chưa bật: đặt `docs_repo: <thư mục>` trong config.yaml rồi khởi động lại.")
        return self.root

    async def _git(self, *args: str, env: dict[str, str] | None = None, check: bool = True, timeout: float = 120) -> tuple[int, str]:
        root = self._need()
        try:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", str(root), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, **(env or {})}
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except (OSError, TimeoutError) as exc:
            raise DocRepoError(f"git {args[0]}: {exc}") from exc
        text = out.decode("utf-8", "replace").strip()
        if check and proc.returncode != 0:
            raise DocRepoError(f"git {args[0]}: {self.office.redact(text)[:400]}")
        return int(proc.returncode or 0), text

    async def ensure(self) -> Path:
        """The folder exists and is a git repository the office can commit to."""
        root = self._need()
        if self._ready and (root / ".git").exists():
            return root
        root.mkdir(parents=True, exist_ok=True)
        if not (root / ".git").exists():
            await self._git("init", "-q", "-b", self.office.settings.docs_branch)
        code, name = await self._git("config", "--local", "user.name", check=False)
        if code != 0 or not name:
            await self._git("config", "--local", "user.name", BOT_NAME)
            await self._git("config", "--local", "user.email", BOT_EMAIL)
        await self._git("config", "--local", "core.autocrlf", "false")  # files are written with \n; what is hashed is what is on disk
        await self._git("config", "--local", "core.quotepath", "false")
        self._ready = True
        return root

    # ---- what the office remembers of each file --------------------------------------------------------------

    async def _row(self, path: str) -> dict[str, Any] | None:
        row = await self.office.db.fetchone("SELECT * FROM docrepo_files WHERE path = ?", (path,))
        return dict(row) if row else None

    async def _record(self, path: str, kind: str, data: bytes, *, ticket_id: int | None = None, doc_key: str | None = None, revision: int | None = None, kb_id: int | None = None) -> None:
        full = self._need() / path
        st = full.stat() if full.exists() else None
        await self.office.db.execute(
            "INSERT INTO docrepo_files(path, kind, ticket_id, doc_key, revision, kb_id, sha, size, mtime_ns, synced_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET kind = excluded.kind, ticket_id = excluded.ticket_id, doc_key = excluded.doc_key, revision = excluded.revision, "
            "kb_id = excluded.kb_id, sha = excluded.sha, size = excluded.size, mtime_ns = excluded.mtime_ns, synced_at = excluded.synced_at",
            (path, kind, ticket_id, doc_key, revision, kb_id, sha_of(data), st.st_size if st else 0, st.st_mtime_ns if st else 0, utcnow()),
        )

    async def _forget(self, path: str, *, delete: bool = False) -> None:
        await self.office.db.execute("DELETE FROM docrepo_files WHERE path = ?", (path,))
        await self.office.db.execute("DELETE FROM docrepo_fts WHERE path = ?", (path,))
        if delete:
            with contextlib.suppress(OSError):
                (self._need() / path).unlink()

    async def _index(self, path: str, text: str) -> None:
        db = self.office.db
        await db.execute("DELETE FROM docrepo_fts WHERE path = ?", (path,))
        for c in chunks(text):
            await db.execute("INSERT INTO docrepo_fts(path, heading, text, line) VALUES (?, ?, ?, ?)", (path, c.heading, c.text, c.line))

    async def _write(self, path: str, text: str, kind: str, **ids: Any) -> bool:
        """Make the file say `text`. True when it had to be written."""
        full = self._need() / path
        data = text.encode("utf-8")
        row = await self._row(path)
        same = full.exists() and full.read_bytes() == data
        if not same:
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_bytes(data)
        if not same or row is None or row["sha"] != sha_of(data) or row["kind"] != kind or row["revision"] != ids.get("revision"):
            await self._record(path, kind, data, **ids)
            await self._index(path, text)
        return not same

    # ---- office → folder -------------------------------------------------------------------------------------

    def touch(self, ticket_id: int | None = None, *, kb_id: int | None = None) -> None:
        """Something of this ticket (or knowledge item) changed: its files are written again at the next pass."""
        if ticket_id is not None:
            self._tickets.add(int(ticket_id))
        if kb_id is not None:
            self._kb.add(int(kb_id))
            self._indexes = True

    def on_event(self, kind: str, data: dict[str, Any]) -> None:
        """An event of the office: which files it may have changed."""
        if kind.startswith("kb."):
            item = data.get("item")
            if isinstance(item, dict) and item.get("id") is not None:
                self.touch(kb_id=int(item["id"]))
            return
        ticket_id = data.get("ticket_id")
        if ticket_id is None and isinstance(data.get("ticket"), dict):
            ticket_id = data["ticket"].get("id")
        if isinstance(ticket_id, int) and (kind.startswith(("ticket.", "doc.")) or kind == "interaction.resolved"):
            self.touch(ticket_id)
            if kind == "interaction.resolved":
                self._urgent = True  # a gate was answered: what was approved should be in the history as its own commit

    async def _is_epic(self, ticket: Ticket) -> bool:
        pack = self.office.docs.pack_of(ticket)
        if any(stage.spawn for stage in pack.flows.get(ticket.stage_template or "", [])):
            return True
        return await self.office.db.fetchone("SELECT 1 FROM tickets WHERE parent_id = ? LIMIT 1", (ticket.id,)) is not None

    async def _wanted(self, ticket: Ticket) -> bool:
        """Does the ticket have a folder? One that has a document, follows a flow of the pack, or has tasks under it."""
        if self.office.docs.applies(ticket):
            return True
        db = self.office.db
        return (
            await db.fetchone("SELECT 1 FROM documents WHERE ticket_id = ? LIMIT 1", (ticket.id,)) is not None
            or await db.fetchone("SELECT 1 FROM tickets WHERE parent_id = ? LIMIT 1", (ticket.id,)) is not None
        )

    async def _dir(self, ticket: Ticket) -> str:
        chain = [ticket]
        while chain[0].parent_id is not None and len(chain) < 12:
            chain.insert(0, await self.office.tickets.get(chain[0].parent_id))
        return layout.ticket_dir([layout.Node(t.key, t.title, await self._is_epic(t)) for t in chain])

    async def _folder_of(self, ticket: Ticket) -> str:
        """The folder of a ticket for a link to it, "" when it has none."""
        return await self._dir(ticket) if await self._wanted(ticket) else ""

    async def _info(self, ticket: Ticket) -> layout.TicketInfo:
        office = self.office
        parent = await office.tickets.get(ticket.parent_id) if ticket.parent_id is not None else None
        spawned = await office.db.fetchone("SELECT item_id FROM doc_spawned WHERE child_id = ?", (ticket.id,))
        team = await office.teams.get_team(ticket.team_id) if ticket.team_id else None
        project = None
        if ticket.project_id:
            with contextlib.suppress(OfficeError):
                project = await office.projects.get(ticket.project_id)
        children = []
        for r in await office.db.fetchall("SELECT id FROM tickets WHERE parent_id = ? ORDER BY id", (ticket.id,)):
            c = await office.tickets.get(int(r["id"]))
            children.append((c.key, c.title, c.status.value, c.stage or "", await self._folder_of(c)))
        docs = []
        for r in await office.db.fetchall("SELECT key, revision, approved_revision, updated_by, updated_at, baseline_json FROM documents WHERE ticket_id = ? ORDER BY key", (ticket.id,)):
            based = json.loads(r["baseline_json"]).get("docs", {}) if r["baseline_json"] else {}
            docs.append(layout.DocInfo(str(r["key"]), int(r["revision"]), r["approved_revision"], str(r["updated_by"]), str(r["updated_at"]), based))
        words = office.docs.pack_of(ticket).syntax.relations
        links = [
            layout.LinkInfo(link.label(words), link.other.key, link.other.title, link.other.status.value, await self._folder_of(link.other), link.to_item, link.detail if link.outgoing else "")
            for link in await office.links.of(ticket)
        ]
        people = [f"{m.user.name} ({', '.join(m.roles)})" if m.roles else m.user.name for m in await office.teams.participants(ticket.id)]
        return layout.TicketInfo(
            key=ticket.key, title=ticket.title, type=ticket.type, status=ticket.status.value, stage=ticket.stage, flow=ticket.stage_template,
            description=office.redact(ticket.description), path=await self._dir(ticket), team=team.name if team else "", project=project.name if project else "",
            parent=(parent.key, parent.title, await self._dir(parent)) if parent else None, item=str(spawned["item_id"]) if spawned else "",
            people=people, children=children, docs=docs, links=links, updated_at=ticket.updated_at,
        )  # fmt: skip

    async def _move(self, ticket_id: int, new_dir: str) -> None:
        """The ticket's folder is called something else now (its title changed, it got tasks): move it, files and all."""
        row = await self.office.db.fetchone("SELECT path FROM docrepo_files WHERE kind = 'ticket' AND ticket_id = ?", (ticket_id,))
        if row is None:
            return
        old_dir = str(row["path"]).rsplit("/", 1)[0]
        if old_dir == new_dir:
            return
        root = self._need()
        if (root / old_dir).is_dir() and not (root / new_dir).exists():
            (root / new_dir).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(root / old_dir), str(root / new_dir))
        db = self.office.db
        for table in ("docrepo_files", "docrepo_fts"):
            await db.execute(f"UPDATE {table} SET path = ? || substr(path, ?) WHERE path LIKE ? ESCAPE '\\'", (new_dir, len(old_dir) + 1, _like(old_dir) + "/%"))

    async def _export_ticket(self, ticket_id: int) -> bool:
        office = self.office
        try:
            ticket = await office.tickets.get(ticket_id)
        except NotFound:
            for r in await office.db.fetchall("SELECT path FROM docrepo_files WHERE ticket_id = ?", (ticket_id,)):
                await self._forget(str(r["path"]), delete=True)
            return True
        if not await self._wanted(ticket):
            return False
        # an edit made outside to one of its files is taken in before the file is written again
        for r in await office.db.fetchall("SELECT path FROM docrepo_files WHERE kind = 'doc' AND ticket_id = ?", (ticket_id,)):
            await self._absorb(str(r["path"]))
        info = await self._info(ticket)
        if any(p.startswith(info.path + "/") for p in self._failed):
            self._tickets.add(ticket.id)  # an edit of one of its files waits to be taken in: the folder is written after it
            return False
        await self._move(ticket.id, info.path)
        changed = False
        for doc in info.docs:
            stored = await office.tickets.get_document(ticket.id, doc.key)
            if stored is None:
                continue
            path = layout.doc_path(info.path, doc.key)
            if await self._write(path, layout.doc_file(info, doc, office.redact(stored.body)), "doc", ticket_id=ticket.id, doc_key=doc.key, revision=doc.revision):
                changed = True
                state = {"approved": ", đã duyệt", "changed": f", sửa sau bản duyệt {doc.approved_revision}"}.get(doc.state, "")
                self._notes.append(f"{ticket.key} {doc.key} bản {doc.revision} ({doc.updated_by}{state})")
        changed |= await self._write(f"{info.path}/{layout.README}", layout.ticket_file(info), "ticket", ticket_id=ticket.id)
        return changed

    async def _export_kb(self, item_id: int) -> bool:
        office = self.office
        rows = await office.db.fetchall("SELECT path FROM docrepo_files WHERE kind = 'kb' AND kb_id = ?", (item_id,))
        try:
            item = await office.knowledge.get(item_id)
        except NotFound:
            item = None
        if item is None or item.status == "rejected":
            for r in rows:
                await self._forget(str(r["path"]), delete=True)
            return bool(rows)
        for r in rows:
            await self._absorb(str(r["path"]))
        if any(str(r["path"]) in self._failed for r in rows):
            return False
        item = await office.knowledge.get(item_id)
        path = layout.kb_path(item.kind, item.id, item.title)
        for r in rows:
            if str(r["path"]) != path:
                await self._forget(str(r["path"]), delete=True)
        ticket_key = ""
        if item.ticket_id:
            with contextlib.suppress(NotFound):
                ticket_key = (await office.tickets.get(item.ticket_id)).key
        changed = await self._write(path, layout.kb_file(item.model_dump(), ticket_key), "kb", kb_id=item.id)
        if changed:
            self._notes.append(f"kiến thức #{item.id} {item.title[:60]} ({item.status})")
        return changed

    async def _export_indexes(self) -> bool:
        office = self.office
        pack = office.docs.pack
        epics: list[tuple[layout.TicketInfo, int]] = []
        others: list[layout.TicketInfo] = []
        for r in await office.db.fetchall("SELECT DISTINCT ticket_id FROM docrepo_files WHERE kind = 'ticket' ORDER BY ticket_id"):
            try:
                ticket = await office.tickets.get(int(r["ticket_id"]))
            except NotFound:
                continue
            if ticket.parent_id is not None:
                continue
            info = await self._info(ticket)
            if info.path.startswith(layout.EPICS + "/"):
                epics.append((info, len(info.children)))
            else:
                others.append(info)
        from actoroffice.services.knowledge import KIND_VI

        items: list[tuple[dict[str, Any], str]] = []
        counts: dict[str, int] = {}
        for r in await office.db.fetchall("SELECT path, kb_id FROM docrepo_files WHERE kind = 'kb' ORDER BY path"):
            with contextlib.suppress(NotFound):
                item = await office.knowledge.get(int(r["kb_id"]))
                items.append((item.model_dump(), str(r["path"])))
                counts[KIND_VI.get(item.kind, item.kind)] = counts.get(KIND_VI.get(item.kind, item.kind), 0) + 1
        library = []
        prefix = f"{layout.KNOWLEDGE}/{layout.LIBRARY}/"
        for r in await office.db.fetchall("SELECT path FROM docrepo_files WHERE kind = 'library' ORDER BY path"):
            path = str(r["path"])
            if path.startswith(prefix):
                head = await office.db.fetchone("SELECT heading FROM docrepo_fts WHERE path = ? AND heading <> '' LIMIT 1", (path,))
                library.append((path, str(head["heading"]).split(" › ")[0] if head else ""))
        keywords = {name: getattr(pack.syntax, name)[0] for name in ("trace", "source", "changes") if getattr(pack.syntax, name)}
        changed = await self._write(layout.README, layout.index_file(epics, others, counts, len(library)), "index")
        changed |= await self._write(f"{layout.KNOWLEDGE}/{layout.README}", layout.kb_index_file(items, KIND_VI, library), "index")
        changed |= await self._write(layout.AGENTS, layout.agents_file(pack.syntax.relations, keywords, {k: v.title for k, v in pack.item_kinds.items()}), "index")
        return changed

    async def flush(self) -> bool:
        """Write the files of everything touched since the last pass. True when a file changed."""
        office = self.office
        changed = False
        if self._tickets:
            around: set[int] = set()
            for ticket_id in list(self._tickets):  # one step out: the cards of its parent, its tasks and what it is linked to name it
                rows = await office.db.fetchall(
                    "SELECT parent_id AS id FROM tickets WHERE id = ? AND parent_id IS NOT NULL UNION SELECT id FROM tickets WHERE parent_id = ? "
                    "UNION SELECT to_id FROM ticket_links WHERE from_id = ? UNION SELECT from_id FROM ticket_links WHERE to_id = ?",
                    (ticket_id, ticket_id, ticket_id, ticket_id),
                )
                around |= {int(r["id"]) for r in rows}
            todo = sorted(self._tickets | around)
            self._tickets.clear()
            for ticket_id in todo:
                try:
                    changed |= await self._export_ticket(ticket_id)
                except OfficeError as exc:
                    log.warning("docs repo: ticket %s not written: %s", ticket_id, exc)
            self._indexes = True
        while self._kb:
            changed |= await self._export_kb(self._kb.pop())
        if self._indexes:
            self._indexes = False
            changed |= await self._export_indexes()
        return changed

    async def reconcile(self) -> None:
        """Everything the office holds is written again where the files differ (first start, or after a restore)."""
        db = self.office.db
        rows = await db.fetchall(
            "SELECT DISTINCT ticket_id AS id FROM documents UNION SELECT id FROM tickets WHERE stage_template IS NOT NULL "
            "UNION SELECT DISTINCT parent_id FROM tickets WHERE parent_id IS NOT NULL"
        )
        self._tickets |= {int(r["id"]) for r in rows if r["id"] is not None}
        self._kb |= {int(r["id"]) for r in await db.fetchall("SELECT id FROM kb_items")}
        self._indexes = True

    # ---- folder → office -------------------------------------------------------------------------------------

    def _walk(self) -> dict[str, tuple[int, int]]:
        root = self._need()
        found: dict[str, tuple[int, int]] = {}
        stack = [root]
        while stack:
            folder = stack.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIP_DIRS:
                        stack.append(Path(entry.path))
                elif entry.name.lower().endswith(layout.TEXT_SUFFIXES):
                    st = entry.stat()
                    if st.st_size <= MAX_FILE:
                        found[Path(entry.path).relative_to(root).as_posix()] = (st.st_size, st.st_mtime_ns)
        return found

    async def scan(self) -> bool:
        """Look the folder over for what was changed outside the office and take it in. True when something was."""
        root = await self.ensure()
        self._failed.clear()  # each look tries every outside edit again
        found = await asyncio.to_thread(self._walk)
        rows = {str(r["path"]): dict(r) for r in await self.office.db.fetchall("SELECT * FROM docrepo_files")}
        took = False
        for path in sorted(found):
            row = rows.get(path)
            if row is not None and (row["size"], row["mtime_ns"]) == found[path]:
                continue
            data = (root / path).read_bytes()
            if row is not None and row["sha"] == sha_of(data):
                await self.office.db.execute("UPDATE docrepo_files SET size = ?, mtime_ns = ? WHERE path = ?", (*found[path], path))
                continue
            took |= await self._take_in(path, data, row)
        for path in sorted(set(rows) - set(found)):  # a file the office knows is gone
            row = rows[path]
            if row["kind"] == "library":
                await self._forget(path)
                self._indexes = True
            elif row["kind"] == "kb" and row["kb_id"] is not None:
                self._kb.add(int(row["kb_id"]))
            elif row["ticket_id"] is not None:
                self._tickets.add(int(row["ticket_id"]))
            else:
                self._indexes = True
            took = True
        return took

    async def _absorb(self, path: str) -> None:
        """Take in the file at `path` if it is not what the office last wrote there."""
        full = self._need() / path
        row = await self._row(path)
        if row is None or not full.exists():
            return
        data = full.read_bytes()
        if row["sha"] != sha_of(data):
            await self._take_in(path, data, row)

    async def _take_in(self, path: str, data: bytes, row: dict[str, Any] | None) -> bool:
        """One file that is not what the office last wrote (or that it never saw). True when the office changed for it."""
        text = data.decode("utf-8", "replace")
        kind = row["kind"] if row else None
        try:
            if kind == "doc":
                return await self._import_doc(path, text, data, row or {})
            if kind == "kb":
                return await self._import_kb(path, text, data, row or {})
            if kind == "ticket":
                self._tickets.add(int(row["ticket_id"]))  # type: ignore[index]
                return True
            if kind == "index":
                self._indexes = True
                return True
            if kind is None:
                folder, _, name = path.rpartition("/")
                owner = await self.office.db.fetchone("SELECT ticket_id FROM docrepo_files WHERE kind = 'ticket' AND path = ?", (f"{folder}/{layout.README}",))
                key = layout.doc_key_of(name)
                if owner is not None and key:
                    return await self._new_doc(path, text, data, int(owner["ticket_id"]), key)
                parts = path.split("/")
                if len(parts) == 3 and parts[0] == layout.KNOWLEDGE and parts[1] in layout.KB_KIND_OF and await self._new_kb(path, text, layout.KB_KIND_OF[parts[1]]):
                    return True
            await self._record(path, "library", data)
            await self._index(path, text)
            if path.startswith(f"{layout.KNOWLEDGE}/{layout.LIBRARY}/"):
                self._indexes = True
            return kind is None
        except OfficeError as exc:
            # Not recorded as seen and not written over: the edit stays in the file and is tried again at the next
            # look (a revision saved at the same moment, say). Until then the office leaves that file alone.
            log.warning("docs repo: %s not taken in: %s", path, exc)
            self.last_error = f"{path}: {exc}"
            self._failed.add(path)
            return False

    async def _author(self, path: str) -> str:
        """Who changed the file: the author of its last commit, unless the change is not committed yet."""
        code, out = await self._git("status", "--porcelain", "--", path, check=False)
        if code == 0 and not out.strip():
            code, name = await self._git("log", "-1", "--format=%an", "--", path, check=False)
            if code == 0 and name.strip() and name.strip() != BOT_NAME:
                return name.strip()[:60]
        return "kho tài liệu"

    async def _merge(self, ours: str, base: str, theirs: str) -> tuple[str, bool]:
        """(the merge of two texts that both come from `base`, whether it was clean). Where both changed the same
        lines, `theirs` is kept."""
        with tempfile.TemporaryDirectory(prefix="ao-merge-") as tmp:
            files = []
            for name, text in (("ours", ours), ("base", base), ("theirs", theirs)):
                file = Path(tmp) / name
                file.write_bytes((text + "\n").encode("utf-8"))
                files.append(str(file))
            code, out = await self._git("merge-file", "-p", *files, check=False)
            if code == 0:
                return layout.norm(out), True
            code, out = await self._git("merge-file", "-p", "--theirs", *files, check=False)
            return (layout.norm(out), False) if code == 0 else (theirs, False)

    async def _import_doc(self, path: str, text: str, data: bytes, row: dict[str, Any]) -> bool:
        office = self.office
        ticket = await office.tickets.get(int(row["ticket_id"]))
        key = str(row["doc_key"])
        doc = await office.tickets.get_document(ticket.id, key)
        if doc is None:
            return await self._new_doc(path, text, data, ticket.id, key)
        _, body = layout.split(text)
        new, ours = layout.norm(body), layout.norm(office.redact(doc.body))
        self._tickets.add(ticket.id)  # either way the file is written again (its head block names the revision)
        if new == ours:
            return False
        clean = True
        if row.get("revision") and int(row["revision"]) != doc.revision:
            base = layout.norm(office.redact(await office.docs._body(ticket.id, key, int(row["revision"]))))
            if new == base:
                return False  # the file is simply the older revision: nothing was edited
            new, clean = await self._merge(ours, base, new)
            if new == ours:
                return False
        author = await self._author(path)
        saved = await office.tickets.put_document(ticket.id, key, new, author=f"git:{author}", base_revision=doc.revision)
        await office.docs.on_saved(await office.tickets.get(ticket.id), key)
        note = f"✏️ `{key}` được sửa trong kho tài liệu bởi {author}: bản {saved.revision}."
        if doc.approved_revision:
            note += f" Bản đã duyệt vẫn là {doc.approved_revision}: bản mới phải được duyệt lại."
        if not clean:
            note += f" Bản {doc.revision} của văn phòng và bản trong kho sửa cùng một chỗ: đã lấy theo kho, bản {doc.revision} còn trong lịch sử."
        await office.tickets.add_comment(ticket.id, note, author_kind="system", author_id=None)
        return True

    async def _new_doc(self, path: str, text: str, data: bytes, ticket_id: int, key: str) -> bool:
        office = self.office
        _, body = layout.split(text)
        if not layout.norm(body):
            return False
        existing = await office.tickets.get_document(ticket_id, key)
        author = await self._author(path)
        saved = await office.tickets.put_document(ticket_id, key, layout.norm(body), author=f"git:{author}", base_revision=existing.revision if existing else None)
        await self._record(path, "doc", data, ticket_id=ticket_id, doc_key=key, revision=existing.revision if existing else saved.revision)
        await office.docs.on_saved(await office.tickets.get(ticket_id), key)
        await office.tickets.add_comment(ticket_id, f"📄 Tài liệu `{key}` được thêm từ kho tài liệu bởi {author} (bản {saved.revision}).", author_kind="system", author_id=None)
        self._tickets.add(ticket_id)
        return True

    async def _import_kb(self, path: str, text: str, data: bytes, row: dict[str, Any]) -> bool:
        office = self.office
        item_id = int(row["kb_id"])
        self._kb.add(item_id)
        self._indexes = True
        try:
            item = await office.knowledge.get(item_id)
        except NotFound:
            return False
        _, body = layout.split(text)
        title, content = layout.kb_parts(body)
        if (title or item.title) == item.title and content == item.content.strip():
            return False
        await office.knowledge.edit(item_id, title=title or item.title, content=content or item.content, by=f"git:{await self._author(path)}")
        return True

    async def _new_kb(self, path: str, text: str, kind: str) -> bool:
        """A file somebody put in a knowledge folder becomes an item (confirmed: a person wrote it), and gets its
        proper name. False when it cannot be one (too long, or a code note without the file it is about): it then
        stays a library file, searchable all the same."""
        from actoroffice.services.knowledge import MAX_CONTENT

        office = self.office
        meta, body = layout.split(text)
        title, content = layout.kb_parts(body)
        title = title or path.rsplit("/", 1)[-1].rsplit(".", 1)[0].replace("-", " ")
        anchor = meta.get("anchor") if isinstance(meta.get("anchor"), dict) else None
        if not content or len(content) > MAX_CONTENT or (kind == "code_note" and not anchor):
            return False
        author = await self._author(path)
        item = await office.knowledge.note(kind=kind, title=title, content=content, by=f"git:{author}", sources=[str(s) for s in meta.get("sources") or []], anchor=anchor)
        await office.knowledge.set_status(item.id, "verified", by=author)
        with contextlib.suppress(OSError):
            (self._need() / path).unlink()
        self._kb.add(item.id)
        self._indexes = True
        self._notes.append(f"kiến thức #{item.id} {title[:60]} (thêm từ kho bởi {author})")
        return True

    # ---- git: commit, pull, push -----------------------------------------------------------------------------

    async def dirty(self) -> bool:
        return bool((await self._git("status", "--porcelain"))[1].strip())

    async def commit(self, message: str | None = None) -> str | None:
        """Commit what is in the folder. Returns the commit, None when there was nothing to commit."""
        await self.ensure()
        if not await self.dirty():
            self._notes.clear()
            return None
        notes = list(dict.fromkeys(self._notes))
        self._notes.clear()
        subject = message or (notes[0] if len(notes) == 1 else f"Đồng bộ tài liệu: {len(notes)} thay đổi" if notes else "Đồng bộ thẻ ticket và danh mục")
        args = ["commit", "-q", "-m", subject[:200]]
        if len(notes) > 1:
            args += ["-m", "\n".join(f"- {n}" for n in notes[:80])]
        await self._git("add", "-A")
        await self._git(*args)
        return (await self._git("rev-parse", "HEAD"))[1]

    async def _remote(self) -> tuple[str, dict[str, str]] | None:
        """(what git fetches from and pushes to, the environment that carries the credential); None: no remote."""
        s = self.office.settings
        tls = core_http.git_tls_config()
        if s.docs_remote and s.docs_connection:
            adapter = await self.office.connections.adapter(s.docs_connection)
            ref = adapter.repo_ref(s.docs_remote)
            auth = await adapter.git_auth(ref, write=True) if ref is not None else None
            if auth is None:
                raise DocRepoError(f"Kết nối '{s.docs_connection}' không cho địa chỉ git của '{s.docs_remote}' (sai repo, hoặc kết nối chưa có token / chưa bật ghi).")
            return auth.url, auth_env(auth, tls)
        if s.docs_remote:
            return s.docs_remote, auth_env(None, tls)
        code, _ = await self._git("remote", "get-url", "origin", check=False)
        return ("origin", auth_env(None, tls)) if code == 0 else None

    async def _pull(self, remote: tuple[str, dict[str, str]]) -> bool:
        """Bring in what the remote has. True when the folder changed."""
        url, env = remote
        branch = self.office.settings.docs_branch
        code, out = await self._git("fetch", "-q", url, branch, env=env, check=False, timeout=300)
        if code != 0:
            if "couldn't find remote ref" in out.lower() or "could not find remote ref" in out.lower():
                return False  # the remote has no such branch yet: the first push creates it
            raise DocRepoError(f"git fetch: {self.office.redact(out)[:300]}")
        if (await self._git("rev-parse", "-q", "--verify", "HEAD", check=False))[0] != 0:
            await self._git("reset", "-q", "--hard", "FETCH_HEAD")  # nothing committed here yet: start from what the remote has
            return True
        if (await self._git("merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD", check=False))[0] == 0:
            return False
        code, out = await self._git("merge", "-q", "--no-edit", "-X", "theirs", "--allow-unrelated-histories", "FETCH_HEAD", check=False)
        if code != 0:
            await self._git("merge", "--abort", check=False)
            raise DocRepoError(f"Không gộp được thay đổi từ remote của kho tài liệu: {self.office.redact(out)[:300]}")
        return True

    async def _push(self, remote: tuple[str, dict[str, str]]) -> None:
        url, env = remote
        branch = self.office.settings.docs_branch
        if (await self._git("rev-parse", "-q", "--verify", "HEAD", check=False))[0] != 0:
            return
        code, out = await self._git("push", "-q", url, f"HEAD:refs/heads/{branch}", env=env, check=False, timeout=300)
        if code != 0:
            raise DocRepoError(f"git push: {self.office.redact(out)[:300]}")

    async def tick(self) -> bool:
        """The quick pass: take in outside edits and write what the office changed. No commit. True when files changed."""
        async with self._lock:
            took = await self.scan()
            return await self.flush() or took

    async def sync(self, *, full: bool = False, remote: bool = True, message: str | None = None) -> dict[str, Any]:
        """The whole pass: outside edits in, the office's changes out, commit, then the remote (fetch, merge, push).
        `full`: write every file again where it differs. A failure of the remote does not undo the local commit."""
        async with self._lock:
            await self.ensure()
            self.last_error = ""  # what this pass runs into, from here on
            if full:
                await self.reconcile()
            await self.scan()
            await self.flush()
            await self.scan()  # what flushing took in on the way (a file edited outside just before it was written)
            await self.flush()
            commits = [c for c in [await self.commit(message)] if c]
            try:
                target = await self._remote() if remote else None
                if target is not None:
                    if await self._pull(target):
                        await self.scan()
                        await self.flush()
                        commits += [c for c in [await self.commit()] if c]
                    await self._push(target)
            except (OfficeError, ConnectorError) as exc:
                self.last_error = "; ".join(e for e in (self.last_error, str(exc)) if e)
                log.warning("docs repo: %s", exc)
            self._urgent = False
            self.last_sync = utcnow()
            return {"commits": commits, "error": self.last_error}

    def is_remote(self, connection_id: str, slug: str) -> bool:
        """Is the repository a code host announced a push to the remote of the documents repository?"""
        s = self.office.settings
        if not self.enabled or not s.docs_remote or (s.docs_connection and s.docs_connection != connection_id):
            return False
        from actoroffice.services.webhooks import _same_repo

        return s.docs_remote.strip("/").lower() == slug.strip("/").lower() or _same_repo(s.docs_remote, slug)

    async def run_forever(self) -> None:
        """Files follow the office within moments; commits wait for quiet; the remote is asked now and then."""
        s = self.office.settings
        try:
            await self.sync(full=True)
        except Exception:
            log.exception("docs repo: first sync failed")
        last_change = last_scan = 0.0
        last_remote = time.monotonic()
        pending = False
        async with self.office.bus.subscribe() as queue:
            while True:
                wait = max(0.5, min(s.docs_scan_s or 5.0, 5.0))
                try:
                    event = await asyncio.wait_for(queue.get(), wait)
                    self.on_event(event.kind, event.data)
                    while not queue.empty():
                        event = queue.get_nowait()
                        self.on_event(event.kind, event.data)
                except TimeoutError:
                    pass
                now = time.monotonic()
                try:
                    if self._tickets or self._kb or self._indexes or (s.docs_scan_s and now - last_scan >= s.docs_scan_s):
                        last_scan = now
                        if await self.tick():
                            pending, last_change = True, now
                    due = s.docs_pull_s and now - last_remote >= s.docs_pull_s
                    if (pending and (self._urgent or now - last_change >= s.docs_commit_s)) or due:
                        await self.sync()
                        pending, last_remote = False, time.monotonic()
                except Exception:  # one bad pass must not stop the next
                    log.exception("docs repo: pass failed")

    # ---- reading: tree, search, grep, read -------------------------------------------------------------------

    def _safe(self, path: str) -> tuple[Path, str]:
        """(the file or folder inside the repository, its relative path). Refuses anything outside it or under .git."""
        root = self._need().resolve()
        full = (root / (path or ".").strip().lstrip("/\\")).resolve()
        if full != root and root not in full.parents:
            raise DocRepoError(f"'{path}' nằm ngoài kho tài liệu")
        rel = full.relative_to(root).as_posix() if full != root else ""
        if rel == ".git" or rel.startswith(".git/"):
            raise DocRepoError("Không đọc thư mục .git")
        return full, rel

    async def tree(self, under: str = "", depth: int = 2) -> list[str]:
        """The folders and files under a folder of the repository, `depth` levels deep, folders first."""
        full, rel = self._safe(under)
        if not full.is_dir():
            raise DocRepoError(f"'{under}' không phải thư mục của kho tài liệu")
        out: list[str] = []

        def walk(folder: Path, prefix: str, left: int) -> None:
            entries = sorted((e for e in os.scandir(folder) if e.name not in SKIP_DIRS), key=lambda e: (not e.is_dir(), e.name.lower()))
            for e in entries:
                out.append(f"{prefix}{e.name}{'/' if e.is_dir() else ''}")
                if e.is_dir() and left > 1:
                    walk(Path(e.path), prefix + e.name + "/", left - 1)

        await asyncio.to_thread(walk, full, f"{rel}/" if rel else "", max(1, min(depth, 6)))
        return out

    async def search(self, query: str, *, under: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Ranked full-text search over every file: the pieces (one per heading) that match most of the words, accents
        ignored. Each hit: path, the headings it sits under, the line it starts on and a snippet."""
        self._need()
        terms = " OR ".join(f'"{t}"' for t in re.findall(r"\w{2,}", query)[:16])
        if not terms:
            return []
        where, params = "docrepo_fts MATCH ?", [terms]
        if under.strip("/"):
            where += " AND path LIKE ? ESCAPE '\\'"
            params.append(_like(under.strip("/")) + "/%")
        rows = await self.office.db.fetchall(
            f"SELECT path, heading, line, snippet(docrepo_fts, 2, '«', '»', '…', 40) AS snip FROM docrepo_fts WHERE {where} "
            # what people wrote before the cards and catalogues the office generates from it (bm25: lower is better)
            f"ORDER BY bm25(docrepo_fts, 0, 4.0, 1.0) * CASE WHEN path = ? OR path LIKE ? THEN {GENERATED_WEIGHT} ELSE 1.0 END LIMIT ?",
            (*params, layout.AGENTS, f"%{layout.README}", max(1, min(limit, 50))),
        )
        return [{"path": r["path"], "heading": r["heading"], "line": int(r["line"] or 1), "snippet": " ".join(str(r["snip"]).split())} for r in rows]

    async def grep(self, pattern: str, *, under: str = "", limit: int = 60, ignore_case: bool = True) -> list[dict[str, Any]]:
        """Exact search: every line of the repository's text files that matches a regular expression (an id such as
        `AO-12/REQ-2`, a name, a phrase)."""
        full, _ = self._safe(under)
        try:
            regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
        except re.error as exc:
            raise DocRepoError(f"Biểu thức không hợp lệ: {exc}") from exc
        root = self._need().resolve()

        def run() -> list[dict[str, Any]]:
            hits: list[dict[str, Any]] = []
            files = [full] if full.is_file() else sorted(root / p for p in self._walk() if full == root or (root / p).is_relative_to(full))
            for file in files:
                try:
                    lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
                except OSError:
                    continue
                for number, line in enumerate(lines, 1):
                    if regex.search(line):
                        hits.append({"path": file.relative_to(root).as_posix(), "line": number, "text": line.strip()[:GREP_LINE]})
                        if len(hits) >= limit:
                            return hits
            return hits

        return await asyncio.to_thread(run)

    async def read(self, path: str, *, heading: str = "", lines: tuple[int, int] | None = None, limit: int = 40_000) -> dict[str, Any]:
        """A file of the repository: all of it, the section under a heading (the heading's words, any case), or a range
        of lines. Returns the text and the lines it covers."""
        full, rel = self._safe(path)
        if not full.is_file():
            raise DocRepoError(f"Không có file '{path}' trong kho tài liệu")
        all_lines = full.read_text(encoding="utf-8", errors="replace").splitlines()
        first, last = 1, len(all_lines)
        if heading.strip():
            want = " ".join(heading.casefold().split())
            start = level = None
            for number, line in enumerate(all_lines, 1):
                m = re.match(r"^(#{1,6})[ \t]+(.*?)[ \t#]*$", line)
                if not m:
                    continue
                if start is None and want in " ".join(m.group(2).casefold().split()):
                    start, level = number, len(m.group(1))
                elif start is not None and len(m.group(1)) <= (level or 6):
                    last = number - 1
                    break
            if start is None:
                titles = [line.lstrip("# ").strip() for line in all_lines if re.match(r"^#{1,6}[ \t]", line)][:40]
                raise DocRepoError(f"File '{rel}' không có mục nào chứa '{heading}'. Các mục: {'; '.join(titles)}")
            first = start
        elif lines:
            first, last = max(1, lines[0]), min(len(all_lines), max(lines))
        text = "\n".join(all_lines[first - 1 : last])
        return {"path": rel, "first": first, "last": last, "total": len(all_lines), "text": text[:limit], "truncated": len(text) > limit}

    async def status(self) -> dict[str, Any]:
        s = self.office.settings
        out: dict[str, Any] = {"enabled": self.enabled, "path": str(self.root) if self.root else None, "branch": s.docs_branch, "remote": s.docs_remote, "connection": s.docs_connection}
        if not self.enabled:
            return out
        await self.ensure()
        kinds = {str(r["kind"]): int(r["n"]) for r in await self.office.db.fetchall("SELECT kind, COUNT(*) AS n FROM docrepo_files GROUP BY kind")}
        code, head = await self._git("log", "-1", "--format=%h %cI %s", check=False)
        out.update({"files": kinds, "uncommitted": await self.dirty(), "head": head if code == 0 else "", "last_sync": self.last_sync, "error": self.last_error})
        return out


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
