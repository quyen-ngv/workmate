"""The gate holds: what people approved is what the next document is checked against, an approval happens whole or not
at all, and what an approver decided stays decided.

The fixture documents of test_docflow are older than some rules of the pack. These tests are about the gate, not about
the wording of documents, so every rule of the pack except the ones named in KEEP is turned down to a warning here,
and the machine checks a stage passes before its gate opens are not run."""

from __future__ import annotations

import json

import pytest

from actoroffice.app import Office
from actoroffice.domain.errors import NotFound, OfficeError, PolicyDenied
from actoroffice.domain.run import TurnResult
from actoroffice.domain.work import TicketStatus
from actoroffice.services import docflow

from .fakes import FakeRuntime, TurnCtx
from .seed import IDENT, git
from .test_docflow import analysis, breakdown, cards, design, designed, done, office, put, runtime, setup, spec  # noqa: F401  (fixtures)

S = TicketStatus
KEEP = {"R-COVER-REQ", "R-COVER-AC", "R-FRESH", "R-BASELINE"}
AC_2 = "- AC-1.2 (từ chối) — Given khách đã hoàn 9 lần hôm nay When yêu cầu hoàn Then bị từ chối kèm lý do\n  Truy vết: {epic}/REQ-2\n"


@pytest.fixture(autouse=True)
async def only_gate_rules(office: Office, monkeypatch):
    await office.docs.save_profile("office", {"rules": {rule: "warn" for rule in office.docs.pack.rules if rule not in KEEP}})

    async def passed(*args, **kwargs):  # what a stage must pass before its gate opens has its own tests
        return []

    monkeypatch.setattr(office.stages.checks, "run", passed)
    yield
    docflow.configure(None)


def uncovered(findings, item: str) -> bool:
    """An open R-COVER-REQ about this requirement, however the reference to it is written (`REQ-2`, `AO-1/REQ-2`)."""
    return any(f.rule == "R-COVER-REQ" and f.status == "open" and (f.item or "").split("/")[-1] == item for f in findings)


def errors(findings) -> list[str]:
    return sorted(f"{f.rule}:{f.item}" for f in findings if f.status == "open" and f.severity == "error")


async def approve(office: Office, ticket, key: str | None = None) -> None:
    sql = "UPDATE documents SET approved_revision = revision WHERE ticket_id = ?" + (" AND key = ?" if key else "")
    await office.db.execute(sql, (ticket.id, key) if key else (ticket.id,))


async def comments(office: Office, ticket) -> str:
    return "\n".join(c.body for c in await office.tickets.comments(ticket.id, limit=100))


async def kids(office: Office, epic) -> list:
    return [await office.tickets.get(r["id"]) for r in await office.db.fetchall("SELECT id FROM tickets WHERE parent_id = ? ORDER BY id", (epic.id,))]


async def tool(ctx: TurnCtx, name: str, args: dict) -> str:
    """A tool call whose refusal is read as text, however the tool reports it."""
    try:
        return await ctx.office(name, args)
    except Exception as exc:
        return f"ERROR {exc}"


async def epic_with_tasks(office: Office, epic) -> None:
    await put(office, epic, "breakdown", breakdown(epic.key))
    for task in ("T-1", "T-2", "T-3"):
        await put(office, epic, f"spec.{task}", spec(epic.key, task))


async def design_card(office: Office, runtime: FakeRuntime, setup, *, assign: bool = True, body=None):
    """A task with an approved spec whose design was written and handed in: the ticket and its open card."""
    team, lan, minh, hung, repo, epic = setup
    await put(office, epic, "analysis", analysis(epic.key))
    await approve(office, epic)
    child = await office.tickets.create("Nâng hạn mức", project_id=epic.project_id, parent_id=epic.id)
    await put(office, child, "spec", spec(epic.key, "T-1"))
    await approve(office, child)
    if assign:
        await office.teams.set_participant(child.id, minh.id, ["dev"])

    async def write(ctx: TurnCtx) -> TurnResult:
        current = await office.tickets.get_document(child.id, "design")
        text = body(child.key, epic.key) if body else design(child.key, epic.key)
        await ctx.office("doc_put", {"key": "design", "body": text, "base_revision": current.revision if current else None})
        said.append(await ctx.office("stage_complete", {"summary": "Design"}))
        return done(ctx)

    said: list[str] = []
    runtime.scripts.append(write)
    await office.stages.start(child.key, template="task-spec", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=30)
    found = await cards(office, child.id)
    assert len(found) == 1, said
    (card,) = found
    return await office.tickets.get(child.id), card, write


# ---- 1. an approved document is not rewritten under the documents written from it ----------------------------------


async def test_an_approved_spec_rewritten_in_the_task_is_still_read_as_approved(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    stage = await office.stages.stage_def(child)
    await put(office, child, "design", design(child.key, epic.key, cover_all=False))
    assert errors(await office.docs.check(child, stage)) == ["R-COVER-AC:AC-1.2"]

    weakened = spec(epic.key, "T-1").replace(AC_2.format(epic=epic.key), "")
    await put(office, child, "spec", weakened)  # not through doc_put: e.g. a revision saved before the guard existed
    found = await office.docs.check(child, stage)
    assert errors(found) == ["R-COVER-AC:AC-1.2", "R-UPSTREAM-EDITED:None"], "the design still answers to the approved spec, and the rewrite is reported"
    assert "R-UPSTREAM-EDITED" in {f.rule for f in await office.docs.blocking(child, stage)}
    ds = await office.docs.docset(child)
    assert ds.own[f"{child.key}/spec"].revision == 1 and ds.edited == {f"{child.key}/spec": 2}

    await put(office, child, "spec", spec(epic.key, "T-1"))  # the approved text is put back
    assert errors(await office.docs.check(child, stage)) == ["R-COVER-AC:AC-1.2"]


async def test_an_approved_analysis_rewritten_during_breakdown_does_not_move_the_goal(office: Office, setup) -> None:
    team, lan, minh, hung, repo, epic = setup
    await office.db.execute("UPDATE tickets SET stage_template = 'epic', stage = 'breakdown', team_id = ? WHERE id = ?", (team.id, epic.id))
    epic = await office.tickets.get(epic.id)
    await put(office, epic, "analysis", analysis(epic.key))
    await approve(office, epic)
    await put(office, epic, "breakdown", breakdown(epic.key).replace("Truy vết: REQ-2", "Truy vết: REQ-1"))
    for task in ("T-1", "T-2", "T-3"):
        await put(office, epic, f"spec.{task}", spec(epic.key, task).replace(AC_2.format(epic=epic.key), ""))
    stage = await office.stages.stage_def(epic)
    found = await office.docs.check(epic, stage)
    before = errors(found)
    assert uncovered(found, "REQ-2")

    renamed = analysis(epic.key).replace("### REQ-2: Từ chối phải kèm lý do", "### REQ-9: Từ chối phải kèm lý do")
    assert renamed != analysis(epic.key)
    await put(office, epic, "analysis", renamed)  # the requirement nobody covered is written out of the approved analysis
    assert errors(await office.docs.check(epic, stage)) == sorted([*before, "R-UPSTREAM-EDITED:None"]), "REQ-2 must still be covered"

    # the stage's own documents are read as they are now: that is how the breakdown is worked on
    await put(office, epic, "spec.T-2", spec(epic.key, "T-2"))
    assert not uncovered(await office.docs.check(epic, stage), "REQ-2")


async def test_the_ai_cannot_save_an_approved_document_outside_its_stage(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    with pytest.raises(OfficeError, match="đã được duyệt.*stage_reopen"):
        await office.docs.guard_put(child, "spec", spec(epic.key, "T-1").replace("9 lần", "99 lần"))
    with pytest.raises(OfficeError, match="đã được duyệt"):
        await office.docs.guard_put(epic, "analysis", analysis(epic.key) + "\nthêm")
    await office.docs.guard_put(child, "spec", spec(epic.key, "T-1"))  # the approved text itself
    await office.docs.guard_put(child, "design", "anything")  # the stage's own document
    await office.docs.guard_put(child, "handoff", "anything")  # not a document of the pack
    await office.docs.guard_put(epic, "breakdown", "anything")  # not written yet
    # at the stage that writes it, an approved document is revised: that is how an epic grows
    await office.db.execute("UPDATE tickets SET stage = 'analyze' WHERE id = ?", (epic.id,))
    await office.docs.guard_put(await office.tickets.get(epic.id), "analysis", analysis(epic.key) + "\nthêm")


async def test_doc_put_refuses_an_approved_upstream_document(office: Office, runtime: FakeRuntime, setup) -> None:
    seen: dict[str, str] = {}
    team, lan, minh, hung, repo, epic = setup

    async def meddle(ctx: TurnCtx) -> TurnResult:
        seen["spec"] = await tool(ctx, "doc_put", {"key": "spec", "body": spec(epic.key, "T-1").replace("9 lần", "99 lần"), "base_revision": 1})
        seen["get"] = await ctx.office("doc_get", {"key": "spec"})
        return done(ctx)

    runtime.scripts.append(meddle)
    await put(office, epic, "analysis", analysis(epic.key))
    await approve(office, epic)
    child = await office.tickets.create("Nâng hạn mức", project_id=epic.project_id, parent_id=epic.id)
    await put(office, child, "spec", spec(epic.key, "T-1"))
    await approve(office, child)
    await office.stages.start(child.key, template="task-spec", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=30)
    assert "đã được duyệt" in seen["spec"] and "stage_reopen" in seen["spec"]
    got = json.loads(seen["get"])
    assert (got["revision"], got["approved_revision"]) == (1, 1) and "99 lần" not in got["body"]


# ---- 2, 3, 12. an approval happens whole, on what the card showed --------------------------------------------------


async def test_a_response_that_cannot_be_carried_out_creates_nothing(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await epic_with_tasks(office, epic)
    for response, named in (
        ({"drop": ["T-3"], "assign": {"T-2": "nobody-like-this"}}, "T-2"),
        ({"flows": {"T-3": "bogus"}}, "T-3"),
        ({"drop": ["T-9"]}, "T-9"),
    ):
        with pytest.raises(OfficeError, match=named):
            await office.docs.spawn(epic, response, by=lan.id)
    assert [k.title for k in await kids(office, epic)] == ["Nâng hạn mức"], "only the task of the fixture: nothing was created"
    assert await office.tickets.open_blockers(epic.id) == []


async def test_a_child_that_was_created_but_not_started_is_started_at_the_next_approval(office: Office, designed, monkeypatch) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await epic_with_tasks(office, epic)
    real, calls = office.stages.start, []

    async def breaks_on_the_second(ref, **kwargs):
        calls.append(ref)
        if len(calls) == 2:
            raise OfficeError("hết chỗ")
        return await real(ref, **kwargs)

    monkeypatch.setattr(office.stages, "start", breaks_on_the_second)
    with pytest.raises(OfficeError, match="hết chỗ"):
        await office.docs.spawn(epic, {"drop": ["T-3"], "assign": {"T-2": minh.id}}, by=lan.id)
    monkeypatch.setattr(office.stages, "start", real)
    made = [k for k in await kids(office, epic) if k.id != child.id]
    assert [(k.title, bool(k.stage)) for k in made] == [("Nâng hạn mức lên 9", True), ("Lý do từ chối", False)]

    again = await office.docs.spawn(await office.tickets.get(epic.id), {"drop": ["T-3"], "assign": {"T-2": minh.id}}, by=lan.id)
    assert [t.title for t in again] == ["Lý do từ chối"] and again[0].stage == "design", "the stranded child is started, nothing is created twice"
    assert [k.title for k in await kids(office, epic) if k.id != child.id] == ["Nâng hạn mức lên 9", "Lý do từ chối"]
    assert [m.user.name for m in await office.teams.participants(again[0].id)] == ["Minh"]
    spec_doc = await office.tickets.get_document(again[0].id, "spec")
    assert spec_doc is not None and (spec_doc.revision, spec_doc.approved_revision) == (1, 1)
    assert len(await office.tickets.open_blockers(epic.id)) == 2


async def test_a_dropped_task_stays_dropped_until_it_is_restored(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await epic_with_tasks(office, epic)
    first = await office.docs.spawn(epic, {"drop": ["T-3"]}, by=lan.id)
    assert [t.title for t in first] == ["Nâng hạn mức lên 9", "Lý do từ chối"]
    assert await office.docs.spawn(await office.tickets.get(epic.id), {}, by=lan.id) == [], "approved again without naming T-3: it does not come back"
    back = await office.docs.spawn(await office.tickets.get(epic.id), {"restore": ["T-3"]}, by=lan.id)
    assert [t.title for t in back] == ["Dọn hằng số cũ"]
    assert await office.docs.spawn(await office.tickets.get(epic.id), {"drop": ["T-3"]}, by=lan.id) == [], "a task that is a ticket is not dropped by a later answer"


async def test_children_made_before_the_link_existed_are_found_by_their_description(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await epic_with_tasks(office, epic)
    await office.docs.spawn(epic, {"drop": ["T-3"]}, by=lan.id)
    await office.db.execute("DELETE FROM doc_spawned WHERE parent_id = ? AND child_id IS NOT NULL", (epic.id,))
    assert await office.docs.spawn(await office.tickets.get(epic.id), {}, by=lan.id) == []


async def test_spawn_copies_the_approved_text_not_a_later_one(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await epic_with_tasks(office, epic)
    await approve(office, epic)
    await put(office, epic, "spec.T-1", spec(epic.key, "T-1").replace("9 lần mỗi ngày", "99 lần mỗi ngày"))  # nobody approved this
    created = await office.docs.spawn(await office.tickets.get(epic.id), {"drop": ["T-2", "T-3"]}, by=lan.id)
    copied = await office.tickets.get_document(created[0].id, "spec")
    assert copied is not None and "99 lần" not in copied.body and copied.approved_revision == copied.revision


async def test_business_gate_is_approved_whole_on_what_the_card_showed(office: Office, runtime: FakeRuntime, setup) -> None:
    team, lan, minh, hung, repo, epic = setup
    E = epic.key
    said: dict[str, str] = {}

    async def analyze(ctx: TurnCtx) -> TurnResult:
        await ctx.office("doc_put", {"key": "analysis", "body": analysis(E), "base_revision": None})
        await ctx.office("stage_complete", {"summary": "xong"})
        return done(ctx)

    async def break_down(ctx: TurnCtx) -> TurnResult:
        await ctx.office("doc_put", {"key": "breakdown", "body": breakdown(E), "base_revision": None})
        for task in ("T-1", "T-2", "T-3"):
            await ctx.office("doc_put", {"key": f"spec.{task}", "body": spec(E, task), "base_revision": None})
        await ctx.office("stage_complete", {"summary": "3 task"})
        # the same turn keeps writing after the card was opened: refused, the card shows what will be approved
        said["late"] = await tool(ctx, "doc_put", {"key": "spec.T-1", "body": spec(E, "T-1").replace("9 lần mỗi ngày", "99 lần mỗi ngày"), "base_revision": 1})
        return done(ctx)

    async def complete_again(ctx: TurnCtx) -> TurnResult:
        await ctx.office("stage_complete", {"summary": "3 task, bản mới"})
        return done(ctx)

    runtime.scripts += [analyze, break_down]
    await office.stages.start(E, template="epic", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=30)
    (card,) = await cards(office, epic.id)
    await office.answer(card.id, {"approved": True}, by=lan.id)
    await office.scheduler.wait_idle(timeout=30)
    (card,) = await cards(office, epic.id)
    assert card.payload["gate"] == "business" and "đang chờ duyệt" in said["late"]
    assert (await office.tickets.get_document(epic.id, "spec.T-1")).revision == 1

    # (2) a response that cannot be carried out: nothing is approved, nothing is created, the card stays
    for bad in ({"assign": {"T-2": "khong-co-ai"}}, {"flows": {"T-1": "bogus"}}):
        with pytest.raises(OfficeError):
            await office.answer(card.id, {"approved": True, **bad}, by=lan.id)
    assert [c.id for c in await cards(office, epic.id)] == [card.id] and await kids(office, epic) == []
    assert (await office.tickets.get(epic.id)).stage == "breakdown"
    assert (await office.tickets.get_document(epic.id, "breakdown")).approved_revision is None

    # (3) a revision saved after the card was opened (here by a person, on the web): the card is withdrawn, not approved
    await put(office, epic, "spec.T-1", spec(E, "T-1").replace("9 lần mỗi ngày", "12 lần mỗi ngày"))
    runtime.scripts.append(complete_again)
    with pytest.raises(OfficeError, match="đã đổi sau khi thẻ được mở"):
        await office.answer(card.id, {"approved": True}, by=lan.id)
    assert (await office.tickets.get_document(epic.id, "spec.T-1")).approved_revision is None and await kids(office, epic) == []
    assert "tài liệu đã đổi sau khi thẻ được mở: `spec.T-1`" in await comments(office, epic)
    await office.scheduler.wait_idle(timeout=30)
    (again,) = await cards(office, epic.id)
    assert again.id != card.id and {d["key"]: d["revision"] for d in again.payload["docs"]}["spec.T-1"] == 2

    await office.answer(again.id, {"approved": True, "drop": ["T-3"], "assign": {"T-1": minh.id}}, by=lan.id)
    children = await kids(office, epic)
    assert [c.title for c in children] == ["Nâng hạn mức lên 9", "Lý do từ chối"]
    copied = await office.tickets.get_document(children[0].id, "spec")
    assert copied is not None and "12 lần mỗi ngày" in copied.body, "the child's spec is the text the approver saw"
    after = await office.tickets.get(epic.id)
    assert after.stage == "close" and after.status == S.BLOCKED


# ---- 4. the epic's spec for a task is approved again ---------------------------------------------------------------


async def test_a_task_gets_the_newly_approved_spec_and_its_design_is_sent_back(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, _ = designed
    await epic_with_tasks(office, epic)
    await approve(office, epic)
    first, second = await office.docs.spawn(epic, {"drop": ["T-3"]}, by=lan.id)
    for task in (first, second):
        await put(office, task, "design", design(task.key, epic.key))
        await approve(office, task, "design")
        await office.db.execute("UPDATE tickets SET status = 'done' WHERE id = ?", (task.id,))

    await put(office, epic, "spec.T-1", spec(epic.key, "T-1").replace("Hạn mức là 9 lần mỗi ngày", "Hạn mức là 15 lần mỗi ngày"))
    await approve(office, epic)
    assert await office.docs.spawn(await office.tickets.get(epic.id), {}, by=lan.id) == []
    copied = await office.tickets.get_document(first.id, "spec")
    assert copied is not None and "15 lần mỗi ngày" in copied.body and (copied.revision, copied.approved_revision) == (2, 2)
    assert "được cập nhật theo bản vừa duyệt lại" in await comments(office, first)
    untouched = await office.tickets.get_document(second.id, "spec")
    assert untouched is not None and untouched.revision == 1 and "cập nhật theo bản" not in await comments(office, second)

    acted = await office.docs.sweep(around=await office.tickets.get(epic.id))
    assert {"ticket": first.key, "doc": "design", "state": "lech", "action": "revise"} in acted
    after = await office.tickets.get(first.id)
    assert after.stage == "design" and after.status == S.TODO, "the finished task is working on its design again"
    assert "R-BASELINE" in {f.rule for f in await office.docs.check(after, await office.stages.stage_def(after)) if f.status == "open"}
    assert (await office.tickets.get(second.id)).status == S.DONE


async def test_a_design_waiting_for_its_card_is_withdrawn_when_the_spec_changes(office: Office, runtime: FakeRuntime, setup) -> None:
    team, lan, minh, hung, repo, epic = setup
    await office.db.execute("UPDATE tickets SET stage_template = 'epic', stage = 'breakdown', team_id = ? WHERE id = ?", (team.id, epic.id))
    await put(office, epic, "analysis", analysis(epic.key))
    await epic_with_tasks(office, epic)
    await approve(office, epic)

    async def write(ctx: TurnCtx) -> TurnResult:
        key = json.loads(await ctx.office("stage_info", {}))["ticket"]
        await ctx.office("doc_put", {"key": "design", "body": design(key, epic.key), "base_revision": None})
        await ctx.office("stage_complete", {"summary": "Design"})
        return done(ctx)

    runtime.scripts.append(write)
    (task,) = await office.docs.spawn(await office.tickets.get(epic.id), {"drop": ["T-2", "T-3"]}, by=lan.id)
    await office.scheduler.wait_idle(timeout=30)
    (card,) = await cards(office, task.id)
    await put(office, epic, "spec.T-1", spec(epic.key, "T-1").replace("Hạn mức là 9 lần mỗi ngày", "Hạn mức là 15 lần mỗi ngày"))
    await approve(office, epic)
    await office.docs.spawn(await office.tickets.get(epic.id), {}, by=lan.id)
    assert card.id not in [c.id for c in await cards(office, task.id)], "nobody approves a design written from the old spec"
    assert "Quay lại giai đoạn Thiết kế" in await comments(office, task)


# ---- 9. evidence -------------------------------------------------------------------------------------------------------


def change_limit(repo, old: int, new: int) -> None:
    limits = repo / "src/shop/limits.py"
    limits.write_text(limits.read_text(encoding="utf-8").replace(f"MAX_PER_DAY = {old}", f"MAX_PER_DAY = {new}"), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", f"limit {new}")


async def test_rewriting_the_item_against_the_new_code_clears_r_fresh(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    stage = await office.stages.stage_def(child)
    assert errors(await office.docs.check(child, stage)) == []
    change_limit(repo, 5, 7)
    assert errors(await office.docs.check(child, stage)) == ["R-FRESH:C-1"]
    # another part of the document is edited: C-1 still describes the old code
    await put(office, child, "design", design(child.key, epic.key).replace("Unit cho AC-1.1 và AC-1.2.", "Unit cho AC-1.1, AC-1.2 và biên."))
    assert errors(await office.docs.check(child, stage)) == ["R-FRESH:C-1"]
    # the author reads the code again and rewrites C-1: the new revision is pinned to the code as it is
    await put(office, child, "design", design(child.key, epic.key).replace("Từ 5 lên 9.", "Từ 7 lên 9 (đã đọc lại code)."))
    assert errors(await office.docs.check(child, stage)) == []
    change_limit(repo, 7, 8)
    assert errors(await office.docs.check(child, stage)) == ["R-FRESH:C-1"], "and it goes stale again when the code moves again"


async def test_accepting_stale_code_does_not_cover_the_next_change(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    stage = await office.stages.stage_def(child)
    await office.docs.check(child, stage)
    change_limit(repo, 5, 7)
    (finding,) = [f for f in await office.docs.check(child, stage) if f.rule == "R-FRESH"]
    await office.docs.accept(child, finding.id, by=hung.id, reason="đã xem, vẫn đúng")
    assert errors(await office.docs.check(child, stage)) == []
    change_limit(repo, 7, 99)
    assert errors(await office.docs.check(child, stage)) == ["R-FRESH:C-1"], "the acceptance was about the change to 7, not about 99"


# ---- 10. accepting ---------------------------------------------------------------------------------------------------


async def test_accepting_needs_the_id_of_one_finding(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await put(office, child, "design", design(child.key, epic.key, cover_all=False))
    stage = await office.stages.stage_def(child)
    (finding,) = [f for f in await office.docs.check(child, stage) if f.severity == "error"]
    for bad in ("", " ", finding.id[:7], "zzzzzzzzzz"):
        with pytest.raises(NotFound):
            await office.docs.accept(child, bad, by=hung.id, reason="x")
    assert errors(await office.docs.check(child, stage)) == ["R-COVER-AC:AC-1.2"]
    assert (await office.docs.accept(child, finding.id[:8], by=hung.id, reason="làm ở task khác")).id == finding.id
    assert errors(await office.docs.check(child, stage)) == []


async def test_an_acceptance_is_about_the_item_as_it_read(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await put(office, child, "design", design(child.key, epic.key, cover_all=False))
    stage = await office.stages.stage_def(child)
    (finding,) = [f for f in await office.docs.check(child, stage) if f.severity == "error"]
    await office.docs.accept(child, finding.id, by=hung.id, reason="AC-1.2 làm ở task khác")
    await put(office, child, "design", design(child.key, epic.key, cover_all=False).replace("Từ 5 lên 9.", "Từ 5 lên 9!"))
    assert errors(await office.docs.check(child, stage)) == [], "another part of the design changed: the acceptance stands"
    # AC-1.2 itself is rewritten and approved again: what was accepted is no longer what is there
    await put(office, child, "spec", spec(epic.key, "T-1").replace("Then bị từ chối kèm lý do", "Then bị từ chối, ghi log và báo quản lý"))
    await approve(office, child, "spec")
    assert "R-COVER-AC:AC-1.2" in errors(await office.docs.check(child, stage))


# ---- 11. the person the card was sent to can answer it ----------------------------------------------------------------


async def test_the_owner_asked_as_fallback_can_approve_and_accept(office: Office, runtime: FakeRuntime, setup) -> None:
    team, lan, minh, hung, repo, epic = setup
    E = epic.key
    await office.db.execute("UPDATE memberships SET roles_json = '[\"dev\"]' WHERE user_id = ?", (lan.id,))  # nobody is BA in this team

    async def analyze(ctx: TurnCtx) -> TurnResult:
        await ctx.office("doc_put", {"key": "analysis", "body": analysis(E), "base_revision": None})
        await ctx.office("stage_complete", {"summary": "xong"})
        return done(ctx)

    runtime.scripts.append(analyze)
    await office.stages.start(E, template="epic", by="user", team_id=team.id, owner_user_id=minh.id)
    await office.scheduler.wait_idle(timeout=30)
    (card,) = await cards(office, epic.id)
    assert card.payload["approver_names"] == ["Minh"] and card.payload["roles"] == ["ba"]
    with pytest.raises(PolicyDenied):
        await office.answer(card.id, {"approved": True}, by=hung.id)
    assert await office.docs._asked_to_approve(await office.tickets.get(epic.id), minh.id, await office.teams.gate_policy(epic, "analysis"))
    await office.answer(card.id, {"approved": True}, by=minh.id)
    assert (await office.tickets.get(epic.id)).stage == "breakdown"


# ---- 15. a task does not answer for the whole epic ---------------------------------------------------------------------


async def test_a_task_is_not_asked_to_cover_every_requirement_of_the_epic(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    only_req_1 = spec(epic.key, "T-1").replace(f"  Truy vết: {epic.key}/REQ-2\n", f"  Truy vết: {epic.key}/REQ-1\n")
    await put(office, child, "spec", only_req_1)
    await approve(office, child, "spec")
    await put(office, child, "design", design(child.key, epic.key))
    found = await office.docs.check(child)
    assert errors(found) == [], "REQ-2 belongs to another task of the epic"
    assert {k: v.value for k, v in (await office.docs.states(child, found)).items()} == {"design": "khop", "spec": "khop"}
    panel = await office.docs.panel(child)
    assert panel is not None and panel["open"] == []
    # at the epic, where the requirements are divided over the tasks, an uncovered one is still found
    await put(office, epic, "breakdown", breakdown(epic.key).replace("Truy vết: REQ-2", "Truy vết: REQ-1"))
    for task in ("T-1", "T-2", "T-3"):
        await put(office, epic, f"spec.{task}", only_req_1)
    epic = await office.tickets.get(epic.id)
    assert uncovered(await office.docs.check(epic, await office.stages.stage_def(epic)), "REQ-2")


# ---- 17. a cause is recorded once it was acted on -----------------------------------------------------------------------


async def test_a_reaction_that_breaks_is_not_recorded_as_done(office: Office, designed, monkeypatch) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await approve(office, child, "design")
    await office.db.execute("UPDATE tickets SET status = 'done' WHERE id = ?", (child.id,))
    await put(office, child, "spec", spec(epic.key, "T-1").replace("Hạn mức là 9 lần mỗi ngày", "Hạn mức là 12 lần mỗi ngày"))
    await approve(office, child, "spec")
    real = office.stages.reopen

    async def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(office.stages, "reopen", broken)
    with pytest.raises(RuntimeError):
        await office.docs.react(await office.tickets.get(child.id))
    assert await office.db.fetchall("SELECT 1 FROM doc_notices WHERE ticket_id = ?", (child.id,)) == []
    monkeypatch.setattr(office.stages, "reopen", real)
    acted = await office.docs.react(await office.tickets.get(child.id))
    assert acted == [{"ticket": child.key, "doc": "design", "state": "lech", "action": "revise"}], "the next sweep acts on it"


async def test_a_stage_that_cannot_be_reopened_is_still_announced(office: Office, designed, monkeypatch) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await approve(office, child, "design")
    await put(office, child, "spec", spec(epic.key, "T-1").replace("Hạn mức là 9 lần mỗi ngày", "Hạn mức là 12 lần mỗi ngày"))
    await approve(office, child, "spec")

    async def refused(*args, **kwargs):
        raise OfficeError("không mở lại được")

    monkeypatch.setattr(office.stages, "reopen", refused)
    acted = await office.docs.react(await office.tickets.get(child.id))
    assert acted == [{"ticket": child.key, "doc": "design", "state": "lech", "action": "notify_author"}]
    assert "`design` (bản 1, đã duyệt) hiện lệch" in await comments(office, child)
    assert await office.docs.react(await office.tickets.get(child.id)) == []


# ---- the cards: who sent, what was sent back, remarks on items, several approvers ---------------------------------------


async def test_the_leads_card_says_who_sent_it_and_the_dev_can_take_it_back(office: Office, runtime: FakeRuntime, setup) -> None:
    team, lan, minh, hung, repo, epic = setup
    child, submit, _ = await design_card(office, runtime, setup)
    with pytest.raises(OfficeError, match="không có bản gửi"):
        await office.stages.withdraw_submission(child.key, minh.id)
    with pytest.raises(OfficeError, match="thuộc người duyệt"):
        await office.answer(submit.id, {"approved": True, "accept": {"abcdefgh": "để sau"}}, by=minh.id)
    await office.answer(submit.id, {"approved": True, "note": "ổn rồi"}, by=minh.id)
    (gate,) = await cards(office, child.id)
    p = gate.payload
    assert (p["submitted_by"], p["submitted_by_name"], p["submit_note"], p["open_count"]) == (minh.id, "Minh", "ổn rồi", 0) and p["submitted_at"]
    # (g) the web answers for a named person: the dev who sent it cannot approve the lead's card
    with pytest.raises(PolicyDenied):
        await office.answer(gate.id, {"approved": True}, by=minh.id)

    with pytest.raises(PolicyDenied):
        await office.stages.withdraw_submission(child.key, lan.id)
    back = await office.stages.withdraw_submission(child.key, minh.id)
    (only,) = await cards(office, child.id)
    assert only.id == back.id and only.payload["phase"] == "submit" and only.payload["approver_names"] == ["Minh"]
    assert "Minh rút lại Thiết kế đã gửi duyệt" in await comments(office, child)
    await office.answer(back.id, {"approved": True}, by=minh.id)
    (gate,) = await cards(office, child.id)
    await office.answer(gate.id, {"approved": True}, by=hung.id)
    assert (await office.tickets.get(child.id)).status == S.DONE
    with pytest.raises(OfficeError):
        await office.stages.withdraw_submission(child.key, minh.id)


async def test_a_rejection_needs_a_note_and_the_next_card_shows_what_was_sent_back(office: Office, runtime: FakeRuntime, setup) -> None:
    team, lan, minh, hung, repo, epic = setup
    child, gate, write = await design_card(office, runtime, setup, assign=False)
    assert "phase" not in gate.payload and "last_rejection" not in gate.payload
    with pytest.raises(OfficeError, match="Cần ghi chú khi trả lại"):
        await office.answer(gate.id, {"approved": False}, by=hung.id)
    runtime.scripts.append(write)
    remarks = [{"doc": "design", "item": "C-1", "text": "nêu rõ đơn vị của hạn mức"}, {"doc": "design", "item": "", "text": "  "}]
    await office.answer(gate.id, {"approved": False, "note": "thiếu trường hợp lỗi", "comments": remarks}, by=hung.id)
    assert [(c["doc"], c["item"], c["text"], c["by"]) for c in await office.docs.comments(child)] == [("design", "C-1", "nêu rõ đơn vị của hạn mức", hung.id)]
    wakes = await office.db.fetchall("SELECT payload_json FROM wake_requests WHERE ticket_id = ? AND payload_json LIKE '%redo%' ORDER BY id DESC LIMIT 1", (child.id,))
    redo = json.dumps(json.loads(wakes[0]["payload_json"]), ensure_ascii=False)  # the wake may hold it merged with others
    assert "thiếu trường hợp lỗi" in redo and "design · C-1: nêu rõ đơn vị của hạn mức" in redo and '"comments"' in redo
    await office.scheduler.wait_idle(timeout=30)
    (again,) = await cards(office, child.id)
    last = again.payload["last_rejection"]
    assert (last["by"], last["by_name"], last["note"], last["revision_by_doc"]) == (hung.id, "Hùng", "thiếu trường hợp lỗi", {"design": 1}) and last["at"]
    assert [c["text"] for c in last["comments"]] == ["nêu rõ đơn vị của hạn mức"]

    await office.answer(again.id, {"approved": True, "comments": [{"doc": "design", "item": "D-1", "text": "ổn, lần sau ghi rõ hơn"}]}, by=hung.id)
    assert await office.docs.comments(child) == [], "an approval closes what was open"
    assert [c["item"] for c in await office.docs.comments(child, open_only=False)] == ["C-1", "D-1"]
    assert "design · D-1: ổn, lần sau ghi rõ hơn" in await comments(office, child)


async def test_two_approvers_both_count_and_their_choices_are_kept(office: Office, runtime: FakeRuntime, setup) -> None:
    from actoroffice.domain.stage import GatePolicy

    team, lan, minh, hung, repo, epic = setup
    tam = await office.teams.create_user("Tâm")
    await office.teams.add_member(team.id, tam.id, ["lead"])
    team = await office.teams.get_team(team.id)
    team.settings.gates["design"] = GatePolicy(roles=["lead"], min=2, fallback=None, accept_findings=["lead"])
    await office.teams.update_team(team)
    child, gate, _ = await design_card(office, runtime, setup, assign=False)
    assert gate.payload["min"] == 2 and sorted(gate.payload["approver_names"]) == ["Hùng", "Tâm"]
    first = await office.answer(gate.id, {"approved": True, "note": "ổn"}, by=hung.id)
    assert first.status.value == "pending" and [(a["by"], a["name"], a["note"]) for a in first.payload["approvals"]] == [(hung.id, "Hùng", "ổn")]
    again = await office.answer(gate.id, {"approved": True, "note": "ổn lắm"}, by=hung.id)
    assert [a["note"] for a in again.payload["approvals"]] == ["ổn lắm"], "the same person twice is one approval"
    await office.answer(gate.id, {"approved": True}, by=tam.id)
    final = await office.interactions.get(gate.id)
    assert final.status.value == "answered" and [a["name"] for a in final.payload["approvals"]] == ["Hùng", "Tâm"] and all(a["at"] for a in final.payload["approvals"])
    assert (await office.tickets.get(child.id)).status == S.DONE


def test_choices_of_several_approvers_are_merged() -> None:
    from actoroffice.services.stages import _merged_choices

    approvals = [
        {"by": "a", "choices": {"drop": ["T-3"], "assign": {"T-1": "minh"}, "flows": {"T-1": "task"}}},
        {"by": "b"},
        {"by": "c", "choices": {"drop": ["T-3", "T-4"], "assign": {"T-1": "lan", "T-2": "minh"}}},
    ]
    assert _merged_choices(approvals) == {"drop": ["T-3", "T-4"], "assign": {"T-1": "lan", "T-2": "minh"}, "flows": {"T-1": "task"}}


# ---- what the card shows ------------------------------------------------------------------------------------------------


async def test_the_card_counts_cross_check_differences_and_says_when_it_did_not_run(office: Office, designed) -> None:
    from actoroffice.doccore.model import make_finding

    team, lan, minh, hung, repo, epic, child = designed
    await office.docs.check(epic)  # the epic's own check pinned the code its analysis cites
    stage = await office.stages.stage_def(child)
    srun = await office.stages.ensure_run(child)
    view = await office.docs.approval_view(child, stage)
    assert view["states"]["design"]["state"] == "khop" and view["crosscheck"]["ran"] is False and view["crosscheck"]["reason"]
    change = next(c for c in view["declared_changes"] if c["changes"] == f"{epic.key}/F-1")
    assert "MAX_PER_DAY = 5" in change["old"], "the text of the parent's item a change is declared against"

    difference = make_finding("X-DESIGN-SPEC", "error", f"{child.key}/design", "C-1", "design nói 9, spec nói 12", "x")
    await office.db.execute(
        "INSERT INTO doc_crosschecks(ticket_id, stage_run_id, input_hash, findings_json, created_at) VALUES (?, ?, 'h', ?, '2026-11-15')",
        (child.id, srun.id, json.dumps([json.loads(difference.model_dump_json())], ensure_ascii=False)),
    )
    for status, summary, ran in (("passed", "đã đối chiếu 3 vòng", True), ("skipped", "⚠ CHƯA đối chiếu được bằng AI (lượt chạy lỗi)", False)):
        checks = [{"id": "crosscheck_clean", "status": status, "summary": summary}]
        await office.db.execute("UPDATE stage_runs SET checks_json = ? WHERE id = ?", (json.dumps(checks, ensure_ascii=False), srun.id))
        view = await office.docs.approval_view(child, stage)
        assert view["crosscheck"] == {"ran": ran, "reason": "" if ran else summary}
    assert view["states"]["design"]["state"] == "loi" and [f["rule"] for f in view["open"]] == ["X-DESIGN-SPEC"]
