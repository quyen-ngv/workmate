"""GitHub (github.com, or GitHub Enterprise Server by changing `base_url`) as a code host. REST API, token auth."""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import re
from typing import Any, ClassVar, Literal
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from actoroffice.adapters._http import TIMEOUT_S, Http, host_of, now_iso
from actoroffice.ports.code import ChangedFile, CloneAuth, PrComment, PullRequest, RepoRef, TreeEntry, WebhookEvent
from actoroffice.ports.connection import Capability, ConnectorError, Health

LABEL = "GitHub"
NAME = "github"
PUBLIC_API_HOST = "api.github.com"
PUBLIC_WEB_HOST = "github.com"
PER_PAGE = 100
MAX_PAGES = 50
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_PR_PATH_RE = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)(?:/|$)")

# ---- Checked on 2026-11-14 against docs.github.com/en/rest and the OpenAPI description GitHub publishes, the page
# "Permissions required for fine-grained personal access tokens", "Rate limits for the REST API", "Validating webhook
# deliveries" and "Webhook events and payloads" (spec V-1, V-4).
REQUIRED_SCOPES: dict[Capability, list[str]] = {  # repository permissions of a fine-grained token: Contents, Metadata, Pull requests
    Capability.CODE_READ: ["contents:read", "metadata:read"],
    Capability.CODE_PR: ["pull_requests:read", "pull_requests:write"],  # write only when creating or replying
    Capability.WEBHOOK: [],
}
# X-GitHub-Api-Version. 2022-11-28 is supported until 2028-03-10; 2026-03-10 is the newer one, and none of its breaking
# changes touches a field read here.
API_VERSION = "2022-11-28"
JSON_ACCEPT = "application/vnd.github+json"
RAW_ACCEPT = "application/vnd.github.raw+json"  # the contents API then answers with the file's bytes
GIT_USERNAME = "x-access-token"  # user name git sends with a token over HTTPS
SIGNATURE_HEADER = "x-hub-signature-256"  # "sha256=<hex HMAC-SHA256 of the body with the webhook secret>"
EVENT_HEADER = "x-github-event"
SCOPES_HEADER = "x-oauth-scopes"  # classic tokens only; fine-grained tokens do not report their permissions
PR_ACTIONS = {"opened": "pr_opened", "reopened": "pr_opened", "synchronize": "pr_updated", "edited": "pr_updated", "ready_for_review": "pr_updated"}
COMMENT_EVENTS = frozenset({"pull_request_review", "pull_request_review_comment"})
CONVERSATION_EVENT = "issue_comment"  # a comment in the conversation of a pull request arrives as a comment on an issue

FILE_STATUS = {"added": "added", "removed": "removed", "modified": "modified", "renamed": "renamed", "copied": "added", "changed": "modified"}


class GitHubConfig(BaseModel):
    base_url: str = Field(default=f"https://{PUBLIC_API_HOST}", description="Địa chỉ API. GitHub Enterprise Server: https://<host>/api/v3")
    org: str = Field(description="Tổ chức (hoặc tài khoản) chứa repo, ví dụ acme")
    repos: list[str] = Field(default_factory=lambda: ["*"], description='Repo được phép, theo mẫu: "payments", "ledger-*"')
    auth: Literal["token", "app"] = Field(default="token", description="token: token cá nhân; app: GitHub App (chưa hỗ trợ)")
    write: bool = Field(default=False, description="Cho phép tạo pull request và trả lời bình luận")

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        if not value.startswith("https://") or not host_of(value):
            raise ValueError("base_url phải là địa chỉ https, ví dụ https://api.github.com")
        return value

    @field_validator("org")
    @classmethod
    def _org(cls, value: str) -> str:
        value = value.strip().strip("/")
        if not _NAME_RE.fullmatch(value):
            raise ValueError("org chỉ gồm chữ, số, dấu chấm, gạch dưới, gạch nối")
        return value


def _safe_path(path: str) -> str:
    path = path.strip().lstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ConnectorError("invalid", f"{LABEL}: đường dẫn file không hợp lệ")
    return quote(path, safe="/")


class GitHub:
    name: ClassVar[str] = NAME
    title: ClassVar[str] = "GitHub"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CODE_READ, Capability.CODE_PR, Capability.WEBHOOK})
    config_schema: ClassVar[type[BaseModel]] = GitHubConfig
    secret_fields: ClassVar[tuple[str, ...]] = ("token", "webhook_secret")
    required_scopes: ClassVar[dict[Capability, list[str]]] = REQUIRED_SCOPES

    def __init__(self, config: GitHubConfig, secret: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._token = secret.get("token", "")
        self._webhook_secret = secret.get("webhook_secret", "")
        api_host = host_of(config.base_url)
        self.web_host = PUBLIC_WEB_HOST if api_host == PUBLIC_API_HOST else api_host
        headers = {"Accept": JSON_ACCEPT, "X-GitHub-Api-Version": API_VERSION}
        blocked: ConnectorError | None = None
        if config.auth == "app":
            blocked = ConnectorError("invalid", f"{LABEL}: đăng nhập bằng GitHub App chưa được xây dựng. Hãy đặt auth: token và dùng token cá nhân (fine-grained).")
        elif not self._token:
            blocked = ConnectorError("unconfigured", f"{LABEL}: chưa có token. Vào Cài đặt › Kết nối để nhập.")
        else:
            headers["Authorization"] = f"Bearer {self._token}"
        self.http = Http(httpx.AsyncClient(base_url=config.base_url, headers=headers, timeout=TIMEOUT_S, transport=transport), label=LABEL, blocked=blocked)

    async def close(self) -> None:
        await self.http.close()

    async def test(self) -> Health:
        blocked = self.http.blocked
        if blocked is not None:
            return Health(state="unconfigured" if blocked.kind == "unconfigured" else "error", checked_at=now_iso(), detail=str(blocked))
        try:
            response = await self.http.request("GET", "/user")
            me = response.json()
        except (ConnectorError, ValueError) as exc:
            return Health(state="error", checked_at=now_iso(), detail=str(exc) if isinstance(exc, ConnectorError) else f"{LABEL}: phản hồi không phải JSON (GET /user)")
        scopes = [s.strip() for s in response.headers.get(SCOPES_HEADER, "").split(",") if s.strip()]
        rate = {k: int(v) for k, v in (("remaining", response.headers.get("x-ratelimit-remaining", "")), ("limit", response.headers.get("x-ratelimit-limit", ""))) if v.isdigit()}
        detail = "Đã kết nối GitHub."
        if not scopes:
            detail += " Token loại fine-grained không cho biết quyền qua API, nên chưa đối chiếu được quyền thiếu."
        return Health(state="ok", checked_at=now_iso(), detail=detail, identity=str((me or {}).get("login") or "") or None, scopes=scopes, rate=rate)

    # ---- repositories

    def _allowed(self, name: str) -> bool:
        return any(fnmatch.fnmatchcase(name, pattern) for pattern in self.config.repos)

    def _name(self, repo: RepoRef) -> str:
        """Repository name of a reference, after checking it belongs to this organisation and to the allowed patterns."""
        org, _, name = repo.slug.strip().strip("/").rpartition("/")
        if org and org.lower() != self.config.org.lower():
            raise ConnectorError("forbidden", f"{LABEL}: repo «{repo.slug[:80]}» không thuộc tổ chức {self.config.org}")
        if not _NAME_RE.fullmatch(name) or name in (".", ".."):
            raise ConnectorError("invalid", f"{LABEL}: tên repo không hợp lệ")
        if not self._allowed(name):
            raise ConnectorError("forbidden", f"{LABEL}: repo «{name}» nằm ngoài các repo được phép của kết nối")
        return name

    def _path(self, repo: RepoRef, tail: str = "") -> str:
        return f"/repos/{self.config.org}/{self._name(repo)}{tail}"

    def _web(self, name: str) -> str:
        return f"https://{self.web_host}/{self.config.org}/{name}"

    async def _paged(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self.http.json("GET", path, params={**(params or {}), "per_page": PER_PAGE, "page": page}) or []
            out += data
            if len(data) < PER_PAGE:
                break
        return out

    async def repos(self, query: str | None = None) -> list[RepoRef]:
        needle = (query or "").strip().lower()
        out: list[RepoRef] = []
        try:
            found = await self._paged(f"/orgs/{self.config.org}/repos")
        except ConnectorError as exc:
            if exc.kind != "not_found":
                raise
            # `org` is a user account, and that listing is for organisations only. The repositories owned by the user of the
            # token, private ones included, come from /user/repos (/users/<name>/repos would list public ones only).
            owned = await self._paged("/user/repos", {"affiliation": "owner"})
            found = [r for r in owned if str((r.get("owner") or {}).get("login") or "").lower() == self.config.org.lower()]
        for item in found:
            name = str(item.get("name") or "")
            if self._allowed(name) and needle in name.lower():
                out.append(RepoRef(slug=f"{self.config.org}/{name}", default_branch=str(item.get("default_branch") or "main"), url=self._web(name)))
        return out

    async def head(self, repo: RepoRef, branch: str | None = None) -> str:
        data = await self.http.json("GET", self._path(repo, f"/branches/{quote(branch or repo.default_branch, safe='/')}")) or {}
        return str((data.get("commit") or {}).get("sha") or "")

    async def tree(self, repo: RepoRef, at: str) -> list[TreeEntry]:
        """Every file at a commit. GitHub cuts very large trees short (`truncated`); such a repository is better mirrored."""
        data = await self.http.json("GET", self._path(repo, f"/git/trees/{quote(at, safe='')}"), params={"recursive": "1"}) or {}
        return [TreeEntry(path=str(e.get("path") or ""), blob_sha=str(e.get("sha") or ""), size=int(e.get("size") or 0)) for e in data.get("tree") or [] if e.get("type") == "blob"]

    async def file(self, repo: RepoRef, path: str, at: str) -> bytes:
        response = await self.http.request("GET", self._path(repo, f"/contents/{_safe_path(path)}"), params={"ref": at}, headers={"Accept": RAW_ACCEPT})
        return response.content

    async def compare(self, repo: RepoRef, base: str, head: str) -> list[ChangedFile]:
        """Files changed from `base` to `head`. GitHub lists at most 300 files for a comparison, on its first page only."""
        data = await self.http.json("GET", self._path(repo, f"/compare/{quote(base, safe='')}...{quote(head, safe='')}"), params={"per_page": PER_PAGE}) or {}
        out: list[ChangedFile] = []
        for item in data.get("files") or []:
            status = FILE_STATUS.get(str(item.get("status") or ""), "modified")
            previous = item.get("previous_filename")
            out.append(ChangedFile(path=str(item.get("filename") or ""), status=status, old_path=str(previous) if status == "renamed" and previous else None))
        return out

    async def clone_auth(self, repo: RepoRef) -> CloneAuth | None:
        if self.http.blocked is not None:
            return None
        return CloneAuth(url=self._web(self._name(repo)) + ".git", username=GIT_USERNAME, password=self._token)

    # ---- pull requests

    def _require_write(self, action: str) -> None:
        if not self.config.write:
            raise ConnectorError("forbidden", f"{LABEL}: kết nối đang ở chế độ chỉ đọc, chưa được phép {action} (bật «write» cho kết nối)")

    @staticmethod
    def _pull(data: dict[str, Any]) -> PullRequest:
        merged = bool(data.get("merged") or data.get("merged_at"))
        state = "merged" if merged else ("open" if data.get("state") == "open" else "declined")
        return PullRequest(
            id=int(data.get("number") or 0),
            title=str(data.get("title") or ""),
            state=state,
            url=str(data.get("html_url") or ""),
            source=str((data.get("head") or {}).get("ref") or ""),
            destination=str((data.get("base") or {}).get("ref") or ""),
            author=str((data.get("user") or {}).get("login") or ""),
            merged=merged,
        )

    async def pr_create(self, repo: RepoRef, source: str, destination: str, title: str, body: str) -> PullRequest:
        self._require_write("tạo pull request")
        return self._pull(await self.http.json("POST", self._path(repo, "/pulls"), json={"title": title, "body": body, "head": source, "base": destination}) or {})

    async def pr_get(self, repo: RepoRef, pr_id: int) -> PullRequest:
        return self._pull(await self.http.json("GET", self._path(repo, f"/pulls/{int(pr_id)}")) or {})

    async def pr_comments(self, repo: RepoRef, pr_id: int) -> list[PrComment]:
        """Review comments (on lines) and conversation comments, oldest first."""
        out: list[PrComment] = []
        for c in await self._paged(self._path(repo, f"/pulls/{int(pr_id)}/comments")):
            out.append(
                PrComment(
                    id=int(c.get("id") or 0),
                    author=str((c.get("user") or {}).get("login") or ""),
                    body=str(c.get("body") or ""),
                    path=c.get("path"),
                    line=c.get("line") or c.get("original_line"),
                    parent_id=c.get("in_reply_to_id"),
                    created=str(c.get("created_at") or ""),
                )
            )
        for c in await self._paged(self._path(repo, f"/issues/{int(pr_id)}/comments")):
            out.append(PrComment(id=int(c.get("id") or 0), author=str((c.get("user") or {}).get("login") or ""), body=str(c.get("body") or ""), created=str(c.get("created_at") or "")))
        return sorted(out, key=lambda c: c.created)

    async def pr_reply(self, repo: RepoRef, pr_id: int, text: str, *, parent_id: int | None = None) -> None:
        """With `parent_id`: a reply in that review thread. Without: a comment in the pull request's conversation."""
        self._require_write("trả lời bình luận")
        tail = f"/issues/{int(pr_id)}/comments"
        if parent_id:
            # GitHub takes a reply on the first comment of a thread only ("replies to replies are not supported"), and a
            # conversation comment has no thread at all: answering one of those is a new conversation comment.
            try:
                parent = await self.http.json("GET", self._path(repo, f"/pulls/comments/{int(parent_id)}")) or {}
            except ConnectorError as exc:
                if exc.kind != "not_found":
                    raise
            else:
                root = int(parent.get("in_reply_to_id") or parent_id)
                tail = f"/pulls/{int(pr_id)}/comments/{root}/replies"
        await self.http.json("POST", self._path(repo, tail), json={"body": text})

    def parse_pr_url(self, url: str) -> tuple[RepoRef, int] | None:
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return None
        m = _PR_PATH_RE.match(parts.path)
        if parts.scheme != "https" or (parts.hostname or "").lower() != self.web_host or not m:
            return None
        org, name, number = m.groups()
        if org.lower() != self.config.org.lower() or not _NAME_RE.fullmatch(name) or not self._allowed(name):
            return None
        return RepoRef(slug=f"{self.config.org}/{name}", url=self._web(name)), int(number)

    # ---- events

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        """None when the signature is missing or wrong, or when the connection has no webhook secret to check it with.
        The webhook must be set to content type `application/json`: a form-encoded delivery is not JSON and gives None."""
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
        issue = payload.get("issue") or {}
        if event == CONVERSATION_EVENT and issue.get("pull_request"):  # `issue.pull_request` is there only when the issue is a pull request
            return WebhookEvent(kind="pr_comment", repo=repo, pr_id=int(issue["number"]) if issue.get("number") is not None else None, raw=payload)
        pull = payload.get("pull_request") or {}
        pr_id = int(pull["number"]) if pull.get("number") is not None else None
        head = pull.get("head") or {}
        if event == "push":
            ref = str(payload.get("ref") or "")
            if not ref.startswith("refs/heads/") or payload.get("deleted"):
                return WebhookEvent(kind="other", repo=repo, raw=payload)
            return WebhookEvent(kind="push", repo=repo, ref=ref[len("refs/heads/") :], commit=str(payload.get("after") or ""), raw=payload)
        if event == "pull_request":
            action = str(payload.get("action") or "")
            kind = ("pr_merged" if pull.get("merged") else "pr_declined") if action == "closed" else PR_ACTIONS.get(action, "other")
            return WebhookEvent(kind=kind, repo=repo, ref=str(head.get("ref") or ""), commit=str(head.get("sha") or ""), pr_id=pr_id, raw=payload)
        if event in COMMENT_EVENTS:
            return WebhookEvent(kind="pr_comment", repo=repo, ref=str(head.get("ref") or ""), commit=str(head.get("sha") or ""), pr_id=pr_id, raw=payload)
        return WebhookEvent(kind="other", repo=repo, raw=payload)
