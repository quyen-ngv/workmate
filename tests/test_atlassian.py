"""Atlassian access for the AI: Confluence read/publish, pull requests on a code host, review watching,
reference repositories, and "what we already know" at the start of a ticket."""

from __future__ import annotations

import json
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.knowledge import MemoryKind, MemoryScope
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import ProjectAccess, TicketStatus
from actoroffice.services.atlassian import Confluence, html_to_text, markdown_to_storage

from .fakes import FakeRuntime, TurnCtx


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


class FakeHost:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.state = "OPEN"
        self.comments: list[dict[str, Any]] = []
        self.replies: list[tuple[int | None, str]] = []

    async def pr_create(self, repo: str, source: str, destination: str, title: str, description: str) -> dict[str, Any]:
        self.created.append({"repo": repo, "source": source, "destination": destination, "title": title, "description": description})
        return {"id": 7, "url": "https://bitbucket.org/acme/billing/pull-requests/7", "state": "OPEN"}

    async def pr_get(self, repo: str, pr_id: int) -> dict[str, Any]:
        return {"id": pr_id, "state": self.state, "title": "x", "url": "", "approvals": []}

    async def pr_comments(self, repo: str, pr_id: int) -> list[dict[str, Any]]:
        return self.comments

    async def pr_reply(self, repo: str, pr_id: int, text: str, *, parent_id: int | None = None) -> dict[str, Any]:
        self.replies.append((parent_id, text))
        return {"id": 99}


async def test_pull_request_flow_with_review_and_merge(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    repo = tmp_path / "billing"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("billing\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    project = await office.projects.create("billing", path=str(repo), access=ProjectAccess.WORKTREE)
    await office.projects.configure(project.id, repo_url="acme/billing")
    host = FakeHost()
    office.atlassian.use(code_host=host)
    ai = await office.ensure_ai()
    await office.org.add_allow_rule(ai.id, "mcp__office__pr_create")
    await office.org.add_allow_rule(ai.id, "mcp__office__pr_reply")

    ticket = await office.tickets.create("Hoàn tiền", description="x", project_id=project.id)
    await office.db.execute("UPDATE tickets SET stage = 'pr', stage_template = 'code' WHERE id = ?", (ticket.id,))
    await office.stages.ensure_run(await office.tickets.get(ticket.id))

    async def open_pr(ctx: TurnCtx) -> TurnResult:
        (ctx.spec.cwd / "refund.py").write_text("def refund(): ...\n", encoding="utf-8")
        git(ctx.spec.cwd, "add", "-A")
        git(ctx.spec.cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "refund")
        out = await ctx.office("pr_create", {"title": "Hoàn tiền 30 ngày", "description": "AC-1 → test_ac1"})
        assert "Opened PR #7" in out
        out = await ctx.office("stage_complete", {"summary": "PR #7"})
        assert "merge" in out
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    async def fix_review(ctx: TurnCtx) -> TurnResult:
        assert "Review mới trên PR #7" in ctx.spec.prompt and "thiếu test VIP" in ctx.spec.prompt
        out = await ctx.office("pr_comments", {})
        assert "#501 Minh src/refund.py:3: thiếu test VIP" in out
        await ctx.office("pr_reply", {"comment_id": 501, "text": "Đã thêm test VIP"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts += [open_pr, fix_review]
    await office.tickets.assign(ticket.id, ai.id, by="user")
    await office.scheduler.wait_idle(timeout=30)
    t = await office.tickets.get(ticket.id)
    assert t.pr_url == "https://bitbucket.org/acme/billing/pull-requests/7" and t.status == TicketStatus.IN_REVIEW
    assert host.created[0]["source"].startswith("t/") and host.created[0]["destination"] == "main" and host.created[0]["repo"] == "acme/billing"
    assert host.created[0]["source"] in git(remote, "branch", "--list")  # the branch was pushed

    host.comments = [{"id": 501, "by": "Minh", "text": "thiếu test VIP", "file": "src/refund.py", "line": 3, "parent": None, "resolved": False, "at": ""}]
    assert await office.prs.poll() == [f"{ticket.key}: 1 new comments"]
    assert await office.prs.poll() == []  # each comment is passed on once
    await office.scheduler.wait_idle(timeout=30)
    assert host.replies == [(501, "Đã thêm test VIP")]

    host.state = "MERGED"
    assert await office.prs.poll() == [f"{ticket.key} merged"]
    assert (await office.tickets.get(ticket.id)).stage == "close"


async def test_external_tools_follow_the_project_permission_mode(office: Office, runtime: FakeRuntime) -> None:
    office.atlassian.use(code_host=FakeHost())
    seen: list[str] = []

    async def try_reply(ctx: TurnCtx) -> TurnResult:
        decision = await ctx.gate.before_tool("mcp__office__pr_reply", {"text": "hi"}, None)
        seen.append(decision.decision.value)
        decision = await ctx.gate.before_tool("mcp__office__pr_comments", {}, None)
        seen.append(decision.decision.value)
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    # default (workspace) mode: acting on the ticket's own PR is covered by the approved plan
    runtime.scripts.append(try_reply)
    ticket = await office.tickets.create("x", description="x")
    await office.stages.start(ticket.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert seen == ["allow", "allow"]

    # read-only mode: a write waits for a person; a read does not
    seen.clear()
    project = await office.projects.get(office.chat.default_project_id)
    await office.projects.set_permissions(project.id, mode="read_only")
    runtime.scripts.append(try_reply)
    other = await office.tickets.create("y", description="y", project_id=project.id)
    await office.stages.start(other.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert seen == ["defer", "allow"]


async def test_confluence_search_read_and_publish() -> None:
    calls: list[tuple[str, str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path.endswith("/rest/api/content/search"):
            assert 'text ~ "hoàn tiền"' in request.url.params["cql"]
            return httpx.Response(200, json={"results": [{"id": "11", "title": "Refund policy", "space": {"key": "BIL"}, "version": {"when": "2025-03-01T00:00"}, "_links": {"webui": "/spaces/BIL/pages/11"}}]})
        if path.endswith("/api/v2/pages/11"):
            return httpx.Response(200, json={"id": "11", "title": "Refund policy", "version": {"number": 4, "createdAt": "2025-03-01"}, "body": {"storage": {"value": "<h2>Rules</h2><p>VIP: 60 ngày</p><ul><li>Khác: 30</li></ul>"}}, "_links": {"webui": "/x"}})
        if path.endswith("/api/v2/spaces"):
            return httpx.Response(200, json={"results": [{"id": "900", "key": "BIL"}]})
        if path.endswith("/api/v2/pages") and request.method == "GET":
            return httpx.Response(200, json={"results": []})
        if path.endswith("/api/v2/pages") and request.method == "POST":
            return httpx.Response(200, json={"id": "12", "_links": {"webui": "/spaces/BIL/pages/12"}})
        return httpx.Response(404)

    wiki = Confluence("https://acme.atlassian.net", "bot@acme.vn", "tok", transport=httpx.MockTransport(handler))
    pages = await wiki.search("hoàn tiền", space="BIL")
    assert pages[0]["id"] == "11" and pages[0]["updated"] == "2025-03-01" and pages[0]["url"].endswith("/wiki/spaces/BIL/pages/11")
    page = await wiki.page("11")
    assert "## Rules" in page["text"] and "VIP: 60 ngày" in page["text"] and "- Khác: 30" in page["text"]
    published = await wiki.upsert_page("BIL", "AO-7 · Hoàn tiền", markdown_to_storage("# Dossier\n- a\n\n| AC | Test |\n|---|---|\n| AC-1 | t1 |"), parent_id="5")
    assert published["id"] == "12"
    post = next(c for c in calls if c[0] == "POST")
    assert post[2]["spaceId"] == "900" and post[2]["parentId"] == "5" and "<table>" in post[2]["body"]["value"]
    await wiki.close()
    assert html_to_text("<p>a &amp; b</p>") == "a & b"


async def test_known_context_is_posted_and_in_the_prompt(office: Office, runtime: FakeRuntime) -> None:
    ai = await office.ensure_ai()
    old = await office.tickets.create("Hoàn tiền đơn hàng thường", description="Luật hoàn tiền 30 ngày")
    await office.db.execute("UPDATE tickets SET status = 'done' WHERE id = ?", (old.id,))
    await office.memory.add(ai, "Hoàn tiền: đơn VIP được 60 ngày (Lan xác nhận)", kind=MemoryKind.FACT, scope=MemoryScope.SHARED, created_by="Lan")
    ticket = await office.tickets.create("Hoàn tiền cho đơn VIP", description="Khách VIP muốn hoàn tiền")
    prompts: list[str] = []

    async def look(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.system_prompt)
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts.append(look)
    started = await office.stages.start(ticket.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert "60 ngày" in prompts[0] and old.key in prompts[0]
    messages = await office.chat.messages(started.origin_conversation_id)
    known = [m.body for m in messages if m.body.startswith("📚")]
    assert known and "60 ngày" in known[0] and old.key in known[0]
