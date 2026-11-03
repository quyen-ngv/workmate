"""SI-13: a direction already delivered to a run wakes nothing more; comments wait while a gate card is open;
a new stage run withdraws the stale gate cards."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus, WakeSource
from actoroffice.domain.team import LOCAL_OWNER_ID
from actoroffice.domain.work import InteractionKind, InteractionStatus
from actoroffice.runtimes.base import GateDecision
from actoroffice.services.wakes import only_comments

from .fakes import FakeRuntime, TurnCtx
from .test_quality import change, findings, repo_project, reviewer, reviewer_runs
from .test_stages import REQUIREMENTS


def ok(ctx: TurnCtx) -> TurnResult:
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


@pytest.fixture
async def people(office: Office):
    team = await office.teams.create_team("Billing", jira_project_key="bil")
    lan = await office.teams.create_user("Lan", telegram_id=111)
    minh = await office.teams.create_user("Minh", telegram_id=222)
    await office.teams.add_member(team.id, lan.id, ["ba"])
    await office.teams.add_member(team.id, minh.id, ["dev"])
    return team, lan, minh


async def ticket_runs(office: Office, ticket_id: int) -> int:
    row = await office.db.fetchone("SELECT COUNT(*) AS n FROM runs WHERE ticket_id = ? AND purpose IS NULL", (ticket_id,))
    return int(row["n"])


async def stage_runs(office: Office, ticket_id: int, stage: str) -> int:
    return len([r for r in await office.stages.runs(ticket_id) if r.stage == stage])


async def steer_wakes(office: Office, ticket_id: int) -> list[dict]:
    return await office.db.fetchall(
        "SELECT status, json_extract(payload_json, '$.dropped') AS dropped FROM wake_requests "
        "WHERE ticket_id = ? AND payload_json LIKE '%\"steer\"%' ORDER BY id",
        (ticket_id,),
    )


async def stage_cards(office: Office, ticket_id: int) -> list:
    return [i for i in await office.interactions.pending(ticket_id=ticket_id) if i.kind == InteractionKind.APPROVE_STAGE]


async def started(office: Office, people) -> int:
    team, lan, _ = people
    ticket = await office.tickets.create("Export CSV", description="x")
    await office.stages.start(ticket.key, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
    return ticket.id


def steered(office: Office, ticket_id: int, by: str, *texts: str, then=None):
    """A run that receives a direction mid-way (the gate denies the next tool call), then finishes."""

    async def script(ctx: TurnCtx) -> TurnResult:
        await office.stages.steer(ticket_id, texts[0], by=by)
        decision = await ctx.gate.before_tool("Read", {"file_path": "a.txt"}, None)
        assert decision.decision == GateDecision.DENY and "NEW DIRECTION" in decision.reason
        for text in texts[1:]:
            await office.stages.steer(ticket_id, text, by=by)
        if then is not None:
            return await then(ctx)
        return ok(ctx)

    return script


# ---- BR-1/BR-2: a direction already delivered needs no second wake ------------------------------------------


async def test_ac1_seen_steer_wake_dropped_after_run(office: Office, runtime: FakeRuntime, people) -> None:
    ticket_id = await started(office, people)
    runtime.scripts.append(steered(office, ticket_id, people[2].id, "Dùng dấu ;"))
    await office.scheduler.wait_idle(timeout=20)
    [wake] = await steer_wakes(office, ticket_id)
    assert wake["status"] == "dropped" and wake["dropped"] == "steer already delivered to the previous run"
    assert await ticket_runs(office, ticket_id) == 1


async def test_ac2_seen_steer_wake_dropped_when_run_parks(office: Office, runtime: FakeRuntime, people) -> None:
    ticket_id = await started(office, people)

    async def ask(ctx: TurnCtx) -> TurnResult:
        await ctx.office("ask_human", {"roles": ["ba"], "question": "Dấu nào?", "options": ["; ", ","]})
        return ok(ctx)

    runtime.scripts.append(steered(office, ticket_id, people[2].id, "Dùng dấu ;", then=ask))
    await office.scheduler.wait_idle(timeout=20)
    [run] = await office.runs.for_ticket(ticket_id)
    assert run.status.value == "parked"
    [wake] = await steer_wakes(office, ticket_id)
    assert wake["status"] == "dropped"
    assert await ticket_runs(office, ticket_id) == 1  # nothing runs until the question is answered

    [question] = [i for i in await office.interactions.pending(ticket_id=ticket_id) if i.kind == InteractionKind.QUESTION]
    await office.answer(question.id, {"answer": ";"}, by=people[1].id)
    await office.scheduler.wait_idle(timeout=20)
    assert await ticket_runs(office, ticket_id) == 2


async def test_ac3_steer_wake_kept_when_run_fails(office: Office, runtime: FakeRuntime, people) -> None:
    ticket_id = await started(office, people)

    async def crash(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.ERROR, error="boom")

    prompts: list[str] = []

    async def again(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.prompt)
        return ok(ctx)

    runtime.scripts += [steered(office, ticket_id, people[2].id, "Dùng dấu ;", then=crash), again]
    await office.scheduler.wait_idle(timeout=20)
    [wake] = await steer_wakes(office, ticket_id)
    assert wake["status"] == "done" and wake["dropped"] is None  # kept, then run: the direction is not lost
    assert await ticket_runs(office, ticket_id) == 2
    assert "Direction from Minh: Dùng dấu ;" in prompts[0]


@pytest.mark.parametrize("other", ["comment", "manual"])
async def test_ac4_wake_with_other_reason_kept(office: Office, runtime: FakeRuntime, people, other: str) -> None:
    ticket_id = await started(office, people)
    ai = await office.ensure_ai()

    async def more(ctx: TurnCtx) -> TurnResult:
        if other == "comment":
            await office.tickets.add_comment(ticket_id, "Nhớ cả header", author_kind="user", author_id=people[1].id)
        else:
            await office.wakes.enqueue(ai.id, WakeSource.MANUAL, ticket_id=ticket_id, payload={"resumed": True})
        return ok(ctx)

    runtime.scripts.append(steered(office, ticket_id, people[2].id, "Dùng dấu ;", then=more))
    await office.scheduler.wait_idle(timeout=20)
    [wake] = await steer_wakes(office, ticket_id)
    assert wake["status"] == "done" and wake["dropped"] is None
    assert await ticket_runs(office, ticket_id) == 2


async def test_ac5_newer_steer_keeps_wake(office: Office, runtime: FakeRuntime, people) -> None:
    ticket_id = await started(office, people)
    prompts: list[str] = []

    async def again(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.prompt)
        return ok(ctx)

    # S2 comes after the last tool call, so it is not delivered; the empty script answers the disposition reminder
    runtime.scripts += [steered(office, ticket_id, people[2].id, "S1", "S2"), ok, again]
    await office.scheduler.wait_idle(timeout=20)
    [wake] = await steer_wakes(office, ticket_id)
    assert wake["status"] == "done" and wake["dropped"] is None
    assert await ticket_runs(office, ticket_id) == 2
    assert "Direction from Minh: S2" in prompts[0]


async def test_ac5_drop_keeps_a_wake_that_changed_meanwhile(office: Office, people) -> None:
    """The run reads the wake, then awaits: a steer coalesced or a claim in between must keep the wake (R-3)."""
    ticket_id = await started(office, people)
    ai = await office.ensure_ai()
    await office.db.execute("UPDATE wake_requests SET status = 'done' WHERE ticket_id = ?", (ticket_id,))
    await office.wakes.enqueue(ai.id, WakeSource.COMMENT, ticket_id=ticket_id, payload={"steer": "S1"})
    seen = await office.wakes.pending_for(ai.id, ticket_id)
    assert seen is not None
    await office.wakes.enqueue(ai.id, WakeSource.COMMENT, ticket_id=ticket_id, payload={"steer": "S2"})
    assert not await office.wakes.drop_unchanged(seen, "x")
    current = await office.wakes.pending_for(ai.id, ticket_id)
    assert current is not None and current.id == seen.id
    async with office.db.tx() as tx:
        assert await office.wakes.claim(current.id, "run_x", tx)
    assert not await office.wakes.drop_unchanged(current, "x")
    row = await office.db.fetchone("SELECT status FROM wake_requests WHERE id = ?", (current.id,))
    assert row["status"] == "claimed"


def test_only_comments() -> None:
    assert only_comments([{"steer": "a"}, {"comment_id": 3}])
    assert not only_comments([])
    assert not only_comments([{"steer": "a"}, {"change": True}])
    assert not only_comments([{"comment_id": 3}, {"interaction_id": "ix"}])
    assert not only_comments([{"stage": "pr"}])


# ---- BR-3/BR-4: comments wait while a gate card is open --------------------------------------------------------


async def rules_ticket(office: Office, tmp_path: Path) -> int:
    """A build stage with only review_clean, then pr and close; a gate rule on src/** asks a lead (gate 'rules')."""
    from actoroffice.domain.stage import StageDef, StageTemplate
    from actoroffice.domain.team import TeamSettings

    project = await repo_project(office, tmp_path)
    ticket = await office.tickets.create("Tổng tiền", description="x", project_id=project.id)
    stages = [StageDef(id="build", checks=["review_clean"]), StageDef(id="pr"), StageDef(id="close")]
    team = await office.teams.create_team("T", settings=TeamSettings(stage_templates={"code": StageTemplate(name="code", stages=stages)}))
    await office.db.execute("UPDATE tickets SET stage = 'build', stage_template = 'code', team_id = ? WHERE id = ?", (team.id, ticket.id))
    await office.rules.propose(
        title="Code cần lead", body="", kind="gate", scope=f"team:{team.id}", spec={"roles": ["lead"], "when": {"paths": ["src/**"]}}, by="user", activate=True
    )
    return ticket.id


def build_done(outcomes: list[str], before_complete=None):
    async def script(ctx: TurnCtx) -> TurnResult:
        change(ctx)
        if before_complete is not None:
            await before_complete(ctx)
        outcomes.append(await ctx.office("stage_complete", {"summary": "xong"}))
        return ok(ctx)

    return script


async def test_ac6_comment_and_steer_wait_for_rules_card(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    ticket_id = await rules_ticket(office, tmp_path)
    outcomes: list[str] = []
    prompts: list[str] = []

    async def pr(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.prompt)
        return ok(ctx)

    runtime.scripts += [build_done(outcomes), reviewer(findings()), pr]
    await office.tickets.assign(ticket_id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=30)
    assert "gate 'rules'" in outcomes[0]

    await office.tickets.add_comment(ticket_id, "Có cần thêm log không?", author_kind="user", author_id=LOCAL_OWNER_ID)
    await office.stages.steer(ticket_id, "Đổi tên hàm thành grand_total", by=LOCAL_OWNER_ID)
    await office.scheduler.wait_idle(timeout=30)
    assert await stage_runs(office, ticket_id, "build") == 1
    assert await reviewer_runs(office) == 1
    assert len(await stage_cards(office, ticket_id)) == 1
    assert await ticket_runs(office, ticket_id) == 1
    assert (await office.stages.status(ticket_id))["status"] == "needs_you"

    [card] = await stage_cards(office, ticket_id)
    await office.answer(card.id, {"approved": True}, by=LOCAL_OWNER_ID)
    await office.scheduler.wait_idle(timeout=30)
    assert (await office.tickets.get(ticket_id)).stage == "pr"
    assert "Đổi tên hàm thành grand_total" in prompts[0] and "Có cần thêm log không?" in prompts[0]


async def test_ac7_comment_waits_for_requirement_card(office: Office, runtime: FakeRuntime, people) -> None:
    ticket_id = await started(office, people)
    _, lan, minh = people
    prompts: list[str] = []

    async def understand(ctx: TurnCtx) -> TurnResult:
        await ctx.office("doc_put", {"key": "requirements", "body": REQUIREMENTS, "base_revision": None})
        await ctx.office("dossier_update", {"section": "context", "body": "Khách cần CSV", "sources": ["msg:1"]})
        out = await ctx.office("stage_complete", {"summary": "2 AC"})
        assert "gate" in out
        return ok(ctx)

    async def plan(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.prompt)
        return ok(ctx)

    runtime.scripts += [understand, plan]
    await office.scheduler.wait_idle(timeout=20)
    assert len(await stage_cards(office, ticket_id)) == 1

    await office.tickets.add_comment(ticket_id, "Thêm cột ngày nhé", author_kind="user", author_id=lan.id)
    await office.stages.steer(ticket_id, "Mã hoá UTF-8", by=minh.id)
    await office.scheduler.wait_idle(timeout=20)
    assert await stage_runs(office, ticket_id, "understand") == 1
    assert len(await stage_cards(office, ticket_id)) == 1
    assert await ticket_runs(office, ticket_id) == 1

    [card] = await stage_cards(office, ticket_id)
    await office.answer(card.id, {"approved": True}, by=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    assert (await office.tickets.get(ticket_id)).stage == "plan"
    assert "Mã hoá UTF-8" in prompts[0] and "Thêm cột ngày nhé" in prompts[0]


async def test_ac8_change_wake_opens_new_attempt_and_cancels_card(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    ticket_id = await rules_ticket(office, tmp_path)
    outcomes: list[str] = []
    runtime.scripts += [build_done(outcomes), reviewer(findings()), ok]
    await office.tickets.assign(ticket_id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=30)
    [card] = await stage_cards(office, ticket_id)

    await office.tickets.change_notice(ticket_id, "Tổng tiền phải làm tròn", by="user")
    await office.scheduler.wait_idle(timeout=30)
    assert [r.attempt for r in await office.stages.runs(ticket_id) if r.stage == "build"] == [1, 2]
    assert (await office.interactions.get(card.id)).status == InteractionStatus.CANCELLED
    assert await stage_cards(office, ticket_id) == []


async def test_ac11_external_gate_follow_up_runs_in_the_same_attempt(office: Office, runtime: FakeRuntime) -> None:
    """Regression: a pr waiting for its merge (external gate) still answers comments in the same attempt."""
    from actoroffice.domain.stage import StageDef, StageTemplate
    from actoroffice.domain.team import TeamSettings

    stages = [StageDef(id="pr", gate="merge"), StageDef(id="close")]
    team = await office.teams.create_team("T", settings=TeamSettings(stage_templates={"code": StageTemplate(name="code", stages=stages)}))
    ticket = await office.tickets.create("PR", description="x")
    await office.db.execute("UPDATE tickets SET stage = 'pr', stage_template = 'code', team_id = ? WHERE id = ?", (team.id, ticket.id))
    outcomes: list[str] = []
    prompts: list[str] = []

    async def open_pr(ctx: TurnCtx) -> TurnResult:
        outcomes.append(await ctx.office("stage_complete", {"summary": "PR #1"}))
        return ok(ctx)

    async def follow_up(ctx: TurnCtx) -> TurnResult:
        prompts.append(ctx.spec.prompt)
        await ctx.office("ticket_set_status", {"status": "in_review", "comment": "đã trả lời, chờ merge"})
        return ok(ctx)

    runtime.scripts += [open_pr, follow_up]
    await office.tickets.assign(ticket.id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert "external gate 'merge'" in outcomes[0]
    await office.tickets.add_comment(ticket.id, "Sửa tên nhánh", author_kind="user", author_id=LOCAL_OWNER_ID)
    await office.scheduler.wait_idle(timeout=20)
    assert "Sửa tên nhánh" in prompts[0]
    assert [r.attempt for r in await office.stages.runs(ticket.id) if r.stage == "pr"] == [1]
    assert await ticket_runs(office, ticket.id) == 2


# ---- BR-5: a new stage run withdraws the stale gate cards --------------------------------------------------------


async def open_card(office: Office, ticket_id: int, stage: str) -> str:
    payload = {"stage": stage, "gate": "requirement", "roles": ["ba"], "min": 1}
    return (await office.interactions.create(InteractionKind.APPROVE_STAGE, payload, ticket_id=ticket_id)).id


@pytest.mark.parametrize("how", ["advance", "redo", "reopen", "switch_template", "retry", "ensure_run"])
async def test_ac9_pending_stage_cards_cancelled_on_new_stage_run(office: Office, people, how: str) -> None:
    ticket_id = await started(office, people)
    ticket = await office.tickets.get(ticket_id)
    stale = [await open_card(office, ticket_id, "understand"), await open_card(office, ticket_id, "understand")]
    if how in ("advance", "redo"):
        answered = await open_card(office, ticket_id, "understand")
        await office.answer(answered, {"approved": how == "advance", "note": "x"}, by=LOCAL_OWNER_ID)
    elif how == "reopen":
        await office.stages.reopen(ticket_id, "understand", reason="thiếu AC", by=LOCAL_OWNER_ID)
    elif how == "switch_template":
        await office.stages.switch_template(ticket_id, "quick", reason="việc nhỏ", by=LOCAL_OWNER_ID)
    elif how == "retry":
        await office.stages.retry(ticket_id, by=LOCAL_OWNER_ID)
    else:
        await office.db.execute(
            "UPDATE stage_runs SET finished_at = '2026-11-03T00:00:00', outcome = 'completed' WHERE ticket_id = ?", (ticket_id,)
        )
        await office.stages.ensure_run(ticket)
    for item_id in stale:
        assert (await office.interactions.get(item_id)).status == InteractionStatus.CANCELLED
    assert await office.interactions.pending(ticket_id=ticket_id) == []
    assert (await office.stages.status(ticket_id))["status"] != "needs_you"


# ---- AC-10: the SI-12 sequence ------------------------------------------------------------------------------------


async def test_ac10_si12_sequence(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    ticket_id = await rules_ticket(office, tmp_path)
    outcomes: list[str] = []

    async def steer_mid_build(ctx: TurnCtx) -> None:
        await office.stages.steer(ticket_id, "Thêm test làm tròn", by=LOCAL_OWNER_ID)
        decision = await ctx.gate.before_tool("Read", {"file_path": "src/app.py"}, None)
        assert decision.decision == GateDecision.DENY and "NEW DIRECTION" in decision.reason

    runtime.scripts += [build_done(outcomes, steer_mid_build), reviewer(findings()), ok]
    await office.tickets.assign(ticket_id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=30)
    assert "gate 'rules'" in outcomes[0]
    assert await stage_runs(office, ticket_id, "build") == 1 and await reviewer_runs(office) == 1

    [card] = await stage_cards(office, ticket_id)
    await office.answer(card.id, {"approved": True}, by=LOCAL_OWNER_ID)
    await office.scheduler.wait_idle(timeout=30)
    assert (await office.tickets.get(ticket_id)).stage == "pr"
    assert await stage_runs(office, ticket_id, "build") == 1
    row = await office.db.fetchone("SELECT COUNT(*) AS n FROM interactions WHERE ticket_id = ? AND kind = 'approve_stage'", (ticket_id,))
    assert int(row["n"]) == 1
    assert await stage_cards(office, ticket_id) == []


async def test_a_tool_call_after_stage_complete_keeps_the_gate_card(office, runtime) -> None:
    """SI-16: step_done right after a passing stage_complete opened a second attempt and cancelled the plan card."""
    from actoroffice.domain.run import TurnResult, TurnStatus

    ticket = await office.tickets.create("x", description="x")
    plan = "Problem: a.\nFix: b.\n- AC-1: Given x, When y, Then z."

    async def scope(ctx):
        await ctx.office("doc_put", {"key": "plan", "body": plan, "base_revision": None})
        await ctx.office("plan_steps", {"steps": ["viết plan"]})
        await ctx.office("stage_complete", {"summary": "xong"})
        await ctx.office("step_done", {"index": 1, "note": "late tick"})
        await ctx.office("stage_info", {})
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts.append(scope)
    await office.stages.start(ticket.id, template="quick", by="user")
    await office.scheduler.wait_idle(timeout=30)
    cards = [i for i in await office.interactions.pending(ticket_id=ticket.id) if i.kind.value == "approve_stage"]
    assert len(cards) == 1 and cards[0].payload["stage"] == "scope"
    assert [r.attempt for r in await office.stages.runs(ticket.id)] == [1]
