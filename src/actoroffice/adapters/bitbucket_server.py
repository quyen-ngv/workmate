"""Bitbucket Server / Data Center (self-hosted) as a code host: repositories, files, pull requests, webhooks.

REST API 1.0 under `<base_url>/rest/api/1.0`, an HTTP access token sent as `Authorization: Bearer`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import hmac
import json
import re
from datetime import UTC, datetime
from typing import Any, ClassVar
from urllib.parse import quote, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from actoroffice.adapters._http import TIMEOUT_S, Http, now_iso, tls
from actoroffice.ports.code import ChangedFile, CloneAuth, PrComment, PullRequest, RepoRef, TreeEntry, WebhookEvent
from actoroffice.ports.connection import Capability, ConnectorError, Health

LABEL = "Bitbucket Server"
NAME = "bitbucket_server"

# ---- Checked on 2026-11-16 against Atlassian's documentation of Bitbucket Data Center 10.5:
#   REST   developer.atlassian.com/server/bitbucket/rest/v1005/ and its OpenAPI document
#          (dac-static.atlassian.com/server/bitbucket/10.5.swagger.v3.json); older servers:
#          docs.atlassian.com/bitbucket-server/rest/7.4.0 … 7.21.0/bitbucket-rest.html
#   tokens confluence.atlassian.com/bitbucketserver/http-access-tokens-939515499.html
#   hooks  confluence.atlassian.com/bitbucketserver/manage-webhooks-938025878.html and event-payload-938025882.html
# What those pages do not say is marked "not documented" below.
API = "/rest/api/1.0"
PAGE = 1000  # asked for; the server applies its own hard limit and says where the next page starts (`nextPageStart`)
MAX_PAGES = 500
REQUIRED_SCOPES: dict[Capability, list[str]] = {  # permissions of an HTTP access token
    Capability.CODE_READ: ["Project read", "Repository read"],
    Capability.CODE_PR: ["Project read", "Repository write"],  # "perform pull request actions" needs repository write
    Capability.WEBHOOK: [],
}
SIGNATURE_HEADER = "x-hub-signature"  # "sha256=<hex HMAC-SHA256 of the body with the webhook's secret>"
EVENT_HEADER = "x-event-key"
# The account a request ran as. Not documented for Bitbucket; when the server does not send it the identity is the
# configured user name, or unknown.
USER_HEADER = "x-ausername"
PR_EVENTS = {
    "pr:opened": "pr_opened",
    "pr:modified": "pr_updated",
    "pr:from_ref_updated": "pr_updated",
    "pr:merged": "pr_merged",
    "pr:declined": "pr_declined",
    "pr:deleted": "pr_declined",
    "pr:comment:added": "pr_comment",
}
PR_STATES = {"OPEN": "open", "MERGED": "merged", "DECLINED": "declined"}
# `type` of a change. The reference lists ADD, COPY, DELETE, MODIFY, MOVE, UNKNOWN; RENAME is accepted as well.
CHANGE_STATUS = {"ADD": "added", "COPY": "added", "MODIFY": "modified", "DELETE": "removed", "MOVE": "renamed", "RENAME": "renamed"}
NO_COMMIT = "0" * 40

_KEY_RE = re.compile(r"~?[A-Za-z0-9_][A-Za-z0-9._@-]*")  # a project key, or "~user" for a personal project
_NAME_RE = re.compile(r"[A-Za-z0-9._-]+")
_PATTERN_RE = re.compile(r"[A-Za-z0-9._~@*?\[\]-]+(/[A-Za-z0-9._*?\[\]-]+)?")
_PR_PATH_RE = re.compile(r"^/(projects|users)/([^/]+)/repos/([^/]+)/pull-requests/(\d+)(?:/|$)")


class BitbucketServerConfig(BaseModel):
    base_url: str = Field(description="Địa chỉ Bitbucket, ví dụ https://gits.tcbs.com.vn (kèm context path nếu có: https://host/bitbucket)")
    projects: list[str] = Field(min_length=1, description='Project key được phép, ví dụ ["PAY", "CORE"]; project cá nhân viết "~tên_người_dùng"')
    repos: list[str] = Field(default_factory=lambda: ["*"], description='Repo được phép, theo mẫu: "*", "PAY/*", "PAY/ledger-api"')
    write: bool = Field(default=False, description="Cho phép tạo pull request và trả lời bình luận")
    clone: bool = Field(default=True, description="Cho phép clone (mirror) repo về máy chạy office; tắt thì chỉ đọc qua API")

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        value = value.strip().rstrip("/")
        try:
            parts = urlsplit(value)
            _ = parts.port
        except ValueError:
            raise ValueError("base_url không phải một URL hợp lệ") from None
        if parts.scheme != "https" or not parts.hostname:
            raise ValueError("base_url phải là https://<host>[/context], ví dụ https://gits.tcbs.com.vn")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("base_url không được kèm tài khoản, tham số hay dấu #")
        if parts.path.endswith(("/rest", API)) or "/rest/api/" in parts.path:
            raise ValueError("base_url là địa chỉ gốc của Bitbucket, không kèm /rest/api/1.0")
        return value

    @field_validator("projects")
    @classmethod
    def _projects(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip().strip("/") for v in value]
        for key in cleaned:
            if not _KEY_RE.fullmatch(key):
                raise ValueError(f"project key «{key[:40]}» không hợp lệ (ví dụ PAY, hoặc ~tên_người_dùng cho project cá nhân)")
        return cleaned

    @field_validator("repos")
    @classmethod
    def _repos(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip().strip("/") for v in value]
        for pattern in cleaned:
            if not _PATTERN_RE.fullmatch(pattern) or any(part in (".", "..") for part in pattern.split("/")):
                raise ValueError(f'mẫu repo «{pattern[:40]}» không hợp lệ (dùng "*", "KEY/*" hoặc "KEY/tên-repo")')
        return cleaned


def _safe_path(path: str) -> str:
    path = path.strip().lstrip("/")
    if not path or any(part in ("", ".", "..") for part in path.split("/")):
        raise ConnectorError("invalid", f"{LABEL}: đường dẫn file không hợp lệ")
    return quote(path, safe="/")


def _path_text(value: Any) -> str:
    """A path of the API as text: it comes as a string, or as an object with `toString` / `components` / `parent` + `name`."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return ""
    if value.get("toString"):
        return str(value["toString"])
    if value.get("components"):
        return "/".join(str(c) for c in value["components"])
    return "/".join(str(p) for p in (value.get("parent"), value.get("name")) if p)


def _when(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, UTC).isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _person(user: Any) -> str:
    user = user if isinstance(user, dict) else {}
    return str(user.get("displayName") or user.get("name") or "")


def _self_link(data: dict[str, Any]) -> str:
    links = (data.get("links") or {}).get("self") or []
    first = links[0] if links and isinstance(links[0], dict) else {}
    return str(first.get("href") or "")


class BitbucketServer:
    name: ClassVar[str] = NAME
    title: ClassVar[str] = "Bitbucket Server / Data Center"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CODE_READ, Capability.CODE_PR, Capability.WEBHOOK})
    config_schema: ClassVar[type[BaseModel]] = BitbucketServerConfig
    secret_fields: ClassVar[tuple[str, ...]] = ("token", "username", "webhook_secret")
    optional_secret_fields: ClassVar[tuple[str, ...]] = ("username",)
    required_scopes: ClassVar[dict[Capability, list[str]]] = REQUIRED_SCOPES

    def __init__(self, config: BitbucketServerConfig, secret: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self._token = secret.get("token", "")
        self._username = secret.get("username", "")
        self._webhook_secret = secret.get("webhook_secret", "")
        headers = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        client = httpx.AsyncClient(base_url=config.base_url, timeout=TIMEOUT_S, transport=transport, headers=headers, verify=tls(transport))
        blocked = None if self._token else ConnectorError("unconfigured", f"{LABEL}: chưa có HTTP access token. Vào Cài đặt › Kết nối để nhập.")
        self.http = Http(client, label=LABEL, blocked=blocked)
        self._old_default_branch = False  # the server has no `/default-branch` (older than 7.5)

    async def close(self) -> None:
        await self.http.close()

    async def test(self) -> Health:
        """Is the address a Bitbucket, is the token accepted, and which of the configured projects it can read."""
        if self.http.blocked is not None:
            return Health(state="unconfigured", checked_at=now_iso(), detail=str(self.http.blocked))
        where = f"{self.config.base_url}{API}"

        def failed(detail: str) -> Health:
            return Health(state="error", checked_at=now_iso(), detail=detail)

        try:
            properties = await self.http.json("GET", f"{API}/application-properties")
        except ConnectorError as exc:
            if exc.kind == "auth":
                return failed(f"{LABEL} từ chối token (401): token sai, đã hết hạn hoặc đã bị thu hồi. Tạo token mới ở Manage account › HTTP access tokens.")
            if exc.kind == "forbidden":
                return failed(f"{LABEL} không cho gọi API (403) ở {where}: tài khoản bị chặn, cần CAPTCHA, hoặc proxy của công ty chặn.")
            if exc.kind in ("not_found", "invalid"):
                return failed(f"Không thấy REST API của Bitbucket ở {where}: kiểm tra base_url (có thể thiếu context path, ví dụ /bitbucket). {exc}")
            return failed(str(exc))
        version = str(properties.get("version") or "") if isinstance(properties, dict) else ""
        if not version:
            return failed(f"Địa chỉ {where} trả lời nhưng không phải Bitbucket Server / Data Center: kiểm tra base_url.")
        visible: list[str] = []
        hidden: list[str] = []
        identity = self._username or None
        for key in self.config.projects:
            try:
                response = await self.http.request("GET", self._project_path(key), params={"limit": 1})
            except ConnectorError as exc:
                if exc.kind not in ("auth", "forbidden", "not_found"):
                    return failed(str(exc))
                hidden.append(key)  # the reference answers 401 for a project the token may not see, 404 for one that does not exist
                continue
            visible.append(key)
            identity = response.headers.get(USER_HEADER, "").strip() or identity
        detail = f"Bitbucket {version} ở {self.config.base_url}."
        if visible:
            detail += f" Đọc được project {', '.join(visible)}."
        if hidden:
            detail += f" Token không có quyền với project {', '.join(hidden)} (hoặc project không tồn tại): cấp quyền Project read cho token, kiểm tra lại key."
        if not visible:
            detail += " Nếu mọi project đều bị từ chối thì có thể token sai hoặc đã hết hạn."
        state = "ok" if not hidden else "degraded" if visible else "error"
        return Health(state=state, checked_at=now_iso(), detail=detail, identity=identity, scopes=[f"project:{k}" for k in visible])

    # ---- repositories

    def _project(self, key: str) -> str | None:
        """The configured spelling of a project key (keys compare without case), None when it is not allowed."""
        return next((p for p in self.config.projects if p.lower() == key.lower()), None)

    def _allowed(self, key: str, name: str) -> bool:
        full = f"{key}/{name}".lower()
        return any(fnmatch.fnmatchcase(full if "/" in p else name.lower(), p.lower()) for p in self.config.repos)

    def _split(self, repo: RepoRef) -> tuple[str, str]:
        """(project key, repository name) of a reference, after checking both are allowed. Besides `KEY/name` this takes
        what is left of a repository URL: `scm/key/name`, `projects/KEY/repos/name`, `users/someone/repos/name`."""
        found = self.repo_ref(repo.slug)
        if found is None:
            raise ConnectorError("invalid", f"{LABEL}: repo phải viết dạng PROJECT/tên-repo (hoặc link repo trên {self.config.base_url})")
        key, _, name = found.slug.partition("/")
        project = self._project(key)
        if project is None:
            raise ConnectorError("forbidden", f"{LABEL}: project «{key[:60]}» nằm ngoài các project được phép của kết nối")
        if not _NAME_RE.fullmatch(name) or name in (".", ".."):
            raise ConnectorError("invalid", f"{LABEL}: tên repo không hợp lệ")
        if not self._allowed(project, name):
            raise ConnectorError("forbidden", f"{LABEL}: repo «{project}/{name}» nằm ngoài các repo được phép của kết nối")
        return project, name

    def repo_ref(self, text: str) -> RepoRef | None:
        """`KEY/name`; a web link `<base>/projects/KEY/repos/name[/browse…]` or `<base>/users/someone/repos/name`; a clone
        link `<base>/scm/key/name.git`, `ssh://git@host:7999/key/name.git`, `git@host:key/name.git`. A link must be on
        this connection's address (host and context path; for ssh the host). Nothing is asked and no allow-list checked."""
        text = (text or "").strip()
        base = urlsplit(self.config.base_url)
        scp = re.fullmatch(r"[\w.-]+@([\w.-]+):(?!/)(.+)", text)
        if scp:  # git@host:key/name.git
            if scp.group(1).lower() != (base.hostname or "").lower():
                return None
            path = scp.group(2)
        elif "://" in text:
            try:
                parts = urlsplit(text)
                port = parts.port
            except ValueError:
                return None
            if (parts.hostname or "").lower() != (base.hostname or "").lower():
                return None
            if parts.scheme in ("http", "https"):
                if (parts.scheme, port) != (base.scheme, base.port) or not parts.path.startswith(base.path + "/"):
                    return None
                path = parts.path[len(base.path) :]
            elif parts.scheme == "ssh":
                path = parts.path
            else:
                return None
        else:
            path = text
        parts_ = [p for p in path.removesuffix("/").removesuffix(".git").split("/") if p]
        if path is text:  # what is left of a link once the host is cut off may still start with the context path
            start = next((i for i, p in enumerate(parts_) if p == "scm" or (p in ("projects", "users") and parts_[i + 2 : i + 3] == ["repos"])), 0)
            parts_ = parts_[start:]
        if len(parts_) >= 4 and parts_[0] in ("projects", "users") and parts_[2] == "repos":
            key, name = parts_[1], parts_[3]
            if parts_[0] == "users" and not key.startswith("~"):
                key = "~" + key
        elif len(parts_) == 3 and parts_[0] == "scm":
            key, name = parts_[1], parts_[2]
        elif len(parts_) == 2 and not text.lower().startswith(("http://", "https://")):  # a web address always says more
            key, name = parts_
        else:
            return None
        if not _KEY_RE.fullmatch(key) or not _NAME_RE.fullmatch(name) or name in (".", ".."):
            return None
        key = self._project(key) or key
        return RepoRef(slug=f"{key}/{name}", url=self._web(key, name))

    @staticmethod
    def _project_path(key: str) -> str:
        return f"{API}/projects/{quote(key, safe='~')}/repos"

    def _path(self, repo: RepoRef, tail: str = "") -> str:
        key, name = self._split(repo)
        return f"{self._project_path(key)}/{name}{tail}"

    def _web(self, key: str, name: str) -> str:
        owner = f"users/{key[1:]}" if key.startswith("~") else f"projects/{key}"
        return f"{self.config.base_url}/{owner}/repos/{name}"

    async def _paged(self, path: str, params: dict[str, Any] | None = None, *, inside: str | None = None) -> list[Any]:
        """All values of a paged listing: `start` / `limit` in, `isLastPage` / `nextPageStart` out. `inside` names the
        key that holds the page when it is not the response itself (`children` of a directory)."""
        out: list[Any] = []
        start = 0
        for _ in range(MAX_PAGES):
            data = await self.http.json("GET", path, params={**(params or {}), "start": start, "limit": PAGE})
            page = data.get(inside) if inside and isinstance(data, dict) else data
            if not isinstance(page, dict):
                return out
            out += page.get("values") or []
            following = page.get("nextPageStart")
            if page.get("isLastPage", True) or following is None:
                return out
            start = int(following)
        raise ConnectorError("invalid", f"{LABEL}: danh sách quá dài, dừng sau {MAX_PAGES} trang (GET {path})")

    async def _default_branch(self, key: str, name: str) -> str:
        """`/default-branch` exists from Bitbucket 7.5; older servers only have `/branches/default` (now deprecated),
        which answers 204 for an empty repository."""
        base = f"{self._project_path(key)}/{name}"
        if not self._old_default_branch:
            try:
                data = await self.http.json("GET", f"{base}/default-branch")
                return str((data or {}).get("displayId") or "")
            except ConnectorError as exc:
                if exc.kind != "not_found":
                    raise
        try:
            data = await self.http.json("GET", f"{base}/branches/default")
        except ConnectorError as exc:
            if exc.kind != "not_found":
                raise
            return ""  # the configured default branch was never pushed
        self._old_default_branch = True
        return str((data or {}).get("displayId") or "")

    async def repos(self, query: str | None = None) -> list[RepoRef]:
        wanted = (query or "").strip().lower()
        found: list[tuple[str, dict[str, Any]]] = []
        for key in self.config.projects:
            for item in await self._paged(self._project_path(key)):
                name = str(item.get("slug") or "")
                if name and self._allowed(key, name) and (not wanted or wanted in name.lower() or wanted in str(item.get("name") or "").lower()):
                    found.append((key, item))
        branches = await asyncio.gather(*(self._default_branch(key, str(item["slug"])) for key, item in found))
        out: list[RepoRef] = []
        for (key, item), branch in zip(found, branches, strict=True):
            name = str(item["slug"])
            ref = RepoRef(slug=f"{key}/{name}", url=_self_link(item).removesuffix("/browse") or self._web(key, name))
            out.append(ref.model_copy(update={"default_branch": branch}) if branch else ref)
        return out

    async def head(self, repo: RepoRef, branch: str | None = None) -> str:
        """The latest commit of a branch; of the default branch when none is named (the server's own rule for `/commits`)."""
        params: dict[str, Any] = {"limit": 1}
        if branch:
            params["until"] = branch
        data = await self.http.json("GET", self._path(repo, "/commits"), params=params) or {}
        values = data.get("values") or []
        if not values or not values[0].get("id"):
            raise ConnectorError("not_found", f"{LABEL}: nhánh «{(branch or 'mặc định')[:80]}» của {repo.slug[:80]} chưa có commit nào")
        return str(values[0]["id"])

    async def tree(self, repo: RepoRef, at: str) -> list[TreeEntry]:
        """Every file at a commit, without reading any file's content.

        `/files` lists all paths in one paged call but nothing else. The identity of each file is then asked per
        directory that holds files, best first:
        1. `/browse/<dir>`: `contentId` (the git blob id) and `size` of each child. The reference names the endpoint but
           shows no directory listing, so the answer is read defensively;
        2. `/last-modified/<dir>` (documented): the last commit that touched each file; identity = hash(path, commit);
        3. the commit being listed: the file then counts as changed on every new commit, which costs a read, never a miss.
        Submodules are left out."""
        base = self._path(repo)
        by_dir: dict[str, list[str]] = {}
        for path in await self._paged(f"{base}/files", {"at": at}):
            directory, _, name = str(path).rpartition("/")
            by_dir.setdefault(directory, []).append(name)
        found = await asyncio.gather(*(self._identities(base, at, directory, set(names)) for directory, names in by_dir.items()))
        out: list[TreeEntry] = []
        for (directory, names), known in zip(by_dir.items(), found, strict=True):
            for name in names:
                if name in known:
                    blob, size = known[name]
                    out.append(TreeEntry(path=f"{directory}/{name}" if directory else name, blob_sha=blob, size=size))
        return out

    async def _identities(self, base: str, at: str, directory: str, names: set[str]) -> dict[str, tuple[str, int]]:
        """name → (identity, size) for the files of one directory; a submodule gets no entry."""
        suffix = f"/{quote(directory, safe='/')}" if directory else ""
        found: dict[str, tuple[str, int]] = {}
        submodules: set[str] = set()
        for child in await self._paged(f"{base}/browse{suffix}", {"at": at}, inside="children"):
            if not isinstance(child, dict):
                continue
            name = _path_text(child.get("path")).rpartition("/")[2]
            if child.get("type") == "SUBMODULE":
                submodules.add(name)
            elif child.get("type") == "FILE" and child.get("contentId"):
                found[name] = (str(child["contentId"]), int(child.get("size") or 0))
        missing = names - found.keys() - submodules
        if missing:
            try:
                data = await self.http.json("GET", f"{base}/last-modified{suffix}", params={"at": at})
            except ConnectorError as exc:
                if exc.kind not in ("not_found", "invalid"):
                    raise
                data = None
            files = (data or {}).get("files") if isinstance(data, dict) else None
            for name in missing:
                commit = (files.get(name) or {}).get("id") if isinstance(files, dict) and isinstance(files.get(name), dict) else None
                path = f"{directory}/{name}" if directory else name
                found[name] = ("c-" + hashlib.sha1(f"{path}\0{commit or at}".encode()).hexdigest(), 0)
        return found

    async def file(self, repo: RepoRef, path: str, at: str) -> bytes:
        """The raw bytes of a file at a commit. Redirects are never followed: a server that answers with one (a file
        kept in Git LFS, a mirror, another host) is refused."""
        response = await self.http.request("GET", self._path(repo, f"/raw/{_safe_path(path)}"), params={"at": at}, allow_redirect=True)
        if response.status_code >= 300:
            raise ConnectorError("invalid", f"{LABEL}: file «{path[:120]}» được trả bằng chuyển hướng (file Git LFS hoặc host khác): office không đi theo, không tải file này")
        return response.content

    async def compare(self, repo: RepoRef, base: str, head: str) -> list[ChangedFile]:
        """`from` is the commit that has the changes, `to` the one they are measured against."""
        out: list[ChangedFile] = []
        for item in await self._paged(self._path(repo, "/compare/changes"), {"from": head, "to": base}):
            kind = str(item.get("type") or "").upper()
            status = CHANGE_STATUS.get(kind, "modified")
            old = _path_text(item.get("srcPath"))
            out.append(ChangedFile(path=_path_text(item.get("path")) or old, status=status, old_path=old if status == "renamed" and old else None))
        return out

    async def clone_auth(self, repo: RepoRef) -> CloneAuth | None:
        """`<base>/scm/<project key, lower case>/<repo>.git`. A token alone goes to git as `Authorization: Bearer`; with a
        user name it is that user's password (the documented way for a personal token, and the one that works with LFS)."""
        return await self.git_auth(repo, write=False) if self.config.clone else None

    async def git_auth(self, repo: RepoRef, *, write: bool) -> CloneAuth | None:
        """The clone URL and the credential for git, whatever the `clone` switch says; pushing needs `write`."""
        if write:
            self._require_write("push nhánh")
        key, name = self._split(repo)
        if self.http.blocked is not None:
            return None
        url = f"{self.config.base_url}/scm/{key.lower()}/{name}.git"
        if self._username:
            return CloneAuth(url=url, username=self._username, password=self._token)
        return CloneAuth(url=url, bearer=self._token)

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
            url=_self_link(data),
            source=str((data.get("fromRef") or {}).get("displayId") or ""),
            destination=str((data.get("toRef") or {}).get("displayId") or ""),
            author=_person((data.get("author") or {}).get("user")),
            merged=state == "merged",
        )

    async def pr_create(self, repo: RepoRef, source: str, destination: str, title: str, body: str) -> PullRequest:
        self._require_write("tạo pull request")
        key, name = self._split(repo)
        where = {"slug": name, "project": {"key": key}}
        payload = {
            "title": title,
            "description": body,
            "fromRef": {"id": f"refs/heads/{source}", "repository": where},
            "toRef": {"id": f"refs/heads/{destination}", "repository": where},
        }
        return self._pull(await self.http.json("POST", self._path(repo, "/pull-requests"), json=payload) or {})

    async def pr_get(self, repo: RepoRef, pr_id: int) -> PullRequest:
        return self._pull(await self.http.json("GET", self._path(repo, f"/pull-requests/{int(pr_id)}")) or {})

    async def pr_comments(self, repo: RepoRef, pr_id: int) -> list[PrComment]:
        """Comments come inside the activity stream: one COMMENTED activity per thread, replies nested in `comments`,
        the place in the diff in `commentAnchor`. Flattened here, oldest first; a reply keeps the place of its thread."""
        found: dict[int, PrComment] = {}

        def walk(comment: dict[str, Any], parent: int | None, path: str | None, line: int | None) -> None:
            number = int(comment.get("id") or 0)
            if number and number not in found:
                found[number] = PrComment(
                    id=number, author=_person(comment.get("author")), body=str(comment.get("text") or ""), path=path, line=line,
                    parent_id=parent, created=_when(comment.get("createdDate")),
                )  # fmt: skip
            for reply in comment.get("comments") or []:
                if isinstance(reply, dict):
                    walk(reply, number or parent, path, line)

        for activity in await self._paged(self._path(repo, f"/pull-requests/{int(pr_id)}/activities")):
            if not isinstance(activity, dict) or activity.get("action") != "COMMENTED" or not isinstance(activity.get("comment"), dict):
                continue
            comment = activity["comment"]
            anchor = activity.get("commentAnchor") or comment.get("anchor") or {}
            line = anchor.get("line")
            walk(comment, None, _path_text(anchor.get("path")) or None, int(line) if isinstance(line, int) else None)
        return [found[k] for k in sorted(found)]

    async def pr_reply(self, repo: RepoRef, pr_id: int, text: str, *, parent_id: int | None = None) -> None:
        self._require_write("trả lời bình luận")
        payload: dict[str, Any] = {"text": text}
        if parent_id:
            payload["parent"] = {"id": int(parent_id)}
        await self.http.json("POST", self._path(repo, f"/pull-requests/{int(pr_id)}/comments"), json=payload)

    def parse_pr_url(self, url: str) -> tuple[RepoRef, int] | None:
        """`<base>/projects/KEY/repos/name/pull-requests/12[/…]`, or `<base>/users/someone/repos/name/pull-requests/12`
        for a personal project (`~someone`). Only on this connection's own address, project and repositories."""
        try:
            parts, base = urlsplit(url.strip()), urlsplit(self.config.base_url)
            same = (parts.scheme, (parts.hostname or "").lower(), parts.port) == (base.scheme, (base.hostname or "").lower(), base.port)
        except ValueError:
            return None
        if not same or not parts.path.startswith(base.path + "/"):
            return None
        m = _PR_PATH_RE.match(parts.path[len(base.path) :])
        if not m:
            return None
        owner, key, name, number = m.groups()
        if owner == "users" and not key.startswith("~"):
            key = "~" + key
        project = self._project(key)
        if project is None or not _NAME_RE.fullmatch(name) or not self._allowed(project, name):
            return None
        return RepoRef(slug=f"{project}/{name}", url=self._web(project, name)), int(number)

    # ---- events

    @staticmethod
    def _slug(repository: Any) -> str:
        repository = repository if isinstance(repository, dict) else {}
        key = str((repository.get("project") or {}).get("key") or "")
        name = str(repository.get("slug") or "")
        return f"{key}/{name}" if key and name else ""

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        """None when the signature is missing or wrong, or when the connection has no webhook secret to check it with.
        Bitbucket's "Test connection" (`diagnostics:ping`) is sent without a signature, so it is refused as well."""
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
        event = lowered.get(EVENT_HEADER, "") or str(payload.get("eventKey") or "")
        if event == "repo:refs_changed":
            repo = self._slug(payload.get("repository"))
            # A delivery carries one changed reference (several pushed together are sent apart); the first branch that
            # still exists is the one reported. A deleted reference and a tag are not a push to follow.
            for change in payload.get("changes") or []:
                ref = change.get("ref") or {}
                ref_id = str(ref.get("id") or change.get("refId") or "")
                commit = str(change.get("toHash") or "")
                is_branch = str(ref.get("type") or "").upper() == "BRANCH" or (not ref.get("type") and ref_id.startswith("refs/heads/"))
                if is_branch and str(change.get("type") or "").upper() != "DELETE" and commit and commit != NO_COMMIT:
                    return WebhookEvent(kind="push", repo=repo, ref=str(ref.get("displayId") or ref_id.removeprefix("refs/heads/")), commit=commit, raw=payload)
            return WebhookEvent(kind="other", repo=repo, raw=payload)
        if event in PR_EVENTS:
            pull = payload.get("pullRequest") or {}
            source = pull.get("fromRef") or {}
            return WebhookEvent(
                kind=PR_EVENTS[event],
                repo=self._slug((pull.get("toRef") or {}).get("repository")),
                ref=str(source.get("displayId") or ""),
                commit=str(source.get("latestCommit") or ""),
                pr_id=int(pull["id"]) if pull.get("id") is not None else None,
                raw=payload,
            )
        return WebhookEvent(kind="other", repo=self._slug(payload.get("repository")), raw=payload)
