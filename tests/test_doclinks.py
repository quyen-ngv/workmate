"""Links between stories: typed lines in the documents (`Phụ thuộc:`, `Liên quan:`, `Thay thế:`), what the core checks
about them, the links between tickets they become, and what a change of a linked item does."""

from __future__ import annotations

import pytest

from actoroffice.app import Office
from actoroffice.doccore import LINK_VALID, PackError, build_pack, load_default, parse, run_rules
from actoroffice.services.docs import DEP_READY

from .test_docflow import LOG, breakdown, design, designed, office, put, runtime, setup, spec  # noqa: F401  (fixtures)

PACK = load_default()


def tasks(key: str, extra_t1: str = "", extra_t2: str = "") -> str:
    """A breakdown of two tasks of epic `key`, with lines under each task."""
    return f"""# Chia task — {key}
Phiên bản: 1 · Cập nhật: 2026-11-16 · Người viết: Lan

## 1. Mục tiêu
Hai task.

## 2. Danh sách task
### T-1: Nâng hạn mức
Truy vết: REQ-1
{extra_t1}
### T-2: Lý do từ chối
Truy vết: REQ-2
{extra_t2}
## 3. Thứ tự và phụ thuộc
| Task | Cần xong trước | Lý do |
|---|---|---|
| T-2 | T-1 | dùng chung bộ đếm |
{LOG.format(n=4)}"""


# ---- the core ---------------------------------------------------------------------------------------------------------


def test_relation_lines_are_read_under_an_item_outside_items_and_in_a_section_of_that_name() -> None:
    doc = parse(tasks("AO-1", "Liên quan: AO-9", "Phụ thuộc: T-1, AO-7/C-2\nThay thế: AO-5") + "\nPhụ thuộc: AO-30\n", "breakdown", PACK)
    by_id = {i.id: i.relations for i in doc.items}
    assert by_id == {"T-1": {"relates": ["AO-9"]}, "T-2": {"depends_on": ["T-1", "AO-7/C-2"], "supersedes": ["AO-5"]}}
    assert doc.relations == {"depends_on": ["AO-30"]}, "a line outside every item links the document as a whole"
    assert "T-1" not in str(doc.relations), "the summary table of the order is not read: the lines under the tasks are"

    section = parse("# s\nPhiên bản: 1\n## Phụ thuộc\n- AO-14 — cần API hạn mức\n- AO-14/C-2 và nhóm thanh toán\n| AO-99 | bảng |\n", "spec", PACK)
    assert section.relations == {"depends_on": ["AO-14", "AO-14/C-2"]}


def test_a_link_to_an_item_that_does_not_exist_and_a_loop_are_errors() -> None:
    doc = parse(tasks("AO-1", "Phụ thuộc: T-2", "Phụ thuộc: T-1, T-9"), "breakdown", PACK)
    findings = run_rules(PACK, {"AO-1/breakdown": doc})
    bad = [f for f in findings if f.rule == LINK_VALID]
    assert len(bad) == 1 and bad[0].item == "T-2" and "T-9" in bad[0].message
    loops = [f for f in findings if f.rule == "R-DEP-ACYCLIC"]
    assert {f.item for f in loops} == {"T-1", "T-2"} and "T-1 → T-2 → T-1" in loops[0].message

    fine = parse(tasks("AO-1", "", "Phụ thuộc: T-1\nLiên quan: AO-44"), "breakdown", PACK)
    assert not [f for f in run_rules(PACK, {"AO-1/breakdown": fine}) if f.rule in (LINK_VALID, "R-DEP-ACYCLIC")], "a ticket as a whole is the office's to check"


def test_a_pack_names_its_relations_and_a_rule_cannot_use_one_it_does_not_have() -> None:
    data = PACK.model_dump(by_alias=True, exclude_none=True)
    data["rules"]["R-X"] = {"type": "acyclic", "relation": "blocks"}
    with pytest.raises(PackError, match="quan hệ 'blocks' không có"):
        build_pack(data)
    data["rules"].pop("R-X")
    data["syntax"]["relations"] = {"depends_on": ["Depends on"], "relates": ["Related"]}
    english = build_pack(data)
    doc = parse(tasks("AO-1", "", "Depends on: T-1"), "breakdown", english)
    assert doc.item("T-2").relations == {"depends_on": ["T-1"]}


# ---- in the office --------------------------------------------------------------------------------------------------


async def sibling(office: Office, designed, title: str = "Lý do từ chối", item: str = "T-2"):
    team, lan, minh, hung, repo, epic, child = designed
    other = await office.tickets.create(title, project_id=epic.project_id, parent_id=epic.id)
    await office.db.execute("UPDATE tickets SET stage_template = 'task-spec', stage = 'design', team_id = ? WHERE id = ?", (team.id, other.id))
    other = await office.tickets.get(other.id)
    await put(office, other, "spec", spec(epic.key, item))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ? AND key = 'spec'", (other.id,))
    return other


async def test_a_design_that_relies_on_an_item_of_another_task_is_called_back_when_that_item_changes(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    other = await sibling(office, designed)
    await put(office, other, "design", design(other.key, epic.key))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ? AND key = 'design'", (other.id,))

    mine = design(child.key, epic.key).replace("Thay đổi: " + epic.key + "/F-1\n", f"Thay đổi: {epic.key}/F-1\nPhụ thuộc: {other.key}/C-2\n", 1)
    await put(office, child, "design", mine)
    (link,) = await office.links.of(child)
    assert (link.relation, link.other.key, link.to_item, link.origin, link.outgoing) == ("depends_on", other.key, "C-2", "doc", True)
    assert (await office.links.of(other))[0].label(PACK.syntax.relations) == "Được phụ thuộc bởi"
    findings = await office.docs.check(await office.tickets.get(child.id))
    assert not [f for f in findings if f.rule in (LINK_VALID, "R-BASELINE")], [f.message for f in findings if f.status == "open"]

    # the other task's design is approved again with C-1 changed: nothing that this design relies on moved
    await put(office, other, "design", design(other.key, epic.key).replace("Từ 5 lên 9.", "Từ 5 lên 9, đọc từ cấu hình."))
    await office.db.execute("UPDATE documents SET approved_revision = 2 WHERE ticket_id = ? AND key = 'design'", (other.id,))
    assert not [f for f in await office.docs.check(await office.tickets.get(child.id)) if f.rule == "R-BASELINE"]

    # now C-2, the item it relies on
    await put(office, other, "design", design(other.key, epic.key).replace("Thêm trường lý do vào câu trả lời.", "Lý do nằm trong header."))
    await office.db.execute("UPDATE documents SET approved_revision = 3 WHERE ticket_id = ? AND key = 'design'", (other.id,))
    (stale,) = [f for f in await office.docs.check(await office.tickets.get(child.id)) if f.rule == "R-BASELINE"]
    assert stale.item == "C-1" and f"{other.key}/C-2" in stale.message

    # a reference to an item the other ticket does not have, and to a ticket that does not exist
    await put(office, child, "design", mine.replace(f"{other.key}/C-2", f"{other.key}/C-9\nLiên quan: ZZ-404"))
    bad = sorted(f.message for f in await office.docs.check(await office.tickets.get(child.id)) if f.rule == LINK_VALID)
    assert len(bad) == 2 and "C-9" in bad[0] + bad[1] and "không có ticket ZZ-404" in bad[0] + bad[1]


async def test_the_breakdown_links_its_tasks_and_those_links_join_their_tickets(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    other = await sibling(office, designed)
    for item, ticket in (("T-1", child), ("T-2", other)):
        await office.db.execute("INSERT INTO doc_spawned(parent_id, item_id, child_id, dropped, created_at) VALUES (?, ?, ?, 0, 'x')", (epic.id, item, ticket.id))
    await put(office, epic, "breakdown", tasks(epic.key, extra_t2="Phụ thuộc: T-1"))
    await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ? AND key = 'breakdown'", (epic.id,))
    await office.links.sync_family(epic)
    (link,) = await office.links.of(other)
    assert (link.relation, link.other.id, link.detail) == ("depends_on", child.id, f"{epic.key}/breakdown T-2")
    assert [t.id for t in await office.links.upstream(other)] == [child.id]

    # a design of what it depends on that is not approved yet: a warning, gone once it is approved
    await put(office, other, "design", design(other.key, epic.key))
    warns = [f for f in await office.docs.check(await office.tickets.get(other.id)) if f.rule == DEP_READY]
    assert len(warns) == 1 and warns[0].severity == "warn" and child.key in warns[0].message
    await office.db.execute("UPDATE documents SET approved_revision = revision WHERE ticket_id = ? AND key = 'design'", (child.id,))
    assert not [f for f in await office.docs.check(await office.tickets.get(other.id)) if f.rule == DEP_READY]

    # the Context block of the dependent task names the task and outlines its design
    stage = await office.stages.stage_def(await office.tickets.get(other.id))
    block = await office.doccontext.context_block(await office.tickets.get(other.id), stage)
    assert "## Linked tickets" in block and f"- Phụ thuộc: {child.key}" in block and f"## Outline of {child.key} `design`" in block and "C-1 Đổi hằng số" in block

    # the line is removed from the breakdown: the link goes with it
    await put(office, epic, "breakdown", tasks(epic.key))
    await office.db.execute("UPDATE documents SET approved_revision = 2 WHERE ticket_id = ? AND key = 'breakdown'", (epic.id,))
    await office.links.sync_family(epic)
    assert await office.links.of(other) == []


async def test_people_add_links_but_never_a_loop_of_dependencies(office: Office, designed) -> None:
    *_, epic, child = designed
    other = await sibling(office, designed)
    await office.links.add(other, child, "depends_on", by="Lan")
    with pytest.raises(Exception, match="thành một vòng"):
        await office.links.add(child, other, "depends_on", by="Lan")
    await office.links.add(child, other, "relates", by="Lan")
    assert {(v["label"], v["ticket"], v["origin"]) for v in await office.links.view(child)} == {("Được phụ thuộc bởi", other.key, "manual"), ("Liên quan", other.key, "manual")}
    await office.links.remove(other, child, "depends_on")
    assert [v["relation"] for v in await office.links.view(child)] == ["relates"]
    with pytest.raises(Exception, match="Quan hệ 'blocks' không có"):
        await office.links.add(child, other, "blocks", by="Lan")
