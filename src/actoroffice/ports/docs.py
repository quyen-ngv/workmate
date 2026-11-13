"""Documents from outside systems: issues, wiki pages, files."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from .connection import Scope


class DocRef(BaseModel):
    connection: str = ""  # connection id ("" = not decided yet)
    ref: str  # "PAY-120" | a page id | a URL | a relative path
    locator: str = ""  # "§2.1", ":40-88"
    kind: str = ""  # adapter name that recognised it


class Attachment(BaseModel):
    name: str
    media_type: str = ""
    size: int = 0
    url: str = ""


class DocContent(BaseModel):
    title: str
    text: str  # markdown
    version: str  # Jira: the `updated` field; Confluence: the page version number; files: a content hash
    url: str = ""
    links: list[DocRef] = Field(default_factory=list)  # documents it points to
    attachments: list[Attachment] = Field(default_factory=list)
    truncated: bool = False
    notes: list[str] = Field(default_factory=list)  # parts that could not be read ("sơ đồ draw.io không có chữ")


class DocHit(BaseModel):
    ref: DocRef
    title: str
    snippet: str = ""
    url: str = ""


class DocSource(Protocol):
    """Doc Core's SourceProvider."""

    def recognize(self, text: str) -> list[DocRef]: ...  # no I/O

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent: ...

    async def search(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]: ...

    def in_scope(self, ref: DocRef, scope: Scope) -> bool: ...


class Issue(BaseModel):
    key: str
    title: str
    type: str = ""
    status: str = ""
    description: str = ""  # markdown
    updated: str = ""
    url: str = ""
    parent: str | None = None
    labels: list[str] = Field(default_factory=list)
    comments: list[dict[str, Any]] = Field(default_factory=list)  # {"author", "created", "body"}
    links: list[DocRef] = Field(default_factory=list)  # linked issues, remote links (wiki pages)
    attachments: list[Attachment] = Field(default_factory=list)
    fields: dict[str, Any] = Field(default_factory=dict)  # configured custom fields, e.g. {"acceptance": "…"}


class Tracker(Protocol):
    async def issue(self, key: str) -> Issue: ...

    async def search(self, jql: str, limit: int = 50) -> list[Issue]: ...

    async def children(self, key: str) -> list[Issue]: ...

    async def comment(self, key: str, text: str) -> None: ...  # needs write


class PageRef(BaseModel):
    id: str
    title: str
    space: str = ""
    url: str = ""


class Page(BaseModel):
    id: str
    title: str
    space: str = ""
    version: int = 0
    url: str = ""
    body: str = ""  # markdown
    links: list[DocRef] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class Wiki(Protocol):
    async def page(self, page_id: str) -> Page: ...

    async def resolve(self, url: str) -> str: ...  # a page URL (also a /x/… tiny link) → page id

    async def children(self, page_id: str) -> list[PageRef]: ...

    async def search(self, text: str, *, space: str | None = None, limit: int = 10) -> list[PageRef]: ...

    async def upsert_page(self, space: str, title: str, body_md: str, *, parent_id: str | None = None) -> Page: ...  # needs write
