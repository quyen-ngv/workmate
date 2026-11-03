"""Enterprise flows with a scripted runtime: BA → Dev Lead (meeting) → devs → QA → acceptance,
requirement changes mid-work, reminders, automations and read-only databases."""

from __future__ import annotations

import asyncio
import contextlib
import re
import sqlite3
import sys
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.core.schedule import next_after, normalize_repeat, parse_when
from actoroffice.domain.automation import AutomationStatus
from actoroffice.domain.chat import ConversationKind
from actoroffice.domain.run import TurnResult, TurnStatus, Usage
from actoroffice.domain.work import InteractionKind, InteractionStatus, ProjectAccess, TicketStatus
from actoroffice.runtimes.base import GateDecision
from actoroffice.services.automations import AutomationError
from actoroffice.tools.registry import ToolError

from .fakes import FakeRuntime, Script, TurnCtx
from .seed import seed_db

S = TicketStatus


@dataclass
class RoleRuntime(FakeRuntime):
    """Routes each turn to the next script of the actor running it (runs may interleave)."""

    by_actor: dict[str, list[Script]] = field(default_factory=dict)
    seen: list[tuple[str, str]] = field(default_factory=list)  # (actor, "chat" | "ticket")

    async def run_turn(self, spec, gate, sink):  # type: ignore[override]
        name = re.match(r"You are (\S+?),", spec.system_prompt).group(1)  # type: ignore[union-attr]
        kind = "chat" if spec.prompt.splitlines()[1].startswith("Conversation:") else "ticket"
        self.seen.append((name, kind))
        queue = self.by_actor.get(name) or []
        script = queue.pop(0) if queue else None
        self.specs.append(spec)
        result = await script(TurnCtx(spec, gate, sink)) if script else None
        return result or TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{spec.run_id}", usage=Usage(input_tokens=1))

    def add(self, actor: str, *scripts: Script) -> None:
        self.by_actor.setdefault(actor, []).extend(scripts)


@pytest.fixture
def runtime() -> RoleRuntime:
    return RoleRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    yield o
    await o.close()


async def hire(office: Office, position: str, name: str):
    pos = await office.org.find_position(position)
    return await office.org.hire(pos.id, name)


def key_of(text: str) -> str:
    return re.search(r"AO-\d+", text).group(0)  # type: ignore[union-attr]


def say(text: str) -> Script:
    async def script(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", text=text)

    return script


async def settle(office: Office) -> None:
    for _ in range(6):  # the notifier resolves answers asynchronously and may enqueue more work
        await office.scheduler.wait_idle(timeout=20)
        await asyncio.sleep(0.05)


# ---- the full BA → Lead → Dev → QA → acceptance flow ---------------------------------------------


async def test_feature_flows_from_ba_through_lead_meeting_to_devs_qa_and_acceptance(office, runtime):
    ba, lead = await hire(office, "ba", "Hoa"), await hire(office, "lead", "Long")
    await hire(office, "dev", "Minh")
    await hire(office, "dev", "Tu")
    await hire(office, "qa", "Quang")
    keys: dict[str, str] = {}
    workspaces: dict[str, Path] = {}

    async def ba_proposes(ctx: TurnCtx) -> TurnResult:
        await ctx.office("propose_ticket", {"title": "Nghỉ phép online", "description": "Scope đã chốt", "type": "feature"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s1", text="Em đã đề xuất ticket.")

    async def ba_requirements(ctx: TurnCtx) -> None:
        workspaces["ba"] = ctx.spec.cwd
        await ctx.office("doc_put", {"key": "requirements", "body": "# Requirements\nAC-1.1 Given..", "base_revision": None})
        out = await ctx.office(
            "ticket_create", {"title": "Giải pháp & kế hoạch: nghỉ phép", "description": "Thiết kế", "assignee": "Long", "type": "design"}
        )
        keys["design"] = key_of(out)
        await ctx.office("ticket_set_status", {"status": "blocked", "comment": "Chờ team giao", "blocked_by": [keys["design"]]})

    async def lead_asks_ba(ctx: TurnCtx) -> None:
        workspaces["lead"] = ctx.spec.cwd
        assert "requirements" in await ctx.office("ticket_get", {"ticket": keys["feature"]})
        out = await ctx.office("ask_colleague", {"colleague": "Hoa", "question": "Nghỉ nửa ngày có tính không? Ai duyệt?"})
        assert "Do not wait" in out
        await ctx.office("ticket_set_status", {"status": "in_review", "comment": "Đang hỏi BA"})

    async def ba_answers_in_meeting(ctx: TurnCtx) -> TurnResult:
        assert "Linked ticket" in ctx.spec.prompt  # the meeting group is tied to the feature
        doc = await ctx.office("doc_get", {"ticket": keys["feature"], "key": "requirements"})
        assert "AC-1.1" in doc
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s2", text="Có tính nửa ngày; quản lý trực tiếp duyệt.")

    async def lead_plans(ctx: TurnCtx) -> None:
        assert "quản lý trực tiếp duyệt" in ctx.spec.prompt  # the BA's answer woke the lead
        await ctx.office("doc_put", {"key": "design", "body": "# Design", "base_revision": None})
        await ctx.office("doc_put", {"key": "plan", "body": "# Plan T-1 T-2", "base_revision": None})
        t1 = key_of(await ctx.office("ticket_create", {"title": "API nghỉ phép", "description": "d", "assignee": "Minh", "due": "2026-10-20"}))
        t2 = key_of(await ctx.office("ticket_create", {"title": "UI nghỉ phép", "description": "d", "assignee": "Tu", "blocked_by": [t1]}))
        qa = key_of(await ctx.office("ticket_create", {"title": "Kiểm thử", "description": "d", "assignee": "Quang", "blocked_by": [t1, t2]}))
        keys.update(t1=t1, t2=t2, qa=qa)
        await ctx.office("ticket_set_status", {"status": "blocked", "comment": "Đã giao việc", "blocked_by": [t1, t2, qa]})

    def worker(name: str) -> Script:
        async def script(ctx: TurnCtx) -> None:
            workspaces[name] = ctx.spec.cwd
            with pytest.raises(ToolError, match="Definition of done"):  # no evidence yet
                await ctx.office("ticket_set_status", {"status": "done", "comment": "xong"})
            for key in ("test-plan", "test-report") if name == "Quang" else ("handoff",):
                await ctx.office("doc_put", {"key": key, "body": f"{key} của {name}", "base_revision": None})
            await ctx.office("ticket_set_status", {"status": "done", "comment": f"{name} xong, test pass"})

        return script

    async def lead_reports(ctx: TurnCtx) -> None:
        await ctx.office("ticket_set_status", {"status": "done", "comment": "Đã tích hợp, test pass"})

    async def ba_accepts(ctx: TurnCtx) -> None:
        await ctx.office("doc_put", {"key": "acceptance", "body": "AC-1.1 pass", "base_revision": None})
        await ctx.office("ticket_set_status", {"status": "done", "comment": "Nghiệm thu: tất cả AC đạt"})

    runtime.add("Hoa", ba_proposes, ba_requirements, ba_answers_in_meeting, ba_accepts)
    runtime.add("Long", lead_asks_ba, lead_plans, lead_reports)
    runtime.add("Minh", worker("Minh"))
    runtime.add("Tu", worker("Tu"))
    runtime.add("Quang", worker("Quang"))

    dm = await office.chat.dm(ba.id)
    await office.chat.post_user(dm.id, "Tôi muốn có tính năng xin nghỉ phép online")
    await settle(office)
    [proposal] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.CONFIRM_TICKET]
    assert proposal.payload["type"] == "feature"
    await office.answer(proposal.id, {"confirmed": True})
    keys["feature"] = (await office.tickets.list(limit=1))[0].key
    await settle(office)

    tree = {t.key: t for t in await office.tickets.tree(keys["feature"])}
    assert all(t.status == S.DONE for t in tree.values()), {k: t.status for k, t in tree.items()}
    assert tree[keys["t1"]].due_at == "2026-10-20"
    assert tree[keys["feature"]].type == "feature" and tree[keys["design"]].type == "design"
    # Everyone works in the feature's one workspace.
    assert len({str(p) for p in workspaces.values()}) == 1 and str(workspaces["ba"]).endswith(keys["feature"])
    # The planning meeting happened in a group tied to the feature, with the BA and the lead.
    rooms = [c for c in await office.chat.list() if c["kind"] == ConversationKind.GROUP.value and c["ticket_id"] == tree[keys["feature"]].id]
    assert len(rooms) == 1
    assert {ba.id, lead.id} <= set((await office.chat.get(rooms[0]["id"])).actor_ids)
    room_msgs = [m.body for m in await office.chat.messages(rooms[0]["id"])]
    assert any("@Hoa" in b for b in room_msgs) and any("quản lý trực tiếp" in b for b in room_msgs)
    # The lead's chat persona was never woken by the BA's answer (its ticket run got it instead).
    assert ("Long", "chat") not in runtime.seen
    # The BA reports acceptance back in the chat where the user asked.
    reports = [m for m in await office.chat.messages(dm.id) if m.meta.get("report")]
    assert any("Nghiệm thu" in m.body for m in reports)
    # The requester's chat only gets the feature's report; the team's reports live in the feature room.
    assert {m.author_id for m in reports} == {ba.id}
    room_reports = [m for m in await office.chat.messages(rooms[0]["id"]) if m.meta.get("report")]
    assert {m.meta["ticket_key"] for m in room_reports} >= {keys["t1"], keys["t2"], keys["qa"], keys["design"]}


async def test_user_can_answer_a_colleague_question_in_the_meeting_group(office, runtime):
    await hire(office, "ba", "Hoa")
    lead = await hire(office, "lead", "Long")
    seen: list[str] = []

    async def ask(ctx: TurnCtx) -> None:
        await ctx.office("ask_colleague", {"colleague": "Hoa", "question": "Deadline?"})
        await ctx.office("ticket_set_status", {"status": "in_review", "comment": "hỏi"})

    async def resume(ctx: TurnCtx) -> None:
        seen.append(ctx.spec.prompt)
        for key in ("design", "plan"):
            await ctx.office("doc_put", {"key": key, "body": key, "base_revision": None})
        await ctx.office("ticket_set_status", {"status": "done", "comment": "ok"})

    runtime.add("Long", ask, resume)
    runtime.add("Hoa", say("(no reply)"))
    ticket = await office.tickets.create("Thiết kế", assignee_actor_id=lead.id)
    await settle(office)
    [q] = await office.interactions.pending(ticket_id=ticket.id)
    await office.chat.post_user(q.payload["conversation_id"], "tiếp tục đi")  # not addressed to anyone: not an answer
    await settle(office)
    assert (await office.interactions.get(q.id)).status == InteractionStatus.PENDING
    await office.chat.post_user(q.payload["conversation_id"], "@Long Deadline là 30/10")
    await settle(office)
    assert (await office.tickets.get(ticket.id)).status == S.DONE
    assert "user answered: @Long Deadline là 30/10" in seen[0]


# ---- requirement changes ----------------------------------------------------------------------


async def test_change_reaches_running_queued_and_done_work(office, runtime):
    ba, dev, qa = await hire(office, "ba", "Hoa"), await hire(office, "dev", "Minh"), await hire(office, "qa", "Quang")
    dm = await office.chat.dm(ba.id)
    root = await office.tickets.create("Báo cáo doanh thu", assignee_actor_id=None, origin_conversation_id=dm.id)
    done = await office.tickets.create("Export PDF", parent_id=root.id, status=S.DONE)
    work = await office.tickets.create("Bảng tổng hợp", parent_id=root.id, assignee_actor_id=dev.id)
    queued = await office.tickets.create("Test báo cáo", parent_id=root.id, assignee_actor_id=qa.id, blocked_by=[work.id], status=S.BLOCKED)
    interrupted: list[str] = []
    first_prompts: list[str] = []

    async def dev_works(ctx: TurnCtx) -> None:
        assert ctx.spec.env["GIT_AUTHOR_NAME"] == "Minh (Developer)" and ctx.spec.env["GIT_AUTHOR_EMAIL"] == "minh@actor-office.local"
        assert await ctx.tool("Write", {"file_path": str(ctx.spec.cwd / "a.py"), "content": "x"}) == GateDecision.ALLOW
        # Meanwhile the BA records a change on the whole tree.
        await office.tickets.change_notice(root.id, "Thêm cột lợi nhuận; bỏ PDF, dùng Excel", by="Hoa", reopen_done=True)
        result = await ctx.gate.before_tool("Write", {"file_path": str(ctx.spec.cwd / "b.py"), "content": "y"}, None)
        interrupted.append(result.reason or "")
        assert result.decision == GateDecision.DENY
        assert await ctx.tool("Write", {"file_path": str(ctx.spec.cwd / "b.py"), "content": "y"}) == GateDecision.ALLOW  # once only
        await ctx.office("ticket_set_status", {"status": "done", "comment": "Xong, đã thêm cột lợi nhuận theo thay đổi"})

    async def qa_tests(ctx: TurnCtx) -> None:
        first_prompts.append(ctx.spec.prompt)
        await ctx.office("ticket_set_status", {"status": "done", "comment": "pass"})

    runtime.add("Minh", dev_works)
    runtime.add("Quang", qa_tests)
    await settle(office)

    assert "REQUIREMENTS CHANGED" in interrupted[0] and "lợi nhuận" in interrupted[0]
    assert "Requirement changes recorded" in first_prompts[0] and "Excel" in first_prompts[0]  # queued work sees it up front
    reopened = await office.tickets.get(done.id)
    assert reopened.status == S.TODO  # reopen_done brought the PDF ticket back
    comments = [c.body for c in await office.tickets.comments(queued.id)]
    assert any(c.startswith("⚠️ Thay đổi yêu cầu") for c in comments)
    notices = [m.body for m in await office.chat.messages(dm.id) if "thay đổi yêu cầu" in m.body]
    assert notices and "mở lại" in notices[0]


async def test_change_request_from_chat_hangs_under_the_feature(office, runtime):
    ba = await hire(office, "ba", "Hoa")
    feature = await office.tickets.create("Nghỉ phép", assignee_actor_id=None)

    async def propose_cr(ctx: TurnCtx) -> TurnResult:
        await ctx.office(
            "propose_ticket",
            {"title": "CR: nghỉ nửa ngày", "description": "...", "type": "change_request", "parent": feature.key},
        )
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", text="Em ghi nhận thay đổi")

    async def handle_cr(ctx: TurnCtx) -> None:
        await ctx.office("doc_put", {"ticket": feature.key, "key": "requirements", "body": "v2", "base_revision": None})
        out = await ctx.office("change_notify", {"summary": "Cho phép nghỉ nửa ngày"})
        assert feature.key in out
        await ctx.office("ticket_set_status", {"status": "done", "comment": "Đã cập nhật yêu cầu"})

    runtime.add("Hoa", propose_cr, handle_cr)
    dm = await office.chat.dm(ba.id)
    await office.chat.post_user(dm.id, "Đổi: cho nghỉ nửa ngày")
    await settle(office)
    [p] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.CONFIRM_TICKET]
    await office.answer(p.id, {"confirmed": True})
    await settle(office)
    cr = (await office.tickets.list(parent_id=feature.id))[0]
    assert cr.type == "change_request" and cr.status == S.DONE
    assert any(c.author_kind == "change" for c in await office.tickets.comments(feature.id))


# ---- reminders --------------------------------------------------------------------------------


def test_schedule_language():
    now = datetime(2026, 10, 9, 10, 0).astimezone()  # a Friday
    assert normalize_repeat("every 15m") == "every:15" and normalize_repeat("hourly") == "every:60"
    assert normalize_repeat(None) is None and normalize_repeat("once") is None
    with pytest.raises(ValueError):
        normalize_repeat("sometimes")
    assert parse_when("in 30m", now) == now + timedelta(minutes=30)
    assert parse_when("09:00", now).day == 10  # already past today → tomorrow
    assert parse_when("2026-10-12 08:30", now).hour == 8
    friday_after = next_after("weekdays@08:00", now)
    assert friday_after.weekday() == 0 and friday_after.hour == 8  # skips the weekend
    assert next_after("weekly:wed@09:30", now).weekday() == 2


# ---- automations ------------------------------------------------------------------------------

GOOD_SCRIPT = """
import os, pathlib, json
out = pathlib.Path(os.environ.get("AO_OUTPUT_DIR", "out"))
out.mkdir(exist_ok=True)
(out / "prices.json").write_text(json.dumps([{"sku": "A", "price": 10}]), encoding="utf-8")
print("collected 1 row")
"""

BAD_SCRIPT = "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n"


# ---- databases --------------------------------------------------------------------------------


async def test_db_tools_are_read_only_and_contained(office, runtime, tmp_path):
    folder = tmp_path / "shop"
    folder.mkdir()
    with sqlite3.connect(folder / "sales.db") as conn:
        conn.execute("CREATE TABLE orders (id INTEGER PRIMARY KEY, region TEXT, amount REAL)")
        conn.executemany("INSERT INTO orders(region, amount) VALUES (?, ?)", [("HN", 10), ("HN", 15), ("HCM", 7)])
    project = await office.projects.create("Shop", path=str(folder), access=ProjectAccess.READ_ONLY)
    analyst = await hire(office, "analyst", "Linh")
    conv = await office.chat.dm(analyst.id, project.id)
    outcomes: dict[str, str] = {}

    async def analyse(ctx: TurnCtx) -> TurnResult:
        outcomes["schema"] = await ctx.office("db_schema", {"database": "sales.db"})
        outcomes["query"] = await ctx.office(
            "db_query", {"database": "sales.db", "sql": "SELECT region, SUM(amount) AS total FROM orders GROUP BY region ORDER BY total DESC"}
        )
        for bad in ("DELETE FROM orders", "SELECT 1; DROP TABLE orders", "ATTACH 'x.db' AS x"):
            with pytest.raises(ToolError):
                await ctx.office("db_query", {"database": "sales.db", "sql": bad})
        with pytest.raises(ToolError):
            await ctx.office("db_query", {"database": "../../home/office.db", "sql": "SELECT 1"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", text="HN dẫn đầu với 25")

    runtime.add("Linh", analyse)
    await office.chat.post_user(conv.id, "Vùng nào doanh thu cao nhất?")
    await settle(office)
    assert "orders (3 rows)" in outcomes["schema"]
    assert outcomes["query"].splitlines()[:2] == ["region\ttotal", "HN\t25.0"]
    with sqlite3.connect(folder / "sales.db") as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 3


async def test_standard_skills_are_installed_once(office):
    installed = await office.install_default_skills()
    assert {"doc-standards", "python-project-standard", "clarifying-questions"} <= set(installed)
    skill = office.skills.get(next(s.ref for s in office.skills.list() if s.ref.name == "doc-standards"))
    assert "templates/requirements.md" in skill.files
    assert await office.install_default_skills() == []  # idempotent


def test_python_launcher_available():
    assert sys.version_info >= (3, 11)


async def test_change_wakes_a_lead_blocked_on_its_devs_and_done_needs_finished_children(office, runtime):
    lead, _dev = await hire(office, "lead", "Long"), await hire(office, "dev", "Minh")
    feature = await office.tickets.create("Báo cáo", assignee_actor_id=None)
    plan = await office.tickets.create("Kế hoạch", parent_id=feature.id, assignee_actor_id=lead.id, status=S.BLOCKED)
    task = await office.tickets.create("Bảng tổng hợp", parent_id=plan.id, assignee_actor_id=None)
    await office.tickets.add_blocker(plan.id, task.id)
    prompts: list[str] = []

    async def replan(ctx: TurnCtx) -> None:
        prompts.append(ctx.spec.prompt)
        with pytest.raises(ToolError, match="not finished"):
            await ctx.office("ticket_set_status", {"status": "done", "comment": "xong"})
        await ctx.office("ticket_create", {"title": "Thêm cột lợi nhuận", "description": "theo thay đổi", "assignee": "Minh"})
        await ctx.office("ticket_set_status", {"status": "blocked", "comment": "Đã lập lại kế hoạch", "blocked_by": [task.key]})

    runtime.add("Long", replan)
    runtime.add("Minh", say("ok"))
    await office.tickets.change_notice(feature.id, "Thêm cột lợi nhuận", by="Hoa")
    await settle(office)
    assert prompts and "still blocked by" in prompts[0] and "Thêm cột lợi nhuận" in prompts[0]
    assert (await office.tickets.get(plan.id)).status == S.BLOCKED
    assert [t.title for t in await office.tickets.list(parent_id=plan.id)][0] == "Thêm cột lợi nhuận"


async def test_confirming_a_proposal_twice_creates_one_ticket(office, runtime):
    ba = await hire(office, "ba", "Hoa")

    async def propose(ctx: TurnCtx) -> TurnResult:
        await ctx.office("propose_ticket", {"title": "Nghỉ phép", "description": "x"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", text="đã đề xuất")

    runtime.add("Hoa", propose)
    dm = await office.chat.dm(ba.id)
    await office.chat.post_user(dm.id, "làm nghỉ phép")
    await settle(office)
    [p] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.CONFIRM_TICKET]
    results = await asyncio.gather(*(office.answer(p.id, {"confirmed": True}) for _ in range(3)), return_exceptions=True)
    assert sum(not isinstance(r, Exception) for r in results) == 1
    assert len([t for t in await office.tickets.list() if t.title == "Nghỉ phép"]) == 1
    assert (await office.interactions.get(p.id)).response["ticket_key"].startswith("AO-")


async def test_merge_brings_a_finished_tree_into_main(office, tmp_path):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    from actoroffice.engine.workspace import branch_name
    from actoroffice.services.merges import MergeError

    project = await office.projects.create("Leave", path=str(tmp_path / "leave"), access=ProjectAccess.WORKTREE)
    dev = await hire(office, "dev", "Minh")
    root = await office.tickets.create("Nghỉ phép", project_id=project.id, assignee_actor_id=None)
    pos = await office.org.get_position(dev.position_id)
    ws = await office.workspaces.realize(root, project, pos, root)
    git = ["git", "-C", str(ws.path), "-c", "user.name=t", "-c", "user.email=t@x"]
    (ws.path / "leave.py").write_text("def balance():\n    return 12\n", encoding="utf-8")
    with pytest.raises(MergeError, match="chưa commit"):
        await office.merges.merge(root.key)
    subprocess.run([*git, "add", "-A"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "leave"], check=True)
    status = await office.merges.status(root.key)
    assert status["available"] and status["ahead"] == 1 and status["branch"] == branch_name(root)
    result = await office.merges.merge(root.key)
    assert result["merged"] and (tmp_path / "leave" / "leave.py").exists()
    assert (await office.merges.status(root.key))["merged"]
    # a conflicting change is aborted and the main folder stays clean
    (tmp_path / "leave" / "leave.py").write_text("def balance():\n    return 14\n", encoding="utf-8")
    main = ["git", "-C", str(tmp_path / "leave"), "-c", "user.name=t", "-c", "user.email=t@x"]
    subprocess.run([*main, "commit", "-qam", "main change"], check=True)
    (ws.path / "leave.py").write_text("def balance():\n    return 10\n", encoding="utf-8")
    subprocess.run([*git, "commit", "-qam", "branch change"], check=True)
    with pytest.raises(MergeError, match="xung đột"):
        await office.merges.merge(root.key)
    assert subprocess.run([*main, "status", "--porcelain"], capture_output=True, text=True).stdout.strip() == ""


def test_project_conventions_are_pointed_out(tmp_path):
    from actoroffice.domain.work import Project
    from actoroffice.engine.context import project_line

    (tmp_path / "AGENTS.md").write_text("Use tabs", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "CONVENTIONS.md").write_text("x", encoding="utf-8")
    p = Project(id="p", name="ERP", description="Hệ thống ERP nội bộ", path=str(tmp_path), access=ProjectAccess.WORKTREE)
    line = project_line(p, "W")
    assert "AGENTS.md, docs/CONVENTIONS.md" in line and "override the office defaults" in line and "ERP nội bộ" in line


async def test_developer_cannot_finish_with_uncommitted_changes(office, runtime, tmp_path):
    import shutil
    import subprocess

    if shutil.which("git") is None:
        pytest.skip("git not installed")
    project = await office.projects.create("Invoice", path=str(tmp_path / "inv"), access=ProjectAccess.WORKTREE)
    dev = await hire(office, "dev", "Duc")
    errors: list[str] = []

    async def work(ctx: TurnCtx) -> None:
        (ctx.spec.cwd / "vat.py").write_text("def vat(x):\n    return round(x * 0.08)\n", encoding="utf-8")
        await ctx.office("doc_put", {"key": "handoff", "body": "ok", "base_revision": None})
        with pytest.raises(ToolError, match="uncommitted") as exc:
            await ctx.office("ticket_set_status", {"status": "done", "comment": "xong"})
        errors.append(str(exc.value))
        git = ["git", "-C", str(ctx.spec.cwd), "-c", "user.name=t", "-c", "user.email=t@x"]
        subprocess.run([*git, "add", "-A"], check=True)
        subprocess.run([*git, "commit", "-qm", "fix vat"], check=True)
        await ctx.office("ticket_set_status", {"status": "done", "comment": "đã commit"})

    runtime.add("Duc", work)
    ticket = await office.tickets.create("Sửa VAT", project_id=project.id, assignee_actor_id=dev.id)
    await settle(office)
    assert "vat.py" in errors[0] and (await office.tickets.get(ticket.id)).status == S.DONE


async def finish_automations(office: Office) -> None:
    for task in list(office.automations._running.values()):
        with contextlib.suppress(Exception):
            await task


async def test_reminders_are_message_flows_that_run_without_llm(office, runtime):
    sec = await hire(office, "assistant", "Thu")
    dm = await office.chat.dm(sec.id)

    async def set_two(ctx: TurnCtx) -> TurnResult:
        out = await ctx.office("automation_save", {
            "name": "uong-nuoc", "description": "Nhắc uống nước mỗi giờ",
            "steps": [{"type": "notify", "text": "Anh ơi, uống nước nhé"}], "when": "in 1m", "schedule": "every:60",
        })
        assert "is active" in out
        await ctx.office("automation_save", {
            "name": "hop-9h", "description": "Nhắc họp", "steps": [{"type": "notify", "text": "Anh ơi, 9h họp ({date})"}], "when": "in 2m",
        })
        with pytest.raises(ToolError):
            await ctx.office("automation_save", {"name": "cu", "description": "x", "steps": [{"type": "notify", "text": "x"}], "when": "2020-01-01 09:00"})
        with pytest.raises(ToolError, match="need a ticket"):  # code cannot be added from chat
            await ctx.office("automation_save", {"name": "s", "description": "x", "steps": [{"type": "script", "file": "a.py"}]})
        assert "uong-nuoc" in await ctx.office("automation_list", {})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", text="Đã đặt 2 lời nhắc")

    runtime.add("Thu", set_two)
    await office.chat.post_user(dm.id, "nhắc anh uống nước mỗi giờ và họp lúc 9h")
    await settle(office)
    turns = len(runtime.specs)
    assert not [i for i in await office.interactions.pending() if i.kind == InteractionKind.APPROVE_AUTOMATION]  # no review
    assert await office.automations.run_due(datetime.now(UTC) + timedelta(minutes=3)) == 2
    await finish_automations(office)
    assert await office.automations.run_due(datetime.now(UTC) + timedelta(minutes=3)) == 0  # not twice
    bodies = [m.body for m in await office.chat.messages(dm.id) if m.meta.get("automation_id")]
    assert sorted(b.split(" (")[0] for b in bodies) == ["Anh ơi, 9h họp", "Anh ơi, uống nước nhé"]
    assert any(re.search(r"\(\d\d/\d\d/\d{4}\)", b) for b in bodies)  # {date} filled in
    once, hourly = await office.automations.get("hop-9h"), await office.automations.get("uong-nuoc")
    assert once.status == AutomationStatus.FINISHED and hourly.status == AutomationStatus.ENABLED and hourly.next_at
    assert len(runtime.specs) == turns  # posting never called the LLM


GOOD_SCRIPT = """
import os, pathlib, json
out = pathlib.Path(os.environ.get("AO_OUTPUT_DIR", "out"))
out.mkdir(exist_ok=True)
(out / "prices.json").write_text(json.dumps([{"sku": "A", "price": 10}]), encoding="utf-8")
print("collected 1 row")
"""

BAD_SCRIPT = "import sys\nprint('boom', file=sys.stderr)\nsys.exit(3)\n"


async def test_script_flow_needs_review_then_runs_and_reports(office, runtime):
    col = await hire(office, "collector", "Khoa")
    dm = await office.chat.dm(col.id)

    async def build(ctx: TurnCtx) -> None:
        (ctx.spec.cwd / "prices.py").write_text(GOOD_SCRIPT, encoding="utf-8")
        with pytest.raises(ToolError):
            await ctx.office("automation_save", {"name": "prices", "description": "x", "steps": [{"type": "script", "file": "../escape.py"}]})
        with pytest.raises(ToolError, match="at most every"):
            await ctx.office("automation_save", {"name": "prices", "description": "x", "steps": [{"type": "script", "file": "prices.py"}], "schedule": "every:1"})
        out = await ctx.office("automation_save", {
            "name": "prices", "description": "Lấy giá hằng ngày", "steps": [{"type": "script", "file": "prices.py"}], "schedule": "daily@07:30",
        })
        assert "review" in out
        await ctx.office("ticket_set_status", {"status": "in_review", "comment": "Chờ duyệt automation"})

    async def after_review(ctx: TurnCtx) -> None:
        assert "approved" in ctx.spec.prompt
        await ctx.office("ticket_set_status", {"status": "done", "comment": "Automation đã chạy"})

    runtime.add("Khoa", build, after_review)
    ticket = await office.tickets.create("Theo dõi giá đối thủ", assignee_actor_id=col.id, origin_conversation_id=dm.id)
    await settle(office)
    automation = await office.automations.get("prices")
    assert automation.status == AutomationStatus.PENDING_REVIEW and automation.schedule == "daily@07:30"
    with pytest.raises(AutomationError):
        await office.automations.run(automation, "manual")  # not before review
    [review] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.APPROVE_AUTOMATION]
    assert "collected 1 row" in review.payload["code"]["prices.py"]
    await office.answer(review.id, {"approved": True, "run_now": True})
    await finish_automations(office)
    await settle(office)
    [run] = await office.automations.runs("prices")
    assert run.status == "succeeded" and "collected 1 row" in (run.output or "")
    assert (office.settings.data_dir / "automations" / "prices" / "out" / "prices.json").exists()
    assert (await office.automations.get("prices")).next_at is not None
    assert any("Automation **prices** chạy xong" in m.body for m in await office.chat.messages(dm.id))
    assert (await office.tickets.get(ticket.id)).status == S.DONE


async def test_flow_passes_output_to_a_message_and_hands_work_to_a_colleague(office, tmp_path):
    analyst, writer = await hire(office, "analyst", "Linh"), await hire(office, "writer", "Lan")
    dm = await office.chat.dm(analyst.id)
    (tmp_path / "kpi.py").write_text("print('doanh thu tuần: 125 triệu')\n", encoding="utf-8")
    await office.automations.save(
        "kpi-tuan",
        steps=[
            {"type": "script", "file": "kpi.py"},
            {"type": "notify", "text": "📊 {output}"},
            {"type": "agent", "actor": "Lan", "prompt": "Viết 3 câu nhận xét cho bản tin nội bộ"},
        ],
        description="KPI tuần",
        schedule="weekly:mon@08:00",
        owner_actor_id=analyst.id,
        conversation_id=dm.id,
        workspace=tmp_path,
    )
    [card] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.APPROVE_AUTOMATION]
    assert [s["type"] for s in card.payload["steps"]] == ["script", "notify", "agent"]
    await office.answer(card.id, {"approved": True, "run_now": False})
    run = await office.automations.run(await office.automations.get("kpi-tuan"), "manual")
    assert run.status == "succeeded", run.error
    assert any(m.body == "📊 doanh thu tuần: 125 triệu" for m in await office.chat.messages(dm.id))
    [task] = [t for t in await office.tickets.list() if t.assignee_actor_id == writer.id]
    assert "125 triệu" in task.description and "kpi-tuan" in task.description
    assert not any("chạy xong" in m.body for m in await office.chat.messages(dm.id))  # the notify step already reported


async def test_failing_automation_opens_one_fix_ticket(office, tmp_path):
    col = await hire(office, "collector", "Khoa")
    (tmp_path / "bad.py").write_text(BAD_SCRIPT, encoding="utf-8")
    await office.automations.save(
        "bad-job", steps=[{"type": "script", "file": "bad.py"}], description="x", owner_actor_id=col.id, conversation_id=None,
        workspace=tmp_path,
    )
    [card] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.APPROVE_AUTOMATION]
    await office.answer(card.id, {"approved": True, "run_now": False})
    automation = await office.automations.get("bad-job")
    first = await office.automations.run(automation, "manual")
    second = await office.automations.run(automation, "manual")
    assert first.status == second.status == "failed" and first.exit_code == 3 and "boom" in (first.error or "")
    fixes = [t for t in await office.tickets.list() if t.type == "automation_fix"]
    assert len(fixes) == 1 and fixes[0].assignee_actor_id == col.id
    assert any("Run #" in c.body for c in await office.tickets.comments(fixes[0].id))
    assert (await office.automations.get(automation.id)).failures == 2


async def test_automation_timeout_kills_the_script(tmp_path):
    from actoroffice.services.automations import _execute

    (tmp_path / "slow.py").write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    code, _out, err = await _execute(tmp_path, "slow.py", 1, "slow")
    assert code == 124 and "Timed out" in err


async def test_automation_relative_writes_land_in_the_output_folder(tmp_path):
    from actoroffice.services.automations import _execute

    (tmp_path / "rel.py").write_text("open('report.csv', 'w').write('a,b\\n')\n", encoding="utf-8")
    code, _out, err = await _execute(tmp_path, "rel.py", 30, "rel")
    assert code == 0, err
    assert (tmp_path / "out" / "report.csv").exists() and not (tmp_path / "report.csv").exists()


async def test_only_the_reviewed_version_can_be_approved_and_run(office, tmp_path):
    from actoroffice.domain.errors import Conflict
    from actoroffice.services.automations import AutomationError

    col = await hire(office, "collector", "Khoa")
    script = tmp_path / "job.py"

    async def save(text: str) -> None:
        script.write_text(text, encoding="utf-8")
        await office.automations.save(
            "job", steps=[{"type": "script", "file": "job.py"}], description="x", owner_actor_id=col.id, conversation_id=None,
            workspace=tmp_path,
        )

    await save("print('v1')\n")
    [v1_card] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.APPROVE_AUTOMATION]
    await save("print('v2')\n")
    assert (await office.interactions.get(v1_card.id)).status == InteractionStatus.CANCELLED  # stale card is gone
    with pytest.raises(Conflict):
        await office.answer(v1_card.id, {"approved": True})
    [v2_card] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.APPROVE_AUTOMATION]
    assert "v2" in v2_card.payload["code"]["job.py"]
    await office.answer(v2_card.id, {"approved": True, "run_now": False})
    automation = await office.automations.get("job")
    assert "v2" in ((await office.automations.run(automation, "manual")).output or "")
    approved_file = office.settings.data_dir / "automations" / "job" / "job.py"
    approved_file.write_text("print('tampered')\n", encoding="utf-8")  # changed on disk: must not run
    with pytest.raises(AutomationError, match="differs"):
        await office.automations.run(automation, "schedule")
    with pytest.raises(Conflict):
        await office.automations.set_enabled("job", True)
    approved_file.write_text("print('v2')\n", encoding="utf-8")
    await office.automations.set_enabled("job", False)
    with pytest.raises(AutomationError, match="disabled"):
        await office.automations.run(automation, "actor")  # an actor may not run a disabled flow


async def test_each_run_records_files_lines_and_commands(office, runtime):
    dev = await hire(office, "dev", "Minh")

    async def work(ctx: TurnCtx) -> None:
        target = ctx.spec.cwd / "app.py"
        assert await ctx.tool("Write", {"file_path": str(target), "content": "a = 1\nb = 2\nc = 3\n"}) == GateDecision.ALLOW
        target.write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        assert await ctx.tool("Edit", {"file_path": str(target), "old_string": "b = 2", "new_string": "b = 20\nd = 4"}) == GateDecision.ALLOW
        assert await ctx.tool("Bash", {"command": "uv run pytest -q"}) == GateDecision.ALLOW
        assert await ctx.tool("Read", {"file_path": str(target)}) == GateDecision.ALLOW
        await ctx.office("doc_put", {"key": "handoff", "body": "ok", "base_revision": None})
        await ctx.office("ticket_set_status", {"status": "done", "comment": "xong"})

    runtime.add("Minh", work)
    ticket = await office.tickets.create("Sửa app", assignee_actor_id=dev.id)
    await settle(office)
    [run] = await office.runs.for_ticket(ticket.id)
    s = run.stats
    assert s["files_changed"] == 1 and s["files"]["app.py"] == {"added": 5, "removed": 1}
    assert (s["lines_added"], s["lines_removed"], s["commands"], s["reads"]) == (5, 1, 1, 1)
    assert s["command_samples"] == ["uv run pytest -q"] and s["office_tools"] == 2
    work_total = await office.runs.work_summary(ticket.id)
    assert work_total["runs"] == 1 and work_total["lines_added"] == 5 and work_total["files_changed"] == 1
