"""Bitbucket Cloud as a code host: repositories, files, pull requests, webhooks. REST API 2.0, email + API token (Basic)."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import re
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from actoroffice.adapters._http import TIMEOUT_S, Http, now_iso
from actoroffice.ports.code import ChangedFile, CloneAuth, PrComment, PullRequest, RepoRef, TreeEntry, WebhookEvent
from actoroffice.ports.connection import Capability, ConnectorError, Health

LABEL = "Bitbucket"
NAME = "bitbucket_cloud"
WEB_HOST = "bitbucket.org"
MAX_PAGES = 50
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_PR_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/pull-requests/(\d+)(?:/|$)")

# ---- Checked on 2026-11-14 against the Bitbucket Cloud REST API 2.0 reference (developer.atlassian.com/cloud/bitbucket/rest),
# its OpenAPI document, and support.atlassian.com/bitbucket-cloud/docs: using-api-tokens, api-token-permissions,
# manage-webhooks, event-payloads (spec V-1, V-4).
API_BASE = "https://api.bitbucket.org/2.0"
REQUIRED_SCOPES: dict[Capability, list[str]] = {  # scopes of an API token; cloning over HTTPS needs read:repository too
    Capability.CODE_READ: ["read:repository:bitbucket"],
    # write only when creating a pull request; commenting on one needs read alone
    Capability.CODE_PR: ["read:pullrequest:bitbucket", "write:pullrequest:bitbucket"],
    Capability.WEBHOOK: [],
}
GIT_USERNAME = "x-bitbucket-api-token-auth"  # user name git sends with an Atlassian API token over HTTPS
# "sha256=<hex HMAC-SHA256 of the body, verbatim, with the webhook secret>"; sent only by a webhook that has a secret
SIGNATURE_HEADER = "x-hub-signature"
EVENT_HEADER = "x-event-key"
SCOPES_HEADER = "x-oauth-scopes"  # not in the reference for API tokens; when Bitbucket does not send it the scopes are "unknown"
TREE_MAX_DEPTH = 30  # `max_depth` of the source listing; the listing has no blob hash, so TreeEntry.blob_sha is ""
HEALTH_PAGELEN = 10  # the smallest page the reference allows
PR_EVENTS = {
    "pullrequest:created": "pr_opened",
    "pullrequest:updated": "pr_updated",
    "pullrequest:fulfilled": "pr_merged",
    "pullrequest:rejected": "pr_declined",
    "pullrequest:comment_created": "pr_comment",
}


def diff_spec(base: str, head: str) -> str:
    """Bitbucket's two-commit spec is "<source>..<destination>": the change that brings `base` to `head`."""
    return f"{quote(head, safe='')}..{quote(base, safe='')}"


PR_STATES = {"OPEN": "open", "MERGED": "merged", "DECLINED": "declined", "SUPERSEDED": "declined"}
FILE_STATUS = {"added": "added", "removed": "removed", "modified": "modified", "renamed": "renamed"}


class BitbucketCloudConfig(BaseModel):
    workspace: str = Field(description="Workspace, ví dụ acme")
    repos: list[str] = Field(default_factory=lambda: ["*"], description='Repo được phép, theo mẫu: "payments", "ledger-*"')
    write: bool = Field(default=False, description="Cho phép tạo pull request và trả lời bình luận")

    @field_validator("workspace")
    @classmethod
    def _workspace(cls, value: str) -> str:
        value = value.strip().strip("/")
        if not _NAME_RE.fullmatch(value):
            raise ValueError("workspace chỉ gồm chữ, số, dấu chấm, gạch dưới, gạch nối")
        return value


def _safe_path(path: str) -> str:
    path = path.strip().lstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ConnectorError("invalid", f"{LABEL}: đường dẫn file không hợp lệ")
    return quote(path, safe="/")


class BitbucketCloud:
    name: ClassVar[str] = NAME
    title: ClassVar[str] = "Bitbucket Cloud"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CODE_READ, Capability.CODE_PR, Capability.WEBHOOK})
    config_schema: ClassVar[type[BaseModel]] = BitbucketCloudConfig
    secret_fields: ClassVar[tuple[str, ...]] = ("email", "token", "webhook_secret")
    required_scopes: ClassVar[dict[Capability, list[str]]] = REQUIRED_SCOPES

    def __init__(self, config: BitbucketCloudConfig, secret: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._email = secret.get("email", "")
        self._token = secret.get("token", "")
        self._webhook_secret = secret.get("webhook_secret", "")
        ready = bool(self._email and self._token)
        client = httpx.AsyncClient(base_url=API_BASE, auth=(self._email, self._token) if ready else None, timeout=TIMEOUT_S, transport=transport, headers={"Accept": "application/json"})
        blocked = None if ready else ConnectorError("unconfigured", f"{LABEL}: chưa có email và API token. Vào Cài đặt › Kết nối để nhập.")
        self.http = Http(client, label=LABEL, blocked=blocked)

    async def close(self) -> None:
        await self.http.close()

    async def test(self) -> Health:
        if self.http.blocked is not None:
            return Health(state="unconfigured", checked_at=now_iso(), detail=str(self.http.blocked))
        try:
            response = await self.http.request("GET", f"/repositories/{self.config.workspace}", params={"pagelen": HEALTH_PAGELEN})
        except ConnectorError as exc:
            return Health(state="error", checked_at=now_iso(), detail=str(exc))
        scopes = [s.strip() for s in response.headers.get(SCOPES_HEADER, "").split(",") if s.strip()]
        detail = f"Đã đọc được workspace {self.config.workspace}."
        if not scopes:
            detail += " Bitbucket không cho biết quyền của token này, nên chưa đối chiếu được quyền thiếu."
        return Health(state="ok", checked_at=now_iso(), detail=detail, identity=self._email or None, scopes=scopes)

    # ---- repositories

    def _allowed(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.config.repos)

    def _name(self, repo: RepoRef) -> str:
        """Repository name of a reference, after checking it belongs to this workspace and to the allowed patterns."""
        workspace, _, name = repo.slug.strip().strip("/").rpartition("/")
        if workspace and workspace.lower() != self.config.workspace.lower():
            raise ConnectorError("forbidden", f"{LABEL}: repo «{repo.slug[:80]}» không thuộc workspace {self.config.workspace}")
        if not _NAME_RE.fullmatch(name) or name in (".", ".."):
            raise ConnectorError("invalid", f"{LABEL}: tên repo không hợp lệ")
        if not self._allowed(name):
            raise ConnectorError("forbidden", f"{LABEL}: repo «{name}» nằm ngoài các repo được phép của kết nối")
        return name

    def _path(self, repo: RepoRef, tail: str = "") -> str:
        return f"/repositories/{self.config.workspace}/{self._name(repo)}{tail}"

    def _ref(self, data: dict[str, Any]) -> RepoRef:
        name = str(data.get("slug") or data.get("name") or "")
        return RepoRef(
            slug=f"{self.config.workspace}/{name}",
            default_branch=str((data.get("mainbranch") or {}).get("name") or "main"),
            url=str(((data.get("links") or {}).get("html") or {}).get("href") or f"https://{WEB_HOST}/{self.config.workspace}/{name}"),
        )

    async def _paged(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """All values of a paged listing. `next` is a full URL: the HTTP helper refuses it unless it is this API's host."""
        out: list[dict[str, Any]] = []
        url: str | None = path
        for _ in range(MAX_PAGES):
            if not url:
                break
            data = await self.http.json("GET", url, params=params) or {}
            out += data.get("values") or []
            url, params = data.get("next"), None
        return out

    async def repos(self, query: str | None = None) -> list[RepoRef]:
        params: dict[str, Any] = {"pagelen": 100}
        if query and query.strip():
            params["q"] = 'name ~ "{}"'.format(query.replace("\\", " ").replace('"', " ").strip())
        found = await self._paged(f"/repositories/{self.config.workspace}", params)
        return [self._ref(r) for r in found if self._allowed(str(r.get("slug") or r.get("name") or ""))]

    async def head(self, repo: RepoRef, branch: str | None = None) -> str:
        data = await self.http.json("GET", self._path(repo, f"/refs/branches/{quote(branch or repo.default_branch, safe='/')}")) or {}
        return str((data.get("target") or {}).get("hash") or "")

    async def tree(self, repo: RepoRef, at: str) -> list[TreeEntry]:
        entries = await self._paged(self._path(repo, f"/src/{quote(at, safe='')}/"), {"max_depth": TREE_MAX_DEPTH, "pagelen": 100})
        return [TreeEntry(path=str(e.get("path") or ""), blob_sha="", size=int(e.get("size") or 0)) for e in entries if e.get("type") == "commit_file"]

    async def file(self, repo: RepoRef, path: str, at: str) -> bytes:
        """The raw bytes of a file. A file kept in LFS is answered with a redirect to another host, which is refused."""
        response = await self.http.request("GET", self._path(repo, f"/src/{quote(at, safe='')}/{_safe_path(path)}"))
        return response.content

    async def compare(self, repo: RepoRef, base: str, head: str) -> list[ChangedFile]:
        out: list[ChangedFile] = []
        for item in await self._paged(self._path(repo, f"/diffstat/{diff_spec(base, head)}"), {"pagelen": 100}):
            old = (item.get("old") or {}).get("path")
            new = (item.get("new") or {}).get("path")
            status = FILE_STATUS.get(str(item.get("status") or ""), "modified")
            out.append(ChangedFile(path=str(new or old or ""), status=status, old_path=str(old) if status == "renamed" and old else None))
        return out

    async def clone_auth(self, repo: RepoRef) -> CloneAuth | None:
        if self.http.blocked is not None:
            return None
        return CloneAuth(url=f"https://{WEB_HOST}/{self.config.workspace}/{self._name(repo)}.git", username=GIT_USERNAME, password=self._token)

    # ---- pull requests

    def _require_write(self, action: str) -> None:
        if not self.config.write:
            raise ConnectorError("forbidden", f"{LABEL}: kết nối đang ở chế độ chỉ đọc, chưa được phép {action} (bật «write» cho kết nối)")

    @staticmethod
    def _pull(data: dict[str, Any]) -> PullRequest:
        state = PR_STATES.get(str(data.get("state") or "").upper(), str(data.get("state") or "").lower())
        return PullRequest(
            id=int(data.get("id") or 0),
            title=str(data.get("title") or ""),
            state=state,
            url=str(((data.get("links") or {}).get("html") or {}).get("href") or ""),
            source=str(((data.get("source") or {}).get("branch") or {}).get("name") or ""),
            destination=str(((data.get("destination") or {}).get("branch") or {}).get("name") or ""),
            author=str((data.get("author") or {}).get("display_name") or ""),
            merged=state == "merged",
        )

    async def pr_create(self, repo: RepoRef, source: str, destination: str, title: str, body: str) -> PullRequest:
        self._require_write("tạo pull request")
        payload = {"title": title, "description": body, "source": {"branch": {"name": source}}, "destination": {"branch": {"name": destination}}, "close_source_branch": True}
        return self._pull(await self.http.json("POST", self._path(repo, "/pullrequests"), json=payload) or {})

    async def pr_get(self, repo: RepoRef, pr_id: int) -> PullRequest:
        return self._pull(await self.http.json("GET", self._path(repo, f"/pullrequests/{int(pr_id)}")) or {})

    async def pr_comments(self, repo: RepoRef, pr_id: int) -> list[PrComment]:
        out: list[PrComment] = []
        for c in await self._paged(self._path(repo, f"/pullrequests/{int(pr_id)}/comments"), {"pagelen": 100}):
            if c.get("deleted"):
                continue
            inline = c.get("inline") or {}
            out.append(
                PrComment(
                    id=int(c.get("id") or 0),
                    author=str((c.get("user") or {}).get("display_name") or ""),
                    body=str((c.get("content") or {}).get("raw") or ""),
                    path=inline.get("path"),
                    line=inline.get("to") or inline.get("from"),
                    parent_id=(c.get("parent") or {}).get("id"),
                    created=str(c.get("created_on") or ""),
                )
            )
        return out

    async def pr_reply(self, repo: RepoRef, pr_id: int, text: str, *, parent_id: int | None = None) -> None:
        self._require_write("trả lời bình luận")
        payload: dict[str, Any] = {"content": {"raw": text}}
        if parent_id:
            payload["parent"] = {"id": int(parent_id)}
        await self.http.json("POST", self._path(repo, f"/pullrequests/{int(pr_id)}/comments"), json=payload)

    def parse_pr_url(self, url: str) -> tuple[RepoRef, int] | None:
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return None
        m = _PR_PATH_RE.match(parts.path)
        if parts.scheme != "https" or (parts.hostname or "").lower() != WEB_HOST or not m:
            return None
        workspace, name, number = m.groups()
        if workspace.lower() != self.config.workspace.lower() or not _NAME_RE.fullmatch(name) or not self._allowed(name):
            return None
        return RepoRef(slug=f"{self.config.workspace}/{name}", url=f"https://{WEB_HOST}/{self.config.workspace}/{name}"), int(number)

    # ---- events

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        """None when the signature is missing or wrong, or when the connection has no webhook secret to check it with."""
        lowered = {k.lower(): v for k, v in headers.items()}
        if not self._webhook_secret:
            return None
        expected = "sha256=" + hmac.new(self._webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected.encode(), lowered.get(SIGNATURE_HEADER, "").strip().encode()):
            return None
        try:
            payload = json.loads(body)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        event = lowered.get(EVENT_HEADER, "")
        repo = str((payload.get("repository") or {}).get("full_name") or "")
        if event == "repo:push":
            # One push can move several references; `new` is null for a deleted one. The first branch is the one reported.
            after = [c.get("new") or {} for c in (payload.get("push") or {}).get("changes") or []]
            new = next((n for n in after if n and n.get("type") in (None, "branch")), None)
            if new is None:
                return WebhookEvent(kind="other", repo=repo, raw=payload)
            return WebhookEvent(kind="push", repo=repo, ref=str(new.get("name") or ""), commit=str((new.get("target") or {}).get("hash") or ""), raw=payload)
        if event in PR_EVENTS:
            pull = payload.get("pullrequest") or {}
            source = pull.get("source") or {}
            return WebhookEvent(
                kind=PR_EVENTS[event],
                repo=repo,
                ref=str((source.get("branch") or {}).get("name") or ""),
                commit=str((source.get("commit") or {}).get("hash") or ""),
                pr_id=int(pull["id"]) if pull.get("id") is not None else None,
                raw=payload,
            )
        return WebhookEvent(kind="other", repo=repo, raw=payload)
