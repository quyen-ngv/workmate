"""The AI cross-check of documents (check `crosscheck_clean`): a finding is fixed by the author or accepted by an
approver, never waved through."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.errors import OfficeError, PolicyDenied
from actoroffice.domain.run import TurnResult, TurnStatus, Usage
from actoroffice.domain.work import ProjectAccess, TicketStatus
from actoroffice.runtimes.base import GateDecision
from actoroffice.services.crosscheck import CROSSCHECKER, MAX_ROUNDS, _ReadOnlyGate, parse_answer

from .fakes import FakeRuntime, TurnCtx
from .seed import build_repo, seed_db
from .test_codeintel import PY_REPO
from .test_docflow import analysis, cards, design, put, spec

S = TicketStatus


def answer(ctx: TurnCtx, findings: list[dict]) -> TurnResult:
    assert ctx.spec.system_prompt.startswith(CROSSCHECKER[:60]), "this turn must be the cross-checker"
    return TurnResult(status=TurnStatus.COMPLETED, session_id=f"x-{ctx.spec.run_id}", text=json.dumps({"findings": findings}, ensure_ascii=False), usage=Usage())


def finding(item: str = "C-1", severity: str = "high", claim: str = "AC-1.2 yêu cầu trả lý do từ chối, C-1 không nói gì về lý do") -> dict:
    return {"check": "X-DESIGN-SPEC", "doc": "design", "item": item, "severity": severity, "claim": claim, "evidence": "spec AC-1.2 / design C-1"}


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
async def task(office: Office, tmp_path: Path):
    """A task with an approved spec under an epic with an approved analysis, handed to Minh, not started yet."""
    team = await office.teams.create_team("Pay")
    lan, minh, hung = [await office.teams.create_user(n) for n in ("Lan", "Minh", "Hùng")]
    for user, role in ((lan, "ba"), (minh, "dev"), (hung, "lead")):
        await office.teams.add_member(team.id, user.id, [role])
    project = await office.projects.create("Shop", path=str(build_repo(tmp_path / "shop", PY_REPO)), access=ProjectAccess.READ_ONLY)
    project = await office.projects.configure(project.id, team_id=team.id)
    epic = await office.tickets.create("Nâng hạn mức", project_id=project.id)
    await put(office, epic, "analysis", analysis(epic.key))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (epic.id,))
    child = await office.tickets.create("Nâng hạn mức lên 9", project_id=project.id, parent_id=epic.id)
    await put(office, child, "spec", spec(epic.key, "T-1"))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (child.id,))
    await office.teams.set_participant(child.id, minh.id, ["dev"])
    return team, lan, minh, hung, epic, child


def write_design(office: Office, child, epic, outcomes: list[str], *, edits: list[str] | None = None):
    """A design turn: saves the design (one edit per attempt) and calls stage_complete until it passes or `edits` run out."""

    async def script(ctx: TurnCtx) -> TurnResult:
        variants = edits or ["Từ 5 lên 9."]
        for text in variants:
            current = await office.tickets.get_document(child.id, "design")
            body = design(child.key, epic.key).replace("Từ 5 lên 9.", text)
            await ctx.office("doc_put", {"key": "design", "body": body, "base_revision": current.revision if current else None})
            out = await ctx.office("stage_complete", {"summary": "Design"})
            outcomes.append(out)
            if "Checks passed" in out:
                break
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}", usage=Usage())

    return script


async def start(office: Office, team, lan, child) -> None:
    await office.stages.start(child.key, template="task-spec", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=30)


def crosscheck_prompts(runtime: FakeRuntime) -> list[str]:
    return [s.prompt for s in runtime.specs if s.system_prompt.startswith(CROSSCHECKER[:60])]


# ---- the answer -------------------------------------------------------------------------------------------------------


def test_parse_answer() -> None:
    assert parse_answer('Kết quả:\n{"findings": [{"check": "X", "doc": "design", "severity": "low", "claim": "a"}]}\n')[0]["claim"] == "a"
    assert parse_answer("", {"findings": []}) == []
    prose = "Code trả {'ok': False} khi từ chối.\n```json\n" + '{"findings": [{"check": "X", "doc": "design", "severity": "high", "claim": "trả {\\"ok\\": false} thiếu reason"}]}' + "\n```"
    assert parse_answer(prose)[0]["claim"] == 'trả {"ok": false} thiếu reason', "braces in the prose before the object do not hide it"
    assert parse_answer('{"findings": "none"}') is None and parse_answer("không có JSON") is None and parse_answer('{"findings": [1, {"a": 1}]}') == [{"a": 1}]


async def test_the_cross_checker_can_only_read() -> None:
    gate = _ReadOnlyGate()
    for allowed in ("Read", "Grep", "mcp__office__code_search", "@office/code_read", "StructuredOutput"):
        assert (await gate.before_tool(allowed, {}, None)).decision == GateDecision.ALLOW, allowed
    for denied in ("Write", "Bash", "mcp__office__doc_put", "@office/ticket_comment", "mcp__other__code_search"):
        assert (await gate.before_tool(denied, {}, None)).decision == GateDecision.DENY, denied


# ---- rounds -----------------------------------------------------------------------------------------------------------


async def test_a_finding_blocks_until_the_document_is_fixed(office: Office, runtime: FakeRuntime, task) -> None:
    team, lan, minh, hung, epic, child = task
    outcomes: list[str] = []
    runtime.scripts += [
        write_design(office, child, epic, outcomes, edits=["Từ 5 lên 9.", "Từ 5 lên 9, trả lý do khi từ chối."]),
        lambda ctx: _async(answer(ctx, [finding()])),
        lambda ctx: _async(answer(ctx, [])),
    ]
    await start(office, team, lan, child)
    assert "crosscheck_clean" in outcomes[0] and "X-DESIGN-SPEC" in outcomes[0] and "C-1" in outcomes[0] and "vòng 1/3" in outcomes[0]
    assert "Checks passed" in outcomes[1]
    first, second = crosscheck_prompts(runtime)
    assert "# Cross-check X-DESIGN-SPEC" in first and "# Cross-check X-DESIGN-CODE" in first and "# Cross-check X-SPEC-CODE" not in first
    assert f"## LEFT document `{child.key}/design`" in first and f"## RIGHT document `{child.key}/spec`" in first
    assert "MAX_PER_DAY = 5" in first, "the cited code is shown to the cross-checker as it was read"
    assert "trả lý do khi từ chối" in second
    (card,) = await cards(office, child.id)
    assert card.payload["phase"] == "submit" and card.payload["doc_core"]["open"] == []
    checks = {c["id"]: c["status"] for c in card.payload["checks"]}
    assert checks["crosscheck_clean"] == "passed" and checks["doc_rules"] == "passed"


async def _async(value):
    return value


async def test_unchanged_documents_are_not_cross_checked_twice(office: Office, runtime: FakeRuntime, task) -> None:
    team, lan, minh, hung, epic, child = task
    outcomes: list[str] = []

    async def twice(ctx: TurnCtx) -> TurnResult:
        await ctx.office("doc_put", {"key": "design", "body": design(child.key, epic.key), "base_revision": None})
        outcomes.append(await ctx.office("stage_complete", {"summary": "Design"}))
        outcomes.append(await ctx.office("stage_complete", {"summary": "Design"}))  # nothing changed in between
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", usage=Usage())

    runtime.scripts += [twice, lambda ctx: _async(answer(ctx, [finding()]))]
    await start(office, team, lan, child)
    assert "vòng 1/3" in outcomes[0] and "vòng 1/3" in outcomes[1]
    assert len(crosscheck_prompts(runtime)) == 1


async def test_after_the_last_round_the_approver_fixes_or_accepts(office: Office, runtime: FakeRuntime, task) -> None:
    team, lan, minh, hung, epic, child = task
    outcomes: list[str] = []
    edits = [f"Từ 5 lên 9 (lần {i})." for i in range(1, MAX_ROUNDS + 1)]
    runtime.scripts += [write_design(office, child, epic, outcomes, edits=edits)] + [lambda ctx: _async(answer(ctx, [finding()]))] * MAX_ROUNDS
    await start(office, team, lan, child)
    assert [("Checks passed" in o) for o in outcomes] == [False, False, True], outcomes
    (submit,) = await cards(office, child.id)
    await office.answer(submit.id, {"approved": True}, by=minh.id)
    (gate,) = await cards(office, child.id)
    (left,) = gate.payload["doc_core"]["open"]
    assert (left["rule"], left["item"], left["severity"]) == ("X-DESIGN-SPEC", "C-1", "error")
    note = next(c["summary"] for c in gate.payload["checks"] if c["id"] == "crosscheck_clean")
    assert "chuyển cho người duyệt" in note

    with pytest.raises(OfficeError, match="Còn 1 điểm lệch chưa xử lý"):
        await office.answer(gate.id, {"approved": True}, by=hung.id)
    with pytest.raises(PolicyDenied):
        await office.docs.accept(await office.tickets.get(child.id), left["id"], by=minh.id, reason="tôi thấy ổn")  # the author cannot
    assert (await office.tickets.get(child.id)).status != S.DONE
    await office.answer(gate.id, {"approved": True, "accept": {left["id"]: "lý do từ chối làm ở task T-2"}}, by=hung.id)
    assert (await office.tickets.get(child.id)).status == S.DONE
    comments = " ".join(c.body for c in await office.tickets.comments(child.id, limit=80))
    assert "Chấp nhận điểm lệch X-DESIGN-SPEC (C-1): lý do từ chối làm ở task T-2" in comments


async def test_an_acceptance_survives_a_new_round_and_other_wording(office: Office, runtime: FakeRuntime, task) -> None:
    team, lan, minh, hung, epic, child = task
    outcomes: list[str] = []
    runtime.scripts += [write_design(office, child, epic, outcomes), lambda ctx: _async(answer(ctx, [finding()]))]
    await start(office, team, lan, child)
    current = await office.tickets.get(child.id)
    (found,) = await office.crosscheck.findings(current)
    await office.docs.accept(current, found.id, by=hung.id, reason="chấp nhận")
    # the AI tries again with the same document: the next round words the same finding differently
    runtime.scripts += [
        write_design(office, child, epic, outcomes, edits=["Từ 5 lên 9 (xem lại)."]),
        lambda ctx: _async(answer(ctx, [finding(claim="C-1 bỏ sót yêu cầu về lý do từ chối")])),
    ]
    await office.stages.retry(child.id, by="user") if (await office.stages.status(current))["status"] == "failed" else None
    office.scheduler.kick()
    await office.wakes.enqueue(current.assignee_actor_id, __import__("actoroffice.domain.run", fromlist=["WakeSource"]).WakeSource.MANUAL, ticket_id=child.id, payload={})
    await office.scheduler.wait_idle(timeout=30)
    assert "Checks passed" in outcomes[-1], outcomes[-1]
    (again,) = await office.crosscheck.findings(await office.tickets.get(child.id))
    assert again.id == found.id and again.status == "accepted" and "bỏ sót" in again.message


async def test_a_low_finding_is_a_remark_not_a_block(office: Office, runtime: FakeRuntime, task) -> None:
    team, lan, minh, hung, epic, child = task
    outcomes: list[str] = []
    runtime.scripts += [write_design(office, child, epic, outcomes), lambda ctx: _async(answer(ctx, [finding(severity="low", claim="D-1 có thể ghi rõ hơn")]))]
    await start(office, team, lan, child)
    assert "Checks passed" in outcomes[0]
    (card,) = await cards(office, child.id)
    assert card.payload["doc_core"]["open"] == [] and [w["rule"] for w in card.payload["doc_core"]["warnings"]] == ["X-DESIGN-SPEC"]


async def test_no_structured_answer_is_skipped_not_passed_off_as_clean(office: Office, runtime: FakeRuntime, task) -> None:
    team, lan, minh, hung, epic, child = task
    outcomes: list[str] = []

    async def prose(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.COMPLETED, session_id="x", text="Tài liệu trông ổn.", usage=Usage())

    runtime.scripts += [write_design(office, child, epic, outcomes), prose]
    await start(office, team, lan, child)
    (card,) = await cards(office, child.id)
    check = next(c for c in card.payload["checks"] if c["id"] == "crosscheck_clean")
    assert check["status"] == "skipped" and "không trả kết quả có cấu trúc" in check["summary"] and "CHƯA đối chiếu được" in check["summary"]


async def test_a_runtime_that_fails_is_said_on_the_card(office: Office, runtime: FakeRuntime, task) -> None:
    team, lan, minh, hung, epic, child = task
    outcomes: list[str] = []

    async def out_of_quota(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.ERROR, session_id="x", text="", error="Kiro: You've reached your monthly usage limit.", usage=Usage())

    runtime.scripts += [write_design(office, child, epic, outcomes), out_of_quota]
    await start(office, team, lan, child)
    (card,) = await cards(office, child.id)
    check = next(c for c in card.payload["checks"] if c["id"] == "crosscheck_clean")
    assert check["status"] == "skipped" and "monthly usage limit" in check["summary"] and "người duyệt cần tự đối chiếu" in check["summary"]


async def test_switched_off_with_ai_review(office: Office, runtime: FakeRuntime, task) -> None:
    team, lan, minh, hung, epic, child = task
    office.settings.ai_review = False
    outcomes: list[str] = []
    runtime.scripts += [write_design(office, child, epic, outcomes)]
    await start(office, team, lan, child)
    assert "Checks passed" in outcomes[0] and crosscheck_prompts(runtime) == []
