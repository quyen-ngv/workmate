"""Code host adapters: Bitbucket Cloud and GitHub. No network: every adapter gets an `httpx.MockTransport`."""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import httpx
import pytest

from actoroffice.adapters.bitbucket_cloud import BitbucketCloud, BitbucketCloudConfig
from actoroffice.adapters.github import GitHub, GitHubConfig
from actoroffice.ports.code import ChangedFile, CloneAuth, RepoRef, TreeEntry
from actoroffice.ports.connection import Capability, ConnectorError


class Fake:
    """Routes by (method, path). A value is a callable(request) → Response, a Response, or JSON for a 200."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        value = self.routes.get((request.method, request.url.path))
        if value is None:
            return httpx.Response(404, json={"message": "no route"})
        if callable(value):
            value = value(request)
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def body(self, method: str, path: str) -> Any:
        return next(json.loads(r.content) for r in self.requests if r.method == method and r.url.path == path)


def signed(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def no_wait(adapter: BitbucketCloud | GitHub) -> list[float]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    adapter.http.sleep = sleep
    return sleeps


# ---- Bitbucket Cloud ---------------------------------------------------------------------------------------

BB = "/2.0/repositories/acme"
BB_SECRET = {"email": "bot@acme.test", "token": "bb-tok", "webhook_secret": "hook-secret"}
PAYMENTS = RepoRef(slug="acme/payments")
BB_PR = {
    "id": 7,
    "title": "Hoàn tiền",
    "state": "OPEN",
    "links": {"html": {"href": "https://bitbucket.org/acme/payments/pull-requests/7"}},
    "source": {"branch": {"name": "t/pay-120"}},
    "destination": {"branch": {"name": "main"}},
    "author": {"display_name": "Bot"},
}
BB_COMMENTS = {
    "values": [
        {"id": 1, "user": {"display_name": "Lan"}, "content": {"raw": "Đổi tên biến"}, "inline": {"path": "a.py", "to": 12}, "created_on": "2026-11-10T01:00:00Z"},
        {"id": 2, "user": {"display_name": "Bot"}, "content": {"raw": "Đã đổi"}, "parent": {"id": 1}, "created_on": "2026-11-10T02:00:00Z"},
        {"id": 3, "deleted": True, "content": {"raw": ""}},
    ]
}
BB_DIFF = {
    "values": [
        {"status": "modified", "old": {"path": "a.py"}, "new": {"path": "a.py"}},
        {"status": "renamed", "old": {"path": "b.py"}, "new": {"path": "c.py"}},
        {"status": "removed", "old": {"path": "d.py"}, "new": None},
        {"status": "added", "old": None, "new": {"path": "e.py"}},
    ]
}


def bitbucket(fake: Fake, secret: dict[str, str] | None = None, **config: Any) -> BitbucketCloud:
    merged = {"workspace": "acme", "repos": ["payments", "ledger-*"], **config}
    return BitbucketCloud(BitbucketCloudConfig(**merged), BB_SECRET if secret is None else secret, transport=fake.transport)


def bitbucket_fake() -> Fake:
    def repos(request: httpx.Request) -> dict[str, Any]:
        if request.url.params.get("page") == "2":
            return {"values": [{"slug": "ledger-api", "mainbranch": {"name": "develop"}}]}
        first = {"slug": "payments", "mainbranch": {"name": "main"}, "links": {"html": {"href": "https://bitbucket.org/acme/payments"}}}
        return {"values": [first, {"slug": "secret-tool"}], "next": "https://api.bitbucket.org/2.0/repositories/acme?page=2"}

    return Fake(
        {
            ("GET", BB): repos,
            ("GET", f"{BB}/payments/refs/branches/main"): {"target": {"hash": "abc123"}},
            ("GET", f"{BB}/payments/refs/branches/feature/x"): {"target": {"hash": "fff999"}},
            ("GET", f"{BB}/payments/src/abc123/"): {"values": [{"type": "commit_file", "path": "src/A.java", "size": 10}, {"type": "commit_directory", "path": "src"}]},
            ("GET", f"{BB}/payments/src/abc123/src/A.java"): httpx.Response(200, content=b"class A {}"),
            ("GET", f"{BB}/payments/diffstat/def456..abc123"): BB_DIFF,
            ("POST", f"{BB}/payments/pullrequests"): BB_PR,
            ("GET", f"{BB}/payments/pullrequests/7"): {**BB_PR, "state": "MERGED"},
            ("GET", f"{BB}/payments/pullrequests/7/comments"): BB_COMMENTS,
            ("POST", f"{BB}/payments/pullrequests/7/comments"): {"id": 9},
        }
    )


def test_bitbucket_declares_what_the_registry_needs() -> None:
    assert (BitbucketCloud.name, BitbucketCloud.config_schema, BitbucketCloud.secret_fields) == ("bitbucket_cloud", BitbucketCloudConfig, ("email", "token", "webhook_secret"))
    assert BitbucketCloud.capabilities == frozenset({Capability.CODE_READ, Capability.CODE_PR, Capability.WEBHOOK}) and BitbucketCloud.title
    assert BitbucketCloud.required_scopes == {
        Capability.CODE_READ: ["read:repository:bitbucket"],
        Capability.CODE_PR: ["read:pullrequest:bitbucket", "write:pullrequest:bitbucket"],
        Capability.WEBHOOK: [],
    }
    config = BitbucketCloudConfig(workspace="acme")
    assert (config.repos, config.write) == (["*"], False)
    with pytest.raises(ValueError):
        BitbucketCloudConfig(workspace="acme/../x")


async def test_bitbucket_reads_repositories_and_code() -> None:
    fake = bitbucket_fake()
    adapter = bitbucket(fake)

    repos = await adapter.repos()
    assert [(r.slug, r.default_branch, r.url) for r in repos] == [
        ("acme/payments", "main", "https://bitbucket.org/acme/payments"),
        ("acme/ledger-api", "develop", "https://bitbucket.org/acme/ledger-api"),
    ]
    await adapter.repos('pay"ments')
    assert fake.requests[-2].url.params["q"] == 'name ~ "pay ments"'

    assert await adapter.head(repos[0]) == "abc123"
    assert await adapter.head(PAYMENTS, "feature/x") == "fff999"
    assert await adapter.tree(PAYMENTS, "abc123") == [TreeEntry(path="src/A.java", blob_sha="", size=10)]
    assert await adapter.file(PAYMENTS, "src/A.java", "abc123") == b"class A {}"
    assert await adapter.compare(PAYMENTS, "abc123", "def456") == [
        ChangedFile(path="a.py", status="modified"),
        ChangedFile(path="c.py", status="renamed", old_path="b.py"),
        ChangedFile(path="d.py", status="removed"),
        ChangedFile(path="e.py", status="added"),
    ]
    assert await adapter.clone_auth(RepoRef(slug="payments")) == CloneAuth(url="https://bitbucket.org/acme/payments.git", username="x-bitbucket-api-token-auth", password="bb-tok")
    await adapter.close()

    assert {r.url.host for r in fake.requests} == {"api.bitbucket.org"}
    assert all(r.headers["authorization"].startswith("Basic ") for r in fake.requests)


async def test_bitbucket_pull_requests() -> None:
    fake = bitbucket_fake()
    adapter = bitbucket(fake, write=True)

    created = await adapter.pr_create(PAYMENTS, "t/pay-120", "main", "Hoàn tiền", "mô tả")
    assert (created.id, created.state, created.url, created.source, created.destination, created.author, created.merged) == (
        7,
        "open",
        "https://bitbucket.org/acme/payments/pull-requests/7",
        "t/pay-120",
        "main",
        "Bot",
        False,
    )
    sent = fake.body("POST", f"{BB}/payments/pullrequests")
    assert (sent["title"], sent["description"], sent["source"], sent["destination"]) == ("Hoàn tiền", "mô tả", {"branch": {"name": "t/pay-120"}}, {"branch": {"name": "main"}})

    got = await adapter.pr_get(PAYMENTS, 7)
    assert (got.state, got.merged) == ("merged", True)

    comments = await adapter.pr_comments(PAYMENTS, 7)
    assert [(c.id, c.author, c.body, c.path, c.line, c.parent_id) for c in comments] == [(1, "Lan", "Đổi tên biến", "a.py", 12, None), (2, "Bot", "Đã đổi", None, None, 1)]

    await adapter.pr_reply(PAYMENTS, 7, "Cảm ơn", parent_id=1)
    assert fake.body("POST", f"{BB}/payments/pullrequests/7/comments") == {"content": {"raw": "Cảm ơn"}, "parent": {"id": 1}}


async def test_bitbucket_writes_are_refused_when_read_only() -> None:
    fake = bitbucket_fake()
    adapter = bitbucket(fake)
    for call in (adapter.pr_create(PAYMENTS, "a", "main", "t", "b"), adapter.pr_reply(PAYMENTS, 7, "x")):
        with pytest.raises(ConnectorError) as err:
            await call
        assert err.value.kind == "forbidden"
    assert fake.requests == []


async def test_bitbucket_only_touches_allowed_repositories_and_its_own_host() -> None:
    fake = bitbucket_fake()
    adapter = bitbucket(fake)
    for slug, kind in (("other/payments", "forbidden"), ("acme/secret-tool", "forbidden"), ("acme/..", "invalid"), ("a/b/payments", "forbidden")):
        with pytest.raises(ConnectorError) as err:
            await adapter.head(RepoRef(slug=slug))
        assert err.value.kind == kind, slug
    with pytest.raises(ConnectorError) as err:
        await adapter.file(PAYMENTS, "../../other/src/x", "abc123")
    assert err.value.kind == "invalid" and fake.requests == []

    fake.routes[("GET", BB)] = {"values": [], "next": "https://evil.example/2.0/repositories/acme?page=2"}
    with pytest.raises(ConnectorError) as err:
        await adapter.repos()
    assert err.value.kind == "invalid" and {r.url.host for r in fake.requests} == {"api.bitbucket.org"}


def test_bitbucket_parse_pr_url() -> None:
    adapter = bitbucket(Fake())
    assert adapter.parse_pr_url("https://bitbucket.org/acme/payments/pull-requests/7/diff") == (RepoRef(slug="acme/payments", url="https://bitbucket.org/acme/payments"), 7)
    for url in (
        "https://bitbucket.org/other/payments/pull-requests/7",
        "https://bitbucket.org/acme/secret-tool/pull-requests/7",
        "https://evil.example/acme/payments/pull-requests/7",
        "https://bitbucket.org.evil.example/acme/payments/pull-requests/7",
        "http://bitbucket.org/acme/payments/pull-requests/7",
        "https://github.com/acme/payments/pull/7",
        "https://bitbucket.org/acme/payments/src/main",
        "không phải địa chỉ",
    ):
        assert adapter.parse_pr_url(url) is None, url


def test_bitbucket_webhook() -> None:
    adapter = bitbucket(Fake())
    push = json.dumps({"repository": {"full_name": "acme/payments"}, "push": {"changes": [{"new": {"type": "branch", "name": "main", "target": {"hash": "abc123"}}}]}}).encode()
    event = adapter.verify_webhook({"X-Event-Key": "repo:push", "X-Hub-Signature": signed("hook-secret", push)}, push)
    assert event is not None and (event.kind, event.repo, event.ref, event.commit, event.pr_id) == ("push", "acme/payments", "main", "abc123", None)

    pull = {"repository": {"full_name": "acme/payments"}, "pullrequest": {"id": 7, "source": {"branch": {"name": "t/pay-120"}, "commit": {"hash": "def"}}}}
    body = json.dumps(pull).encode()
    kinds = {"pullrequest:created": "pr_opened", "pullrequest:updated": "pr_updated", "pullrequest:fulfilled": "pr_merged", "pullrequest:rejected": "pr_declined"}
    kinds |= {"pullrequest:comment_created": "pr_comment", "repo:fork": "other"}
    for key, kind in kinds.items():
        event = adapter.verify_webhook({"x-event-key": key, "x-hub-signature": signed("hook-secret", body)}, body)
        assert event is not None and event.kind == kind, key
    assert event.raw == pull
    event = adapter.verify_webhook({"x-event-key": "pullrequest:created", "x-hub-signature": signed("hook-secret", body)}, body)
    assert event is not None and (event.pr_id, event.ref, event.commit) == (7, "t/pay-120", "def")

    assert adapter.verify_webhook({"X-Event-Key": "repo:push", "X-Hub-Signature": signed("wrong", push)}, push) is None
    assert adapter.verify_webhook({"X-Event-Key": "repo:push"}, push) is None
    assert adapter.verify_webhook({"X-Event-Key": "repo:push", "X-Hub-Signature": signed("hook-secret", push)}, push + b" ") is None
    unsigned = bitbucket(Fake(), secret={"email": "e", "token": "t"})
    assert unsigned.verify_webhook({"X-Event-Key": "repo:push", "X-Hub-Signature": signed("", push)}, push) is None


VENDOR_SECRET = "It\u0027s a Secret to Everybody"  # the secret both vendors use in their worked example


def test_bitbucket_webhook_signature_matches_the_vendor_example_and_push_shapes() -> None:
    # support.atlassian.com/bitbucket-cloud/docs/manage-webhooks: this secret and this payload give this header value.
    assert signed(VENDOR_SECRET, b"Hello World!") == "sha256=a4771c39fbe90f317c7824e83ddef3caae9cb3d976c214ace1f2937e133263c9"

    adapter = bitbucket(Fake())

    def deliver(changes: list[dict[str, Any]]) -> Any:
        body = json.dumps({"repository": {"full_name": "acme/payments"}, "push": {"changes": changes}}).encode()
        headers = {"X-Event-Key": "repo:push", "X-Hook-UUID": "h", "X-Request-UUID": "r", "X-Attempt-Number": "1", "X-Hub-Signature": signed("hook-secret", body)}
        return adapter.verify_webhook(headers, body)

    tag = {"new": {"type": "tag", "name": "v1", "target": {"hash": "aaa"}}, "old": None}
    branch = {"new": {"type": "branch", "name": "main", "target": {"type": "commit", "hash": "bbb"}}, "old": {"type": "branch", "name": "main", "target": {"hash": "000"}}}
    deleted = {"new": None, "old": {"type": "branch", "name": "gone", "target": {"hash": "ccc"}}}
    event = deliver([tag, deleted, branch])  # one push can move several references: the branch is the one reported
    assert (event.kind, event.ref, event.commit) == ("push", "main", "bbb")
    assert deliver([tag]).kind == "other" and deliver([deleted]).kind == "other"


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (403, "forbidden"), (404, "not_found"), (400, "invalid"), (500, "network")])
async def test_bitbucket_error_kinds(status: int, kind: str) -> None:
    fake = Fake({("GET", f"{BB}/payments/pullrequests/7"): httpx.Response(status, text="bb-tok leaked body")})
    adapter = bitbucket(fake)
    sleeps = no_wait(adapter)
    with pytest.raises(ConnectorError) as err:
        await adapter.pr_get(PAYMENTS, 7)
    assert err.value.kind == kind and "bb-tok" not in str(err.value) and "leaked" not in str(err.value)
    assert len(sleeps) == (3 if status == 500 else 0)


async def test_bitbucket_retries_after_429_and_reports_health() -> None:
    answers = [httpx.Response(429, headers={"Retry-After": "4"}), httpx.Response(200, json={"target": {"hash": "abc123"}})]
    fake = bitbucket_fake()
    fake.routes[("GET", f"{BB}/payments/refs/branches/main")] = lambda request: answers.pop(0)
    adapter = bitbucket(fake)
    sleeps = no_wait(adapter)
    assert await adapter.head(PAYMENTS) == "abc123" and sleeps == [4.0]

    health = await adapter.test()
    assert (health.state, health.identity) == ("ok", "bot@acme.test")
    assert int(fake.requests[-1].url.params["pagelen"]) >= 10  # the reference gives 10 as the smallest page

    fake = bitbucket_fake()
    bare = bitbucket(fake, secret={})
    assert (await bare.test()).state == "unconfigured" and await bare.clone_auth(PAYMENTS) is None
    with pytest.raises(ConnectorError) as err:
        await bare.repos()
    assert err.value.kind == "unconfigured" and fake.requests == []


# ---- GitHub ------------------------------------------------------------------------------------------------

GH = "/repos/acme/payments"
GH_SECRET = {"token": "gh-tok", "webhook_secret": "hook-secret"}
GH_PR = {
    "number": 12,
    "title": "Hoàn tiền",
    "state": "open",
    "html_url": "https://github.com/acme/payments/pull/12",
    "head": {"ref": "t/pay-120", "sha": "def"},
    "base": {"ref": "main"},
    "user": {"login": "bot"},
    "merged": False,
}
GH_FILES = {
    "files": [
        {"filename": "a.py", "status": "modified"},
        {"filename": "c.py", "status": "renamed", "previous_filename": "b.py"},
        {"filename": "d.py", "status": "removed"},
        {"filename": "e.py", "status": "added"},
        {"filename": "f.py", "status": "copied"},
    ]
}


def github(fake: Fake, secret: dict[str, str] | None = None, **config: Any) -> GitHub:
    return GitHub(GitHubConfig(**{"org": "acme", "repos": ["pay*"], **config}), GH_SECRET if secret is None else secret, transport=fake.transport)


def github_fake(prefix: str = "") -> Fake:
    return Fake(
        {
            ("GET", f"{prefix}/orgs/acme/repos"): [{"name": "payments", "default_branch": "main"}, {"name": "infra", "default_branch": "master"}, {"name": "pay-web", "default_branch": "dev"}],
            ("GET", f"{prefix}{GH}/branches/main"): {"commit": {"sha": "abc"}},
            ("GET", f"{prefix}{GH}/git/trees/abc"): {"tree": [{"path": "a.py", "type": "blob", "sha": "s1", "size": 3}, {"path": "dir", "type": "tree", "sha": "s2"}]},
            ("GET", f"{prefix}{GH}/contents/src/a.py"): httpx.Response(200, content=b"x = 1\n"),
            ("GET", f"{prefix}{GH}/compare/abc...def"): GH_FILES,
            ("POST", f"{prefix}{GH}/pulls"): GH_PR,
            ("GET", f"{prefix}{GH}/pulls/12"): {**GH_PR, "state": "closed", "merged": True},
            ("GET", f"{prefix}{GH}/pulls/13"): {**GH_PR, "number": 13, "state": "closed"},
            ("GET", f"{prefix}{GH}/pulls/12/comments"): [
                {"id": 2, "user": {"login": "bot"}, "body": "Đã đổi", "path": "a.py", "line": 12, "in_reply_to_id": 1, "created_at": "2026-11-10T03:00:00Z"},
                {"id": 1, "user": {"login": "lan"}, "body": "Đổi tên biến", "path": "a.py", "line": 12, "created_at": "2026-11-10T01:00:00Z"},
            ],
            ("GET", f"{prefix}{GH}/issues/12/comments"): [{"id": 5, "user": {"login": "minh"}, "body": "LGTM", "created_at": "2026-11-10T02:00:00Z"}],
            ("GET", f"{prefix}{GH}/pulls/comments/1"): {"id": 1, "user": {"login": "lan"}, "body": "Đổi tên biến", "path": "a.py", "line": 12},
            ("GET", f"{prefix}{GH}/pulls/comments/2"): {"id": 2, "user": {"login": "bot"}, "body": "Đã đổi", "path": "a.py", "line": 12, "in_reply_to_id": 1},
            ("POST", f"{prefix}{GH}/pulls/12/comments/1/replies"): {"id": 6},
            ("POST", f"{prefix}{GH}/issues/12/comments"): {"id": 7},
            ("GET", f"{prefix}/user"): httpx.Response(200, json={"login": "bot"}, headers={"X-OAuth-Scopes": "repo, read:org", "X-RateLimit-Remaining": "4990", "X-RateLimit-Limit": "5000"}),
        }
    )


def test_github_declares_what_the_registry_needs() -> None:
    assert (GitHub.name, GitHub.config_schema, GitHub.secret_fields) == ("github", GitHubConfig, ("token", "webhook_secret"))
    assert GitHub.capabilities == frozenset({Capability.CODE_READ, Capability.CODE_PR, Capability.WEBHOOK}) and GitHub.title
    assert GitHub.required_scopes == {Capability.CODE_READ: ["contents:read", "metadata:read"], Capability.CODE_PR: ["pull_requests:read", "pull_requests:write"], Capability.WEBHOOK: []}
    config = GitHubConfig(org="acme")
    assert (config.base_url, config.repos, config.auth, config.write) == ("https://api.github.com", ["*"], "token", False)
    for bad in ({"org": "acme", "base_url": "http://api.github.com"}, {"org": "acme/x"}, {"org": "acme", "auth": "magic"}):
        with pytest.raises(ValueError):
            GitHubConfig(**bad)


async def test_github_reads_repositories_and_code() -> None:
    fake = github_fake()
    adapter = github(fake)

    repos = await adapter.repos()
    assert [(r.slug, r.default_branch, r.url) for r in repos] == [("acme/payments", "main", "https://github.com/acme/payments"), ("acme/pay-web", "dev", "https://github.com/acme/pay-web")]
    assert [r.slug for r in await adapter.repos("WEB")] == ["acme/pay-web"]

    assert await adapter.head(repos[0]) == "abc"
    assert await adapter.tree(repos[0], "abc") == [TreeEntry(path="a.py", blob_sha="s1", size=3)]
    assert await adapter.file(repos[0], "src/a.py", "abc") == b"x = 1\n"
    assert (fake.requests[-1].headers["accept"], fake.requests[-1].url.params["ref"]) == ("application/vnd.github.raw+json", "abc")
    assert await adapter.compare(repos[0], "abc", "def") == [
        ChangedFile(path="a.py", status="modified"),
        ChangedFile(path="c.py", status="renamed", old_path="b.py"),
        ChangedFile(path="d.py", status="removed"),
        ChangedFile(path="e.py", status="added"),
        ChangedFile(path="f.py", status="added"),
    ]
    assert await adapter.clone_auth(repos[0]) == CloneAuth(url="https://github.com/acme/payments.git", username="x-access-token", password="gh-tok")
    await adapter.close()

    assert {r.url.host for r in fake.requests} == {"api.github.com"}
    assert all(r.headers["authorization"] == "Bearer gh-tok" and r.headers["x-github-api-version"] for r in fake.requests)


async def test_github_pull_requests() -> None:
    fake = github_fake()
    adapter = github(fake, write=True)
    repo = RepoRef(slug="acme/payments")

    created = await adapter.pr_create(repo, "t/pay-120", "main", "Hoàn tiền", "mô tả")
    assert (created.id, created.state, created.url, created.source, created.destination, created.author, created.merged) == (
        12,
        "open",
        "https://github.com/acme/payments/pull/12",
        "t/pay-120",
        "main",
        "bot",
        False,
    )
    assert fake.body("POST", f"{GH}/pulls") == {"title": "Hoàn tiền", "body": "mô tả", "head": "t/pay-120", "base": "main"}

    merged = await adapter.pr_get(repo, 12)
    declined = await adapter.pr_get(repo, 13)
    assert (merged.state, merged.merged, declined.state, declined.merged) == ("merged", True, "declined", False)

    comments = await adapter.pr_comments(repo, 12)
    assert [(c.id, c.author, c.path, c.line, c.parent_id) for c in comments] == [(1, "lan", "a.py", 12, None), (5, "minh", None, None, None), (2, "bot", "a.py", 12, 1)]

    await adapter.pr_reply(repo, 12, "Cảm ơn", parent_id=1)
    await adapter.pr_reply(repo, 12, "Xong")
    assert fake.body("POST", f"{GH}/pulls/12/comments/1/replies") == {"body": "Cảm ơn"}
    assert fake.body("POST", f"{GH}/issues/12/comments") == {"body": "Xong"}


async def test_github_reply_goes_to_the_first_comment_of_the_thread() -> None:
    # "This must be the ID of a top-level review comment, not a reply to that comment. Replies to replies are not supported."
    fake = github_fake()
    adapter = github(fake, write=True)
    repo = RepoRef(slug="acme/payments")
    await adapter.pr_reply(repo, 12, "Trả lời một câu trả lời", parent_id=2)
    assert [r.url.path for r in fake.requests] == [f"{GH}/pulls/comments/2", f"{GH}/pulls/12/comments/1/replies"]

    await adapter.pr_reply(repo, 12, "Trả lời bình luận chung", parent_id=5)  # 5 is a conversation comment: it has no thread
    assert [r.url.path for r in fake.requests][2:] == [f"{GH}/pulls/comments/5", f"{GH}/issues/12/comments"]
    assert json.loads(fake.requests[-1].content) == {"body": "Trả lời bình luận chung"}


async def test_github_lists_the_repositories_of_a_user_account() -> None:
    # /orgs/{org}/repos is for organisations: for a user account GitHub answers 404; what the user owns is under /user/repos.
    mine = [{"name": "payments", "default_branch": "main", "private": True, "owner": {"login": "Acme"}}, {"name": "pay-fork", "default_branch": "main", "owner": {"login": "someone"}}]
    fake = Fake({("GET", "/orgs/acme/repos"): httpx.Response(404, json={"message": "Not Found"}), ("GET", "/user/repos"): mine})
    repos = await github(fake).repos()
    assert [r.slug for r in repos] == ["acme/payments"]
    assert fake.requests[-1].url.params["affiliation"] == "owner"


async def test_github_writes_are_refused_when_read_only_and_repositories_are_checked() -> None:
    fake = github_fake()
    adapter = github(fake)
    repo = RepoRef(slug="acme/payments")
    for call in (adapter.pr_create(repo, "a", "main", "t", "b"), adapter.pr_reply(repo, 12, "x")):
        with pytest.raises(ConnectorError) as err:
            await call
        assert err.value.kind == "forbidden"
    for slug, kind in (("other/payments", "forbidden"), ("acme/infra", "forbidden"), ("acme/pay ments", "invalid")):
        with pytest.raises(ConnectorError) as err:
            await adapter.head(RepoRef(slug=slug))
        assert err.value.kind == kind, slug
    with pytest.raises(ConnectorError) as err:
        await adapter.file(repo, "a/../../x", "abc")
    assert err.value.kind == "invalid" and fake.requests == []


async def test_github_enterprise_uses_its_own_host() -> None:
    fake = github_fake("/api/v3")
    adapter = github(fake, base_url="https://ghe.acme.test/api/v3/")
    repo = RepoRef(slug="acme/payments")
    assert await adapter.head(repo) == "abc"
    assert (fake.requests[-1].url.host, fake.requests[-1].url.path) == ("ghe.acme.test", "/api/v3/repos/acme/payments/branches/main")
    auth = await adapter.clone_auth(repo)
    assert auth is not None and auth.url == "https://ghe.acme.test/acme/payments.git"
    assert adapter.parse_pr_url("https://ghe.acme.test/acme/payments/pull/3") == (RepoRef(slug="acme/payments", url="https://ghe.acme.test/acme/payments"), 3)
    assert adapter.parse_pr_url("https://github.com/acme/payments/pull/3") is None


def test_github_parse_pr_url() -> None:
    adapter = github(Fake())
    assert adapter.parse_pr_url("https://github.com/Acme/payments/pull/12/files") == (RepoRef(slug="acme/payments", url="https://github.com/acme/payments"), 12)
    for url in (
        "https://github.com/other/payments/pull/12",
        "https://github.com/acme/infra/pull/12",
        "https://github.com.evil.example/acme/payments/pull/12",
        "http://github.com/acme/payments/pull/12",
        "https://bitbucket.org/acme/payments/pull-requests/7",
        "https://github.com/acme/payments/issues/12",
        "",
    ):
        assert adapter.parse_pr_url(url) is None, url


def test_github_webhook() -> None:
    adapter = github(Fake())
    repository = {"full_name": "acme/payments"}

    def deliver(event: str, payload: dict[str, Any], secret: str = "hook-secret") -> Any:
        body = json.dumps(payload).encode()
        return adapter.verify_webhook({"X-GitHub-Event": event, "X-Hub-Signature-256": signed(secret, body)}, body)

    push = deliver("push", {"ref": "refs/heads/main", "after": "abc", "repository": repository})
    assert (push.kind, push.repo, push.ref, push.commit, push.pr_id) == ("push", "acme/payments", "main", "abc", None)
    assert deliver("push", {"ref": "refs/tags/v1", "after": "abc", "repository": repository}).kind == "other"

    pull = {"number": 12, "merged": False, "head": {"ref": "t/pay-120", "sha": "def"}}
    table = {"opened": "pr_opened", "reopened": "pr_opened", "synchronize": "pr_updated", "closed": "pr_declined", "labeled": "other"}
    for action, kind in table.items():
        event = deliver("pull_request", {"action": action, "pull_request": pull, "repository": repository})
        assert (event.kind, event.pr_id, event.ref, event.commit) == (kind, 12, "t/pay-120", "def"), action
    assert deliver("pull_request", {"action": "closed", "pull_request": {**pull, "merged": True}, "repository": repository}).kind == "pr_merged"
    review = deliver("pull_request_review", {"action": "submitted", "pull_request": pull, "repository": repository})
    assert (review.kind, review.pr_id) == ("pr_comment", 12)
    on_line = deliver("pull_request_review_comment", {"action": "created", "comment": {"id": 2}, "pull_request": pull, "repository": repository})
    assert (on_line.kind, on_line.pr_id, on_line.ref) == ("pr_comment", 12, "t/pay-120")
    # A comment in the conversation of a pull request is an `issue_comment` whose issue carries `pull_request`.
    said = deliver("issue_comment", {"action": "created", "issue": {"number": 12, "pull_request": {"url": "https://api.github.com/repos/acme/payments/pulls/12"}}, "repository": repository})
    assert (said.kind, said.pr_id, said.repo) == ("pr_comment", 12, "acme/payments")
    assert deliver("issue_comment", {"action": "created", "issue": {"number": 4}, "repository": repository}).kind == "other"  # a plain issue
    deleted = deliver("push", {"ref": "refs/heads/gone", "after": "0000000000000000000000000000000000000000", "deleted": True, "repository": repository})
    assert deleted.kind == "other"
    # docs.github.com/en/webhooks/using-webhooks/validating-webhook-deliveries: this secret and this payload give this header value.
    assert signed(VENDOR_SECRET, b"Hello, World!") == "sha256=757107ea0eb2509fc211221cce984b8a37570b6d7586c22c46f4379c8b043e17"
    assert deliver("star", {"repository": repository}).kind == "other"

    assert deliver("push", {"ref": "refs/heads/main", "repository": repository}, secret="wrong") is None
    assert adapter.verify_webhook({"X-GitHub-Event": "push"}, b"{}") is None
    assert adapter.verify_webhook({"X-GitHub-Event": "push", "X-Hub-Signature-256": signed("hook-secret", b"not json")}, b"not json") is None
    unsigned = github(Fake(), secret={"token": "t"})
    assert unsigned.verify_webhook({"X-GitHub-Event": "push", "X-Hub-Signature-256": signed("", b"{}")}, b"{}") is None


@pytest.mark.parametrize(("status", "kind"), [(401, "auth"), (403, "forbidden"), (404, "not_found"), (422, "invalid"), (502, "network")])
async def test_github_error_kinds(status: int, kind: str) -> None:
    fake = Fake({("GET", f"{GH}/pulls/12"): httpx.Response(status, json={"message": "gh-tok leaked body"})})
    adapter = github(fake)
    no_wait(adapter)
    with pytest.raises(ConnectorError) as err:
        await adapter.pr_get(RepoRef(slug="acme/payments"), 12)
    assert err.value.kind == kind and "gh-tok" not in str(err.value) and "leaked" not in str(err.value)


async def test_github_rate_limit_retries_then_succeeds() -> None:
    answers = [httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}), httpx.Response(429, headers={"Retry-After": "5"}), httpx.Response(200, json={"commit": {"sha": "abc"}})]
    fake = Fake({("GET", f"{GH}/branches/main"): lambda request: answers.pop(0)})
    adapter = github(fake)
    sleeps = no_wait(adapter)
    assert await adapter.head(RepoRef(slug="acme/payments")) == "abc"
    assert len(sleeps) == 2 and sleeps[1] == 5.0


async def test_github_used_up_quota_is_reported_with_the_time_to_wait_and_not_retried() -> None:
    # "You should not retry your request until after the time specified by the x-ratelimit-reset header" (UTC epoch seconds).
    limited = httpx.Response(403, headers={"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "0", "x-ratelimit-reset": "1800003600", "x-ratelimit-resource": "core"})
    fake = Fake({("GET", f"{GH}/branches/main"): limited})
    adapter = github(fake)
    sleeps = no_wait(adapter)
    adapter.http.clock = lambda: 1_800_000_000.0
    with pytest.raises(ConnectorError) as err:
        await adapter.head(RepoRef(slug="acme/payments"))
    assert (err.value.kind, err.value.retry_after, len(fake.requests), sleeps) == ("rate_limited", 3600.0, 1, [])


async def test_github_health_missing_token_and_app_auth() -> None:
    health = await github(github_fake()).test()
    assert (health.state, health.identity, health.scopes, health.rate) == ("ok", "bot", ["repo", "read:org"], {"remaining": 4990, "limit": 5000})

    fake = github_fake()
    bare = github(fake, secret={})
    assert (await bare.test()).state == "unconfigured" and await bare.clone_auth(RepoRef(slug="acme/payments")) is None

    app = github(fake, auth="app")
    health = await app.test()
    assert health.state == "error" and "GitHub App" in health.detail
    with pytest.raises(ConnectorError) as err:
        await app.repos()
    assert err.value.kind == "invalid" and "GitHub App" in str(err.value) and fake.requests == []
