"""Connections (registry, layering, "not configured" as a normal state) and the code tools a ticket run gets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from actoroffice.adapters import registry
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.work import ProjectAccess
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Health, Scope
from actoroffice.tools.code_tools import CODE_TOOLS
from actoroffice.tools.registry import ToolCtx, ToolError

from .fakes import FakeRuntime
from .seed import IDENT, build_repo, git, seed_db
from .test_codeintel import PY_REPO


class FakeConfig(BaseModel):
    site: str
    projects: list[str] = []
    write: bool = False


class FakeTracker:
    name: ClassVar[str] = "fake_tracker"
    title: ClassVar[str] = "Tracker giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.TRACKER, Capability.DOCS})
    config_schema: ClassVar[type[BaseModel]] = FakeConfig
    secret_fields: ClassVar[tuple[str, ...]] = ("token",)
    closed = 0

    def __init__(self, config: FakeConfig, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config, self.secret = config, secret

    async def test(self) -> Health:
        if self.secret["token"] == "bad":
            raise ConnectorError("auth", "token sai")
        return Health(state="ok", identity="bot@acme", detail="token=" + self.secret["token"])

    async def close(self) -> None:
        FakeTracker.closed += 1


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    registry.register("fake_tracker", FakeTracker)
    yield o
    registry.unregister("fake_tracker")
    await o.close()


# ---- registry ---------------------------------------------------------------------------------------------------------


def test_every_builtin_adapter_loads_and_describes_itself() -> None:
    for name in ("jira_cloud", "confluence_cloud", "bitbucket_cloud", "github"):
        info = registry.describe(name)
        assert info["title"] and info["capabilities"] and info["config_schema"]["properties"], name
        assert "token" in info["secret_fields"]
    assert Capability.CODE_READ in registry.capabilities("github") and Capability.WIKI in registry.capabilities("confluence_cloud")


def test_unknown_adapter_and_bad_config_are_refused() -> None:
    with pytest.raises(ConnectorError) as unknown:
        registry.adapter_class("sharepoint")
    assert unknown.value.kind == "invalid" and "github" in str(unknown.value)
    with pytest.raises(ConnectorError) as bad:
        registry.validate(ConnectionSpec(id="gh", use="github", config={"repos": "not-a-list"}))
    assert bad.value.kind == "invalid" and "org" in str(bad.value) and "repos" in str(bad.value)


def test_write_is_one_switch_on_the_connection() -> None:
    assert registry.validate(ConnectionSpec(id="gh", use="github", config={"org": "acme"})).write is False
    assert registry.validate(ConnectionSpec(id="gh", use="github", config={"org": "acme"}, write=True)).write is True


# ---- connections ------------------------------------------------------------------------------------------------------


async def test_overview_lists_every_adapter_before_anything_is_connected(office: Office) -> None:
    overview = {a["name"]: a for a in await office.connections.overview()}
    assert {"jira_cloud", "confluence_cloud", "bitbucket_cloud", "github", "fake_tracker"} <= set(overview)
    assert all(a["state"] == "unconfigured" and a["connections"] == [] for a in overview.values())
    assert await office.connections.resolve(Capability.TRACKER) is None
    assert not await office.connections.has(Capability.CODE_READ)


async def test_save_validates_and_keeps_what_was_running(office: Office) -> None:
    good = ConnectionSpec(id="jira-main", use="fake_tracker", config={"site": "https://acme.atlassian.net"})
    await office.connections.save(good, secret={"token": "s3cret-token"})
    for bad in (
        ConnectionSpec(id="Bad Id", use="fake_tracker", config={"site": "x"}),
        ConnectionSpec(id="jira-main", use="fake_tracker", config={}),
        ConnectionSpec(id="jira-main", use="nope", config={}),
        ConnectionSpec(id="jira-main", use="fake_tracker", config={"site": "x"}, scope="team:missing"),
    ):
        with pytest.raises(ConnectorError):
            await office.connections.save(bad)
    kept = await office.connections.get("jira-main")
    assert kept is not None and kept.config == {"site": "https://acme.atlassian.net"} and kept.secret_ref == "jira-main"
    assert "s3cret-token" not in (await office.db.fetchone("SELECT * FROM connections WHERE id = 'jira-main'"))["config_json"]


async def test_health_is_stored_and_never_shows_the_secret(office: Office) -> None:
    await office.connections.save(ConnectionSpec(id="jira-main", use="fake_tracker", config={"site": "s"}), secret={"token": "s3cret-token"})
    health = await office.connections.health("jira-main")
    assert health.state == "ok" and health.identity == "bot@acme" and "s3cret-token" not in health.detail and health.checked_at
    await office.connections.save(ConnectionSpec(id="jira-main", use="fake_tracker", config={"site": "s"}), secret={"token": "bad"})
    assert (await office.connections.health("jira-main")).state == "error"
    shown = next(a for a in await office.connections.overview() if a["name"] == "fake_tracker")
    assert shown["state"] == "connected" and shown["connections"][0]["health"]["state"] == "error"


async def test_missing_secret_is_unconfigured_not_a_crash(office: Office) -> None:
    await office.connections.save(ConnectionSpec(id="jira-main", use="fake_tracker", config={"site": "s"}))
    assert (await office.connections.health("jira-main")).state == "unconfigured"
    assert await office.connections.resolve(Capability.TRACKER) is None


async def test_nearest_connection_wins_project_then_team_then_office(office: Office) -> None:
    team = await office.teams.create_team("Pay")
    other = await office.teams.create_team("Platform")
    project = await office.projects.create("payments", path=None, access=ProjectAccess.READ_ONLY)
    for cid, scope in (("at-office", "office"), ("at-team", f"team:{team.id}"), ("at-project", f"project:{project.id}"), ("at-other", f"team:{other.id}")):
        await office.connections.save(ConnectionSpec(id=cid, use="fake_tracker", config={"site": cid}, scope=scope), secret={"token": "t-" + cid})

    async def nearest(scope: Scope | None) -> str:
        found = await office.connections.resolve(Capability.TRACKER, scope)
        assert found is not None
        return found[0].id

    assert await nearest(None) == "at-office"
    assert await nearest(Scope(team_id=team.id)) == "at-team"
    assert await nearest(Scope(team_id=team.id, project_id=project.id)) == "at-project"
    assert await nearest(Scope(team_id="unknown")) == "at-office"
    assert [s.id for s in await office.connections.candidates(Capability.TRACKER, Scope(team_id=team.id))] == ["at-team", "at-office"], "another team's is never used"
    assert await office.connections.candidates(Capability.CODE_READ, Scope(team_id=team.id)) == []


async def test_disable_and_remove(office: Office) -> None:
    spec = ConnectionSpec(id="jira-main", use="fake_tracker", config={"site": "s"})
    await office.connections.save(spec, secret={"token": "tok-123456"})
    await office.connections.adapter("jira-main")
    before = FakeTracker.closed
    await office.connections.save(spec.model_copy(update={"enabled": False, "secret_ref": "jira-main"}))
    assert FakeTracker.closed == before + 1 and await office.connections.resolve(Capability.TRACKER) is None
    await office.connections.remove("jira-main")
    assert await office.connections.get("jira-main") is None


# ---- code tools -------------------------------------------------------------------------------------------------------


def tool(name: str):
    return next(t for t in CODE_TOOLS if t.name == name).handler


@pytest.fixture
async def shop_ticket(office: Office, tmp_path: Path) -> tuple[ToolCtx, Path]:
    repo = build_repo(tmp_path / "shop", PY_REPO)
    project = await office.projects.create("Shop", path=str(repo), access=ProjectAccess.READ_ONLY)
    ticket = await office.tickets.create("Đổi hạn mức hoàn tiền", project_id=project.id)
    return ToolCtx(office, None, None, "run_1", ticket_id=ticket.id), repo  # type: ignore[arg-type]


async def test_first_use_builds_the_index_and_answers_cite_their_source(shop_ticket) -> None:
    ctx, repo = shop_ticket
    assert not (ctx.office.settings.data_dir / "index.db").exists()
    found = await tool("code_search")({"query": "LimitService"}, ctx)
    head = git(repo, "rev-parse", "HEAD")
    assert f"commit {head[:10]}" in found and "### shop:src/shop/limits.py:" in found and "class LimitService" in found
    listed = await tool("repo_list")({}, ctx)
    assert "shop: ready, mức graph" in listed


async def test_outline_symbol_refs_read_impact(shop_ticket) -> None:
    ctx, repo = shop_ticket
    outline = await tool("code_outline")({"path": "src/shop/limits.py"}, ctx)
    assert "[class] class LimitService" in outline and "[method] def check(self, customer_id: int) -> bool" in outline and "return self._count" not in outline
    assert "Nguồn: shop:src/shop/limits.py:9-10" in await tool("code_symbol")({"name": "LimitService.check"}, ctx)
    refs = await tool("code_refs")({"symbol": "refund_allowed"}, ctx)
    assert "shop.refund.create_refund → shop.refund.refund_allowed  [calls]" in refs
    guess = await tool("code_refs")({"symbol": "LimitService.check"}, ctx)
    assert "(đoán theo tên)" in guess
    read = await tool("code_read")({"path": "src/shop/limits.py", "start": 9, "end": 10}, ctx)
    assert read.startswith("Nguồn: shop:src/shop/limits.py:9-10 · commit") and "    9      def check" in read
    impact = await tool("code_impact")({"targets": ["LimitService._count"]}, ctx)
    assert "POST /v1/refunds/{}" in impact and "shop.refund.create_refund" in impact
    pack = await tool("code_pack")({"question": "refund limit", "budget": 3000}, ctx)
    assert pack.startswith("Gói đọc ~") and "## Khung shop:" in pack
    smap = await tool("service_map")({}, ctx)
    assert "Service (1): shop" in smap and "shop → audit.internal [external]" in smap and "```mermaid" in smap


async def test_code_read_is_from_the_commit_not_the_working_tree_and_follows_new_commits(shop_ticket) -> None:
    ctx, repo = shop_ticket
    await tool("code_search")({"query": "MAX_PER_DAY"}, ctx)
    (repo / "src/shop/limits.py").write_text("MAX_PER_DAY = 9\n", encoding="utf-8")
    assert "MAX_PER_DAY = 5" in await tool("code_read")({"path": "src/shop/limits.py", "start": 3}, ctx)
    git(repo, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", "raise the limit")
    assert "MAX_PER_DAY = 9" in await tool("code_search")({"query": "MAX_PER_DAY"}, ctx), "a new commit on main is indexed on next use"
    old = git(repo, "rev-parse", "HEAD~1")
    assert "MAX_PER_DAY = 5" in await tool("code_read")({"path": "src/shop/limits.py", "start": 3, "at": old}, ctx)


async def test_errors_say_what_to_do(shop_ticket, office: Office) -> None:
    ctx, _ = shop_ticket
    with pytest.raises(ToolError, match="Không có file"):
        await tool("code_outline")({"path": "src/nope.py"}, ctx)
    with pytest.raises(ToolError, match="không có"):
        await tool("code_read")({"path": "src/nope.py"}, ctx)
    with pytest.raises(ToolError, match="repo_list"):
        await tool("code_search")({"query": "x", "repo": "other"}, ctx)
    with pytest.raises(ToolError):
        await tool("code_read")({"path": "../../etc/passwd"}, ctx)


async def test_no_code_source_is_a_normal_state(office: Office) -> None:
    bare = await office.tickets.create("Không có code")
    ctx = ToolCtx(office, None, None, "run_2", ticket_id=bare.id)  # type: ignore[arg-type]
    assert "chưa có nguồn code" in await tool("repo_list")({}, ctx)
    with pytest.raises(ToolError, match="Bitbucket / GitHub"):
        await tool("code_search")({"query": "anything"}, ctx)


async def test_a_team_never_reads_another_teams_code(office: Office, tmp_path: Path) -> None:
    pay, platform = await office.teams.create_team("Pay"), await office.teams.create_team("Platform")
    projects = {}
    for team, name, files in ((pay, "payments", {"a.py": "def pay_secret():\n    return 1\n"}), (platform, "infra", {"b.py": "def infra_secret():\n    return 2\n"})):
        project = await office.projects.create(name, path=str(build_repo(tmp_path / name, files)), access=ProjectAccess.READ_ONLY)
        project = await office.projects.configure(project.id, team_id=team.id)
        projects[name] = project
    ticket = await office.tickets.create("Việc của Pay", project_id=projects["payments"].id)
    await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (pay.id, ticket.id))
    ctx = ToolCtx(office, None, None, "run_3", ticket_id=ticket.id)  # type: ignore[arg-type]
    assert "pay_secret" in await tool("code_search")({"query": "pay_secret"}, ctx)
    assert "Không tìm thấy" in await tool("code_search")({"query": "infra_secret"}, ctx)
    assert "infra" not in await tool("repo_list")({}, ctx)


def test_every_ticket_run_gets_the_code_tools() -> None:
    from actoroffice.engine.executor import TICKET_TOOLS

    names = {t.name for t in TICKET_TOOLS}
    assert {t.name for t in CODE_TOOLS} <= names and all(t.read_only for t in CODE_TOOLS)
    assert len(names) == len(TICKET_TOOLS), "no two tools share a name"


async def test_a_missing_argument_is_an_error_the_model_can_fix(shop_ticket) -> None:
    ctx, _ = shop_ticket
    with pytest.raises(ToolError, match="Thiếu tham số `symbol`"):
        await tool("code_refs")({"direction": "callers"}, ctx)
    assert "shop.refund.create_refund" in await tool("code_refs")({"name": "refund_allowed"}, ctx), "the name models use instead is accepted"
