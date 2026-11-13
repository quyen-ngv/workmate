"""A connection: one account on one outside system, and what it can do."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, ClassVar, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class Capability(StrEnum):
    DOCS = "docs"  # read a document by reference, search
    TRACKER = "tracker"  # issues: read, search, comment
    WIKI = "wiki"  # pages: read, search, children, publish
    CODE_READ = "code_read"  # list repositories, read files, clone
    CODE_PR = "code_pr"  # pull requests: create, read, comment
    WEBHOOK = "webhook"  # receives pushed events


class Scope(BaseModel):
    """Who asks and for what. Adapters decide access with it (another team's ticket, a space that is not allowed)."""

    team_id: str | None = None
    project_id: str | None = None
    ticket_key: str | None = None
    user_id: str | None = None


class ConnectionSpec(BaseModel):
    id: str  # "jira-main", "gh-acme"
    use: str  # adapter name: "jira_cloud", "github"…
    scope: str = "office"  # "office" | "team:<id>" | "project:<id>"
    config: dict[str, Any] = Field(default_factory=dict)  # by the adapter's config_schema; never holds a secret
    secret_ref: str | None = None  # file name (no extension) in <data_dir>/secrets/
    enabled: bool = True
    write: bool = False  # read-only unless set: comments, pull requests and publishing need it


class Health(BaseModel):
    state: str = "unconfigured"  # unconfigured | ok | degraded | error
    checked_at: str = ""
    detail: str = ""  # for people: what is missing or wrong and how to fix it
    identity: str | None = None  # the account the token belongs to
    scopes: list[str] = Field(default_factory=list)
    rate: dict[str, Any] = Field(default_factory=dict)


ERROR_KINDS = ("auth", "forbidden", "not_found", "rate_limited", "network", "invalid", "unconfigured")


class ConnectorError(Exception):
    """Every failure of an outside system, sorted into a kind people and tools can act on."""

    def __init__(self, kind: str, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.kind = kind if kind in ERROR_KINDS else "invalid"
        self.retry_after = retry_after


@runtime_checkable
class Adapter(Protocol):
    """What every adapter class declares. The registry builds an instance from a ConnectionSpec and its secret:
    `cls(config: <config_schema>, secret: dict[str, str], *, transport=None)`."""

    name: ClassVar[str]
    title: ClassVar[str]
    capabilities: ClassVar[frozenset[Capability]]
    config_schema: ClassVar[type[BaseModel]]
    secret_fields: ClassVar[tuple[str, ...]]

    async def test(self) -> Health: ...

    async def close(self) -> None: ...
