"""A project knows which repository on which code-host connection it is: the canonical slug, the merge button and the
`pr_linked` check, pushing with the connection's credential, indexing without a local folder, webhooks."""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar

import httpx
import pytest
from pydantic import BaseModel

from actoroffice.adapters import registry
from actoroffice.adapters.bitbucket_cloud import BitbucketCloud, BitbucketCloudConfig
from actoroffice.adapters.bitbucket_server import BitbucketServer, BitbucketServerConfig
from actoroffice.adapters.github import GitHub, GitHubConfig
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import ProjectAccess, TicketStatus
from actoroffice.ports.code import CloneAuth, PrComment, PullRequest, RepoRef, TreeEntry, WebhookEvent
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Health, Scope
from actoroffice.services.atlassian import code_hosts, has_code_host
from actoroffice.services.checks import check_pr_linked
from actoroffice.services.projects import ProjectError
from actoroffice.services.webhooks import _is_project_repo

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db
from .test_merge import at_pr, commit
from .test_web import HEADERS, client, login  # noqa: F401  (fixture re-export)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

TOKEN = "bbs-token-5f1c9a7e2d"
TCBS = "https://gits.tcbs.com.vn"
IDENT = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


class Config(BaseModel):
    root: str = ""  # the folder that holds this "server"'s bare repositories
    write: bool = False


class FakeHost:
    """A code host at https://code.test with one owner, ACME. Its repositories are bare repositories in `root`; the
    repository `secret` is outside the connection's allow-list; cloning is not allowed (the index reads the API)."""

    name: ClassVar[str] = "fake_host"
    title: ClassVar[str] = "Code host giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CODE_READ, Capability.CODE_PR, Capability.WEBHOOK})
    config_schema: ClassVar[type[BaseModel]] = Config
    secret_fields: ClassVar[tuple[str, ...]] = ("token",)
    created: ClassVar[list[tuple[str, str, str]]] = []
    files: ClassVar[dict[str, bytes]] = {"src/pay.py": b"def pay() -> int:\n    return 1\n"}
    head_commit: ClassVar[str] = "c" * 40

    def __init__(self, config: Config, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config = config
        self._token = secret.get("token", "")

    def repo_ref(self, text: str) -> RepoRef | None:
        text = text.strip()
        if "://" not in text and text.endswith(".git"):  # a bare repository of this "server"
            return RepoRef(slug=f"ACME/{Path(text).stem}")
        m = re.fullmatch(r"(?:https://code\.test/)?acme/([\w-]+?)(?:\.git)?(?:/.*)?", text, re.IGNORECASE)
        return RepoRef(slug=f"ACME/{m.group(1)}") if m else None

    def _name(self, repo: RepoRef) -> str:
        name = repo.slug.partition("/")[2]
        if name == "secret":
            raise ConnectorError("forbidden", "repo «ACME/secret» nằm ngoài các repo được phép của kết nối")
        return name

    async def git_auth(self, repo: RepoRef, *, write: bool) -> CloneAuth | None:
        if write and not self.config.write:
            raise ConnectorError("forbidden", "kết nối đang ở chế độ chỉ đọc")
        return CloneAuth(url=str(Path(self.config.root) / f"{self._name(repo)}.git"), bearer=self._token)

    async def clone_auth(self, repo: RepoRef) -> CloneAuth | None:
        return None

    async def repos(self, query: str | None = None) -> list[RepoRef]:
        return [RepoRef(slug="ACME/shop", default_branch="develop")]

    async def head(self, repo: RepoRef, branch: str | None = None) -> str:
        self._name(repo)
        return FakeHost.head_commit

    async def tree(self, repo: RepoRef, at: str) -> list[TreeEntry]:
        return [TreeEntry(path=p, blob_sha=f"b{i}", size=len(b)) for i, (p, b) in enumerate(FakeHost.files.items())]

    async def file(self, repo: RepoRef, path: str, at: str) -> bytes:
        return FakeHost.files[path]

    async def pr_create(self, repo: RepoRef, source: str, destination: str, title: str, body: str) -> PullRequest:
        FakeHost.created.append((repo.slug, source, destination))
        return PullRequest(id=7, title=title, state="open", url=f"https://code.test/{repo.slug}/pull/7", source=source, destination=destination)

    async def pr_get(self, repo: RepoRef, pr_id: int) -> PullRequest:
        return PullRequest(id=pr_id, title="Hoàn tiền", state="open")

    async def pr_comments(self, repo: RepoRef, pr_id: int) -> list[PrComment]:
        return []

    def parse_pr_url(self, url: str) -> tuple[RepoRef, int] | None:
        m = re.match(r"^https://code\.test/(ACME/[\w-]+)/pull/(\d+)", url)
        return (RepoRef(slug=m.group(1)), int(m.group(2))) if m else None

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> WebhookEvent | None:
        data = json.loads(body)
        return WebhookEvent(kind="push", repo=data["repo"], ref=data["ref"], commit=data.get("commit", ""))

    async def test(self) -> Health:
        return Health(state="ok")

    async def close(self) -> None:
        return None


@pytest.fixture(autouse=True)
def fake_host():
    registry.register("fake_host", FakeHost)
    FakeHost.created.clear()
    FakeHost.head_commit = "c" * 40
    yield
    registry.unregister("fake_host")


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    yield o
    await o.close()


async def connect(office: Office, tmp_path: Path, *, write: bool = True, connection: str = "code") -> Path:
    """A connection to the fake host; returns the folder of its bare repositories."""
    root = tmp_path / "server"
    root.mkdir(exist_ok=True)
    await office.connections.save(ConnectionSpec(id=connection, use="fake_host", config={"root": str(root), "write": write}, write=write), secret={"token": TOKEN})
    return root


async def connect_tcbs(office: Office) -> None:
    spec = ConnectionSpec(id="tcbs", use="bitbucket_server", config={"base_url": TCBS, "projects": ["ICONNECT"]})
    await office.connections.save(spec, secret={"token": TOKEN})
    office.projects._index_soon = lambda project: None  # indexing would clone from the real address: not in a test


def local_repo(path: Path, *, origin: Path | None = None) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("shop\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, *IDENT, "commit", "-q", "-m", "init")
    if origin is not None:
        git(path, "remote", "add", "origin", str(origin))
    return path


async def index_tasks(office: Office) -> None:
    await asyncio.gather(*[t for t in office._tasks if t.get_name().startswith("index-")])


# ---- P2: one way to name a repository, per adapter ---------------------------------------------------------------


def no_calls(request: httpx.Request) -> httpx.Response:
    raise AssertionError(f"repo_ref must not ask the server: {request.url}")


def server(base: str = TCBS, **config: Any) -> BitbucketServer:
    return BitbucketServer(BitbucketServerConfig(base_url=base, projects=["ICONNECT", "~lan"], **config), {"token": TOKEN}, transport=httpx.MockTransport(no_calls))


def test_bitbucket_server_takes_the_slug_the_web_links_and_the_clone_links() -> None:
    adapter = server()
    for text in (
        "ICONNECT/tcbs-bond-trading",
        "iconnect/tcbs-bond-trading",
        f"{TCBS}/projects/ICONNECT/repos/tcbs-bond-trading",
        f"{TCBS}/projects/ICONNECT/repos/tcbs-bond-trading/browse/src/main?at=refs%2Fheads%2Fdevelop",
        f"{TCBS}/scm/iconnect/tcbs-bond-trading.git",
        "ssh://git@gits.tcbs.com.vn:7999/iconnect/tcbs-bond-trading.git",
        "git@gits.tcbs.com.vn:iconnect/tcbs-bond-trading.git",
        f"  {TCBS}/projects/ICONNECT/repos/tcbs-bond-trading/pull-requests/3  ",
    ):
        ref = adapter.repo_ref(text)
        assert ref is not None and ref.slug == "ICONNECT/tcbs-bond-trading", text
        assert ref.url == f"{TCBS}/projects/ICONNECT/repos/tcbs-bond-trading"
    assert adapter.repo_ref(f"{TCBS}/users/lan/repos/notes").slug == "~lan/notes"
    assert adapter.repo_ref(f"{TCBS}/scm/~lan/notes.git").slug == "~lan/notes"
    assert adapter.repo_ref("OTHER/tool").slug == "OTHER/tool", "the allow-list is not this method's business"
    for foreign in (
        "https://bitbucket.org/iconnect/tcbs-bond-trading",
        "https://gits.other.vn/projects/ICONNECT/repos/tcbs-bond-trading",
        "git@github.com:iconnect/tcbs-bond-trading.git",
        f"{TCBS}/bitbucket/projects/ICONNECT/repos/tcbs-bond-trading",
        f"{TCBS}:8443/scm/iconnect/tcbs-bond-trading.git",
        "http://gits.tcbs.com.vn/scm/iconnect/tcbs-bond-trading.git",
        f"{TCBS}/projects/ICONNECT",
        "tcbs-bond-trading",
        "ICONNECT/a b",
        "ICONNECT/..",
        "",
    ):
        assert adapter.repo_ref(foreign) is None, foreign
    behind = server(f"{TCBS}/bitbucket")
    assert behind.repo_ref(f"{TCBS}/bitbucket/scm/iconnect/api.git").slug == "ICONNECT/api"
    assert behind.repo_ref(f"{TCBS}/scm/iconnect/api.git") is None, "another context path is another server"


def test_github_and_bitbucket_cloud_take_their_own_addresses_only() -> None:
    hub = GitHub(GitHubConfig(org="acme"), {"token": "t"}, transport=httpx.MockTransport(no_calls))
    cloud = BitbucketCloud(BitbucketCloudConfig(workspace="acme"), {"email": "a@b", "token": "t"}, transport=httpx.MockTransport(no_calls))
    for text in ("acme/pay", "Acme/pay", "pay", "https://github.com/acme/pay", "https://github.com/acme/pay.git", "https://github.com/acme/pay/tree/main/src", "git@github.com:acme/pay.git", "ssh://git@github.com/acme/pay.git"):
        assert hub.repo_ref(text).slug == "acme/pay", text
    for text in ("https://bitbucket.org/acme/pay", "https://github.com/other/pay", "git@gitlab.com:acme/pay.git", "https://github.com/acme", "acme/pay/extra", "acme/a b", ""):
        assert hub.repo_ref(text) is None, text
    for text in ("acme/pay", "pay", "https://bitbucket.org/acme/pay", "https://bitbucket.org/acme/pay/src/main/", "https://bitbucket.org/acme/pay.git", "git@bitbucket.org:acme/pay.git"):
        ref = cloud.repo_ref(text)
        assert ref.slug == "acme/pay" and ref.url == "https://bitbucket.org/acme/pay", text
    for text in ("https://github.com/acme/pay", "https://bitbucket.org/other/pay", "git@github.com:acme/pay.git"):
        assert cloud.repo_ref(text) is None, text


async def test_git_auth_is_the_clone_address_and_credential_whatever_the_clone_switch_says() -> None:
    repo = RepoRef(slug="ICONNECT/api")
    closed = server(clone=False)
    assert await closed.clone_auth(repo) is None
    auth = await closed.git_auth(repo, write=False)
    assert auth == CloneAuth(url=f"{TCBS}/scm/iconnect/api.git", bearer=TOKEN)
    with pytest.raises(ConnectorError) as refused:
        await closed.git_auth(repo, write=True)
    assert refused.value.kind == "forbidden" and "chỉ đọc" in str(refused.value)
    assert (await server(write=True).git_auth(repo, write=True)).url == f"{TCBS}/scm/iconnect/api.git"
    with pytest.raises(ConnectorError) as outside:
        await server().git_auth(RepoRef(slug="OTHER/api"), write=False)
    assert outside.value.kind == "forbidden"
    hub = GitHub(GitHubConfig(org="acme"), {"token": "ghp"}, transport=httpx.MockTransport(no_calls))
    assert await hub.clone_auth(RepoRef(slug="acme/pay")) == await hub.git_auth(RepoRef(slug="acme/pay"), write=False)
    with pytest.raises(ConnectorError):
        await hub.git_auth(RepoRef(slug="acme/pay"), write=True)


# ---- P2: a project is bound to a connection and a canonical repository -------------------------------------------


async def test_a_project_saved_with_a_browse_link_keeps_the_canonical_slug_and_its_connection(office: Office, tmp_path: Path) -> None:
    await connect_tcbs(office)
    project = await office.projects.create("bond", path=None, access=ProjectAccess.READ_ONLY)
    saved = await office.projects.configure(
        project.id, code_connection="tcbs", repo=f"{TCBS}/projects/ICONNECT/repos/tcbs-bond-trading/browse", main_branch="develop"
    )
    assert (saved.code_connection, saved.repo_url, saved.main_branch) == ("tcbs", "ICONNECT/tcbs-bond-trading", "develop")
    assert (await office.projects.get(project.id)).code_connection == "tcbs", "stored with the project, no migration needed"
    for bad, why in (
        ("https://github.com/iconnect/tcbs-bond-trading", "không phải một repo của kết nối «tcbs»"),
        ("OTHER/tool", "không được phép dùng repo «OTHER/tool»"),
    ):
        with pytest.raises(ProjectError, match=why) as refused:
            await office.projects.configure(project.id, repo=bad)
        assert refused.value.code == 422
    with pytest.raises(ProjectError, match="Không có kết nối «nope»"):
        await office.projects.configure(project.id, code_connection="nope")
    with pytest.raises(ProjectError, match="Tên nhánh chính"):
        await office.projects.configure(project.id, main_branch="-x")
    assert (await office.projects.get(project.id)).repo_url == "ICONNECT/tcbs-bond-trading", "a refused save changes nothing"
    cleared = await office.projects.configure(project.id, code_connection="")
    assert cleared.code_connection is None and cleared.repo_url == "ICONNECT/tcbs-bond-trading"


async def test_an_older_free_form_address_keeps_working_and_is_normalised_when_a_connection_is_set(office: Office) -> None:
    await connect_tcbs(office)
    project = await office.projects.create("old", path=None, access=ProjectAccess.READ_ONLY)
    old = await office.projects.configure(project.id, repo_url=f"{TCBS}/scm/iconnect/ledger.git")
    assert old.repo_url == f"{TCBS}/scm/iconnect/ledger.git" and old.code_connection is None, "as before: free text"
    assert (await office.projects.configure(project.id, code_connection="tcbs")).repo_url == "ICONNECT/ledger"
    other = await office.projects.create("else", path=None, access=ProjectAccess.READ_ONLY)
    await office.projects.configure(other.id, repo_url="https://github.com/acme/pay.git")
    with pytest.raises(ProjectError, match="không phải một repo của kết nối"):
        await office.projects.configure(other.id, code_connection="tcbs")
    team = await office.teams.create_team("Khác")
    await office.connections.save(
        ConnectionSpec(id="theirs", use="bitbucket_server", scope=f"team:{team.id}", config={"base_url": TCBS, "projects": ["ICONNECT"]}), secret={"token": TOKEN}
    )
    with pytest.raises(ProjectError, match="thuộc phạm vi team:"):
        await office.projects.configure(project.id, code_connection="theirs")


async def test_the_folder_is_checked_against_the_repository_and_the_branch(office: Office, tmp_path: Path) -> None:
    root = await connect(office, tmp_path)
    repo = local_repo(tmp_path / "shop", origin=root / "other.git")
    project = await office.projects.create("shop", path=str(repo), access=ProjectAccess.WORKTREE)
    warnings: list[str] = []
    saved = await office.projects.configure(project.id, code_connection="code", repo="https://code.test/acme/shop", main_branch="develop", warnings=warnings)
    assert saved.repo_url == "ACME/shop" and saved.main_branch == "develop", "saved all the same"
    assert len(warnings) == 2 and "không có nhánh «develop»" in warnings[0]
    assert "other.git" in warnings[1] and "khác repo «ACME/shop» của kết nối «code»" in warnings[1]
    git(repo, "remote", "set-url", "origin", str(root / "shop.git"))
    fine: list[str] = []
    await office.projects.configure(project.id, main_branch="main", warnings=fine)
    assert fine == []
    await index_tasks(office)


# ---- P1: is there a code host? -----------------------------------------------------------------------------------


async def test_with_only_a_bitbucket_server_connection_the_work_is_merged_as_a_pull_request(office: Office, tmp_path: Path) -> None:
    root, _repo, ws = await at_pr(office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    project = await office.projects.get(root.project_id)
    ctx = SimpleNamespace(office=office, ticket=root, workspace=ws, project=project)
    scope = office.sourcehub.scope(root)
    assert not await has_code_host(office, scope)
    assert (await office.merges.panel(root.key))["available"] and (await check_pr_linked(ctx)).status == "passed", "no code host: reviewed on this machine"

    await connect_tcbs(office)  # read-only, no old configuration
    assert office.atlassian.code_host() is None and await has_code_host(office, scope)
    assert (await office.merges.panel(root.key)) == {"available": False}
    result = await check_pr_linked(ctx)
    assert result.status == "failed" and "chưa có link PR" in result.summary


async def test_a_bound_project_has_its_own_code_host_and_no_other(office: Office, tmp_path: Path) -> None:
    await connect(office, tmp_path)
    await connect_tcbs(office)
    project = await office.projects.create("bond", path=None, access=ProjectAccess.READ_ONLY)
    scope = Scope(project_id=project.id)
    assert [h.spec.id for h in await code_hosts(office, scope)] == ["code", "tcbs"]
    await office.projects.configure(project.id, code_connection="tcbs", repo="ICONNECT/bond")
    (host,) = await code_hosts(office, scope)
    assert host.spec.id == "tcbs" and host.repo == RepoRef(connection="tcbs", slug="ICONNECT/bond")
    assert host._ref("") is host.repo and host._ref(f"{TCBS}/scm/iconnect/api.git").slug == "ICONNECT/api"
    assert await code_hosts(office, scope, write=True) == [], "its connection is read-only: no other host stands in"
    await index_tasks(office)


# ---- P4: the branch is pushed to the connection's repository with the connection's credential ---------------------


async def test_the_branch_is_pushed_to_the_repository_of_the_connection_and_the_token_shows_nowhere(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    root = await connect(office, tmp_path)
    for name in ("shop", "elsewhere"):
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(root / f"{name}.git")], check=True)
    repo = local_repo(tmp_path / "shop", origin=root / "elsewhere.git")  # the folder was cloned from another repository
    project = await office.projects.create("shop", path=str(repo), access=ProjectAccess.WORKTREE)
    await office.projects.configure(project.id, code_connection="code", repo="https://code.test/ACME/shop.git")
    ai = await office.ensure_ai()
    await office.org.add_allow_rule(ai.id, "mcp__office__pr_create")
    ticket = await office.tickets.create("Hoàn tiền", description="x", project_id=project.id)
    await office.db.execute("UPDATE tickets SET stage = 'pr', stage_template = 'code' WHERE id = ?", (ticket.id,))
    await office.stages.ensure_run(await office.tickets.get(ticket.id))
    said: list[str] = []

    async def open_pr(ctx: TurnCtx) -> TurnResult:
        (ctx.spec.cwd / "refund.py").write_text("def refund(): ...\n", encoding="utf-8")
        git(ctx.spec.cwd, "add", "-A")
        git(ctx.spec.cwd, *IDENT, "commit", "-q", "-m", "refund")
        said.append(await ctx.office("pr_create", {"title": "Hoàn tiền 30 ngày", "description": "AC-1"}))
        said.append(await ctx.office("stage_complete", {"summary": "PR #7"}))
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts += [open_pr]
    await office.tickets.assign(ticket.id, ai.id, by="user")
    await office.scheduler.wait_idle(timeout=30)
    assert said and "Opened PR #7 https://code.test/ACME/shop/pull/7" in said[0], said
    done = await office.tickets.get(ticket.id)
    assert done.pr_url == "https://code.test/ACME/shop/pull/7" and done.status == TicketStatus.IN_REVIEW
    ((slug, branch, destination),) = FakeHost.created
    assert slug == "ACME/shop" and destination == "main" and branch.startswith("t/")
    assert branch in git(root / "shop.git", "branch", "--list"), "the branch arrived in the connection's repository"
    assert git(root / "elsewhere.git", "branch", "--list") == "", "nothing went to the folder's origin"
    comments = "\n".join(c.body for c in await office.tickets.comments(ticket.id))
    assert "elsewhere.git" in comments and "không phải repo «ACME/shop» của kết nối «code»" in comments
    events = "\n".join(r["payload_json"] for r in await office.db.fetchall("SELECT payload_json FROM run_events"))
    config = (repo / ".git" / "config").read_text(encoding="utf-8")
    for text in ("\n".join(said), comments, events, config):
        assert TOKEN not in text
    assert "shop.git" not in config and "extraheader" not in config.lower(), "the push left nothing in .git/config"
    assert git(repo, "remote", "get-url", "origin").endswith("elsewhere.git")


# ---- P5: a project without a folder on this machine --------------------------------------------------------------


async def test_a_project_without_a_folder_is_indexed_through_its_connection(office: Office, tmp_path: Path) -> None:
    await connect(office, tmp_path)
    team = await office.teams.create_team("Shop")
    project = await office.projects.create("shop-remote", path=None, access=ProjectAccess.READ_ONLY)
    assert await office.codeintel.repos(project_id=project.id) == []
    await office.projects.configure(project.id, team_id=team.id, code_connection="code", repo="https://code.test/acme/shop", main_branch="develop")
    (repo,) = await office.codeintel.repos(project_id=project.id)
    assert repo.path is None and repo.branch == "develop" and repo.remote == RepoRef(connection="code", slug="ACME/shop", default_branch="develop")
    assert repo in await office.codeintel.all_repos() and repo in await office.codeintel.repos(team_id=team.id)
    await index_tasks(office)  # saving it started the indexing; the save itself did not wait
    (state,) = await office.codeintel.status([repo])
    assert state.state == "ready" and state.indexed_commit == "c" * 40
    assert (await office.codeintel.read(repo, "src/pay.py"))["text"].startswith("def pay()")
    assert not (office.settings.data_dir / "repos").exists(), "read through the API: nothing was cloned"

    # a push to that repository on that connection brings it up to date; another connection or branch does not
    FakeHost.head_commit = "d" * 40
    current = await office.projects.get(project.id)
    assert _is_project_repo(current, "code", "acme/shop") and not _is_project_repo(current, "other", "ACME/shop") and not _is_project_repo(current, "code", "ACME/pay")
    assert await office.webhooks.receive("code", {}, json.dumps({"repo": "ACME/shop", "ref": "main"}).encode()) is not None
    await office.webhooks.wait()
    assert (await office.codeintel.status([repo]))[0].indexed_commit == "c" * 40
    await office.webhooks.receive("code", {}, json.dumps({"repo": "ACME/shop", "ref": "develop"}).encode())
    await office.webhooks.wait()
    assert (await office.codeintel.status([repo]))[0].indexed_commit == "d" * 40
    assert "indexed code/acme/shop" in (await office.webhooks.recent())[0]["result"]


async def test_a_push_is_matched_by_connection_and_slug_for_a_bound_project_with_a_folder(office: Office, tmp_path: Path) -> None:
    root = await connect(office, tmp_path)
    await connect(office, tmp_path, connection="mirror")
    repo = local_repo(tmp_path / "shop", origin=root / "shop.git")
    project = await office.projects.create("shop", path=str(repo), access=ProjectAccess.WORKTREE)
    await office.projects.configure(project.id, code_connection="code", repo="ACME/shop")
    await index_tasks(office)
    body = json.dumps({"repo": "ACME/shop", "ref": "main"}).encode()
    await office.webhooks.receive("mirror", {}, body)
    await office.webhooks.wait()
    assert "no repository" in (await office.webhooks.recent())[0]["result"], "the same slug on another connection is another repository"
    await office.webhooks.receive("code", {}, body)
    await office.webhooks.wait()
    assert "indexed shop" in (await office.webhooks.recent())[0]["result"]


async def test_a_remote_repository_is_named_by_slug_or_link(office: Office, tmp_path: Path) -> None:
    await connect(office, tmp_path)
    repo = await office.codeintel.remote_repo("https://code.test/acme/shop/src/main", Scope())
    assert repo.slug == "code/acme/shop" and repo.remote.slug == "ACME/shop" and repo.branch == "develop"
    with pytest.raises(ConnectorError) as wrong:
        await office.codeintel.remote_repo("https://github.com/acme/shop", Scope(), connection="code")
    assert wrong.value.kind == "invalid" and "không phải một repo của kết nối code" in str(wrong.value)


# ---- the web ------------------------------------------------------------------------------------------------------


def test_the_setup_form_saves_connection_repository_and_branch_and_returns_warnings(client, tmp_path: Path) -> None:  # noqa: F811
    login(client)
    root = tmp_path / "server"
    root.mkdir()
    saved = client.post(
        "/api/connections", json={"id": "code", "use": "fake_host", "config": {"root": str(root)}, "secret": {"token": TOKEN}}, headers=HEADERS
    )
    assert saved.status_code == 200, saved.text
    repo = local_repo(tmp_path / "shop", origin=root / "other.git")
    body = {"name": "shop", "path": str(repo), "code_connection": "code", "repo_url": "https://code.test/acme/shop", "main_branch": "release"}
    r = client.post("/api/setup/project", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert (answer["project"]["code_connection"], answer["project"]["repo_url"], answer["project"]["main_branch"]) == ("code", "ACME/shop", "release")
    assert len(answer["warnings"]) == 2 and "«release»" in answer["warnings"][0] and "other.git" in answer["warnings"][1]
    listed = client.get("/api/setup").json()["projects"][0]
    assert listed["code_connection"] == "code" and listed["repo_url"] == "ACME/shop"

    refused = client.post("/api/setup/project", json={"name": "pay", "path": "", "code_connection": "code", "repo_url": "https://github.com/acme/pay"}, headers=HEADERS)
    assert refused.status_code == 422 and "không phải một repo của kết nối «code»" in refused.text
    assert [p["name"] for p in client.get("/api/setup").json()["projects"]] == ["shop"], "the refused project was not left behind"
    remote = client.post("/api/setup/project", json={"name": "pay", "path": "", "code_connection": "code", "repo_url": "acme/pay"}, headers=HEADERS)
    assert remote.status_code == 200 and remote.json()["project"]["path"] is None and remote.json()["warnings"] == []

    wrong = client.post("/api/index/remote", json={"slug": "https://github.com/acme/shop", "connection": "code"}, headers=HEADERS)
    assert wrong.status_code == 422, wrong.text
    indexed = client.post("/api/index/remote", json={"slug": "https://code.test/acme/shop", "connection": "code"}, headers=HEADERS)
    assert indexed.status_code == 200 and indexed.json()["state"] == "ready", indexed.text
