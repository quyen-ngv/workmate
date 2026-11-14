"""Tool metadata (`ToolDef.effect / activity / reads_source / needs / untrusted`) instead of sets of tool names, tools
offered only when what they need is connected, and the old Jira / Confluence / code-host tools through connections with
the configuration from before connections as the fallback."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from actoroffice.adapters import registry
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.engine.activity import describe_call
from actoroffice.engine.executor import TICKET_TOOLS
from actoroffice.engine.gate import acts_outside
from actoroffice.ports.code import PrComment, PullRequest, RepoRef
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Health, Scope
from actoroffice.ports.docs import DocContent, DocRef, Issue, PageRef
from actoroffice.security.policy import brings_untrusted
from actoroffice.services.atlassian import parse_pr, save_bitbucket
from actoroffice.services.jira import JiraMirror, save_credentials
from actoroffice.services.sources import _matches
from actoroffice.tools.atlassian_tools import ATLASSIAN_TOOLS
from actoroffice.tools.chat_tools import CHAT_TOOLS
from actoroffice.tools.infra_tools import INFRA_TOOLS
from actoroffice.tools.knowledge_tools import KNOWLEDGE_TOOLS
from actoroffice.tools.registry import ToolCtx, ToolDef, ToolError, catalog, schema, tool_def
from actoroffice.tools.stage_tools import STAGE_TOOLS
from actoroffice.tools.work_tools import CHAT_WORK_TOOLS

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db

EVERY_LIST = [TICKET_TOOLS, STAGE_TOOLS, ATLASSIAN_TOOLS, INFRA_TOOLS, CHAT_TOOLS, KNOWLEDGE_TOOLS, CHAT_WORK_TOOLS]
CONNECTED = {"jira_issue", "jira_search", "jira_comment", "wiki_search", "wiki_page", "pr_create", "pr_comments", "pr_reply"}

# ---- fakes: a tracker, a wiki and a code host as adapters ------------------------------------------------------


class Config(BaseModel):
    write: bool = False


class FakeTracker:
    """Issues at https://jira.test/browse/<KEY>. A bare key is not recognised in free text (no project list)."""

    name: ClassVar[str] = "meta_tracker"
    title: ClassVar[str] = "Tracker giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS, Capability.TRACKER})
    config_schema: ClassVar[type[BaseModel]] = Config
    secret_fields: ClassVar[tuple[str, ...]] = ()
    comments: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, config: Config, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config = config

    def recognize(self, text: str) -> list[DocRef]:
        return [DocRef(ref=m.group(1), kind=self.name) for m in re.finditer(r"https://jira\.test/browse/([A-Z]+-\d+)", text)]

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        return True

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        if ref.ref != "BIL-3":
            raise ConnectorError("not_found", f"không có issue {ref.ref}")
        return DocContent(title="BIL-3: Hoàn tiền", text="Cho hoàn tiền trong 30 ngày.", version="2026-01-01", url="https://jira.test/browse/BIL-3")

    async def search(self, jql: str, limit: int = 50) -> list[Issue]:
        return [Issue(key="BIL-3", title="Hoàn tiền", status="To Do")]

    async def comment(self, key: str, text: str) -> None:
        if not self.config.write:
            raise ConnectorError("forbidden", "kết nối chỉ đọc")
        FakeTracker.comments.append((key, text))

    async def test(self) -> Health:
        return Health(state="ok")

    async def close(self) -> None:
        return None


class FakePages:
    name: ClassVar[str] = "meta_wiki"
    title: ClassVar[str] = "Wiki giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS, Capability.WIKI})
    config_schema: ClassVar[type[BaseModel]] = Config
    secret_fields: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: Config, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config = config

    def recognize(self, text: str) -> list[DocRef]:
        return [DocRef(ref=m.group(1), kind=self.name) for m in re.finditer(r"https://wiki\.test/pages/(\d+)", text)]

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        return True

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        return DocContent(title="Spec hoàn tiền", text=f"Trang {ref.ref}.", version="4", url=f"https://wiki.test/pages/{ref.ref}")

    async def search(self, text: str, scope: Scope | None = None, limit: int = 10, *, space: str | None = None) -> list[Any]:
        return [PageRef(id="12345", title="Spec hoàn tiền", space=space or "BIL", url="https://wiki.test/pages/12345")]

    async def test(self) -> Health:
        return Health(state="ok")

    async def close(self) -> None:
        return None


class FakeCode:
    """Pull requests at https://git.test/<org>/<repo>/pull/<n>; only the organisation `acme` is its own."""

    name: ClassVar[str] = "meta_code"
    title: ClassVar[str] = "Code host giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CODE_PR})
    config_schema: ClassVar[type[BaseModel]] = Config
    secret_fields: ClassVar[tuple[str, ...]] = ()
    merged: ClassVar[bool] = False
    replies: ClassVar[list[tuple[str, int, str, int | None]]] = []

    def __init__(self, config: Config, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config = config

    def parse_pr_url(self, url: str) -> tuple[RepoRef, int] | None:
        m = re.match(r"^https://git\.test/(acme/[\w-]+)/pull/(\d+)", url)
        return (RepoRef(slug=m.group(1)), int(m.group(2))) if m else None

    async def pr_get(self, repo: RepoRef, pr_id: int) -> PullRequest:
        return PullRequest(id=pr_id, title="Hoàn tiền", state="merged" if FakeCode.merged else "open", merged=FakeCode.merged)

    async def pr_comments(self, repo: RepoRef, pr_id: int) -> list[PrComment]:
        return [PrComment(id=501, author="Minh", body="thiếu test VIP", path="src/refund.py", line=3)]

    async def pr_reply(self, repo: RepoRef, pr_id: int, text: str, *, parent_id: int | None = None) -> None:
        if not self.config.write:
            raise ConnectorError("forbidden", "kết nối chỉ đọc")
        FakeCode.replies.append((repo.slug, pr_id, text, parent_id))

    async def test(self) -> Health:
        return Health(state="ok")

    async def close(self) -> None:
        return None


class OldHost:
    """The client of the configuration from before connections (`Atlassian.use(code_host=…)`)."""

    async def pr_get(self, repo: str, pr_id: int) -> dict[str, Any]:
        return {"id": pr_id, "state": "OPEN", "title": "cũ", "url": "", "approvals": ["Lan"]}

    async def pr_comments(self, repo: str, pr_id: int) -> list[dict[str, Any]]:
        return []


FAKES = {"meta_tracker": FakeTracker, "meta_wiki": FakePages, "meta_code": FakeCode}


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    for name, cls in FAKES.items():
        registry.register(name, cls)
    FakeTracker.comments.clear()
    FakeCode.replies.clear()
    FakeCode.merged = False
    yield o
    for name in FAKES:
        registry.unregister(name)
    await o.close()


async def connect(office: Office, use: str, *, scope: str = "office", write: bool = False) -> None:
    await office.connections.save(ConnectionSpec(id=use.replace("_", "-") + "-" + scope.replace(":", "-"), use=use, scope=scope, write=write))


async def ticket_ctx(office: Office, description: str = "", team_id: str | None = None) -> tuple[Any, ToolCtx]:
    ticket = await office.tickets.create("Hoàn tiền", description=description)
    if team_id:
        await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (team_id, ticket.id))
    ticket = await office.tickets.get(ticket.id)
    office.gates["run_1"] = SimpleNamespace(tainted=False, read_dirs=[])
    return ticket, ToolCtx(office, None, None, "run_1", ticket_id=ticket.id)  # type: ignore[arg-type]


def call(name: str):
    return next(t for t in ATLASSIAN_TOOLS if t.name == name).handler


async def offered(office: Office, ticket: Any) -> set[str]:
    return {t.name for t in await office.executor._offered(ATLASSIAN_TOOLS, ticket)}


# ---- metadata says what the name sets said ----------------------------------------------------------------------


def names(**wanted: Any) -> set[str]:
    return {t.name for t in catalog().values() if all(getattr(t, k) == v for k, v in wanted.items())}


def test_metadata_holds_what_the_name_sets_held() -> None:
    assert names(activity="read") == {
        "list_dir", "doc_get", "ticket_get", "skill_read", "stage_info", "jira_issue", "wiki_page", "code_read", "code_outline", "code_symbol", "source_get",
    }  # fmt: skip
    assert names(activity="search") == {
        "history_search", "memory_search", "kb_search", "rule_search", "tickets_list", "code_search", "code_refs", "code_impact", "code_pack", "service_map",
        "repo_list", "source_search",
    }  # fmt: skip
    assert names(activity="source") == {"db_query", "db_schema", "logs_search", "logs_errors", "logs_cluster", "logs_trace", "kafka_peek", "mq_peek", "redis_scan"}
    assert names(activity="quiet") == {"plan_steps", "step_done"}
    assert names(effect="external") == {"jira_comment", "pr_create", "pr_reply", "repo_fetch"}
    assert names(untrusted=True) == {"source_get", "source_search"}
    assert {t.name for t in catalog().values() if t.reads_source} == {"ticket_get", "doc_get", "jira_issue", "wiki_page"}
    assert {t.name for t in catalog().values() if t.needs} == CONNECTED
    assert all(t.effect == ("read" if t.read_only else "write") for t in catalog().values() if t.effect != "external")


def test_one_lookup_covers_every_tool_a_run_can_get() -> None:
    for tools in EVERY_LIST:
        for tool in tools:
            known = tool_def(tool.name)
            assert known is not None, tool.name
            same = (known.effect, known.activity, known.needs, known.untrusted, known.reads_source is None)
            assert same == (tool.effect, tool.activity, tool.needs, tool.untrusted, tool.reads_source is None), f"{tool.name} is defined twice with different metadata"
    assert tool_def("Read") is None and tool_def("mcp__office__jira_issue") is None and tool_def(None) is None


def test_defaults_and_wrong_values() -> None:
    async def handler(_args: dict[str, Any], _ctx: ToolCtx) -> str:
        return ""

    plain = ToolDef("x", "", schema({}), handler)
    assert (plain.effect, plain.activity, plain.needs, plain.untrusted, plain.reads_source) == ("write", "office", frozenset(), False, None)
    assert ToolDef("x", "", schema({}), handler, read_only=True).effect == "read"
    with pytest.raises(ValueError, match="unknown effect"):
        ToolDef("x", "", schema({}), handler, effect="outside")
    with pytest.raises(ValueError, match="activity"):
        ToolDef("x", "", schema({}), handler, activity="reading")


def test_activity_gate_policy_and_sources_read_the_metadata() -> None:
    assert describe_call("jira_issue", {"key": "BIL-3"}).kind == "read" and describe_call("Read", {"file_path": "a.py"}).kind == "read"
    assert describe_call("source_search", {"query": "x"}).kind == "search" and describe_call("Grep", {"pattern": "x"}).kind == "search"
    assert describe_call("db_query", {"sql": "select 1"}).kind == "source" and describe_call("WebFetch", {"url": "u"}).kind == "source"
    assert describe_call("plan_steps", {"steps": []}) is None and describe_call("jira_comment", {"body": "x"}).kind == "office"
    assert describe_call("mcp__office__jira_issue", {"key": "BIL-3"}).kind == "office"  # only a bare name is one of ours here
    assert acts_outside("mcp__office__pr_create") and acts_outside("@office/repo_fetch")
    assert not acts_outside("mcp__office__pr_comments") and not acts_outside("pr_create") and not acts_outside("Bash")
    assert brings_untrusted("mcp__office__source_get") and not brings_untrusted("mcp__office__jira_issue") and not brings_untrusted("Read")
    # the "read" mark of a ticket's sources, from the fields kept of a call
    assert _matches("ticket", "AO-3", {"tool": "ticket_get", "ticket": "ao-3"}) and _matches("ticket", "AO-3", {"tool": "ticket_get", "path": "AO-3"})
    assert _matches("ticket", "AO-3", {"tool": "doc_get", "ticket": "AO-3"}) and not _matches("ticket", "AO-3", {"tool": "doc_get", "path": "AO-3"})
    assert _matches("jira", "BIL-3", {"tool": "jira_issue", "key": "bil-3"}) and not _matches("jira", "BIL-3", {"tool": "ticket_get", "ticket": "BIL-3"})
    assert not _matches("ticket", "BIL-3", {"tool": "jira_issue", "key": "BIL-3"})
    assert _matches("url", "https://x.test/browse/BIL-3", {"tool": "jira_issue", "key": "BIL-3"}) and not _matches("url", "https://x.test/browse/BIL-31", {"tool": "jira_issue", "key": "BIL-3"})
    assert _matches("url", "https://x.test/wiki/pages/12345/Spec", {"tool": "wiki_page", "id": "12345"}) and not _matches("url", "https://x.test/pages/123456", {"tool": "wiki_page", "id": "12345"})
    assert not _matches("url", "https://x.test/pages/12345", {"tool": "source_get", "id": "12345"}) and not _matches("jira", "BIL-3", {"tool": "nothing", "key": "BIL-3"})


def test_parse_pr_takes_the_urls_of_the_adapters_given() -> None:
    github = "https://git.test/acme/billing/pull/12"
    assert parse_pr("https://bitbucket.org/acme/billing/pull-requests/7") == ("acme/billing", 7)
    assert parse_pr(github) is None
    code = FakeCode(Config(), {})
    assert parse_pr(github, [code]) == ("acme/billing", 12)
    assert parse_pr("https://bitbucket.org/acme/billing/pull-requests/7", [code]) == ("acme/billing", 7)
    assert parse_pr("https://git.test/other/x/pull/1", [code]) is None


# ---- needs: a tool is offered only when what it needs is connected -------------------------------------------


async def test_a_team_with_nothing_connected_gets_no_jira_wiki_or_pr_tools(office: Office, runtime: FakeRuntime) -> None:
    seen: list[set[str]] = []

    async def look(ctx: TurnCtx) -> TurnResult:
        seen.append({t.name for t in ctx.spec.internal_tools})
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts.append(look)
    ticket = await office.tickets.create("x", description="x")
    await office.stages.start(ticket.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert seen and not seen[0] & CONNECTED
    assert {"repo_fetch", "repos_list", "source_get", "source_search", "stage_complete", "ticket_get"} <= seen[0]

    # the configuration from before connections counts as the capability it gives
    office.atlassian.use(code_host=OldHost())
    runtime.scripts.append(look)
    other = await office.tickets.create("y", description="y")
    await office.stages.start(other.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert seen[1] & CONNECTED == {"pr_create", "pr_comments", "pr_reply"}


async def test_old_configuration_and_connections_both_satisfy_needs(office: Office) -> None:
    ticket, _ = await ticket_ctx(office)
    assert await offered(office, ticket) == {"repo_fetch", "repos_list"}
    # old: Jira credentials give the tracker and, on the same site, the wiki
    save_credentials(office.settings.data_dir, "https://acme.atlassian.net", "bot@acme.vn", "tok")
    assert await offered(office, ticket) == {"repo_fetch", "repos_list", "jira_issue", "jira_search", "jira_comment", "wiki_search", "wiki_page"}
    await office.jira.close()
    await office.atlassian.close()


async def test_a_connection_satisfies_needs_in_its_scope_only(office: Office) -> None:
    team = await office.teams.create_team("Billing", jira_project_key="BIL")
    other = await office.teams.create_team("Ledger")
    await connect(office, "meta_tracker", scope=f"team:{team.id}")
    await connect(office, "meta_code")
    mine, _ = await ticket_ctx(office, team_id=team.id)
    theirs, _ = await ticket_ctx(office, team_id=other.id)
    assert await offered(office, mine) & CONNECTED == {"jira_issue", "jira_search", "jira_comment", "pr_create", "pr_comments", "pr_reply"}
    assert await offered(office, theirs) & CONNECTED == {"pr_create", "pr_comments", "pr_reply"}  # the office's code host, not Billing's Jira
    await office.connections.save(ConnectionSpec(id="meta-code-office", use="meta_code", enabled=False))
    assert await offered(office, theirs) & CONNECTED == set()


# ---- the old tools through connections ------------------------------------------------------------------------


async def test_jira_issue_through_a_connection_is_source_get(office: Office) -> None:
    team = await office.teams.create_team("Billing", jira_project_key="BIL")
    await connect(office, "meta_tracker")
    ticket, ctx = await ticket_ctx(office, description="Theo BIL-3", team_id=team.id)
    assert [(s["kind"], s["status"]) for s in await office.sources.list(ticket)] == [("jira", "unread")]
    out = await call("jira_issue")({"key": "bil-3"}, ctx)  # a bare key no connection recognises in text: the tracker reads it
    assert out.startswith("# BIL-3: Hoàn tiền\nNguồn: https://jira.test/browse/BIL-3 · phiên bản 2026-01-01 · qua kết nối meta-tracker-office")
    assert "<document>\nCho hoàn tiền trong 30 ngày.\n</document>" in out and "Đã ghi nhận là nguồn đã đọc của ticket: BIL-3" in out
    assert [s["status"] for s in await office.sources.list(ticket)] == ["read"]
    assert office.gates["run_1"].tainted, "content of an outside system taints the run, as source_get does"
    with pytest.raises(ToolError, match="không có issue"):
        await call("jira_issue")({"key": "BIL-9"}, ctx)
    assert await call("jira_search")({"jql": "project = BIL"}, ctx) == "BIL-3 · Hoàn tiền [To Do]"


async def test_wiki_tools_through_a_connection(office: Office) -> None:
    await connect(office, "meta_wiki")
    ticket, ctx = await ticket_ctx(office, description="Theo https://wiki.test/pages/12345")
    assert await call("wiki_search")({"text": "hoàn tiền", "space": "PAY"}, ctx) == "12345 · Spec hoàn tiền (space PAY) https://wiki.test/pages/12345"
    assert office.gates["run_1"].tainted
    out = await call("wiki_page")({"id": "12345"}, ctx)
    assert out.startswith("# Spec hoàn tiền\nNguồn: https://wiki.test/pages/12345 · phiên bản 4 · qua kết nối meta-wiki-office") and "Trang 12345." in out
    assert [s["status"] for s in await office.sources.list(ticket)] == ["read"]
    with pytest.raises(ToolError, match="id is required"):
        await call("wiki_page")({}, ctx)


async def test_old_configuration_still_answers_as_before(office: Office) -> None:
    ticket, ctx = await ticket_ctx(office)
    with pytest.raises(ToolError, match="Jira is not connected"):
        await call("jira_issue")({"key": "BIL-3"}, ctx)
    with pytest.raises(ToolError, match="Confluence is not connected"):
        await call("wiki_page")({"id": "1"}, ctx)
    with pytest.raises(ToolError, match="No code host connected"):
        await call("pr_comments")({}, ctx)
    with pytest.raises(ToolError, match="No code host is connected: this repository is reviewed on this machine"):
        await call("pr_create")({"title": "x", "description": "x"}, ctx)
    office.atlassian.use(code_host=OldHost())
    out = await call("pr_comments")({"url": "https://bitbucket.org/acme/billing/pull-requests/7"}, ctx)
    assert out == "PR #7 [OPEN] cũ · approved by: Lan" and not office.gates["run_1"].tainted
    with pytest.raises(ToolError, match="No pull request linked"):
        await call("pr_comments")({}, ctx)


async def test_writes_need_a_connection_with_write_on(office: Office) -> None:
    await connect(office, "meta_tracker")
    await connect(office, "meta_code")
    ticket, ctx = await ticket_ctx(office)
    url = "https://git.test/acme/billing/pull/12"
    assert await call("pr_comments")({"url": url}, ctx) == "PR #12 [OPEN] Hoàn tiền\n- #501 Minh src/refund.py:3: thiếu test VIP"
    for name, args in (("jira_comment", {"key": "BIL-3", "body": "x"}), ("pr_reply", {"url": url, "text": "x"}), ("pr_create", {"title": "x", "description": "x"})):
        with pytest.raises(ToolError, match="read-only"):
            await call(name)(args, ctx)
    await office.connections.save(ConnectionSpec(id="meta-tracker-office", use="meta_tracker", write=True))
    await office.connections.save(ConnectionSpec(id="meta-code-office", use="meta_code", write=True))
    assert await call("jira_comment")({"key": "bil-3", "body": "Đã rõ"}, ctx) == "Commented on BIL-3."
    assert await call("pr_reply")({"url": url, "text": "Đã thêm test", "comment_id": 501}, ctx) == "Replied on PR #12."
    assert (FakeTracker.comments, FakeCode.replies) == ([("BIL-3", "Đã rõ")], [("acme/billing", 12, "Đã thêm test", 501)])
    with pytest.raises(ToolError, match="No connected code host has the pull request"):
        await call("pr_comments")({"url": "https://git.test/other/x/pull/1"}, ctx)


async def test_the_watcher_follows_a_pull_request_of_a_connection(office: Office) -> None:
    await connect(office, "meta_code")
    ticket = await office.tickets.create("Hoàn tiền", description="x")
    await office.stages.start(ticket.id, template="code", by="user")
    await office.db.execute("UPDATE tickets SET stage = 'pr', pr_url = ? WHERE id = ?", ("https://git.test/acme/billing/pull/12", ticket.id))
    await office.stages.ensure_run(await office.tickets.get(ticket.id))
    assert await office.prs.poll() == [f"{ticket.key}: 1 new comments"]
    comments = await office.tickets.comments_after(ticket.id, "review", 0)
    assert comments and comments[-1].author_id == "meta_code" and "thiếu test VIP" in comments[-1].body
    FakeCode.merged = True
    assert await office.prs.poll() == [f"{ticket.key} merged"]  # what a merge does to the stage: tests/test_atlassian.py


# ---- import of the old configuration --------------------------------------------------------------------------


async def test_import_legacy_creates_equivalent_connections_once(office: Office) -> None:
    data = office.settings.data_dir
    assert await office.connections.import_legacy() == []
    save_credentials(data, "https://acme.atlassian.net", "bot@acme.vn", "jira-token")
    save_bitbucket(data, "bot@acme.vn", "bb-token", "acme")
    await office.connections.save(ConnectionSpec(id="confluence-main", use="meta_wiki"))  # already there: left as it is
    created = await office.connections.import_legacy()
    assert [(c.id, c.use, c.scope, c.write) for c in created] == [("jira-main", "jira_cloud", "office", True), ("bitbucket-main", "bitbucket_cloud", "office", True)]
    assert created[0].config == {"site": "https://acme.atlassian.net"} and created[1].config == {"workspace": "acme"}
    assert json.loads((data / "secrets" / "jira-main.json").read_text(encoding="utf-8")) == {"email": "bot@acme.vn", "token": "jira-token"}
    assert json.loads((data / "secrets" / "bitbucket-main.json").read_text(encoding="utf-8")) == {"email": "bot@acme.vn", "token": "bb-token"}
    assert (await office.connections.get("confluence-main")).use == "meta_wiki"
    assert json.loads((data / "secrets" / "jira.json").read_text(encoding="utf-8"))["base_url"] == "https://acme.atlassian.net", "the old file stays"
    assert await office.connections.import_legacy() == []
    assert await office.connections.has(Capability.CODE_PR) and await office.connections.has(Capability.TRACKER)
    assert isinstance(office.jira, JiraMirror) and office.jira.ready(), "the old path keeps working next to the connections"
    await office.jira.close()
