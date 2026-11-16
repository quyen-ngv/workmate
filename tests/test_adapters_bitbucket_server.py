"""Bitbucket Server / Data Center adapter. No network: a small fake server shaped like the documented REST 1.0 answers
(developer.atlassian.com/server/bitbucket/rest/v1005/) and webhook payloads (confluence.atlassian.com/bitbucketserver/
event-payload-938025882.html) stands behind an `httpx.MockTransport`."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from typing import Any

import httpx
import pytest

from actoroffice.adapters import registry
from actoroffice.adapters.bitbucket_server import BitbucketServer, BitbucketServerConfig
from actoroffice.ports.code import ChangedFile, CloneAuth, RepoRef
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Scope
from actoroffice.services.atlassian import ConnectedCodeHost, parse_pr
from actoroffice.services.webhooks import _same_repo

from .test_codeintel import PY_REPO
from .test_docweb import web  # noqa: F401  (fixture)

BASE = "https://gits.acme.test"
REST = "/rest/api/1.0"
TOKEN = "bbs-token-0123456789abcdef"
HOOK_SECRET = "hook-secret-0123456789"
SECRET = {"token": TOKEN, "webhook_secret": HOOK_SECRET}
LEDGER = RepoRef(slug="PAY/ledger")
FILES = {"README.md": b"# Ledger\n", "src/A.java": b"class A {}", "src/main/B.java": b"class B {}\n", "docs/guide.md": b"guide"}


def blob(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def path_of(text: str) -> dict[str, Any]:
    parent, _, name = text.rpartition("/")
    return {"components": text.split("/"), "parent": parent, "name": name, "extension": name.rpartition(".")[2], "toString": text}


def user(name: str) -> dict[str, Any]:
    return {"name": name.lower(), "emailAddress": f"{name.lower()}@acme.test", "id": 1, "displayName": name, "active": True, "slug": name.lower(), "type": "NORMAL"}


def repository(key: str, name: str, base: str = BASE) -> dict[str, Any]:
    owner = f"users/{key[1:]}" if key.startswith("~") else f"projects/{key}"
    return {
        "slug": name, "id": 7, "name": name.title(), "scmId": "git", "state": "AVAILABLE", "forkable": True, "public": False, "archived": False,
        "project": {"key": key, "id": 3, "name": key, "public": False, "type": "PERSONAL" if key.startswith("~") else "NORMAL"},
        "links": {"clone": [{"href": f"{base}/scm/{key.lower()}/{name}.git", "name": "http"}], "self": [{"href": f"{base}/{owner}/repos/{name}/browse"}]},
    }  # fmt: skip


def pull(state: str = "OPEN", number: int = 12) -> dict[str, Any]:
    def ref(branch: str, commit: str) -> dict[str, Any]:
        return {"id": f"refs/heads/{branch}", "displayId": branch, "latestCommit": commit, "type": "BRANCH", "repository": repository("PAY", "ledger")}

    return {
        "id": number, "version": 1, "title": "Hoàn tiền", "description": "Mô tả", "state": state, "open": state == "OPEN", "closed": state != "OPEN",
        "fromRef": ref("t/pay-120", "b" * 40), "toRef": ref("main", "a" * 40), "locked": False,
        "author": {"user": user("Bot"), "role": "AUTHOR", "approved": False, "status": "UNAPPROVED"}, "reviewers": [], "participants": [],
        "links": {"self": [{"href": f"{BASE}/projects/PAY/repos/ledger/pull-requests/{number}"}]},
    }  # fmt: skip


def comment(number: int, who: str, text: str, at: int, replies: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "properties": {"repositoryId": 7}, "id": number, "version": 0, "text": text, "author": user(who), "createdDate": at, "updatedDate": at,
        "comments": replies or [], "tasks": [], "severity": "NORMAL", "state": "OPEN",
    }  # fmt: skip


ACTIVITIES = [  # newest first, as the server lists them
    {"id": 104, "createdDate": 1762740000000, "user": user("Lan"), "action": "APPROVED"},
    {"id": 103, "createdDate": 1762736400000, "user": user("Hùng"), "action": "COMMENTED", "commentAction": "ADDED", "comment": comment(5, "Hùng", "Nhìn chung ổn", 1762736400000)},
    {"id": 102, "createdDate": 1762732800000, "user": user("Bot"), "action": "RESCOPED", "fromHash": "b" * 40, "previousFromHash": "c" * 40},
    {
        "id": 101, "createdDate": 1762729200000, "user": user("Lan"), "action": "COMMENTED", "commentAction": "ADDED",
        "comment": comment(1, "Lan", "Đổi tên biến", 1762729200000, [comment(2, "Bot", "Đã đổi", 1762732800000, [comment(3, "Lan", "Cảm ơn", 1762734000000)])]),
        "commentAnchor": {"line": 12, "lineType": "ADDED", "fileType": "TO", "path": "src/A.java", "srcPath": "src/A.java"},
    },
]  # fmt: skip
CHANGES = [
    {"contentId": "1" * 40, "fromContentId": "2" * 40, "path": path_of("src/A.java"), "type": "MODIFY", "nodeType": "FILE"},
    {"contentId": "3" * 40, "path": path_of("new/c.py"), "srcPath": path_of("old/b.py"), "type": "MOVE", "nodeType": "FILE", "percentUnchanged": 98},
    {"fromContentId": "4" * 40, "path": {"components": ["gone", "d.py"], "parent": "gone", "name": "d.py", "extension": "py"}, "type": "DELETE", "nodeType": "FILE"},
    {"contentId": "5" * 40, "path": path_of("e.py"), "type": "ADD", "nodeType": "FILE"},
    {"contentId": "5" * 40, "path": path_of("f.py"), "srcPath": path_of("e.py"), "type": "COPY", "nodeType": "FILE"},
    {"path": path_of("g.py"), "type": "UNKNOWN", "nodeType": "FILE"},
]


class Server:
    """A small Bitbucket Server. Listings are paged the documented way: `start` / `limit` in, `size`, `limit`, `isLastPage`,
    `start`, `values` and `nextPageStart` out, with a hard page limit the server applies whatever the client asks."""

    def __init__(self, *, page: int = 1000, context: str = "", legacy: bool = False, content_ids: bool = True, last_modified: bool = True) -> None:
        self.page = page
        self.context = context
        self.legacy = legacy  # older than 7.5: no /default-branch
        self.content_ids = content_ids  # /browse tells contentId and size
        self.last_modified = last_modified
        self.version = "8.19.1"
        self.username = "svc-office"  # sent back as X-AUSERNAME; "" = the server does not send the header
        self.projects = {"PAY": ["ledger", "gateway", "secret-tool"], "~lan": ["notes"]}
        self.denied: set[str] = set()
        self.default_branch = "main"
        self.branches = {"main": "c1", "feature/x": "c9"}
        self.commits: dict[str, dict[str, bytes]] = {"c1": dict(FILES), "c9": {"README.md": b"x"}}
        self.touched: dict[str, str] = {}  # path → the last commit that changed it (default: c1)
        self.submodules: set[str] = set()
        self.raw: dict[str, httpx.Response] = {}  # path → a special answer of /raw
        self.extra: dict[tuple[str, str], Any] = {
            ("GET", "/compare/changes"): lambda request: self.paged(request, CHANGES),
            ("POST", "/pull-requests"): httpx.Response(201, json=pull()),
            ("GET", "/pull-requests/12"): pull("MERGED"),
            ("GET", "/pull-requests/13"): pull("DECLINED", 13),
            ("GET", "/pull-requests/12/activities"): lambda request: self.paged(request, ACTIVITIES),
            ("POST", "/pull-requests/12/comments"): httpx.Response(201, json=comment(9, "Bot", "ok", 1762740000000)),
        }
        self.requests: list[httpx.Request] = []

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths(self, tail: str) -> list[str]:
        return [r.url.path for r in self.requests if tail in r.url.path]

    def body(self, method: str, tail: str) -> Any:
        return next(json.loads(r.content) for r in self.requests if r.method == method and r.url.path.endswith(tail))

    def paged(self, request: httpx.Request, values: list[Any]) -> dict[str, Any]:
        start = int(request.url.params.get("start", 0))
        limit = min(int(request.url.params.get("limit", 25)), self.page)
        chunk = values[start : start + limit]
        last = start + limit >= len(values)
        out: dict[str, Any] = {"size": len(chunk), "limit": limit, "isLastPage": last, "values": chunk, "start": start}
        if not last:
            out["nextPageStart"] = start + limit
        return out

    @staticmethod
    def error(status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"errors": [{"context": None, "message": message, "exceptionName": None}]})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answer(request)
        response = answer if isinstance(answer, httpx.Response) else httpx.Response(200, json=answer)
        if self.username and response.status_code < 400:
            response.headers["X-AUSERNAME"] = self.username
        return response

    def answer(self, request: httpx.Request) -> Any:
        path = request.url.path
        if not path.startswith(self.context + REST + "/"):
            return httpx.Response(404, text="<html>Page not found</html>")
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return self.error(401, "Authentication failed. Please check your credentials and try again.")
        tail = path[len(self.context + REST) :]
        if tail == "/application-properties":
            return {"version": self.version, "buildNumber": "8019001", "buildDate": "1711000000000", "displayName": "Bitbucket"}
        m = re.fullmatch(r"/projects/([^/]+)/repos(?:/([^/]+)(/.*)?)?", tail)
        if not m:
            return self.error(404, "no such resource")
        key = next((k for k in self.projects if k.lower() == m.group(1).lower()), None)
        if key is None:
            return self.error(404, f"Project {m.group(1)} does not exist.")
        if key in self.denied:
            return self.error(401, "You are not permitted to access this resource")
        if m.group(2) is None:
            return self.paged(request, [repository(key, name, BASE + self.context) for name in self.projects[key]])
        if m.group(2) not in self.projects[key]:
            return self.error(404, "Repository does not exist.")
        return self.in_repo(request, m.group(3) or "")

    def in_repo(self, request: httpx.Request, rest: str) -> Any:
        at = request.url.params.get("at", "")
        files = self.commits.get(at)
        branch = {"id": f"refs/heads/{self.default_branch}", "displayId": self.default_branch, "type": "BRANCH"}
        if rest == "/default-branch":
            return self.error(404, "no such resource") if self.legacy else branch
        if rest == "/branches/default":
            return {**branch, "latestCommit": self.branches.get(self.default_branch, "c1"), "isDefault": True}
        if rest == "/commits":
            commit = self.branches.get(request.url.params.get("until", self.default_branch))
            return self.paged(request, [{"id": commit, "displayId": commit}]) if commit else self.error(404, "Commit does not exist.")
        if rest == "/files":
            return self.paged(request, sorted(files)) if files is not None else self.error(404, "Commit does not exist.")
        for name in ("/browse", "/last-modified"):
            if rest == name or rest.startswith(name + "/"):
                directory = rest[len(name) + 1 :]
                if files is None:
                    return self.error(404, "Commit does not exist.")
                return self.browse(request, files, directory) if name == "/browse" else self.modified(files, directory)
        if rest.startswith("/raw/"):
            name = rest[len("/raw/") :]
            if name in self.raw:
                return self.raw[name]
            return httpx.Response(200, content=files[name]) if files and name in files else self.error(404, "The path does not exist.")
        value = self.extra.get((request.method, rest))
        if value is None:
            return self.error(404, "no such resource")
        return value(request) if callable(value) else value

    def browse(self, request: httpx.Request, files: dict[str, bytes], directory: str) -> Any:
        prefix = f"{directory}/" if directory else ""
        children: dict[str, dict[str, Any]] = {}
        for path, data in sorted(files.items()):
            if not path.startswith(prefix):
                continue
            name, _, below = path[len(prefix) :].partition("/")
            if below:
                children[name] = {"path": path_of(name), "node": blob(name.encode()), "type": "DIRECTORY"}
            elif path in self.submodules:
                children[name] = {"path": path_of(name), "node": blob(data), "type": "SUBMODULE"}
            else:
                children[name] = {"path": path_of(name), "type": "FILE", **({"contentId": blob(data), "size": len(data)} if self.content_ids else {})}
        return {"path": path_of(directory) if directory else {"components": [], "name": "", "toString": ""}, "revision": request.url.params["at"],
                "children": self.paged(request, list(children.values()))}  # fmt: skip

    def modified(self, files: dict[str, bytes], directory: str) -> Any:
        if not self.last_modified:
            return self.error(404, "no such resource")
        prefix = f"{directory}/" if directory else ""
        own = {p[len(prefix) :]: p for p in files if p.startswith(prefix) and "/" not in p[len(prefix) :]}
        commits = {name: {"id": self.touched.get(path, "c1"), "displayId": "c", "message": "m"} for name, path in own.items()}
        return {"files": commits, "latestCommit": {"id": "c1"}}


def server_adapter(server: Server, secret: dict[str, str] | None = None, **config: Any) -> BitbucketServer:
    merged = {"base_url": BASE + server.context, "projects": ["PAY", "~lan"], "repos": ["PAY/ledger", "PAY/gate*", "~lan/*"], **config}
    return BitbucketServer(BitbucketServerConfig(**merged), SECRET if secret is None else secret, transport=server.transport)


def signed(body: bytes, secret: str = HOOK_SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ---- what it declares, configuration -------------------------------------------------------------------------------


def test_declares_itself_and_is_built_in() -> None:
    assert (BitbucketServer.name, BitbucketServer.title) == ("bitbucket_server", "Bitbucket Server / Data Center (tự cài, địa chỉ riêng của công ty)")
    assert BitbucketServer.capabilities == frozenset({Capability.CODE_READ, Capability.CODE_PR, Capability.WEBHOOK})
    assert BitbucketServer.secret_fields == ("token", "username", "webhook_secret") and BitbucketServer.optional_secret_fields == ("username",)
    assert registry.BUILTIN["bitbucket_server"].endswith(":BitbucketServer") and registry.adapter_class("bitbucket_server") is BitbucketServer
    info = registry.describe("bitbucket_server")
    assert set(info["config_schema"]["properties"]) == {"base_url", "projects", "repos", "write", "clone"} and "Repository write" in info["required_scopes"]["code_pr"]
    config = BitbucketServerConfig(base_url=" https://gits.tcbs.com.vn/ ", projects=[" PAY "])
    assert (config.base_url, config.projects, config.repos, config.write, config.clone) == ("https://gits.tcbs.com.vn", ["PAY"], ["*"], False, True)
    assert BitbucketServerConfig(base_url="https://host.test:8443/bitbucket/", projects=["~lan.nguyen"]).base_url == "https://host.test:8443/bitbucket"


@pytest.mark.parametrize(
    ("config", "field"),
    [
        ({"projects": ["PAY"]}, "base_url"),
        ({"base_url": "http://gits.acme.test", "projects": ["PAY"]}, "base_url"),
        ({"base_url": "gits.acme.test", "projects": ["PAY"]}, "base_url"),
        ({"base_url": "https://bot:pw@gits.acme.test", "projects": ["PAY"]}, "base_url"),
        ({"base_url": "https://gits.acme.test/rest/api/1.0", "projects": ["PAY"]}, "base_url"),
        ({"base_url": BASE}, "projects"),
        ({"base_url": BASE, "projects": []}, "projects"),
        ({"base_url": BASE, "projects": ["PAY/x"]}, "projects"),
        ({"base_url": BASE, "projects": "PAY"}, "projects"),
        ({"base_url": BASE, "projects": ["PAY"], "repos": ["a/b/c"]}, "repos"),
        ({"base_url": BASE, "projects": ["PAY"], "repos": ["../x"]}, "repos"),
        ({"base_url": BASE, "projects": ["PAY"], "clone": "maybe"}, "clone"),
    ],
)
def test_a_bad_configuration_is_refused_naming_the_field(config: dict[str, Any], field: str) -> None:
    with pytest.raises(ConnectorError) as refused:
        registry.validate(ConnectionSpec(id="tcbs", use="bitbucket_server", config=config))
    assert refused.value.kind == "invalid" and f"{field}:" in str(refused.value)


def test_the_token_is_required_and_write_is_the_switch_of_the_connection() -> None:
    spec = ConnectionSpec(id="tcbs", use="bitbucket_server", config={"base_url": BASE, "projects": ["PAY"]}, write=True)
    with pytest.raises(ConnectorError) as missing:
        registry.build(spec, {"username": "bot"})
    assert missing.value.kind == "unconfigured" and "token" in str(missing.value) and "username" not in str(missing.value)
    adapter = registry.build(spec, {"token": TOKEN}, transport=Server().transport)
    assert isinstance(adapter, BitbucketServer) and adapter.config.write is True


# ---- repositories and code -----------------------------------------------------------------------------------------


async def test_lists_allowed_repositories_over_two_pages() -> None:
    server = Server(page=2)
    adapter = server_adapter(server)
    repos = await adapter.repos()
    assert [(r.slug, r.default_branch, r.url) for r in repos] == [
        ("PAY/ledger", "main", f"{BASE}/projects/PAY/repos/ledger"),
        ("PAY/gateway", "main", f"{BASE}/projects/PAY/repos/gateway"),
        ("~lan/notes", "main", f"{BASE}/users/lan/repos/notes"),
    ], "secret-tool is outside the allowed patterns"
    listing = [r for r in server.requests if r.url.path == f"{REST}/projects/PAY/repos"]
    assert [(r.url.params["start"], r.url.params["limit"]) for r in listing] == [("0", "1000"), ("2", "1000")], "the second page starts where the server said"
    assert all(r.headers["authorization"] == f"Bearer {TOKEN}" for r in server.requests)
    assert [r.slug for r in await adapter.repos("GATE")] == ["PAY/gateway"]


async def test_default_branch_falls_back_on_a_server_older_than_7_5() -> None:
    server = Server(legacy=True)
    server.default_branch = "master"
    adapter = server_adapter(server)
    assert [r.default_branch for r in await adapter.repos()] == ["master", "master", "master"]
    assert len(server.paths("/branches/default")) == 3 and len(server.paths("/default-branch")) >= 1
    server.requests.clear()
    await adapter.repos()
    assert not server.paths("/default-branch"), "once known, the missing endpoint is not asked again"


async def test_head_of_a_branch_and_of_the_default_branch() -> None:
    server = Server()
    adapter = server_adapter(server)
    assert await adapter.head(LEDGER) == "c1" and "until" not in server.requests[-1].url.params
    assert await adapter.head(LEDGER, "feature/x") == "c9"
    assert (server.requests[-1].url.path, server.requests[-1].url.params["until"]) == (f"{REST}/projects/PAY/repos/ledger/commits", "feature/x")
    with pytest.raises(ConnectorError) as missing:
        await adapter.head(LEDGER, "nope")
    assert missing.value.kind == "not_found"


async def test_tree_has_the_blob_id_and_size_and_changes_when_a_file_changes() -> None:
    server = Server(page=2)  # /files and every directory listing come in pages of two
    adapter = server_adapter(server)
    before = {e.path: e for e in await adapter.tree(LEDGER, "c1")}
    assert set(before) == set(FILES)
    assert (before["src/A.java"].blob_sha, before["src/A.java"].size) == (blob(FILES["src/A.java"]), 10)
    assert not server.paths("/raw/") and not server.paths("/last-modified"), "no file content is read to list the tree"
    browsed = sorted({r.url.path.partition("/browse")[2] for r in server.requests if "/browse" in r.url.path})
    assert browsed == ["", "/docs", "/src", "/src/main"], "one listing per directory that holds files"

    server.commits["c2"] = {**FILES, "src/A.java": b"class A { int x; }", "src/new.py": b"x = 1\n"}
    after = {e.path: e for e in await adapter.tree(LEDGER, "c2")}
    changed = sorted(p for p in after if p not in before or after[p].blob_sha != before[p].blob_sha)
    assert changed == ["src/A.java", "src/new.py"] and after["README.md"] == before["README.md"]


async def test_tree_falls_back_to_the_last_commit_of_each_file_then_to_the_listed_commit() -> None:
    server = Server(content_ids=False)  # /browse does not tell content ids here
    adapter = server_adapter(server)
    first = {e.path: e.blob_sha for e in await adapter.tree(LEDGER, "c1")}
    assert set(first) == set(FILES) and all(sha.startswith("c-") for sha in first.values()) and len(set(first.values())) == len(FILES)
    server.commits["c2"] = {**FILES, "src/A.java": b"changed"}
    server.touched["src/A.java"] = "c2"
    second = {e.path: e.blob_sha for e in await adapter.tree(LEDGER, "c2")}
    assert [p for p in second if second[p] != first[p]] == ["src/A.java"], "only the file a newer commit touched has a new identity"

    server.last_modified = False  # neither endpoint helps: every file counts as changed at every commit, none is missed
    at_c1 = {e.path: e.blob_sha for e in await adapter.tree(LEDGER, "c1")}
    at_c2 = {e.path: e.blob_sha for e in await adapter.tree(LEDGER, "c2")}
    assert set(at_c1) == set(FILES) and all(at_c1[p] != at_c2[p] for p in FILES) and all(at_c1.values())


async def test_tree_leaves_submodules_out() -> None:
    server = Server()
    server.commits["c1"]["vendor/lib"] = b"submodule"
    server.submodules.add("vendor/lib")
    assert "vendor/lib" not in {e.path for e in await server_adapter(server).tree(LEDGER, "c1")}


async def test_reads_a_raw_file_and_refuses_a_redirect() -> None:
    server = Server()
    server.commits["c1"]["docs/Hướng dẫn #1.md"] = "nội dung".encode()
    server.raw["big.bin"] = httpx.Response(302, headers={"Location": "https://lfs.other.test/objects/abc"})
    adapter = server_adapter(server)
    assert await adapter.file(LEDGER, "src/A.java", "c1") == b"class A {}"
    assert (server.requests[-1].url.path, server.requests[-1].url.params["at"]) == (f"{REST}/projects/PAY/repos/ledger/raw/src/A.java", "c1")
    assert await adapter.file(LEDGER, "/docs/Hướng dẫn #1.md", "c1") == "nội dung".encode()
    assert server.requests[-1].url.raw_path.startswith(b"/rest/api/1.0/projects/PAY/repos/ledger/raw/docs/H%C6%B0%E1%BB%9Bng%20d%E1%BA%ABn%20%231.md?")
    with pytest.raises(ConnectorError) as redirected:
        await adapter.file(LEDGER, "big.bin", "c1")
    assert redirected.value.kind == "invalid" and "LFS" in str(redirected.value) and "chuyển hướng" in str(redirected.value)
    assert not [r for r in server.requests if r.url.host != "gits.acme.test"], "the redirect is not followed"
    for bad in ("", "../etc/passwd", "a//b", "a/./b"):
        with pytest.raises(ConnectorError) as refused:
            await adapter.file(LEDGER, bad, "c1")
        assert refused.value.kind == "invalid"
    with pytest.raises(ConnectorError) as missing:
        await adapter.file(LEDGER, "nope.txt", "c1")
    assert missing.value.kind == "not_found"


async def test_compare_maps_every_change_type() -> None:
    server = Server(page=4)
    adapter = server_adapter(server)
    assert await adapter.compare(LEDGER, "c0", "c1") == [
        ChangedFile(path="src/A.java", status="modified"),
        ChangedFile(path="new/c.py", status="renamed", old_path="old/b.py"),
        ChangedFile(path="gone/d.py", status="removed"),
        ChangedFile(path="e.py", status="added"),
        ChangedFile(path="f.py", status="added"),
        ChangedFile(path="g.py", status="modified"),
    ]
    calls = [r for r in server.requests if r.url.path.endswith("/compare/changes")]
    assert len(calls) == 2 and (calls[0].url.params["from"], calls[0].url.params["to"]) == ("c1", "c0"), "`from` is the side that has the changes"


async def test_clone_address_and_how_git_gets_the_token() -> None:
    server = Server(context="/bitbucket")
    assert await server_adapter(server).clone_auth(LEDGER) == CloneAuth(url=f"{BASE}/bitbucket/scm/pay/ledger.git", bearer=TOKEN)
    named = await server_adapter(server, {"token": TOKEN, "username": "svc-office"}).clone_auth(RepoRef(slug="~lan/notes"))
    assert named == CloneAuth(url=f"{BASE}/bitbucket/scm/~lan/notes.git", username="svc-office", password=TOKEN)
    assert await server_adapter(server, clone=False).clone_auth(LEDGER) is None
    assert await server_adapter(server, {}).clone_auth(LEDGER) is None


async def test_a_context_path_is_part_of_every_call() -> None:
    server = Server(context="/bitbucket")
    adapter = server_adapter(server)
    assert await adapter.head(LEDGER) == "c1" and server.requests[-1].url.path == f"/bitbucket{REST}/projects/PAY/repos/ledger/commits"
    assert (await adapter.repos("ledger"))[0].url == f"{BASE}/bitbucket/projects/PAY/repos/ledger"
    assert (await adapter.test()).state == "ok"


async def test_repositories_outside_the_connection_and_other_hosts_are_refused() -> None:
    server = Server()
    adapter = server_adapter(server)
    for slug, kind in (("PAY/secret-tool", "forbidden"), ("CORE/ledger", "forbidden"), ("ledger", "invalid"), ("PAY/..", "invalid"), ("PAY/a b", "invalid")):
        with pytest.raises(ConnectorError) as refused:
            await adapter.head(RepoRef(slug=slug))
        assert refused.value.kind == kind, slug
    assert not server.requests, "nothing was asked of the server"
    assert await adapter.head(RepoRef(slug="pay/ledger")) == "c1" and server.requests[-1].url.path.startswith(f"{REST}/projects/PAY/"), "keys compare without case"
    # what is left of a repository URL once the host is cut off names the same repository
    for slug in ("scm/pay/ledger", "scm/pay/ledger.git", "projects/PAY/repos/ledger", "bitbucket/scm/pay/ledger"):
        assert await adapter.head(RepoRef(slug=slug)) == "c1"
    assert await adapter.head(RepoRef(slug="users/lan/repos/notes")) == "c1" and "/projects/~lan/repos/notes/" in server.requests[-1].url.path
    seen = len(server.requests)
    with pytest.raises(ConnectorError) as other:
        await adapter.http.json("GET", "https://evil.test/rest/api/1.0/projects")
    assert other.value.kind == "invalid" and "host khác" in str(other.value) and len(server.requests) == seen


async def test_failures_are_sorted_into_kinds_and_never_carry_the_token() -> None:
    server = Server()
    server.denied.add("PAY")
    adapter = server_adapter(server)
    with pytest.raises(ConnectorError) as denied:
        await adapter.repos()
    assert denied.value.kind == "auth" and TOKEN not in str(denied.value)
    blocked = server_adapter(server, {"webhook_secret": HOOK_SECRET})
    with pytest.raises(ConnectorError) as unconfigured:
        await blocked.head(LEDGER)
    assert unconfigured.value.kind == "unconfigured" and (await blocked.test()).state == "unconfigured"
    assert TOKEN not in repr(adapter.config) and TOKEN not in json.dumps(registry.describe("bitbucket_server"))


# ---- pull requests -------------------------------------------------------------------------------------------------


async def test_pull_request_create_get_comments_and_reply() -> None:
    server = Server(page=2)
    adapter = server_adapter(server, write=True)

    created = await adapter.pr_create(LEDGER, "t/pay-120", "main", "Hoàn tiền", "Mô tả")
    assert (created.id, created.state, created.source, created.destination, created.author, created.merged) == (12, "open", "t/pay-120", "main", "Bot", False)
    assert created.url == f"{BASE}/projects/PAY/repos/ledger/pull-requests/12"
    where = {"slug": "ledger", "project": {"key": "PAY"}}
    assert server.body("POST", "/pull-requests") == {
        "title": "Hoàn tiền", "description": "Mô tả",
        "fromRef": {"id": "refs/heads/t/pay-120", "repository": where}, "toRef": {"id": "refs/heads/main", "repository": where},
    }  # fmt: skip

    merged = await adapter.pr_get(LEDGER, 12)
    assert (merged.state, merged.merged) == ("merged", True)
    assert (await adapter.pr_get(LEDGER, 13)).state == "declined"

    comments = await adapter.pr_comments(LEDGER, 12)
    assert [(c.id, c.author, c.body, c.path, c.line, c.parent_id) for c in comments] == [
        (1, "Lan", "Đổi tên biến", "src/A.java", 12, None),
        (2, "Bot", "Đã đổi", "src/A.java", 12, 1),
        (3, "Lan", "Cảm ơn", "src/A.java", 12, 2),
        (5, "Hùng", "Nhìn chung ổn", None, None, None),
    ], "threads flattened, oldest first; approvals and rescopes are not comments"
    assert comments[0].created == "2025-11-09T23:00:00+00:00"
    assert len(server.paths("/pull-requests/12/activities")) == 2, "the activity stream came in two pages"

    await adapter.pr_reply(LEDGER, 12, "Đã sửa", parent_id=1)
    assert server.body("POST", "/pull-requests/12/comments") == {"text": "Đã sửa", "parent": {"id": 1}}
    server.requests.clear()
    await adapter.pr_reply(LEDGER, 12, "Chung")
    assert server.body("POST", "/pull-requests/12/comments") == {"text": "Chung"}


async def test_a_read_only_connection_refuses_to_write() -> None:
    server = Server()
    adapter = server_adapter(server)
    for call in (adapter.pr_create(LEDGER, "a", "main", "t", "b"), adapter.pr_reply(LEDGER, 12, "x")):
        with pytest.raises(ConnectorError) as refused:
            await call
        assert refused.value.kind == "forbidden" and "chỉ đọc" in str(refused.value)
    assert not server.requests
    assert (await adapter.pr_get(LEDGER, 12)).id == 12, "reading stays allowed"


def test_parse_pr_url() -> None:
    adapter = server_adapter(Server())
    ref, number = adapter.parse_pr_url(f"{BASE}/projects/PAY/repos/ledger/pull-requests/12/overview?commentId=5")
    assert (ref.slug, ref.url, number) == ("PAY/ledger", f"{BASE}/projects/PAY/repos/ledger", 12)
    assert adapter.parse_pr_url(f" {BASE}/projects/pay/repos/ledger/pull-requests/7 ")[0].slug == "PAY/ledger"
    personal, number = adapter.parse_pr_url(f"{BASE}/users/lan/repos/notes/pull-requests/3/diff")
    assert (personal.slug, personal.url, number) == ("~lan/notes", f"{BASE}/users/lan/repos/notes", 3)
    assert adapter.parse_pr_url(f"{BASE}/projects/~lan/repos/notes/pull-requests/3")[0].slug == "~lan/notes"
    for foreign in (
        "https://bitbucket.org/acme/ledger/pull-requests/12",
        "https://other.test/projects/PAY/repos/ledger/pull-requests/12",
        "http://gits.acme.test/projects/PAY/repos/ledger/pull-requests/12",
        f"{BASE}:8443/projects/PAY/repos/ledger/pull-requests/12",
        f"{BASE}/bitbucket/projects/PAY/repos/ledger/pull-requests/12",
        f"{BASE}/projects/CORE/repos/ledger/pull-requests/12",
        f"{BASE}/projects/PAY/repos/secret-tool/pull-requests/12",
        f"{BASE}/projects/PAY/repos/ledger/pull-requests/abc",
        f"{BASE}/projects/PAY/repos/ledger/browse",
        "https://[bad/projects/PAY/repos/ledger/pull-requests/1",
        "",
    ):
        assert adapter.parse_pr_url(foreign) is None, foreign
    behind = server_adapter(Server(context="/bitbucket"))
    assert behind.parse_pr_url(f"{BASE}/bitbucket/projects/PAY/repos/ledger/pull-requests/12")[1] == 12
    assert behind.parse_pr_url(f"{BASE}/projects/PAY/repos/ledger/pull-requests/12") is None


async def test_the_office_finds_this_host_for_its_pull_request_urls_and_repository_addresses() -> None:
    server = Server()
    adapter = server_adapter(server, write=True)
    url = f"{BASE}/projects/PAY/repos/ledger/pull-requests/12"
    assert parse_pr(url) is None and parse_pr(url, [adapter]) == ("PAY/ledger", 12)
    host = ConnectedCodeHost(ConnectionSpec(id="tcbs", use="bitbucket_server", write=True), adapter)
    assert host.parse_pr(url) == ("PAY/ledger", 12) and host.parse_pr("https://github.com/acme/x/pull/1") is None
    assert (await host.pr_get("PAY/ledger", 12))["state"] == "MERGED"
    # a project's repo_url is what the tools pass when no repository is named
    for address in (f"{BASE}/scm/pay/ledger.git", f"{BASE}/projects/PAY/repos/ledger", "ssh://git@gits.acme.test:7999/pay/ledger.git"):
        created = await host.pr_create(address, "t/pay-120", "main", "Hoàn tiền", "Mô tả")
        assert created == {"id": 12, "url": url, "state": "OPEN"}, address
        assert server.requests[-1].url.path == f"{REST}/projects/PAY/repos/ledger/pull-requests"


# ---- webhooks ------------------------------------------------------------------------------------------------------


def push(changes: list[dict[str, Any]]) -> bytes:
    return json.dumps({"eventKey": "repo:refs_changed", "date": "2026-11-16T09:00:00+0700", "actor": user("Lan"), "repository": repository("PAY", "ledger"), "changes": changes}).encode()


def change(ref: str, to: str, kind: str = "UPDATE", ref_type: str = "BRANCH") -> dict[str, Any]:
    return {"ref": {"id": ref, "displayId": ref.split("/", 2)[2], "type": ref_type}, "refId": ref, "fromHash": "1" * 40, "toHash": to, "type": kind}


def deliver(adapter: BitbucketServer, event: str, body: bytes, **headers: str) -> Any:
    return adapter.verify_webhook({"X-Event-Key": event, "X-Request-Id": "r-1", "X-Hub-Signature": signed(body), **headers}, body)


def test_webhook_signature_good_bad_missing() -> None:
    adapter = server_adapter(Server())
    body = push([change("refs/heads/main", "a" * 40)])
    event = adapter.verify_webhook({"x-event-key": "repo:refs_changed", "x-hub-signature": signed(body)}, body)
    assert event is not None and (event.kind, event.repo, event.ref, event.commit) == ("push", "PAY/ledger", "main", "a" * 40)
    assert adapter.verify_webhook({"X-Event-Key": "repo:refs_changed", "X-Hub-Signature": signed(body, "another-secret")}, body) is None
    assert adapter.verify_webhook({"X-Event-Key": "repo:refs_changed", "X-Hub-Signature": signed(body)}, body + b" ") is None
    assert adapter.verify_webhook({"X-Event-Key": "repo:refs_changed"}, body) is None
    assert adapter.verify_webhook({"X-Event-Key": "repo:refs_changed", "X-Hub-Signature": signed(body).removeprefix("sha256=")}, body) is None
    no_secret = server_adapter(Server(), {"token": TOKEN})
    assert no_secret.verify_webhook({"X-Event-Key": "repo:refs_changed", "X-Hub-Signature": signed(body)}, body) is None
    assert deliver(adapter, "repo:refs_changed", b"not json") is None and deliver(adapter, "repo:refs_changed", b"[1]") is None
    ping = b'{"test": true}'  # "Test connection" is sent without a signature (BSERV-20159): refused like any unsigned delivery
    assert adapter.verify_webhook({"X-Event-Key": "diagnostics:ping"}, ping) is None
    assert deliver(adapter, "diagnostics:ping", ping).kind == "other"


def test_webhook_push_reports_the_first_branch_that_moved() -> None:
    adapter = server_adapter(Server())
    added = deliver(adapter, "repo:refs_changed", push([change("refs/heads/feature/x", "b" * 40, "ADD")]))
    assert (added.kind, added.ref, added.commit) == ("push", "feature/x", "b" * 40)
    several = deliver(adapter, "repo:refs_changed", push([change("refs/tags/v1", "c" * 40, "ADD", "TAG"), change("refs/heads/main", "d" * 40)]))
    assert (several.kind, several.ref, several.commit) == ("push", "main", "d" * 40)
    for only in (change("refs/heads/old", "0" * 40, "DELETE"), change("refs/tags/v1", "c" * 40, "ADD", "TAG")):
        ignored = deliver(adapter, "repo:refs_changed", push([only]))
        assert (ignored.kind, ignored.repo, ignored.ref) == ("other", "PAY/ledger", "")
    personal = json.dumps({"eventKey": "repo:refs_changed", "repository": repository("~lan", "notes"), "changes": [change("refs/heads/main", "e" * 40)]}).encode()
    assert deliver(adapter, "repo:refs_changed", personal).repo == "~lan/notes"


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        ("pr:opened", "pr_opened"),
        ("pr:modified", "pr_updated"),
        ("pr:from_ref_updated", "pr_updated"),
        ("pr:merged", "pr_merged"),
        ("pr:declined", "pr_declined"),
        ("pr:deleted", "pr_declined"),
        ("pr:comment:added", "pr_comment"),
        ("pr:reviewer:approved", "other"),
        ("repo:modified", "other"),
    ],
)
def test_webhook_event_keys(key: str, kind: str) -> None:
    adapter = server_adapter(Server())
    payload: dict[str, Any] = {"eventKey": key, "date": "2026-11-16T09:00:00+0700", "actor": user("Lan"), "pullRequest": pull()}
    if key == "pr:comment:added":
        payload |= {"comment": comment(62, "Lan", "I am a PR comment", 1762729200000), "commentParentId": 43}
    event = deliver(adapter, key, json.dumps(payload).encode())
    assert event.kind == kind
    if kind != "other":
        assert (event.repo, event.ref, event.commit, event.pr_id) == ("PAY/ledger", "t/pay-120", "b" * 40, 12)


def test_a_project_address_on_bitbucket_server_is_the_pushed_repository() -> None:
    for address in (
        "https://gits.tcbs.com.vn/scm/pay/ledger.git",
        "https://gits.tcbs.com.vn/projects/PAY/repos/ledger",
        "https://gits.tcbs.com.vn/projects/PAY/repos/ledger/browse",
        "https://host.test/bitbucket/projects/PAY/repos/ledger/",
        "ssh://git@gits.tcbs.com.vn:7999/pay/ledger.git",
    ):
        assert _same_repo(address, "PAY/ledger"), address
        assert not _same_repo(address, "PAY/edger") and not _same_repo(address, "CORE/ledger"), address
    assert _same_repo("https://gits.tcbs.com.vn/users/lan/repos/notes", "~lan/notes") and _same_repo("https://gits.tcbs.com.vn/scm/~lan/notes.git", "~lan/notes")
    assert not _same_repo("https://gits.tcbs.com.vn/users/lan/repos/notes", "lan/notes")
    assert _same_repo("https://github.com/acme/pay.git", "acme/pay") and _same_repo("git@bitbucket.org:acme/pay", "acme/pay"), "the other hosts as before"
    assert not _same_repo("https://gits.tcbs.com.vn/projects/PAY/repos/ledger", "") and not _same_repo(None, "PAY/ledger")


# ---- health --------------------------------------------------------------------------------------------------------


async def test_health_tells_the_account_the_version_and_the_visible_projects() -> None:
    server = Server()
    health = await server_adapter(server).test()
    assert (health.state, health.identity, health.scopes) == ("ok", "svc-office", ["project:PAY", "project:~lan"])
    assert "8.19.1" in health.detail and "PAY" in health.detail and TOKEN not in health.detail
    server.username = ""  # the header is not documented: without it the configured user name stands in, else nobody
    assert (await server_adapter(server).test()).identity is None
    assert (await server_adapter(server, {"token": TOKEN, "username": "lan"}).test()).identity == "lan"


async def test_health_names_the_projects_the_token_cannot_see() -> None:
    server = Server()
    server.denied.add("PAY")
    some = await server_adapter(server, projects=["PAY", "~lan", "NOPE"]).test()
    assert some.state == "degraded" and "Token không có quyền với project PAY, NOPE" in some.detail and "Đọc được project ~lan" in some.detail
    none = await server_adapter(server, projects=["PAY"]).test()
    assert none.state == "error" and "Token không có quyền với project PAY" in none.detail and "hết hạn" in none.detail


async def test_health_tells_a_bad_token_from_a_wrong_address() -> None:
    server = Server()
    expired = await server_adapter(server, {"token": "expired-token"}).test()
    assert expired.state == "error" and "401" in expired.detail and "token" in expired.detail.lower() and "expired-token" not in expired.detail
    lost = await server_adapter(Server(context="/bitbucket"), base_url=BASE).test()  # the context path is missing from base_url
    assert lost.state == "error" and "base_url" in lost.detail and f"{BASE}/rest/api/1.0" in lost.detail
    server.extra.clear()
    not_bitbucket = BitbucketServer(
        BitbucketServerConfig(base_url=BASE, projects=["PAY"]), SECRET,
        transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>Đăng nhập SSO</html>")),
    )  # fmt: skip
    page = await not_bitbucket.test()
    assert page.state == "error" and "base_url" in page.detail
    forbidden = BitbucketServer(BitbucketServerConfig(base_url=BASE, projects=["PAY"]), SECRET, transport=httpx.MockTransport(lambda request: httpx.Response(403)))
    assert "403" in (await forbidden.test()).detail

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    offline = BitbucketServer(BitbucketServerConfig(base_url=BASE, projects=["PAY"]), SECRET, transport=httpx.MockTransport(down))

    async def no_sleep(_seconds: float) -> None:
        return None

    offline.http.sleep = no_sleep
    unreachable = await offline.test()
    assert unreachable.state == "error" and "lỗi mạng" in unreachable.detail


# ---- end to end: the office indexes a repository through this adapter's API, and follows its pushes -----------------


def test_the_office_indexes_a_repository_through_the_api_when_cloning_is_off(web) -> None:  # noqa: F811
    office, c, team, _people, _project = web
    server = Server(page=3)
    server.projects = {"SHOP": ["shop"]}
    server.commits = {"c1": {p: t.encode() for p, t in PY_REPO.items()}}
    server.branches = {"main": "c1"}

    class Wired(BitbucketServer):
        def __init__(self, config: BitbucketServerConfig, secret: dict[str, str]) -> None:
            super().__init__(config, secret, transport=server.transport)

    registry.register("bitbucket_server", Wired)
    try:

        async def go():
            spec = ConnectionSpec(id="tcbs", use="bitbucket_server", scope=f"team:{team.id}", config={"base_url": BASE, "projects": ["SHOP"], "clone": False})
            await office.connections.save(spec, secret={"token": TOKEN})
            repo = await office.codeintel.remote_repo("SHOP/shop", Scope(team_id=team.id))
            state = await office.codeintel.index(repo)
            hits = office.codeintel.query.search("LimitService", [repo.slug])
            piece = await office.codeintel.read(repo, "src/shop/limits.py", lines=(3, 3))
            first = len(server.paths("/raw/"))
            server.commits["c2"] = {**server.commits["c1"], "src/shop/limits.py": PY_REPO["src/shop/limits.py"].replace("MAX_PER_DAY = 5", "MAX_PER_DAY = 9").encode()}
            server.branches["main"] = "c2"
            server.requests.clear()
            again = await office.codeintel.index(repo)
            return repo, state, hits, piece, first, again, [r.slug for r in await office.codeintel.all_repos()]

        repo, state, hits, piece, first, again, swept = c.portal.call(go)
        assert state.state == "ready" and state.indexed_commit == "c1" and state.files >= 4 and hits and hits[0].path == "src/shop/limits.py"
        assert "MAX_PER_DAY = 5" in piece["text"] and piece["commit"] == "c1"
        assert again.state == "ready" and again.indexed_commit == "c2"
        assert [p.rpartition("/raw/")[2] for p in server.paths("/raw/")] == ["src/shop/limits.py"], "only the file that changed is read again"
        assert first > 1 and repo.slug == "tcbs/shop/shop" and repo.slug in swept
        assert not (office.settings.data_dir / "repos").exists(), "nothing was cloned"
    finally:
        registry.unregister("bitbucket_server")


def test_a_signed_push_from_bitbucket_server_brings_the_project_index_up_to_date(web) -> None:  # noqa: F811
    office, c, team, _people, project = web

    async def connect():
        spec = ConnectionSpec(id="tcbs", use="bitbucket_server", config={"base_url": "https://gits.tcbs.com.vn", "projects": ["SHOP"]})
        await office.connections.save(spec, secret={"token": TOKEN, "webhook_secret": HOOK_SECRET})
        await office.projects.configure(project.id, repo_url="https://gits.tcbs.com.vn/projects/SHOP/repos/shop/browse")

    c.portal.call(connect)
    assert c.get(f"/api/map?team={team.id}").status_code == 200  # indexed once
    body = json.dumps({"eventKey": "repo:refs_changed", "repository": repository("SHOP", "shop"), "changes": [change("refs/heads/main", "a" * 40)]}).encode()
    headers = {"X-Event-Key": "repo:refs_changed", "X-Hub-Signature": signed(body), "Content-Type": "application/json", "Host": "office.acme.test"}
    answer = c.post("/hooks/tcbs", content=body, headers=headers)
    assert answer.status_code == 200 and answer.json() == {"ok": True, "kind": "push"}
    c.portal.call(office.webhooks.wait)
    (event,) = c.get("/api/hooks").json()
    assert event["payload"]["repo"] == "SHOP/shop" and event["payload"]["ref"] == "main" and "indexed shop" in event["result"]
    assert c.post("/hooks/tcbs", content=body, headers={**headers, "X-Hub-Signature": signed(body, "wrong")}).status_code == 401
