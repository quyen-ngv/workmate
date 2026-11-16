"""Adapters that reach nothing, for tests of what the office does through a connection: a code host with pull requests
(`fake_code`), an issue tracker (`fake_tracker`) and a wiki (`fake_wiki`). State is on the class, reset by `install()`.

    install()                      # in a fixture, before use
    await connect(office, "fake_code", write=True)
    ...
    uninstall()                    # at teardown
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from pydantic import BaseModel

from actoroffice.adapters import registry
from actoroffice.app import Office
from actoroffice.ports.code import PrComment, PullRequest, RepoRef
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Health
from actoroffice.ports.docs import Issue, Page, PageRef


class Config(BaseModel):
    write: bool = False


class _Base:
    config_schema: ClassVar[type[BaseModel]] = Config
    secret_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: Config, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config = config

    def _writes(self) -> None:
        if not self.config.write:
            raise ConnectorError("forbidden", "kết nối chỉ đọc")

    async def test(self) -> Health:
        return Health(state="ok")

    async def close(self) -> None:
        return None


class FakeCodeHost(_Base):
    """Pull requests at https://git.test/<owner>/<repo>/pull/<n>."""

    name: ClassVar[str] = "fake_code"
    title: ClassVar[str] = "Code host giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CODE_PR})
    created: ClassVar[list[dict[str, Any]]] = []
    state: ClassVar[str] = "open"
    comments: ClassVar[list[PrComment]] = []
    replies: ClassVar[list[tuple[int | None, str]]] = []

    def parse_pr_url(self, url: str) -> tuple[RepoRef, int] | None:
        m = re.match(r"^https://git\.test/([\w-]+/[\w-]+)/pull/(\d+)", url)
        return (RepoRef(slug=m.group(1)), int(m.group(2))) if m else None

    async def pr_create(self, repo: RepoRef, source: str, destination: str, title: str, body: str) -> PullRequest:
        self._writes()
        FakeCodeHost.created.append({"repo": repo.slug, "source": source, "destination": destination, "title": title, "description": body})
        return PullRequest(id=7, title=title, state="open", url=f"https://git.test/{repo.slug}/pull/7", source=source, destination=destination)

    async def pr_get(self, repo: RepoRef, pr_id: int) -> PullRequest:
        return PullRequest(id=pr_id, title="x", state=FakeCodeHost.state, merged=FakeCodeHost.state == "merged")

    async def pr_comments(self, repo: RepoRef, pr_id: int) -> list[PrComment]:
        return list(FakeCodeHost.comments)

    async def pr_reply(self, repo: RepoRef, pr_id: int, text: str, *, parent_id: int | None = None) -> None:
        self._writes()
        FakeCodeHost.replies.append((parent_id, text))


class FakeTracker(_Base):
    """Issues at https://jira.test/browse/<KEY>; `search` returns every issue and records the JQL."""

    name: ClassVar[str] = "fake_tracker"
    title: ClassVar[str] = "Tracker giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.TRACKER})
    issues: ClassVar[dict[str, Issue]] = {}
    searches: ClassVar[list[str]] = []
    comments: ClassVar[list[tuple[str, str]]] = []

    @classmethod
    def add(cls, key: str, title: str, description: str, labels: list[str], *, reporter_email: str = "lan@acme.vn", reporter_name: str = "Lan") -> None:
        cls.issues[key] = Issue(
            key=key, title=title, description=description, labels=labels, url=f"https://jira.test/browse/{key}", reporter_email=reporter_email, reporter_name=reporter_name
        )

    async def issue(self, key: str) -> Issue:
        if key not in FakeTracker.issues:
            raise ConnectorError("not_found", f"không có issue {key}")
        return FakeTracker.issues[key]

    async def search(self, jql: str, limit: int = 50) -> list[Issue]:
        FakeTracker.searches.append(jql)
        return list(FakeTracker.issues.values())[:limit]

    async def comment(self, key: str, text: str) -> None:
        self._writes()
        FakeTracker.comments.append((key, text))


class FakeWiki(_Base):
    """Pages at https://wiki.test/<space>/<n>; `upsert_page` records what was published (markdown)."""

    name: ClassVar[str] = "fake_wiki"
    title: ClassVar[str] = "Wiki giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.WIKI})
    published: ClassVar[list[dict[str, Any]]] = []

    async def search(self, text: str, *, space: str | None = None, limit: int = 10) -> list[PageRef]:
        return []

    async def upsert_page(self, space: str, title: str, body_md: str, *, parent_id: str | None = None) -> Page:
        self._writes()
        FakeWiki.published.append({"space": space, "title": title, "body": body_md, "parent_id": parent_id})
        number = len(FakeWiki.published)
        return Page(id=str(number), title=title, space=space, url=f"https://wiki.test/{space}/{number}", body=body_md)


FAKES: dict[str, type] = {cls.name: cls for cls in (FakeCodeHost, FakeTracker, FakeWiki)}


def install() -> None:
    for name, cls in FAKES.items():
        registry.register(name, cls)
    FakeCodeHost.created, FakeCodeHost.state, FakeCodeHost.comments, FakeCodeHost.replies = [], "open", [], []
    FakeTracker.issues, FakeTracker.searches, FakeTracker.comments = {}, [], []
    FakeWiki.published = []


def uninstall() -> None:
    for name in FAKES:
        registry.unregister(name)


async def connect(office: Office, use: str, *, scope: str = "office", write: bool = False, connection_id: str | None = None) -> ConnectionSpec:
    return await office.connections.save(ConnectionSpec(id=connection_id or use.replace("_", "-"), use=use, scope=scope, write=write))
