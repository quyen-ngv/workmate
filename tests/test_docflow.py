"""Doc Core in the office: an epic is analysed, its business is confirmed by the BA, each task is handed to a developer,
designed, sent by the developer and approved by the lead. Documents are checked against the pack at every step."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.errors import OfficeError, PolicyDenied
from actoroffice.domain.run import TurnResult, TurnStatus, Usage
from actoroffice.domain.work import InteractionKind, ProjectAccess, Ticket, TicketStatus
from actoroffice.services.docflow import pack_gates, pack_templates

from .fakes import FakeRuntime, TurnCtx
from .seed import IDENT, build_repo, git, seed_db
from .test_codeintel import PY_REPO

S = TicketStatus
LOG = "\n## {n}. Change log\n| Phiên bản | Ngày | Thay đổi | Người |\n|---|---|---|---|\n| 1 | 2026-11-13 | Bản đầu | Lan |\n"


def analysis(key: str, source: str = "src/shop/limits.py:3", answer: bool = True) -> str:
    return f"""# Phân tích — {key} Hạn mức hoàn tiền
Phiên bản: 1 · Cập nhật: 2026-11-13 · Người viết: Lan

## 1. Phạm vi
- Nâng hạn mức hoàn tiền mỗi ngày.

## 2. Yêu cầu
### REQ-1: Hạn mức hoàn tiền mỗi ngày là 9
Lấy từ mô tả epic.

### REQ-2: Từ chối phải kèm lý do
Lấy từ mô tả epic.

### Ngoài phạm vi
- REQ-3: Báo cáo tuần — lý do: để đợt sau.

## 3. Hiện trạng
### F-1: Hạn mức hiện là 5 lần mỗi ngày
Hằng số trong code.
Nguồn: {source}

## 4. Mâu thuẫn và thiếu
Không có.

## 5. Câu hỏi mở
### Q-1: Khách VIP có hạn mức riêng không?
{"Trả lời: Không, dùng chung." if answer else ""}

## 6. Giả định
- A-1: Cách đếm số lần không đổi.
{LOG.format(n=7)}"""


def breakdown(key: str) -> str:
    return f"""# Chia task — {key} Hạn mức hoàn tiền
Phiên bản: 1 · Cập nhật: 2026-11-13 · Người viết: Lan

## 1. Mục tiêu
Theo bản phân tích 1.

## 2. Danh sách task
### T-1: Nâng hạn mức lên 9
Khách hoàn được tới 9 lần mỗi ngày.
Truy vết: REQ-1

### T-2: Lý do từ chối
Khách thấy lý do khi bị từ chối.
Truy vết: REQ-2

### T-3: Dọn hằng số cũ
Việc phụ.
Truy vết: REQ-1

## 3. Thứ tự và phụ thuộc
Không có.
{LOG.format(n=4)}"""


def spec(epic: str, task: str) -> str:
    return f"""# Nghiệp vụ — {epic} {task}
Phiên bản: 1 · Cập nhật: 2026-11-13 · Người viết: Lan

## 1. Mục tiêu
- Đáp ứng yêu cầu của epic {epic}.

## 2. User stories
### US-1: Hoàn tiền trong hạn mức
Là khách hàng, tôi muốn hoàn tiền nhiều lần hơn, để không phải chờ sang ngày.

- AC-1.1 (happy path) — Given khách đã hoàn 8 lần hôm nay When yêu cầu hoàn Then yêu cầu được chấp nhận
  Truy vết: {epic}/REQ-1
- AC-1.2 (từ chối) — Given khách đã hoàn 9 lần hôm nay When yêu cầu hoàn Then bị từ chối kèm lý do
  Truy vết: {epic}/REQ-2

## 3. Quy tắc nghiệp vụ
### BR-1: Hạn mức là 9 lần mỗi ngày
Áp cho mọi khách.
Truy vết: {epic}/REQ-1
Thay đổi: {epic}/F-1

## 4. Thay đổi so với hiện trạng
| Quy tắc | Hiện trạng | Sau khi làm |
|---|---|---|
| BR-1 | 5 lần | 9 lần |

## 5. Phụ thuộc
Không có.

## 6. Giả định
- A-1: Không đổi cách đếm.
{LOG.format(n=7)}"""


def design(key: str, epic: str, *, cover_all: bool = True, source: str = "src/shop/limits.py:3") -> str:
    second = "\n### C-2 [mới]: Thông báo lý do từ chối\nThêm trường lý do vào câu trả lời.\nTruy vết: AC-1.2\n" if cover_all else ""
    return f"""# Design — {key} Nâng hạn mức
Phiên bản: 1 · Cập nhật: 2026-11-13 · Người viết: Minh

## 1. Bối cảnh
Viết theo nghiệp vụ bản 1.

## 2. Thay đổi
### C-1: Đổi hằng số hạn mức
Từ 5 lên 9.
Truy vết: AC-1.1, BR-1
Nguồn: {source}
Thay đổi: {epic}/F-1
{second}
## 3. Mô hình dữ liệu
Không đổi.

## 4. Giao diện
Không đổi.

## 5. Quyết định kỹ thuật
### D-1: Giữ hạn mức là hằng số trong code
- Phương án: đưa vào cấu hình, hoặc giữ hằng số. Chọn giữ hằng số.

## 6. Kiểm thử
Unit cho AC-1.1 và AC-1.2.

## 7. Rủi ro
Không có.

## 8. Truy vết
| AC / BR | Thay đổi |
|---|---|
| AC-1.1 | C-1 |

## 9. Các bước làm
1. Sửa hằng số (C-1)
{LOG.format(n=10)}"""


def done(ctx: TurnCtx) -> TurnResult:
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", usage=Usage(input_tokens=10, output_tokens=5))


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


@pytest.fixture
async def setup(office: Office, tmp_path: Path):
    team = await office.teams.create_team("Pay")
    lan, minh, hung = [await office.teams.create_user(n) for n in ("Lan", "Minh", "Hùng")]
    await office.teams.add_member(team.id, lan.id, ["ba"])
    await office.teams.add_member(team.id, minh.id, ["dev"])
    await office.teams.add_member(team.id, hung.id, ["lead"])
    repo = build_repo(tmp_path / "shop", PY_REPO)
    project = await office.projects.create("Shop", path=str(repo), access=ProjectAccess.READ_ONLY)
    project = await office.projects.configure(project.id, team_id=team.id)
    epic = await office.tickets.create("Nâng hạn mức hoàn tiền", description="Hạn mức mỗi ngày lên 9; từ chối phải có lý do.", project_id=project.id)
    return team, lan, minh, hung, repo, epic


async def cards(office: Office, ticket_id: int):
    return [i for i in await office.interactions.pending(ticket_id=ticket_id) if i.kind == InteractionKind.APPROVE_STAGE]


async def put(office: Office, ticket: Ticket, key: str, body: str) -> None:
    current = await office.tickets.get_document(ticket.id, key)
    await office.tickets.put_document(ticket.id, key, body, author="Copilot", base_revision=current.revision if current else None)
    await office.docs.on_saved(await office.tickets.get(ticket.id), key)


# ---- the pack as office workflows -----------------------------------------------------------------------------------


def test_pack_flows_become_stage_templates() -> None:
    flows = pack_templates()
    assert [s.id for s in flows["epic"].stages] == ["analyze", "breakdown", "close"]
    assert [s.id for s in flows["task-spec"].stages] == ["design"]
    task = {s.id: s for s in flows["task"].stages}
    assert list(task) == ["design", "build", "pr", "close"]
    assert "repo:test" in task["build"].checks and task["pr"].gate == "merge", "a task that goes on to code is built like any code ticket"
    assert "doc_rules" in task["design"].checks and task["design"].instructions
    gate = pack_gates()["design"]
    assert (gate.roles, gate.fallback, gate.submit_by, gate.accept_findings) == (["lead"], None, ["dev"], ["lead"])


# ---- the whole flow ---------------------------------------------------------------------------------------------------


async def test_epic_to_approved_design(office: Office, runtime: FakeRuntime, setup) -> None:
    team, lan, minh, hung, repo, epic = setup
    E = epic.key

    async def analyze(ctx: TurnCtx) -> TurnResult:
        assert "Current stage: analyze" in ctx.spec.system_prompt and "code_pack" in ctx.spec.system_prompt
        assert "## 3. Hiện trạng" in await ctx.office("doc_template", {"type": "analysis"})
        await ctx.office("doc_put", {"key": "analysis", "body": analysis(E, source="src/shop/nope.py:3", answer=False), "base_revision": None})
        report = await ctx.office("doc_check", {})
        assert "R-EVIDENCE" in report and "nope.py" in report and "R-OPEN-Q" in report and "Q-1" in report
        failed = await ctx.office("stage_complete", {"summary": "xong"})
        assert "doc_rules" in failed and "R-EVIDENCE" in failed
        await ctx.office("doc_put", {"key": "analysis", "body": analysis(E), "base_revision": 1})
        assert "không có điểm nào cần sửa" in await ctx.office("doc_check", {})
        out = await ctx.office("stage_complete", {"summary": "Phân tích: 3 yêu cầu, 1 hiện trạng"})
        assert "Lan" in out and "analysis" in out
        return done(ctx)

    runtime.scripts.append(analyze)
    await office.stages.start(E, template="epic", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=30)
    (card,) = await cards(office, epic.id)
    assert card.payload["gate"] == "analysis" and card.payload["approver_names"] == ["Lan"]
    view = card.payload["doc_core"]
    assert view["states"]["analysis"]["state"] == "khop" and [a["id"] for a in view["assumptions"]] == ["A-1"]
    with pytest.raises(PolicyDenied):
        await office.answer(card.id, {"approved": True}, by=minh.id)

    async def break_down(ctx: TurnCtx) -> TurnResult:
        assert "Current stage: breakdown" in ctx.spec.system_prompt
        await ctx.office("doc_put", {"key": "breakdown", "body": breakdown(E), "base_revision": None})
        for task in ("T-1", "T-2"):
            await ctx.office("doc_put", {"key": f"spec.{task}", "body": spec(E, task), "base_revision": None})
        report = await ctx.office("doc_check", {})
        assert "R-SPEC-PER-TASK" in report and "T-3" in report and "spec.T-3" in report
        await ctx.office("doc_put", {"key": "spec.T-3", "body": spec(E, "T-3"), "base_revision": None})
        assert "0 lỗi" in await ctx.office("doc_check", {}) or "không có điểm nào" in await ctx.office("doc_check", {})
        out = await ctx.office("stage_complete", {"summary": "3 task, mỗi task có nghiệp vụ"})
        assert "business" in out
        return done(ctx)

    async def design_task(ctx: TurnCtx) -> TurnResult:
        info = json.loads(await ctx.office("stage_info", {}))
        key = info["ticket"]
        assert info["template"] == "task-spec" and "Current stage: design" in ctx.spec.system_prompt
        assert "AC-1.1" in await ctx.office("doc_get", {"key": "spec"}), "the task starts with its approved spec"
        await ctx.office("doc_put", {"key": "design", "body": design(key, E, cover_all=False), "base_revision": None})
        report = await ctx.office("doc_check", {})
        assert "R-COVER-AC" in report and "AC-1.2" in report
        await ctx.office("doc_put", {"key": "design", "body": design(key, E), "base_revision": 1})
        await ctx.office("stage_complete", {"summary": "Design phủ mọi AC"})
        return done(ctx)

    runtime.scripts += [break_down, design_task, design_task]
    await office.answer(card.id, {"approved": True}, by=lan.id)
    await office.scheduler.wait_idle(timeout=30)

    (card,) = await cards(office, epic.id)
    assert card.payload["gate"] == "business" and card.payload["spawn"]["people"] == [{"id": minh.id, "name": "Minh"}]
    assert [(t["id"], t["has_spec"]) for t in card.payload["doc_core"]["tasks"]] == [("T-1", True), ("T-2", True), ("T-3", True)]
    answer = {"approved": True, "drop": ["T-3"], "flows": {"T-1": "task-spec", "T-2": "task-spec"}, "assign": {"T-1": minh.id}}
    await office.answer(card.id, answer, by=lan.id)
    await office.scheduler.wait_idle(timeout=30)

    children = [await office.tickets.get(r["id"]) for r in await office.db.fetchall("SELECT id FROM tickets WHERE parent_id = ? ORDER BY id", (epic.id,))]
    assert [c.title for c in children] == ["Nâng hạn mức lên 9", "Lý do từ chối"], "T-3 was dropped on the card"
    first, second = children
    assert all(c.stage_template == "task-spec" and c.team_id == team.id and c.project_id == epic.project_id for c in children)
    copied = await office.tickets.get_document(first.id, "spec")
    assert copied is not None and copied.approved_revision == 1 and "AC-1.1" in copied.body
    after = await office.tickets.get(epic.id)
    assert after.stage == "close" and after.status == S.BLOCKED and await office.tickets.open_blockers(epic.id) == [first.key, second.key]

    # T-1 was handed to Minh: Minh sends it, then the lead approves. Nobody else can do either.
    (submit,) = await cards(office, first.id)
    assert submit.payload["phase"] == "submit" and submit.payload["approver_names"] == ["Minh"] and submit.payload["final_roles"] == ["lead"]
    trace = {t["id"]: t["traces"] for t in submit.payload["doc_core"]["trace"]}
    assert trace["C-1"] == ["AC-1.1", "BR-1"] and trace["C-2"] == ["AC-1.2"]
    assert submit.payload["doc_core"]["declared_changes"][0]["changes"] == f"{E}/F-1"
    assert submit.payload["doc_core"]["baseline"]["design"] == {f"{first.key}/spec": 1, f"{E}/analysis": 2}
    with pytest.raises(PolicyDenied):
        await office.answer(submit.id, {"approved": True}, by=hung.id)
    await office.answer(submit.id, {"approved": True, "note": "ổn"}, by=minh.id)
    (gate,) = await cards(office, first.id)
    assert "phase" not in gate.payload and gate.payload["roles"] == ["lead"] and gate.payload["approver_names"] == ["Hùng"]
    for other in (minh.id, lan.id):
        with pytest.raises(PolicyDenied):
            await office.answer(gate.id, {"approved": True}, by=other)
    await office.answer(gate.id, {"approved": True}, by=hung.id)
    assert (await office.tickets.get(first.id)).status == S.DONE
    doc = await office.tickets.get_document(first.id, "design")
    assert doc is not None and doc.approved_revision == 2
    comments = " ".join(c.body for c in await office.tickets.comments(first.id, limit=50))
    assert "Minh gửi Thiết kế cho Lead duyệt" in comments and "Hùng duyệt" in comments

    # T-2 was handed to nobody: the card goes straight to the lead, and the ticket says why.
    (gate,) = await cards(office, second.id)
    assert "phase" not in gate.payload and gate.payload["approver_names"] == ["Hùng"]
    assert "chưa được giao" in " ".join(c.body for c in await office.tickets.comments(second.id, limit=50))
    await office.answer(gate.id, {"approved": True}, by=hung.id)
    assert await office.tickets.open_blockers(epic.id) == [], "the epic no longer waits once its tasks are done"


async def test_author_can_take_the_design_back_instead_of_sending(office: Office, runtime: FakeRuntime, setup) -> None:
    team, lan, minh, hung, repo, epic = setup
    await put(office, epic, "analysis", analysis(epic.key))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (epic.id,))
    child = await office.tickets.create("Nâng hạn mức", project_id=epic.project_id, parent_id=epic.id)
    await put(office, child, "spec", spec(epic.key, "T-1"))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (child.id,))
    await office.teams.set_participant(child.id, minh.id, ["dev"])

    async def design_once(ctx: TurnCtx) -> TurnResult:
        current = await office.tickets.get_document(child.id, "design")
        await ctx.office("doc_put", {"key": "design", "body": design(child.key, epic.key), "base_revision": current.revision if current else None})
        await ctx.office("stage_complete", {"summary": "Design"})
        return done(ctx)

    runtime.scripts += [design_once, design_once]
    await office.stages.start(child.key, template="task-spec", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=30)
    (submit,) = await cards(office, child.id)
    await office.answer(submit.id, {"approved": False, "note": "thiếu trường hợp lỗi"}, by=minh.id)
    await office.scheduler.wait_idle(timeout=30)
    (again,) = await cards(office, child.id)
    assert again.payload["phase"] == "submit", "after reworking, it comes back to the author first"
    assert "thiếu trường hợp lỗi" in " ".join(c.body for c in await office.tickets.comments(child.id, limit=50))


# ---- matching over time (service level) -----------------------------------------------------------------------------


@pytest.fixture
async def designed(office: Office, setup):
    """An epic with an approved analysis and a child task with an approved spec and a design."""
    team, lan, minh, hung, repo, epic = setup
    await office.db.execute("UPDATE tickets SET stage_template = 'epic', stage = 'breakdown', team_id = ? WHERE id = ?", (team.id, epic.id))
    await put(office, epic, "analysis", analysis(epic.key))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (epic.id,))
    child = await office.tickets.create("Nâng hạn mức", project_id=epic.project_id, parent_id=epic.id)
    await office.db.execute("UPDATE tickets SET stage_template = 'task-spec', stage = 'design', team_id = ? WHERE id = ?", (team.id, child.id))
    await put(office, child, "spec", spec(epic.key, "T-1"))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (child.id,))
    await put(office, child, "design", design(child.key, epic.key))
    return team, lan, minh, hung, repo, await office.tickets.get(epic.id), await office.tickets.get(child.id)


def open_errors(findings) -> list[str]:
    return sorted(f"{f.rule}:{f.item}" for f in findings if f.status == "open" and f.severity == "error")


async def test_a_source_that_does_not_exist_is_refused(office: Office, designed) -> None:
    *_, epic, child = designed
    assert open_errors(await office.docs.check(child)) == []
    for bad in ("src/shop/missing.py:3", "src/shop/limits.py:900", "nope:src/shop/limits.py:3"):
        await put(office, child, "design", design(child.key, epic.key, source=bad))
        assert open_errors(await office.docs.check(child)) == ["R-EVIDENCE:C-1"], bad


async def test_code_changed_under_a_pinned_source_makes_the_design_stale(office: Office, designed) -> None:
    *_, repo, epic, child = designed
    assert (await office.docs.states(child))["design"].value == "khop"
    (repo / "README.md").write_text("# Shop\n\nMore.\n", encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", "docs only")
    assert open_errors(await office.docs.check(child)) == [], "a commit that does not touch the cited lines changes nothing"

    limits = repo / "src/shop/limits.py"
    limits.write_text(limits.read_text(encoding="utf-8").replace("MAX_PER_DAY = 5", "MAX_PER_DAY = 7"), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", "someone else changed the limit")
    findings = await office.docs.check(child)
    assert open_errors(findings) == ["R-FRESH:C-1"]
    assert (await office.docs.states(child, findings))["design"].value == "cu"

    # a new revision that still cites the same lines does not hide the change: the pin stays with the item
    await put(office, child, "design", design(child.key, epic.key).replace("Từ 5 lên 9.", "Từ 5 lên 9 (đã xem lại)."))
    assert open_errors(await office.docs.check(child)) == ["R-FRESH:C-1"]


async def test_spec_changed_after_the_design_only_matters_for_traced_items(office: Office, designed) -> None:
    *_, epic, child = designed
    untouched = spec(epic.key, "T-1").replace("Không đổi cách đếm.", "Không đổi cách đếm, đã xác nhận.")
    await put(office, child, "spec", untouched)
    await office.db.execute("UPDATE documents SET approved_revision = 2 WHERE ticket_id = ? AND key = 'spec'", (child.id,))
    assert open_errors(await office.docs.check(child)) == [], "an edit to an item the design does not trace to calls nobody back"

    changed = untouched.replace("Hạn mức là 9 lần mỗi ngày", "Hạn mức là 12 lần mỗi ngày")
    await put(office, child, "spec", changed)
    await office.db.execute("UPDATE documents SET approved_revision = 3 WHERE ticket_id = ? AND key = 'spec'", (child.id,))
    findings = await office.docs.check(child)
    assert [f.rule for f in findings if f.status == "open" and f.severity == "error"] == ["R-BASELINE"]
    assert (await office.docs.states(child, findings))["design"].value == "lech"
    await put(office, child, "design", design(child.key, epic.key).replace("Từ 5 lên 9.", "Từ 5 lên 12."))
    assert open_errors(await office.docs.check(child)) == [], "a design revised against the new spec matches again"


async def test_only_an_approver_may_accept_a_finding(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await put(office, child, "design", design(child.key, epic.key, cover_all=False))
    stage = await office.stages.stage_def(child)
    (finding,) = [f for f in await office.docs.check(child, stage) if f.severity == "error"]
    assert (finding.rule, finding.item) == ("R-COVER-AC", "AC-1.2")
    for other in (minh.id, lan.id):
        with pytest.raises(PolicyDenied):
            await office.docs.accept(child, finding.id, by=other, reason="để sau")
    with pytest.raises(OfficeError, match="lý do"):
        await office.docs.accept(child, finding.id, by=hung.id, reason=" ")
    await office.docs.accept(child, finding.id, by=hung.id, reason="AC-1.2 làm ở task khác")
    after = await office.docs.check(child, stage)
    assert open_errors(after) == [] and [f.reason for f in after if f.status == "accepted"] == ["AC-1.2 làm ở task khác"]
    await put(office, child, "design", design(child.key, epic.key, cover_all=False).replace("Từ 5 lên 9.", "Từ 5 lên 9!"))
    assert open_errors(await office.docs.check(child, stage)) == [], "the acceptance survives a new revision: the finding id is stable"


async def test_other_workflows_are_untouched(office: Office) -> None:
    ticket = await office.tickets.create("Việc thường")
    await office.tickets.put_document(ticket.id, "plan", "# Plan\n", author="x", base_revision=None)
    await office.docs.on_saved(ticket, "plan")
    assert await office.docs.check(ticket) == [] and not office.docs.applies(ticket)
