"""A small, clear change (AO-7: keep the tab bar's right-hand buttons visible) takes the quick workflow: the AI
switches to it from understand, writes a short plan, gets one approval, makes the change and opens the PR."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import InteractionKind, TicketStatus

from .fakes import FakeRuntime, TurnCtx

PLAN = """\
Problem: .tabs-right in team.css:118 has no flex-shrink, so many tabs push it out.
Fix: wrap the tabs in .tabs-scroll (overflow-x: auto); .tabs-right gets flex-shrink: 0.
Files: ui team.css, team.js renderTabs().
Check: open 10 tickets at 1280 px; the buttons stay visible.
- AC-1: Given 10 open tickets, When I look at the tab bar, Then Files/Changes/Dossier/Rules/Access are visible.
"""


def done(ctx: TurnCtx) -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")


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


async def gates(office: Office, ticket_id: int):
    return [i for i in await office.interactions.pending(ticket_id=ticket_id) if i.kind == InteractionKind.APPROVE_STAGE]


async def test_small_change_switches_to_quick_and_needs_one_approval(office: Office, runtime: FakeRuntime) -> None:
    ticket = await office.tickets.create("sửa thanh tab bar ui", description="Nhiều tab đẩy mất Files, Changes, Dossier.")
    prompts: list[str] = []

    async def judge(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.system_prompt)
        said = await ctx.office("stage_switch", {"template": "quick", "reason": "Sửa CSS nhỏ, nguyên nhân rõ (team.css)"})
        assert "now follows workflow 'quick' from stage scope" in said
        return done(ctx)

    async def scope(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.system_prompt)
        await ctx.office("doc_put", {"key": "plan", "body": PLAN, "base_revision": None})
        await ctx.office("stage_complete", {"summary": "Sửa .tabs-right"})
        return done(ctx)

    async def build(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.system_prompt)
        result = await ctx.office("stage_complete", {"summary": "Đã sửa team.css/team.js, commit abc123. Kiểm tra: mở 10 ticket."})
        assert "Next stage: pr" in result
        return done(ctx)

    async def pr(ctx: TurnCtx) -> TurnResult:
        await ctx.office("pr_link", {"url": "https://github.com/acme/x/pull/7"})
        await ctx.office("stage_complete", {"summary": "PR #7"})
        return done(ctx)

    runtime.scripts += [judge, scope]
    await office.stages.start(ticket.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=30)

    t = await office.tickets.get(ticket.id)
    assert (t.stage_template, t.stage) == ("quick", "scope")
    assert "First judge the size" in prompts[0] and "stage_switch" in prompts[0]
    assert "at most 15 lines" in prompts[1] and "Do not read skills" in prompts[1]
    assert [r.outcome for r in await office.stages.runs(ticket.id)][:1] == ["switched"]

    # one approval for the whole change (the plan gate), then build → pr
    [gate] = await gates(office, ticket.id)
    assert gate.payload["gate"] == "plan"
    runtime.scripts += [build, pr]
    await office.answer(gate.id, {"approved": True})
    await office.scheduler.wait_idle(timeout=30)
    assert "No handoff document and no plan_steps needed" in prompts[2]

    t = await office.tickets.get(ticket.id)
    assert t.stage == "pr" and t.pr_url.endswith("/pull/7")
    assert await gates(office, ticket.id) == []  # no requirement, knowledge or other gates on the way
    await office.stages.external_passed(ticket.id, "merge", by="user", note="merged")
    assert (await office.tickets.get(ticket.id)).status == TicketStatus.DONE  # no close/KB stage


async def test_a_short_plan_still_needs_an_acceptance_criterion(office: Office, runtime: FakeRuntime) -> None:
    ticket = await office.tickets.create("đổi màu nút", description="x")
    outcome: list[str] = []

    async def scope(ctx: TurnCtx) -> TurnResult:
        await ctx.office("doc_put", {"key": "plan", "body": "Đổi màu nút Gửi trong team.css.", "base_revision": None})
        outcome.append(await ctx.office("stage_complete", {"summary": "x"}))
        return done(ctx)

    runtime.scripts.append(scope)
    await office.stages.start(ticket.id, template="quick", by="user")
    await office.scheduler.wait_idle(timeout=30)
    assert "plan_ac" in outcome[0]


async def test_people_switch_the_workflow_and_not_after_the_pr(office: Office) -> None:
    ticket = await office.tickets.create("x", description="x")
    await office.stages.start(ticket.id, template="quick", by="user")
    full = await office.stages.switch_template(ticket.id, "code", reason="có quyết định nghiệp vụ", by="user")
    assert (full.stage_template, full.stage) == ("code", "understand")
    with pytest.raises(Exception):
        await office.stages.switch_template(ticket.id, "automation", reason="x", by="user")
    await office.db.execute("UPDATE tickets SET stage = 'pr' WHERE id = ?", (ticket.id,))
    with pytest.raises(Exception, match="pull request"):
        await office.stages.switch_template(ticket.id, "quick", reason="x", by="user")


async def test_without_a_code_host_the_committed_branch_is_the_review(tmp_path) -> None:
    import subprocess
    from types import SimpleNamespace

    from actoroffice.services.checks import check_pr_linked

    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], check=True)
    subprocess.run([*git, "switch", "-q", "-c", "t/si-1-fix"], check=True)
    office = SimpleNamespace(atlassian=SimpleNamespace(code_host=lambda: None))
    ctx = SimpleNamespace(office=office, ticket=SimpleNamespace(pr_url=None), workspace=repo, project=SimpleNamespace(main_branch="main"))

    assert (await check_pr_linked(ctx)).status == "failed"  # nothing committed yet
    (repo / "a.txt").write_text("x", encoding="utf-8")
    assert "chưa commit" in (await check_pr_linked(ctx)).summary
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "fix"], check=True)
    result = await check_pr_linked(ctx)
    assert result.status == "passed" and "t/si-1-fix" in result.summary
