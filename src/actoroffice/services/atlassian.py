"""Code hosts of a ticket, through connections: which connection serves a repository or a pull request URL, and the
adapter behind the method names and shapes the tools and the pull request watcher use (`ConnectedCodeHost`).

Everything outside the office (Jira, Confluence, Bitbucket, GitHub…) is reached through a connection
(services/connections.py + adapters/). Nothing here reads a credential.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Iterable
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import OfficeError
from actoroffice.ports.code import CloneAuth, RepoRef
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Scope

if TYPE_CHECKING:
    from actoroffice.app import Office


class AtlassianError(OfficeError):
    code = 502
    kind = ""  # the ConnectorError kind when the failure came from a connection ("forbidden", "not_found"…)


def parse_pr(url: str, adapters: Iterable[Any] = ()) -> tuple[str, int] | None:
    """("workspace/repo", number) of a pull request URL one of the given code-host adapters owns (by its
    `parse_pr_url`). None: nobody's."""
    for adapter in adapters:
        found = adapter.parse_pr_url(url or "")
        if found is not None:
            return found[0].slug, found[1]
    return None


class ConnectedCodeHost:
    """A code-host connection (`ports.code.CodeHost`) behind the methods and shapes the tools and the pull request
    watcher use. A failure is an AtlassianError carrying the connector's `kind`.
    What the port does not tell stays unknown: `approvals` is None, no comment is `resolved`, a reply has no id."""

    def __init__(self, spec: ConnectionSpec, adapter: Any, *, repo: RepoRef | None = None) -> None:
        self.spec = spec
        self.adapter = adapter
        self.repo = repo  # the project's repository on this connection, when the project is bound to it
        self.label = spec.use  # who says "merged" / "new review" in the ticket

    def _ref(self, repo: str) -> RepoRef:
        """The repository a call names: the project's own when nothing else is named, else what the adapter makes of
        the text (slug or link). An adapter that cannot tell (`repo_ref` missing) gets the text as the slug."""
        text = (repo or "").strip()
        if self.repo is not None and (not text or text.lower() == self.repo.slug.lower()):
            return self.repo
        resolve = getattr(self.adapter, "repo_ref", None)
        if resolve is None:
            return RepoRef(connection=self.spec.id, slug=text.rstrip("/").removesuffix(".git"))
        found = resolve(text)
        if found is None:
            error = AtlassianError(f"«{text[:120]}» không phải một repo của kết nối {self.spec.id}")
            error.kind = "invalid"
            raise error
        return found.model_copy(update={"connection": self.spec.id})

    async def git_auth(self, repo: str, *, write: bool) -> CloneAuth | None:
        """Where and with which credential git reaches the repository; None when the adapter cannot say."""
        ask = getattr(self.adapter, "git_auth", None)
        if ask is None:
            return None
        return await self._do(ask(self._ref(repo), write=write))

    def same_repo(self, address: str) -> bool | None:
        """Whether a git address (the `origin` of a folder) is the repository of the project. None: cannot tell."""
        resolve = getattr(self.adapter, "repo_ref", None)
        if resolve is None or self.repo is None:
            return None
        found = resolve(address)
        return found is not None and found.slug.lower() == self.repo.slug.lower()

    @staticmethod
    async def _do(call: Awaitable[Any]) -> Any:
        try:
            return await call
        except ConnectorError as exc:
            error = AtlassianError(str(exc))
            error.kind = exc.kind
            raise error from exc

    def parse_pr(self, url: str) -> tuple[str, int] | None:
        found = self.adapter.parse_pr_url(url or "")
        return (found[0].slug, found[1]) if found is not None else None

    async def pr_create(self, repo: str, source: str, destination: str, title: str, description: str) -> dict[str, Any]:
        pr = await self._do(self.adapter.pr_create(self._ref(repo), source, destination, title, description))
        return {"id": pr.id, "url": pr.url, "state": pr.state.upper()}

    async def pr_get(self, repo: str, pr_id: int) -> dict[str, Any]:
        pr = await self._do(self.adapter.pr_get(self._ref(repo), pr_id))
        return {"id": pr.id, "state": pr.state.upper(), "title": pr.title, "url": pr.url, "approvals": None}

    async def pr_comments(self, repo: str, pr_id: int) -> list[dict[str, Any]]:
        comments = await self._do(self.adapter.pr_comments(self._ref(repo), pr_id))
        return [
            {"id": c.id, "by": c.author, "text": c.body[:4000], "file": c.path, "line": c.line, "parent": c.parent_id, "resolved": False, "at": c.created[:16]}
            for c in comments
        ]

    async def pr_reply(self, repo: str, pr_id: int, text: str, *, parent_id: int | None = None) -> dict[str, Any]:
        await self._do(self.adapter.pr_reply(self._ref(repo), pr_id, text, parent_id=parent_id))
        return {}


async def code_hosts(office: Office, scope: Scope | None, *, write: bool = False) -> list[ConnectedCodeHost]:
    """The code hosts a ticket can use, best first. A project bound to a connection (`Project.code_connection`) has that
    host and no other. Otherwise: the connections of the scope (project, team, office; only those with `write` on when
    the call writes)."""
    project = None
    if scope is not None and scope.project_id:
        with contextlib.suppress(OfficeError):
            project = await office.projects.get(scope.project_id)
    if project is not None and project.code_connection:
        spec = await office.connections.get(project.code_connection)
        if spec is None or not spec.enabled or (write and not spec.write):
            return []
        repo = RepoRef(connection=spec.id, slug=project.repo_url, default_branch=project.main_branch) if project.repo_url else None
        try:
            return [ConnectedCodeHost(spec, await office.connections.adapter(spec.id), repo=repo)]
        except ConnectorError:
            return []  # no secret yet: as good as not connected
    hosts: list[ConnectedCodeHost] = []
    for spec in await office.connections.candidates(Capability.CODE_PR, scope):
        if write and not spec.write:
            continue
        try:
            hosts.append(ConnectedCodeHost(spec, await office.connections.adapter(spec.id)))
        except ConnectorError:
            continue  # no secret yet: as good as not connected
    return hosts


async def has_code_host(office: Office, scope: Scope | None) -> bool:
    """Is there a code host for this ticket (a connection, read-only or not)? With one, the
    work is reviewed and merged there as a pull request; without one, on this machine."""
    return bool(await code_hosts(office, scope))


async def pr_host(office: Office, scope: Scope | None, url: str, *, write: bool = False) -> tuple[Any, str, int] | None:
    """Who serves a pull request URL, as (client, "workspace/repo", number): the nearest connection whose adapter says
    the URL is one of its pull requests. None: nobody."""
    for host in await code_hosts(office, scope, write=write):
        parsed = host.parse_pr(url)
        if parsed is not None:
            return host, parsed[0], parsed[1]
    return None
