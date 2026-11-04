"""Team copilot: one AI engineer, people with roles, a ticket going through stages with human gates."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.errors import PolicyDenied
from actoroffice.domain.ids import new_id
from actoroffice.domain.run import TurnResult, TurnStatus, Usage
from actoroffice.domain.work import InteractionKind, InteractionStatus, Project, ProjectAccess, Ticket, TicketStatus
from actoroffice.runtimes.base import GateDecision
from actoroffice.tools.proposal_tools import propose_tickets

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db

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
    seed_db(o.settings.db_path)
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


# ---- SI-31: one ticket at a time per project ------------------------------------------------------------


IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *IDENTITY, *args], check=True, capture_output=True, text=True).stdout.strip()


async def code_project(office: Office, tmp_path: Path, name: str = "Leave") -> Project:
    """A code project on a git worktree (an empty folder becomes a repo with one commit on main)."""
    return await office.projects.create(name, path=str(tmp_path / name.lower()), access=ProjectAccess.WORKTREE)


async def begin(office: Office, project: Project, title: str) -> Ticket:
    ticket = await office.tickets.create(title, description="x", project_id=project.id)
    return await office.stages.start(ticket.id, template="code", by="user")


async def drop_wakes(office: Office, *tickets: Ticket) -> None:
    for ticket in tickets:
        await office.db.execute("DELETE FROM wake_requests WHERE ticket_id = ?", (ticket.id,))


async def close(office: Office, ticket: Ticket, to: TicketStatus) -> None:
    if to == S.DONE:
        ai = await office.ensure_ai()
        run_id = new_id("run")
        await office.tickets.checkout(ticket.id, ai.id, run_id)
        await office.tickets.transition(ticket.id, S.DONE, by="Copilot", run_id=run_id)
    else:
        await office.tickets.transition(ticket.id, to, by="user")


async def assert_waits(office: Office, ticket: Ticket, front: Ticket) -> None:
    """`ticket` waits for `front` in the project's queue: blocked, no run starts for it, its wake is kept."""
    current = await office.tickets.get(ticket.id)
    assert current.status == S.BLOCKED
    assert await office.tickets.open_blockers(ticket.id) == [front.key]
    assert await office.tickets.queue_blockers(ticket.id) == [front.key]
    assert await office.scheduler.tick() == []
    assert await office.wakes.pending_for((await office.ensure_ai()).id, ticket.id) is not None


@pytest.mark.parametrize("path", ["stages.start", "backlog", "proposal"])
async def test_ac1_second_ticket_waits(office: Office, tmp_path: Path, path: str) -> None:
    from .test_propose_tickets import cards, ctx_for

    project = await code_project(office, tmp_path)
    a = await begin(office, project, "SI-a")
    await drop_wakes(office, a)  # SI-a's own run is not what this test looks at
    if path == "stages.start":
        b = await begin(office, project, "SI-b")
    elif path == "backlog":
        b = await office.tickets.create("SI-b", description="x", project_id=project.id, status=S.BACKLOG)
        b = await office.proposals.start_backlog(b.key, by="user")
    else:
        dm = await office.chat.dm((await office.ensure_ai()).id)
        item = {"title": "SI-b", "why": "w", "description": "d", "workflow": "code", "project": project.name}
        await propose_tickets({"items": [item]}, await ctx_for(office, conversation=dm.id))
        [card] = await cards(office)
        await office.answer(card.id, {"approved": True}, by="user")
        b = await office.tickets.get((await office.interactions.get(card.id)).response["created"][0])
    assert b.stage == "understand"
    await assert_waits(office, b, a)
    comments = [c.body for c in await office.tickets.comments(b.id)]
    assert any(f"Chờ {a.key} xong" in body for body in comments)


async def test_ac2_status_waiting_label_beats_pending_wake(office: Office, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    a = await begin(office, project, "SI-a")
    b = await begin(office, project, "SI-b")
    assert await office.wakes.pending_for((await office.ensure_ai()).id, b.id) is not None
    status = await office.stages.status(b.id)
    assert (status["status"], status["status_label"], status["waiting_for"]) == ("blocked", f"đang chờ {a.key} xong", a.key)
    plain = await office.stages.status(a.id)
    assert plain["status"] == "queued" and plain["waiting_for"] is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_ac3_done_releases_and_worktree_from_latest_main(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    repo = Path(project.path or "")
    a = await begin(office, project, "SI-a")
    await drop_wakes(office, a)
    b = await begin(office, project, "SI-b")
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    git(repo, "add", "a.py")
    git(repo, "commit", "-q", "-m", "SI-a merged")
    tip = git(repo, "rev-parse", "main")

    await close(office, a, S.DONE)

    assert (await office.tickets.get(b.id)).status == S.TODO
    assert await office.tickets.open_blockers(b.id) == []
    wake = await office.wakes.pending_for((await office.ensure_ai()).id, b.id)
    assert wake is not None and "blockers_resolved" in [r["source"] for r in wake.payload["reasons"]]
    heads: list[str] = []

    async def first_turn(ctx: TurnCtx) -> TurnResult:
        heads.append(git(ctx.spec.cwd, "rev-parse", "HEAD"))
        return done(ctx)

    runtime.scripts.append(first_turn)
    assert len(await office.scheduler.tick()) == 1
    await office.scheduler.wait_idle(timeout=20)
    assert heads[0] == tip


async def test_ac4_cancel_releases(office: Office, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    a = await begin(office, project, "SI-a")
    b = await begin(office, project, "SI-b")
    await close(office, a, S.CANCELLED)
    assert (await office.tickets.get(b.id)).status == S.TODO
    assert await office.wakes.pending_for((await office.ensure_ai()).id, b.id) is not None
    assert (await office.stages.status(b.id))["waiting_for"] is None


async def test_ac5_cancel_middle_moves_up(office: Office, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    a = await begin(office, project, "SI-a")
    await drop_wakes(office, a)
    b = await begin(office, project, "SI-b")
    c = await begin(office, project, "SI-c")
    assert await office.tickets.queue_blockers(c.id) == [b.key]

    await close(office, b, S.CANCELLED)

    await assert_waits(office, c, a)
    assert (await office.stages.status(c.id))["status_label"] == f"đang chờ {a.key} xong"
    await close(office, a, S.DONE)
    assert (await office.tickets.get(c.id)).status == S.TODO
    assert await office.tickets.open_blockers(c.id) == []


async def test_ac6_option_off_runs_as_before(office: Office, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    await office.projects.set_one_at_a_time(project.id, False)
    await begin(office, project, "SI-a")
    b = await begin(office, project, "SI-b")
    assert b.status == S.TODO
    assert await office.tickets.open_blockers(b.id) == [] and await office.tickets.queue_blockers(b.id) == []
    assert await office.wakes.pending_for((await office.ensure_ai()).id, b.id) is not None


async def test_ac7_subtickets_and_plain_tickets_not_queued(office: Office, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    ai = await office.ensure_ai()
    a = await begin(office, project, "SI-a")
    a1 = await office.tickets.create("SI-a1", description="x", parent_id=a.id, assignee_actor_id=ai.id)
    t = await office.tickets.create("SI-t", description="x", project_id=project.id, assignee_actor_id=ai.id)
    for ticket in (a1, t):
        current = await office.tickets.get(ticket.id)
        assert current.status == S.TODO and await office.tickets.open_blockers(ticket.id) == []
        assert await office.wakes.pending_for(ai.id, ticket.id) is not None

    await close(office, a, S.DONE)
    b = await begin(office, project, "SI-b")
    assert b.status == S.TODO and await office.tickets.open_blockers(b.id) == []


async def test_ac8_turn_off_releases_queue_only(office: Office, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    ai = await office.ensure_ai()
    a = await begin(office, project, "SI-a")
    d = await office.tickets.create("SI-d", description="x")  # a real dependency, in another project
    b = await office.tickets.create("SI-b", description="x", project_id=project.id, blocked_by=[d.id])
    b = await office.stages.start(b.id, template="code", by="user")
    e = await begin(office, project, "SI-e")
    assert sorted(await office.tickets.open_blockers(b.id)) == sorted([a.key, d.key])
    await drop_wakes(office, e)

    project = await office.projects.set_one_at_a_time(project.id, False)
    released = await office.tickets.release_queue(project.id)

    assert released == 1 and not project.runs_one_at_a_time
    assert (await office.tickets.get(b.id)).status == S.BLOCKED
    assert await office.tickets.open_blockers(b.id) == [d.key] and await office.tickets.queue_blockers(b.id) == []
    assert (await office.stages.status(b.id))["waiting_for"] is None
    assert (await office.tickets.get(e.id)).status == S.TODO and await office.tickets.open_blockers(e.id) == []
    assert await office.wakes.pending_for(ai.id, e.id) is not None


async def test_ac8_turn_off_does_not_wake_released_tickets_again(office: Office, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    ai = await office.ensure_ai()
    a = await begin(office, project, "SI-a")
    b = await begin(office, project, "SI-b")
    await close(office, a, S.DONE)  # SI-b runs now; its queue edge to the closed SI-a stays
    await drop_wakes(office, b)

    await office.projects.set_one_at_a_time(project.id, False)

    assert await office.tickets.release_queue(project.id) == 0
    assert await office.wakes.pending_for(ai.id, b.id) is None


async def test_ac9_ticket_just_started_counts_as_latest(office: Office, tmp_path: Path) -> None:
    """Review F2: a ticket whose first stage run is not written yet is the newest of the project, never the oldest."""
    project = await code_project(office, tmp_path)
    await begin(office, project, "SI-y")
    x = await office.tickets.create("SI-x", description="x", project_id=project.id)
    await office.db.execute("UPDATE tickets SET stage = 'understand', stage_template = 'code' WHERE id = ?", (x.id,))
    z = await begin(office, project, "SI-z")
    assert await office.tickets.queue_blockers(z.id) == [x.key]


async def test_ac9_turn_on_keeps_running_then_queues_after_latest(office: Office, tmp_path: Path) -> None:
    project = await code_project(office, tmp_path)
    await office.projects.set_one_at_a_time(project.id, False)
    a = await begin(office, project, "SI-a")
    b = await begin(office, project, "SI-b")

    project = await office.projects.set_one_at_a_time(project.id, True)

    assert project.runs_one_at_a_time
    for ticket in (a, b):
        assert (await office.tickets.get(ticket.id)).status == S.TODO and await office.tickets.open_blockers(ticket.id) == []
    c = await begin(office, project, "SI-c")
    assert await office.tickets.queue_blockers(c.id) == [b.key]


async def test_ac10_defaults_by_kind_and_access(office: Office, tmp_path: Path) -> None:
    cases = [("code", ProjectAccess.WORKTREE, True), ("code", ProjectAccess.DIRECT, True), ("code", ProjectAccess.READ_ONLY, False), ("reference", ProjectAccess.WORKTREE, False)]
    for kind, access, expected in cases:
        stored = Project(id=f"old-{kind}-{access.value}", name=f"{kind}-{access.value}", kind=kind, access=access).model_dump(exclude={"one_at_a_time", "runs_one_at_a_time"})
        assert "one_at_a_time" not in stored  # a project saved before SI-31
        await office.db.execute(
            "INSERT INTO projects(id, name, config_json, created_at) VALUES (?, ?, ?, ?)", (stored["id"], stored["name"], json.dumps(stored), "2026-11-01T00:00:00Z")
        )
        assert (await office.projects.get(stored["id"])).runs_one_at_a_time is expected
        folder = tmp_path / f"new-{kind}-{access.value}"
        folder.mkdir()
        created = await office.projects.create(f"new-{kind}-{access.value}", path=str(folder), access=access)
        created = await office.projects.configure(created.id, kind=kind)
        assert created.runs_one_at_a_time is expected
        assert created.model_dump()["runs_one_at_a_time"] is expected


async def test_ac10_migration_adds_blocker_kind(office: Office) -> None:
    columns = {r["name"]: r for r in await office.db.fetchall("PRAGMA table_info(ticket_blockers)")}
    assert columns["kind"]["dflt_value"] == "'dep'" and columns["kind"]["notnull"] == 1
    a = await office.tickets.create("A", description="x")
    b = await office.tickets.create("B", description="x", blocked_by=[a.id])
    row = await office.db.fetchone("SELECT kind FROM ticket_blockers WHERE ticket_id = ?", (b.id,))
    assert row["kind"] == "dep" and await office.tickets.queue_blockers(b.id) == []
