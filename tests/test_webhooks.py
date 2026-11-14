"""Deliveries from code hosts, the index kept fresh in the background, and a repository read through the API when it
must not be cloned."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from typing import Any, ClassVar

from pydantic import BaseModel

from actoroffice.adapters import registry
from actoroffice.ports.code import RepoRef, TreeEntry, WebhookEvent
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Health

from .seed import IDENT, git
from .test_codeintel import PY_REPO
from .test_docweb import web  # noqa: F401  (fixture)

SECRET = "hook-secret-0123456789"


def gh_push(repo: str, branch: str = "main", secret: str = SECRET) -> tuple[dict[str, str], bytes]:
    body = json.dumps({"ref": f"refs/heads/{branch}", "after": "a" * 40, "repository": {"full_name": repo}}).encode()
    sign = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"X-GitHub-Event": "push", "X-Hub-Signature-256": sign, "Content-Type": "application/json", "Host": "office.acme.test"}, body


def connect_github(office, c) -> None:
    async def go():
        await office.connections.save(ConnectionSpec(id="gh", use="github", config={"org": "acme"}), secret={"token": "ghp_x", "webhook_secret": SECRET})

    c.portal.call(go)


def test_a_signed_push_brings_the_index_up_to_date(web) -> None:
    office, c, team, _people, project = web
    connect_github(office, c)
    c.portal.call(lambda: office.projects.configure(project.id, repo_url="https://github.com/acme/shop.git"))
    assert c.get(f"/api/map?team={team.id}").status_code == 200  # indexed at the first commit
    before = c.get(f"/api/index?team={team.id}").json()[0]["indexed_commit"]
    with open(f"{project.path}/src/shop/extra.py", "w", encoding="utf-8") as f:
        f.write("def extra() -> int:\n    return 1\n")
    git(project.path, "add", "-A")
    git(project.path, *IDENT, "commit", "-qm", "extra")

    headers, body = gh_push("acme/shop")
    answer = c.post("/hooks/gh", content=body, headers=headers)  # another host name, no cookie, no office header
    assert answer.status_code == 200 and answer.json() == {"ok": True, "kind": "push"}
    c.portal.call(office.webhooks.wait)
    after = c.get(f"/api/index?team={team.id}").json()[0]["indexed_commit"]
    assert after != before and after == git(project.path, "rev-parse", "HEAD")
    (event,) = c.get("/api/hooks").json()
    assert event["kind"] == "push" and event["payload"]["repo"] == "acme/shop" and "indexed shop" in event["result"] and "full_name" not in json.dumps(event)

    headers, body = gh_push("acme/shop", branch="feature/x")
    assert c.post("/hooks/gh", content=body, headers=headers).status_code == 200
    c.portal.call(office.webhooks.wait)
    assert "no repository" in c.get("/api/hooks").json()[0]["result"], "a push to another branch does not touch the index"


def test_a_delivery_that_is_not_signed_by_the_connection_is_refused(web) -> None:
    office, c, _team, _people, _project = web
    connect_github(office, c)
    headers, body = gh_push("acme/shop", secret="somebody-else")
    assert c.post("/hooks/gh", content=body, headers=headers).status_code == 401
    headers, body = gh_push("acme/shop")
    assert c.post("/hooks/nope", content=body, headers=headers).status_code == 401, "an unknown connection says no more than a bad signature"
    assert c.post("/hooks/gh", content=body + b" ", headers=headers).status_code == 401, "a body changed on the way"
    assert c.get("/api/hooks").json() == []
    assert c.get("/api/hooks", headers={"Host": "office.acme.test"}).status_code == 403, "only /hooks/ is open to other hosts"

    async def audited():
        return await office.db.fetchall("SELECT outcome FROM audit_log WHERE action = 'webhook'")

    assert len(c.portal.call(audited)) == 3


def test_the_background_pass_indexes_every_repository(web) -> None:
    office, c, team, _people, project = web

    async def go():
        assert [r.slug for r in await office.codeintel.all_repos()] == ["shop"]
        first = await office.codeintel.sweep()
        with open(f"{project.path}/README.md", "a", encoding="utf-8") as f:
            f.write("\nmore\n")
        git(project.path, *IDENT, "commit", "-qam", "readme")
        second = await office.codeintel.sweep()
        third = await office.codeintel.sweep()
        return first, second, third

    first, second, third = c.portal.call(go)
    assert first[0].state == "ready" and second[0].indexed_commit != first[0].indexed_commit and third[0].indexed_commit == second[0].indexed_commit

    async def loop_once():
        office.settings.index_sweep_s = 0.01
        task = asyncio.create_task(office.codeintel.run_forever(0.01))
        await asyncio.sleep(0.2)
        task.cancel()
        return (await office.codeintel.status(await office.codeintel.all_repos()))[0].state

    assert c.portal.call(loop_once) == "ready"


# ---- a repository that must not be cloned ------------------------------------------------------------------------------


class NoCloneConfig(BaseModel):
    pass


class NoCloneHost:
    """A code host that serves PY_REPO through its API and refuses to be cloned."""

    name: ClassVar[str] = "noclone"
    title: ClassVar[str] = "No clone"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CODE_READ})
    config_schema: ClassVar[type[BaseModel]] = NoCloneConfig
    secret_fields: ClassVar[tuple[str, ...]] = ()
    files: ClassVar[dict[str, str]] = dict(PY_REPO)
    reads: ClassVar[list[str]] = []
    rev: ClassVar[str] = "c1"

    def __init__(self, config: BaseModel, secret: dict[str, str]) -> None:
        pass

    async def test(self) -> Health:
        return Health(state="ok")

    async def close(self) -> None:
        pass

    async def head(self, repo: RepoRef, branch: str | None = None) -> str:
        return self.rev

    async def tree(self, repo: RepoRef, at: str) -> list[TreeEntry]:
        return [TreeEntry(path=p, blob_sha=hashlib.sha1(t.encode()).hexdigest(), size=len(t)) for p, t in self.files.items()]

    async def file(self, repo: RepoRef, path: str, at: str) -> bytes:
        if path not in self.files:
            raise ConnectorError("not_found", f"không có {path}")
        self.reads.append(path)
        return self.files[path].encode()

    async def clone_auth(self, repo: RepoRef) -> Any:
        return None

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        return None


def test_a_repository_that_must_not_be_cloned_is_indexed_through_the_api(web) -> None:
    office, c, team, _people, _project = web
    registry.register("noclone", NoCloneHost)
    NoCloneHost.files, NoCloneHost.reads, NoCloneHost.rev = dict(PY_REPO), [], "c1"
    try:

        async def go():
            from actoroffice.ports.connection import Scope

            await office.connections.save(ConnectionSpec(id="vault", use="noclone", scope=f"team:{team.id}"))
            repo = await office.codeintel.remote_repo("acme/shop", Scope(team_id=team.id))
            state = await office.codeintel.index(repo)
            hits = office.codeintel.query.search("LimitService", [repo.slug])
            piece = await office.codeintel.read(repo, "src/shop/limits.py", lines=(3, 3))
            first = len(NoCloneHost.reads)
            NoCloneHost.files["src/shop/limits.py"] = PY_REPO["src/shop/limits.py"].replace("MAX_PER_DAY = 5", "MAX_PER_DAY = 9")
            NoCloneHost.rev = "c2"
            NoCloneHost.reads.clear()
            again = await office.codeintel.index(repo)
            swept = [r.slug for r in await office.codeintel.all_repos()]
            return repo, state, hits, piece, first, again, swept

        repo, state, hits, piece, first, again, swept = c.portal.call(go)
        assert state.state == "ready" and state.indexed_commit == "c1" and state.files >= 4 and hits and hits[0].path == "src/shop/limits.py"
        assert "MAX_PER_DAY = 5" in piece["text"] and piece["commit"] == "c1"
        assert again.indexed_commit == "c2" and NoCloneHost.reads == ["src/shop/limits.py"], "only the file that changed is asked for again"
        assert first > 1 and repo.slug in swept, "a repository indexed from a code host is kept fresh by the background pass"
        assert not (office.settings.data_dir / "repos").exists(), "nothing was cloned"
    finally:
        registry.unregister("noclone")
