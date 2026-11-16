"""Pull requests on a code host through a connection (a fake adapter), review watching, and "what we already know" at
the start of a ticket."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.knowledge import MemoryKind, MemoryScope
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import ProjectAccess, TicketStatus
from actoroffice.ports.code import PrComment

from .fake_adapters import FakeCodeHost, connect, install, uninstall
from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    install()
    yield o
    uninstall()
    await o.close()


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
    host = FakeCodeHost
    await connect(office, "fake_code", write=True)
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
    assert t.pr_url == "https://git.test/acme/billing/pull/7" and t.status == TicketStatus.IN_REVIEW
    assert host.created[0]["source"].startswith("t/") and host.created[0]["destination"] == "main" and host.created[0]["repo"] == "acme/billing"
    assert host.created[0]["source"] in git(remote, "branch", "--list")  # the branch was pushed

    host.comments = [PrComment(id=501, author="Minh", body="thiếu test VIP", path="src/refund.py", line=3)]
    assert await office.prs.poll() == [f"{ticket.key}: 1 new comments"]
    assert await office.prs.poll() == []  # each comment is passed on once
    await office.scheduler.wait_idle(timeout=30)
    assert host.replies == [(501, "Đã thêm test VIP")]

    host.state = "merged"
    assert await office.prs.poll() == [f"{ticket.key} merged"]
    assert (await office.tickets.get(ticket.id)).stage == "close"


async def test_external_tools_follow_the_project_permission_mode(office: Office, runtime: FakeRuntime) -> None:
    await connect(office, "fake_code", write=True)
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
