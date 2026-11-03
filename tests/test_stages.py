"""Team copilot: one AI engineer, people with roles, a ticket going through stages with human gates."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.errors import PolicyDenied
from actoroffice.domain.run import TurnResult, TurnStatus, Usage
from actoroffice.domain.work import InteractionKind, InteractionStatus, TicketStatus
from actoroffice.runtimes.base import GateDecision

from .fakes import FakeRuntime, TurnCtx

S = TicketStatus

REQUIREMENTS = """\
# Hoàn tiền
## Acceptance criteria
- AC-1: Given đơn đã thanh toán trong 30 ngày, When khách yêu cầu hoàn tiền, Then hệ thống tạo lệnh hoàn.
- AC-2: Given đơn quá 30 ngày, When khách yêu cầu, Then hệ thống từ chối với lý do.
"""

PLAN_GOOD = """\
## Phương án A: thêm RefundPolicy
Ưu: tách luật. Nhược: thêm class.
## Phương án B: sửa trực tiếp RefundService
Ưu: nhanh. Nhược: khó test.
Khuyến nghị: A.
## Test plan
| AC | Test |
| AC-1 | test_ac1_refund_within_30_days |
| AC-2 | test_ac2_refund_rejected_after_30_days |
"""


def done(ctx: TurnCtx, text: str = "") -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", text=text, usage=Usage(input_tokens=100, output_tokens=20))


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
    hoa = await office.teams.create_user("Hoa", telegram_id=333)
    await office.teams.add_member(team.id, lan.id, ["ba"])
    await office.teams.add_member(team.id, minh.id, ["dev"])
    await office.teams.add_member(team.id, hoa.id, ["qe"])
    return team, lan, minh, hoa


async def pending(office: Office, kind: InteractionKind, ticket_id: int):
    return [i for i in await office.interactions.pending(ticket_id=ticket_id) if i.kind == kind]


async def test_code_ticket_goes_through_stages_with_role_gates(office: Office, runtime: FakeRuntime, people) -> None:
    team, lan, minh, hoa = people
    ticket = await office.tickets.create("Hoàn tiền trong 30 ngày", description="Khách muốn hoàn tiền.")

    # understand #1: the AI asks the BA a multiple-choice question, then ends its turn
    async def ask(ctx: TurnCtx) -> TurnResult:
        assert "Current stage: understand" in ctx.spec.system_prompt
        assert "Lan: BA" in ctx.spec.system_prompt
        out = await ctx.office("ask_human", {"roles": ["ba"], "question": "Đơn VIP có giới hạn 30 ngày không?", "options": ["Có", "Không"], "recommended": "Có"})
        assert "Lan" in out
        return done(ctx)

    # understand #2: requirements + dossier, then finish the stage → BA gate
    async def write_requirements(ctx: TurnCtx) -> TurnResult:
        assert "Lan: Có" in ctx.spec.prompt  # the answer, with who answered
        await ctx.office("doc_put", {"key": "requirements", "body": REQUIREMENTS, "base_revision": None})
        out = await ctx.office("stage_complete", {"summary": "AC xong"})
        assert "dossier" in out  # the dossier is a required output: the check fails first
        await ctx.office("dossier_update", {"section": "context", "body": "Khách cần hoàn tiền", "sources": ["msg:1"]})
        out = await ctx.office("stage_complete", {"summary": "Requirement có 2 AC"})
        assert "Lan" in out and "gate" in out
        return done(ctx)

    runtime.scripts += [ask, write_requirements]
    await office.stages.start(ticket.key, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)

    t = await office.tickets.get(ticket.id)
    assert t.status == S.IN_REVIEW and t.stage == "understand"
    status = await office.stages.status(t, viewer=lan.id)
    assert status["status"] == "needs_you" and status["who"] == ["Lan"] and status["needs_viewer"]
    assert not (await office.stages.status(t, viewer=minh.id))["needs_viewer"]

    question = (await pending(office, InteractionKind.QUESTION, ticket.id))[0]
    with pytest.raises(PolicyDenied):
        await office.answer(question.id, {"answer": "Có"}, by=minh.id)  # a Dev cannot answer a BA question
    await office.answer(question.id, {"answer": "Có"}, by=lan.id)
    await office.scheduler.wait_idle(timeout=20)

    gate = (await pending(office, InteractionKind.APPROVE_STAGE, ticket.id))[0]
    assert gate.payload["approver_names"] == ["Lan"] and gate.payload["stage"] == "understand"
    assert {c["id"] for c in gate.payload["checks"]} == {"outputs", "ac_testable"}
    with pytest.raises(PolicyDenied):
        await office.answer(gate.id, {"approved": True}, by=hoa.id)

    # plan: a fresh session; the first plan has no options → checks fail; then a good plan → Dev gate
    async def plan(ctx: TurnCtx) -> TurnResult:
        assert ctx.spec.resume_session_id is None
        assert "Current stage: plan" in ctx.spec.system_prompt and "Khách cần hoàn tiền" in ctx.spec.system_prompt
        await ctx.office("doc_put", {"key": "plan", "body": "Sửa RefundService.", "base_revision": None})
        out = await ctx.office("stage_complete", {"summary": "plan"})
        assert "plan_has_options" in out and "plan_has_ac_map" in out
        await ctx.office("doc_put", {"key": "plan", "body": PLAN_GOOD, "base_revision": 1})
        out = await ctx.office("stage_complete", {"summary": "2 phương án, khuyến nghị A"})
        assert "Minh" in out
        return done(ctx)

    runtime.scripts.append(plan)
    await office.answer(gate.id, {"approved": True}, by=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    assert (await office.tickets.get_document(ticket.id, "requirements")).approved_revision == 1

    # the Dev sends the plan back: same stage, attempt 2, the note reaches the AI
    async def replan(ctx: TurnCtx) -> TurnResult:
        assert "Reviewer's note: Dùng phương án B" in ctx.spec.prompt
        assert "attempt 2" in ctx.spec.system_prompt
        await ctx.office("stage_complete", {"summary": "Theo phương án B"})
        return done(ctx)

    runtime.scripts.append(replan)
    plan_gate = (await pending(office, InteractionKind.APPROVE_STAGE, ticket.id))[0]
    assert plan_gate.payload["approver_names"] == ["Minh"]
    await office.answer(plan_gate.id, {"approved": False, "note": "Dùng phương án B"}, by=minh.id)
    await office.scheduler.wait_idle(timeout=20)

    # build: steps are required, then handoff; no git workspace → tests_changed / repo checks are skipped
    async def build(ctx: TurnCtx) -> TurnResult:
        assert "Current stage: build" in ctx.spec.system_prompt
        out = await ctx.office("stage_complete", {"summary": "xong"})
        assert "steps_done" in out and "handoff" in out
        await ctx.office("plan_steps", {"steps": ["Viết test AC-1", "Code", "Chạy test"]})
        for i in (1, 2, 3):
            await ctx.office("step_done", {"index": i})
        await ctx.office("doc_put", {"key": "handoff", "body": "Chạy pytest", "base_revision": None})
        out = await ctx.office("stage_complete", {"summary": "Đã code và test"})
        assert "Next stage: pr" in out
        return done(ctx)

    async def open_pr(ctx: TurnCtx) -> TurnResult:
        out = await ctx.office("stage_complete", {"summary": "PR"})
        assert "pr_linked" in out
        await ctx.office("pr_link", {"url": "https://bitbucket.org/acme/billing/pull-requests/7"})
        out = await ctx.office("stage_complete", {"summary": "PR #7"})
        assert "external gate 'merge'" in out
        return done(ctx)

    runtime.scripts += [build, open_pr]
    plan_gate = (await pending(office, InteractionKind.APPROVE_STAGE, ticket.id))[0]
    await office.answer(plan_gate.id, {"approved": True}, by=minh.id)
    await office.scheduler.wait_idle(timeout=20)

    t = await office.tickets.get(ticket.id)
    assert t.stage == "pr" and t.pr_url and t.status == S.IN_REVIEW
    assert (await office.stages.status(t))["status"] == "waiting_external"

    async def close(ctx: TurnCtx) -> TurnResult:
        out = await ctx.office("stage_complete", {"summary": "Đã ghi kiến thức"})
        assert "kb_noted" in out  # knowledge must be written down (or explicitly "none")
        await ctx.office("kb_note", {"kind": "fact", "title": "Hạn hoàn tiền", "content": "Đơn thường 30 ngày, VIP 60 ngày", "sources": ["Lan, Q&A AO"]})
        await ctx.office("kb_note", {"kind": "decision", "title": "Tách RefundPolicy", "content": "Chọn phương án B theo Minh"})
        out = await ctx.office("stage_complete", {"summary": "Đã ghi 2 mục kiến thức"})
        assert "gate 'knowledge'" in out
        return done(ctx)

    runtime.scripts.append(close)
    await office.stages.external_passed(ticket.id, "merge", by=minh.id, note="PR #7 merged")
    await office.scheduler.wait_idle(timeout=20)

    knowledge_gate = (await pending(office, InteractionKind.APPROVE_STAGE, ticket.id))[0]
    assert knowledge_gate.payload["gate"] == "knowledge" and len(knowledge_gate.payload["kb"]) == 2
    await office.answer(knowledge_gate.id, {"approved": False, "note": "bỏ 2"}, by=lan.id)  # keep #1, drop #2
    statuses = [(i.title, i.status) for i in await office.knowledge.list(ticket_id=ticket.id)]
    assert sorted(statuses) == [("Hạn hoàn tiền", "verified"), ("Tách RefundPolicy", "rejected")]

    t = await office.tickets.get(ticket.id)
    assert t.status == S.DONE and t.stage == "close"
    runs = await office.stages.runs(ticket.id)
    assert [(r.stage, r.attempt, r.outcome) for r in runs] == [
        ("understand", 1, "completed"),
        ("plan", 1, "rejected"),
        ("plan", 2, "completed"),
        ("build", 1, "completed"),
        ("pr", 1, "completed"),
        ("close", 1, "completed"),
    ]
    assert runs[0].usage["input_tokens"] == 200  # two runs in the understand stage
    assert len({spec.resume_session_id for spec in runtime.specs if spec.resume_session_id}) <= 1


async def test_ac8_merge_gate_lists_review_acs(office: Office, runtime: FakeRuntime, people) -> None:
    from actoroffice.domain.stage import GatePolicy
    from actoroffice.domain.team import TeamSettings

    team = await office.teams.create_team("Merge nội bộ", settings=TeamSettings(gates={"merge": GatePolicy(roles=["dev"], fallback="owner", external=False)}))
    ticket = await office.tickets.create("Hoàn tiền", description="x")
    await office.db.execute(
        "UPDATE tickets SET stage = 'pr', stage_template = 'code', team_id = ?, pr_url = 'https://bitbucket.org/acme/billing/pull-requests/7' WHERE id = ?",
        (team.id, ticket.id),
    )
    requirements = REQUIREMENTS + "- AC-3 [review]: Given README, When đọc, Then có mục hoàn tiền.\n  Cách kiểm: đọc diff\n"
    await office.tickets.put_document(ticket.id, "requirements", requirements, author="Copilot", base_revision=None)
    await office.tickets.put_document(ticket.id, "handoff", "| AC-3 | đã đọc diff README |\n", author="Copilot", base_revision=None)
    outcomes: list[str] = []

    async def open_pr(ctx: TurnCtx) -> TurnResult:
        outcomes.append(await ctx.office("stage_complete", {"summary": "PR #7"}))
        return done(ctx)

    runtime.scripts.append(open_pr)
    await office.tickets.assign(ticket.id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert "gate 'merge'" in outcomes[0] and "external" not in outcomes[0]
    [gate] = await pending(office, InteractionKind.APPROVE_STAGE, ticket.id)
    assert gate.payload["review_acs"] == [
        {"id": "AC-3", "title": "AC-3 [review]: Given README, When đọc, Then có mục hoàn tiền.", "how": "đọc diff", "done": "| AC-3 | đã đọc diff README |"}
    ]


async def test_ai_cannot_mark_a_staged_ticket_done(office: Office, runtime: FakeRuntime, people) -> None:
    team, lan, *_ = people
    ticket = await office.tickets.create("Báo cáo", description="x")

    async def cheat(ctx: TurnCtx) -> TurnResult:
        from actoroffice.tools.registry import ToolError

        with pytest.raises(ToolError):
            await ctx.office("ticket_set_status", {"status": "done", "comment": "xong"})
        await ctx.office("ticket_set_status", {"status": "todo", "comment": "làm tiếp"})
        return done(ctx)

    runtime.scripts.append(cheat)
    await office.stages.start(ticket.key, template="research", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    assert (await office.tickets.get(ticket.id)).status != S.DONE


async def test_steer_interrupts_a_running_stage_and_pause_holds_wakes(office: Office, runtime: FakeRuntime, people) -> None:
    team, lan, minh, _ = people
    ticket = await office.tickets.create("Export CSV", description="x")
    seen: list[str] = []

    async def work(ctx: TurnCtx) -> TurnResult:
        await office.stages.steer(ticket.id, "Dùng dấu ; thay vì ,", by=minh.id)
        decision = await ctx.gate.before_tool("Read", {"file_path": "a.txt"}, None)
        seen.append(decision.decision.value)
        assert decision.decision == GateDecision.DENY
        return done(ctx)

    runtime.scripts.append(work)
    await office.stages.start(ticket.key, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    assert seen == ["deny"]

    await office.stages.pause(ticket.id, by=lan.id)
    await office.wakes.enqueue((await office.ensure_ai()).id, __import__("actoroffice.domain.run", fromlist=["WakeSource"]).WakeSource.MANUAL, ticket_id=ticket.id)
    before = len(runtime.specs)
    await office.scheduler.tick()
    assert len(runtime.specs) == before  # paused: nothing starts
    assert (await office.stages.status(ticket.id))["status"] == "paused"
    await office.stages.resume(ticket.id, by=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    assert len(runtime.specs) > before


async def test_activity_lines_have_a_sequence(office: Office, runtime: FakeRuntime, people) -> None:
    team, lan, *_ = people
    ticket = await office.tickets.create("Đọc code", description="x")

    async def work(ctx: TurnCtx) -> TurnResult:
        await ctx.sink("text", {"text": "Đọc RefundService để hiểu luật hiện tại."})
        await ctx.sink("tool_use", {"tool": "Read", "input": {"file_path": "src/main/java/a/b/RefundService.java"}, "id": "t1"})
        await ctx.sink("tool_result", {"id": "t1", "error": False, "text": "..."})
        await ctx.sink("tool_use", {"tool": "Bash", "input": {"command": "mvn -q test"}, "id": "t2"})
        await ctx.sink("tool_result", {"id": "t2", "error": True, "text": "Tests run: 3, Failures: 1"})
        await ctx.sink("tool_use", {"tool": "Edit", "input": {"file_path": "x/Refund.java", "old_string": "a", "new_string": "b\nc"}, "id": "t3"})
        return done(ctx)

    runtime.scripts.append(work)
    await office.stages.start(ticket.key, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    run = (await office.runs.for_ticket(ticket.id))[0]
    acts = await office.runs.activities(run.id)
    seqs = [a["seq"] for a in acts]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    kinds = [(a["kind"], a["status"]) for a in acts]
    assert ("narration", "ok") in kinds and ("read", "running") in kinds and ("read", "ok") in kinds
    assert ("command", "error") in kinds
    edit = next(a for a in acts if a["kind"] == "edit")
    assert edit["fields"]["added"] == 2 and edit["fields"]["removed"] == 1
    assert "a/b/RefundService.java" in next(a for a in acts if a["kind"] == "read")["title"]


async def test_question_for_a_role_and_people_lookup(office: Office, people) -> None:
    team, lan, minh, hoa = people
    ticket = await office.tickets.create("X", description="x")
    await office.db.execute("UPDATE tickets SET team_id = ?, owner_user_id = ? WHERE id = ?", (team.id, lan.id, ticket.id))
    t = await office.tickets.get(ticket.id)
    assert await office.teams.roles_for(minh.id, t) == {"dev"}
    assert await office.teams.roles_for(lan.id, t) == {"ba", "owner"}
    assert "dev" in await office.teams.roles_for("user", t)  # the local owner is admin
    await office.teams.set_participant(ticket.id, hoa.id, ["dev"])  # this ticket's own roles win over the team's
    from actoroffice.domain.stage import GatePolicy

    approvers = await office.teams.approvers(t, GatePolicy(roles=["dev"]))
    assert [u.name for u in approvers] == ["Hoa"]
    assert (await office.teams.user_by_telegram(222)).name == "Minh"
    item = await office.interactions.create(InteractionKind.QUESTION, {"question": "?", "roles": ["qe"]}, ticket_id=ticket.id)
    with pytest.raises(PolicyDenied):
        await office.answer(item.id, {"answer": "a"}, by=minh.id)
    resolved = await office.answer(item.id, {"answer": "a"}, by=hoa.id)
    assert resolved.status == InteractionStatus.ANSWERED and resolved.response["answered_by"] == "Hoa"
