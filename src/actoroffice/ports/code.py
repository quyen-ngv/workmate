"""Code hosts (Bitbucket, GitHub…): repositories, files, pull requests, pushed events."""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class RepoRef(BaseModel):
    connection: str = ""
    slug: str  # "acme/payments"
    default_branch: str = "main"
    url: str = ""


class TreeEntry(BaseModel):
    path: str
    blob_sha: str
    size: int = 0


class ChangedFile(BaseModel):
    path: str
    status: str = "modified"  # added | modified | removed | renamed
    old_path: str | None = None


class CloneAuth(BaseModel):
    url: str  # https clone URL without credentials
    username: str = ""
    password: str = ""  # token; handed to git through the child's environment only, never written to .git/config


class PullRequest(BaseModel):
    id: int
    title: str = ""
    state: str = ""  # open | merged | declined
    url: str = ""
    source: str = ""
    destination: str = ""
    author: str = ""
    merged: bool = False


class PrComment(BaseModel):
    id: int
    author: str = ""
    body: str = ""
    path: str | None = None
    line: int | None = None
    parent_id: int | None = None
    created: str = ""


class WebhookEvent(BaseModel):
    kind: str  # push | pr_opened | pr_updated | pr_merged | pr_declined | pr_comment | other
    repo: str = ""  # slug
    ref: str = ""  # branch
    commit: str = ""
    pr_id: int | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class CodeHost(Protocol):
    # ---- read
    async def repos(self, query: str | None = None) -> list[RepoRef]: ...

    async def head(self, repo: RepoRef, branch: str | None = None) -> str: ...

    async def tree(self, repo: RepoRef, at: str) -> list[TreeEntry]: ...

    async def file(self, repo: RepoRef, path: str, at: str) -> bytes: ...

    async def compare(self, repo: RepoRef, base: str, head: str) -> list[ChangedFile]: ...

    async def clone_auth(self, repo: RepoRef) -> CloneAuth | None: ...  # None: cloning is not allowed

    # ---- pull requests (create and reply need write)
    async def pr_create(self, repo: RepoRef, source: str, destination: str, title: str, body: str) -> PullRequest: ...

    async def pr_get(self, repo: RepoRef, pr_id: int) -> PullRequest: ...

    async def pr_comments(self, repo: RepoRef, pr_id: int) -> list[PrComment]: ...

    async def pr_reply(self, repo: RepoRef, pr_id: int, text: str, *, parent_id: int | None = None) -> None: ...

    def parse_pr_url(self, url: str) -> tuple[RepoRef, int] | None: ...

    # ---- events
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None: ...  # None: bad signature
