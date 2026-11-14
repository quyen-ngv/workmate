"""Code intelligence for the office: which repositories a ticket may see, keeping their index fresh, and reading code
at a commit.

The index lives in <data_dir>/index.db, apart from office.db: it is large and can be rebuilt at any time. Indexing
runs in a worker thread, one repository at a time, so it never blocks a ticket run.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.codeintel import services as service_map
from actoroffice.codeintel.index import IndexOptions, index_repo, state_of
from actoroffice.codeintel.query import CodeQuery
from actoroffice.codeintel.store import IndexStore
from actoroffice.codeintel.sync import GitSource, Source, SyncError, is_git, local_source, mirror
from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.work import Project, Ticket
from actoroffice.ports.code import RepoRef
from actoroffice.ports.connection import Capability, ConnectorError, Scope
from actoroffice.ports.intel import IndexState, Subgraph, cite

if TYPE_CHECKING:
    from actoroffice.app import Office

READ_LIMIT_LINES = 400
READ_LIMIT_BYTES = 256 * 1024


class CodeIntelError(OfficeError):
    code = 400


@dataclass(frozen=True)
class Repo:
    slug: str
    project_id: str | None
    team_id: str | None
    path: Path | None  # a local folder
    branch: str
    remote: RepoRef | None = None  # a repository on a code host (mirrored into <data_dir>/repos/)


def slug_of(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._/-]+", "-", name.strip()).strip("-").lower() or "repo"


class CodeIntelService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self._store: IndexStore | None = None
        self._lock = asyncio.Lock()  # one indexing pass at a time
        self.options = IndexOptions()

    @property
    def store(self) -> IndexStore:
        if self._store is None:
            self._store = IndexStore(self.office.settings.data_dir / "index.db")
        return self._store

    @property
    def query(self) -> CodeQuery:
        return CodeQuery(self.store)

    def close(self) -> None:
        if self._store is not None:
            self._store.close()
            self._store = None

    # ---- which repositories ----------------------------------------------------------------------------------

    def _repo_of(self, project: Project) -> Repo | None:
        if project.kind not in ("code", "reference") or not project.path:
            return None
        path = Path(project.path)
        if not path.is_dir():
            return None
        return Repo(slug_of(project.name), project.id, project.team_id, path, project.main_branch)

    async def repos(self, *, team_id: str | None = None, project_id: str | None = None) -> list[Repo]:
        """Repositories in scope: one project, or every code project of a team (projects without a team belong to
        everyone). A team never sees another team's code."""
        found: list[Repo] = []
        for project in await self.office.projects.list():
            if project_id is not None and project.id != project_id:
                continue
            if project_id is None and project.team_id is not None and project.team_id != team_id:
                continue
            repo = self._repo_of(project)
            if repo is not None:
                found.append(repo)
        return found

    async def for_ticket(self, ticket: Ticket | None) -> list[Repo]:
        """What a ticket may read: its project first, then the other repositories of its team."""
        if ticket is None:
            return await self.repos()
        team = await self.repos(team_id=ticket.team_id)
        own = [r for r in team if r.project_id == ticket.project_id]
        if not own and ticket.project_id:
            own = await self.repos(project_id=ticket.project_id)
        return own + [r for r in team if r not in own]

    async def remote_repo(self, slug: str, scope: Scope) -> Repo | None:
        """A repository on a connected code host, by `connection/owner/name` or `owner/name`."""
        found = await self.office.connections.resolve(Capability.CODE_READ, scope)
        if found is None:
            return None
        spec, _ = found
        return Repo(slug_of(f"{spec.id}/{slug}"), scope.project_id, scope.team_id, None, "main", RepoRef(connection=spec.id, slug=slug))

    # ---- sources ---------------------------------------------------------------------------------------------

    async def source(self, repo: Repo) -> Source:
        if repo.path is not None:
            return await asyncio.to_thread(local_source, repo.path, repo.branch)
        assert repo.remote is not None
        host = await self.office.connections.adapter(repo.remote.connection)
        auth = await host.clone_auth(repo.remote)
        if auth is None:
            raise CodeIntelError(f"kết nối '{repo.remote.connection}' không cho clone {repo.remote.slug}")
        self.office.redact.register(auth.password)
        dest = self.office.settings.data_dir / "repos" / repo.remote.connection / f"{repo.remote.slug}.git"
        return await asyncio.to_thread(mirror, auth.url, dest, repo.branch, auth)

    # ---- indexing --------------------------------------------------------------------------------------------

    async def index(self, repo: Repo, *, options: IndexOptions | None = None) -> IndexState:
        async with self._lock:
            try:
                source = await self.source(repo)
            except (SyncError, ConnectorError, OfficeError) as exc:
                self.store.ensure_repo(repo.slug, state="error", error=self.office.redact(str(exc))[:300])
                return state_of(self.store, repo.slug)
            self.office.bus.publish("index.started", repo=repo.slug)
            state = await asyncio.to_thread(
                index_repo, self.store, repo.slug, source, options or self.options, project_id=repo.project_id, team_id=repo.team_id,
                root=str(repo.path or ""), default_branch=repo.branch, connection_id=repo.remote.connection if repo.remote else None,
            )  # fmt: skip
            self.office.bus.publish("index.finished" if state.state != "error" else "index.failed", repo=repo.slug, state=state.model_dump())
            return state

    async def ensure(self, repos: list[Repo]) -> list[IndexState]:
        """Index what is not indexed yet or is behind its source. A pass that finds nothing changed costs one
        `git rev-parse` and `git ls-tree`."""
        states = []
        for repo in repos:
            row = self.store.repo(repo.slug)
            head = None
            if row is not None and row["state"] == "ready" and repo.path is not None:
                try:
                    head = await asyncio.to_thread(lambda r=repo: local_source(r.path, r.branch).commit())  # type: ignore[arg-type]
                except SyncError:
                    head = None
            if row is not None and row["state"] == "ready" and head is not None and head == row["indexed_commit"]:
                states.append(state_of(self.store, repo.slug))
            else:
                states.append(await self.index(repo))
        return states

    async def status(self, repos: list[Repo]) -> list[IndexState]:
        return [state_of(self.store, r.slug) for r in repos]

    def drop(self, slug: str) -> bool:
        return self.store.drop_repo(slug)

    # ---- reading ---------------------------------------------------------------------------------------------

    async def read(self, repo: Repo, path: str, *, lines: tuple[int, int] | None = None, at: str | None = None) -> dict[str, Any]:
        """A piece of a file at a commit, straight from the source (never from the index), with the hash of what was
        returned: the evidence a document pins."""
        clean = path.replace("\\", "/").strip().lstrip("/")
        if ".." in clean.split("/"):
            raise CodeIntelError("đường dẫn không hợp lệ")
        source = await self.source(repo)
        commit = at or await asyncio.to_thread(source.commit)
        try:
            data = await asyncio.to_thread(source.read, clean, at)
        except SyncError as exc:
            raise NotFound(f"không có {clean} trong {repo.slug} ở {commit[:10]}") from exc
        if b"\0" in data[:8192]:
            raise CodeIntelError(f"{clean} là file nhị phân")
        all_lines = data[:READ_LIMIT_BYTES * 8].decode("utf-8", "replace").split("\n")
        total = len(all_lines)
        start, end = lines or (1, total)
        start = max(1, start)
        end = min(total, max(start, end))
        if start > total:
            raise NotFound(f"{clean} chỉ có {total} dòng (hỏi dòng {start})")
        truncated = end - start + 1 > READ_LIMIT_LINES
        end = min(end, start + READ_LIMIT_LINES - 1)
        text = "\n".join(all_lines[start - 1 : end])
        return {
            "repo": repo.slug, "path": clean, "start": start, "end": end, "total_lines": total, "text": text, "commit": commit,
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(), "cite": cite(repo.slug, clean, start, end), "truncated": truncated,
        }  # fmt: skip

    async def exists(self, repo: Repo, path: str, *, at: str | None = None) -> bool:
        source = await self.source(repo)
        return await asyncio.to_thread(source.exists, path.replace("\\", "/").strip().lstrip("/"), at)

    # ---- service map -----------------------------------------------------------------------------------------

    async def map(
        self, repos: list[Repo], *, focus: str | None = None, depth: int = 1, tables: str = "shared", service: str | None = None, module: str | None = None
    ) -> Subgraph:
        """The system map; with `service` the inside of one service; with `service` and `module` one module."""
        declared = {}
        for repo in repos:
            try:
                source = await self.source(repo)
                text = (await asyncio.to_thread(source.read, service_map.DECLARED_FILE)).decode("utf-8", "replace")
            except (SyncError, OfficeError, ConnectorError):
                text = ""
            declared[repo.slug] = service_map.load_declared(text)
        try:
            graph = await asyncio.to_thread(service_map.build_map, self.query, [r.slug for r in repos], declared, tables=tables, service=service, module=module)
        except KeyError as exc:
            raise NotFound(str(exc.args[0])) from exc
        return service_map.focus(graph, focus, depth) if focus else graph

    def _git_dir(self, repo: Repo) -> Path | None:
        """Where the history of a repository is: its folder, or the mirror of a repository on a code host."""
        if repo.path is not None:
            return repo.path if is_git(repo.path) else None
        if repo.remote is not None:
            dest = self.office.settings.data_dir / "repos" / repo.remote.connection / f"{repo.remote.slug}.git"
            return dest if (dest / "HEAD").is_file() else None
        return None

    def _map_at(self, repos: list[Repo], ref: str, tables: str) -> tuple[Subgraph, list[str]]:
        """The system map as it is at a branch or commit: a copy of the index is brought to that ref (only the files
        that differ are read again) and thrown away afterwards. Repositories that do not have the ref stay as indexed."""
        tmp = self.office.settings.data_dir / "tmp" / f"map-{uuid.uuid4().hex[:12]}.db"
        tmp.parent.mkdir(parents=True, exist_ok=True)
        copy = sqlite3.connect(str(tmp))
        try:
            self.store.conn.backup(copy)
        finally:
            copy.close()
        store = IndexStore(tmp)
        try:
            found, declared = [], {}
            for repo in repos:
                folder = self._git_dir(repo)
                source: Source | None = None
                if folder is not None:
                    at = GitSource(folder, ref)
                    try:
                        at.commit()
                    except SyncError:
                        at = None  # type: ignore[assignment]
                    if at is not None:
                        found.append(repo.slug)
                        source = at
                        index_repo(store, repo.slug, at, self.options, project_id=repo.project_id, team_id=repo.team_id, root=str(repo.path or ""), default_branch=repo.branch)
                try:
                    if source is None:
                        source = local_source(repo.path, repo.branch) if repo.path is not None else (GitSource(folder, repo.branch) if folder else None)
                    text = source.read(service_map.DECLARED_FILE).decode("utf-8", "replace") if source is not None else ""
                except SyncError:
                    text = ""
                declared[repo.slug] = service_map.load_declared(text)
            return service_map.build_map(CodeQuery(store), [r.slug for r in repos], declared, tables=tables), found
        finally:
            store.close()
            for suffix in ("", "-wal", "-shm"):
                with contextlib.suppress(OSError):
                    Path(str(tmp) + suffix).unlink()

    async def map_diff(self, repos: list[Repo], *, head: str, base: str | None = None, tables: str = "shared") -> dict[str, Any]:
        """What a branch or commit (`head`) changes on the system map against `base` (the indexed main branch when
        not given): services, topics, tables and edges that appear or go away."""
        await self.ensure(repos)
        async with self._lock:
            if base:
                old, _ = await asyncio.to_thread(self._map_at, repos, base, tables)
            else:
                old = await self.map(repos, tables=tables)
            new, found = await asyncio.to_thread(self._map_at, repos, head, tables)
        if not found:
            raise NotFound(f"không repo nào trong phạm vi có nhánh hoặc commit '{head}'")
        return {**service_map.diff_maps(old, new), "head_ref": head, "base_ref": base or "", "repos": found}
