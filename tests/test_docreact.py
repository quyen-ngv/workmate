"""After approval the world moves on: an approved document that no longer matches is acted on once, by the pack's
`on_change`; an epic can grow without redoing what exists."""

from __future__ import annotations

import pytest

from actoroffice.app import Office
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.work import TicketStatus

from .seed import IDENT, git
from .test_docflow import analysis, breakdown, design, designed, office, put, runtime, setup, spec  # noqa: F401  (fixtures)

S = TicketStatus


async def approve(office: Office, ticket, key: str) -> None:
    await office.db.execute("UPDATE documents SET approved_revision = revision WHERE ticket_id = ? AND key = ?", (ticket.id, key))


async def finish(office: Office, ticket) -> None:
    await office.db.execute("UPDATE tickets SET status = 'done' WHERE id = ?", (ticket.id,))


async def comments(office: Office, ticket) -> str:
    return "\n".join(c.body for c in await office.tickets.comments(ticket.id, limit=100))


async def test_spec_change_that_touches_the_design_sends_it_back_once(office: Office, designed) -> None:
    *_, epic, child = designed
    await approve(office, child, "design")
    await finish(office, child)
    assert await office.docs.react(child) == [], "an approved design that matches: nothing happens"

    changed = spec(epic.key, "T-1").replace("Hạn mức là 9 lần mỗi ngày", "Hạn mức là 12 lần mỗi ngày")
    await put(office, child, "spec", changed)
    await approve(office, child, "spec")
    done = await office.docs.react(await office.tickets.get(child.id))
    assert done == [{"ticket": child.key, "doc": "design", "state": "lech", "action": "revise"}]
    after = await office.tickets.get(child.id)
    assert after.stage == "design" and after.status == S.TODO, "a finished task is reopened at the stage that wrote the document"
    text = await comments(office, child)
    assert "Quay lại giai đoạn Thiết kế" in text and "`design` đã lệch" in text and "BR-1" in text
    assert await office.docs.react(after) == [], "the same cause is not announced twice"


async def test_spec_change_the_design_does_not_trace_to_is_ignored(office: Office, designed) -> None:
    *_, epic, child = designed
    await approve(office, child, "design")
    await put(office, child, "spec", spec(epic.key, "T-1").replace("Không đổi cách đếm.", "Không đổi cách đếm, đã xác nhận."))
    await approve(office, child, "spec")
    assert await office.docs.react(await office.tickets.get(child.id)) == []


async def test_code_changed_after_approval_notifies_once_and_does_not_reopen(office: Office, designed) -> None:
    *_, repo, epic, child = designed
    assert await office.docs.check(child) is not None  # the gate's check pinned the cited code when the design was approved
    await approve(office, child, "design")
    await finish(office, child)
    limits = repo / "src/shop/limits.py"
    limits.write_text(limits.read_text(encoding="utf-8").replace("MAX_PER_DAY = 5", "MAX_PER_DAY = 7"), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", "limit changed elsewhere")
    done = await office.docs.sweep()
    assert {"ticket": child.key, "doc": "design", "state": "cu", "action": "notify_author"} in done
    after = await office.tickets.get(child.id)
    assert after.status == S.DONE and "code được trích đã đổi ở C-1" in await comments(office, child)
    assert await office.docs.sweep() == []
    # the code changes again: a new cause, a new notice
    limits.write_text(limits.read_text(encoding="utf-8").replace("MAX_PER_DAY = 7", "MAX_PER_DAY = 8"), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", "again")
    assert len([d for d in await office.docs.sweep() if d["ticket"] == child.key]) == 1


async def test_a_document_being_revised_is_left_alone(office: Office, designed) -> None:
    *_, epic, child = designed
    await approve(office, child, "design")
    await put(office, child, "design", design(child.key, epic.key).replace("Từ 5 lên 9.", "Từ 5 lên 9 (đang sửa)."))  # a newer, unapproved revision
    await put(office, child, "spec", spec(epic.key, "T-1").replace("9 lần mỗi ngày", "12 lần mỗi ngày"))
    await approve(office, child, "spec")
    assert [d for d in await office.docs.react(await office.tickets.get(child.id)) if d["doc"] == "design"] == []


async def test_other_workflows_and_cancelled_tickets_are_skipped(office: Office, designed) -> None:
    *_, epic, child = designed
    await approve(office, child, "design")
    await office.db.execute("UPDATE tickets SET status = 'cancelled' WHERE id = ?", (child.id,))
    await put(office, child, "spec", spec(epic.key, "T-1").replace("9 lần mỗi ngày", "12 lần mỗi ngày"))
    await approve(office, child, "spec")
    assert await office.docs.sweep() == []


# ---- an epic that grows -------------------------------------------------------------------------------------------------


async def test_epic_grows_back_to_analysis_and_keeps_its_tasks(office: Office, designed) -> None:
    team, lan, *_, epic, child = designed
    await put(office, epic, "breakdown", breakdown(epic.key))
    for task in ("T-1", "T-2", "T-3"):
        await put(office, epic, f"spec.{task}", spec(epic.key, task))
    created = await office.docs.spawn(epic, {"drop": ["T-3"]}, by=lan.id)
    assert len(created) == 2

    grown = await office.docs.epic_grew(await office.tickets.get(epic.id), "Thêm: khách VIP có hạn mức 20.", by=lan.id)
    assert grown.stage == "analyze" and grown.status == S.TODO
    assert "## Bổ sung" in grown.description and "khách VIP có hạn mức 20" in grown.description and "(Lan)" in grown.description
    assert "Quay lại giai đoạn Phân tích" in await comments(office, epic)

    # the breakdown is approved again with one more task: only that one becomes a ticket
    more = breakdown(epic.key).replace("## 3. Thứ tự", "### T-4: Hạn mức VIP\nKhách VIP hoàn tới 20 lần.\nTruy vết: REQ-1\n\n## 3. Thứ tự")
    await put(office, epic, "breakdown", more)
    await put(office, epic, "spec.T-4", spec(epic.key, "T-4"))
    await office.db.execute("UPDATE tickets SET stage = 'breakdown' WHERE id = ?", (epic.id,))
    again = await office.docs.spawn(await office.tickets.get(epic.id), {"drop": ["T-3"]}, by=lan.id)
    assert [t.title for t in again] == ["Hạn mức VIP"]
    rows = await office.db.fetchall("SELECT title FROM tickets WHERE parent_id = ? AND title <> 'Nâng hạn mức' ORDER BY id", (epic.id,))
    assert [r["title"] for r in rows] == ["Nâng hạn mức lên 9", "Lý do từ chối", "Hạn mức VIP"]


async def test_growing_needs_an_epic_and_something_to_add(office: Office, designed) -> None:
    team, lan, *_, epic, child = designed
    with pytest.raises(OfficeError, match="nội dung"):
        await office.docs.epic_grew(epic, "  ", by=lan.id)
    with pytest.raises(OfficeError, match="không phải epic"):
        await office.docs.epic_grew(child, "thêm", by=lan.id)
    await office.db.execute("UPDATE tickets SET stage = 'analyze' WHERE id = ?", (epic.id,))
    same = await office.docs.epic_grew(await office.tickets.get(epic.id), "Thêm một ý khi đang phân tích.", by=lan.id)
    assert same.stage == "analyze" and "➕ Lan bổ sung epic" in await comments(office, epic)


async def test_approving_a_new_analysis_reaches_the_tasks_written_from_it(office: Office, designed) -> None:
    """The gate's approval sweeps the tickets around it: a task whose spec traces to a changed requirement hears it."""
    team, lan, minh, hung, repo, epic, child = designed
    await approve(office, child, "design")
    await finish(office, child)
    await put(office, epic, "analysis", analysis(epic.key).replace("Hạn mức hoàn tiền mỗi ngày là 9", "Hạn mức hoàn tiền mỗi ngày là 15"))
    await approve(office, epic, "analysis")
    done = await office.docs.sweep(around=await office.tickets.get(epic.id))
    assert {"ticket": child.key, "doc": "spec", "state": "lech", "action": "revise"} in done or any(d["ticket"] == child.key for d in done)
