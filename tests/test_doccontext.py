"""What the office works out around a ticket's documents so the AI starts informed and cannot make things up: the
Context block of a stage prompt, the documents a stage must have opened, grounding of what an item says in what it
cites, the reach of a change in the code, what a redo starts from, and what an approval leaves in the knowledge base."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from actoroffice.app import Office
from actoroffice.domain.ids import new_id
from actoroffice.domain.org import Actor
from actoroffice.domain.run import Run, RunKind, WakeRequest, WakeSource
from actoroffice.domain.stage import StageRun, default_templates
from actoroffice.engine import prompt_pack
from actoroffice.services import doccontext
from actoroffice.services.doccontext import DocContext, glossary, is_name, numbers_of
from actoroffice.services.docflow import pack_templates

from .test_docflow import analysis, breakdown, design, designed, office, put, runtime, setup, spec  # noqa: F401  (fixtures)

LIMIT_CHECK = "src/shop/limits.py:9-10"  # LimitService.check, called by the refund endpoint


async def did(office: Office, ticket, tool: str, args: dict[str, Any]) -> None:
    """One finished tool call of a run of the ticket's current stage attempt, as the runtime reports it."""
    ai = await office.ensure_ai()
    srun = await office.stages.ensure_run(await office.tickets.get(ticket.id))
    run = await office.runs.start(Run(id=new_id("run"), actor_id=ai.id, kind=RunKind.TICKET, ticket_id=ticket.id))
    await office.db.execute("UPDATE runs SET stage_run_id = ? WHERE id = ?", (srun.id, run.id))
    await office.runs.event(run.id, "tool_use", {"tool": f"mcp__office__{tool}", "input": args, "id": "t1"})
    await office.runs.event(run.id, "tool_result", {"id": "t1", "text": "ok", "error": False})


async def check(office: Office, ticket, check_id: str):
    ticket = await office.tickets.get(ticket.id)
    stage = await office.stages.stage_def(ticket)
    srun = await office.stages.ensure_run(ticket)
    (result,) = await office.stages.checks.run(ticket, stage, srun, workspace=None, run_id=None, only=[check_id])
    return result


# ---- the documents a stage is written from must have been opened --------------------------------------------------------


async def test_a_design_must_open_what_it_is_written_from_in_this_attempt(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await put(office, epic, "breakdown", breakdown(epic.key))
    result = await check(office, child, "sources_read")
    assert result.status == "failed", result.summary
    assert f'{epic.key}/analysis [doc_get(ticket="{epic.key}", key="analysis")]' in result.summary and f"{epic.key}/breakdown" in result.summary
    assert f"{child.key}/spec" not in result.summary, "the office put the whole spec in the prompt: it is read"

    await did(office, child, "doc_get", {"ticket": epic.key, "key": "analysis"})
    assert f"{epic.key}/analysis" not in (await check(office, child, "sources_read")).summary
    await did(office, child, "doc_get", {"ticket": epic.key, "key": "breakdown"})
    assert (await check(office, child, "sources_read")).status == "passed"

    await office.stages._open_run(child.id, "design")  # the gate sent it back: a fresh session knows nothing of what was read
    assert (await check(office, child, "sources_read")).status == "failed"


async def test_a_spec_too_long_for_the_prompt_must_be_opened(office: Office, designed, monkeypatch) -> None:
    *_, epic, child = designed
    monkeypatch.setattr(doccontext, "SPEC_CAP", 200)
    stage = await office.stages.stage_def(child)
    block = await office.doccontext.context_block(child, stage)
    assert "the first 200 characters only" in block and '`spec`: only its first 200 of' in block and 'doc_get(key="spec")' in block
    result = await check(office, child, "sources_read")
    assert f'{child.key}/spec [doc_get(key="spec")]' in result.summary
    await did(office, child, "doc_get", {"key": "spec"})
    assert f"{child.key}/spec" not in (await check(office, child, "sources_read")).summary


# ---- the Context block --------------------------------------------------------------------------------------------------


async def test_the_context_block_says_where_the_ticket_sits_and_what_is_settled(office: Office, designed, capsys) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    await put(office, epic, "breakdown", breakdown(epic.key).replace("## 3. Thứ tự và phụ thuộc\nKhông có.", "## 3. Thứ tự và phụ thuộc\nSau T-1 mới làm được T-2."))
    await office.db.execute("UPDATE tickets SET description = ? WHERE id = ?", (f"Nâng hạn mức.\n\n_Task của epic {epic.key} (mục T-1 của {epic.key})._", child.id))
    sibling = await office.tickets.create("Lý do từ chối", project_id=epic.project_id, parent_id=epic.id)
    await office.db.execute("UPDATE tickets SET stage_template = 'task-spec', stage = 'design', team_id = ? WHERE id = ?", (team.id, sibling.id))
    sibling = await office.tickets.get(sibling.id)
    await office.teams.set_participant(sibling.id, minh.id, ["dev"])
    await put(office, sibling, "spec", spec(epic.key, "T-2"))
    await put(office, sibling, "design", design(sibling.key, epic.key))
    await office.docs.check(sibling)  # pins what its design cites
    await office.docs.check(await office.tickets.get(child.id))
    await office.knowledge.note(kind="code_note", title="Hạn mức đếm theo ngày UTC", content="Bộ đếm reset lúc 00:00 UTC.", by="x", team_id=team.id, anchor={"path": "src/shop/limits.py"})
    verified = await office.knowledge.note(kind="fact", title="Khách VIP", content="VIP dùng chung hạn mức.", by="x", team_id=team.id)
    verified = await office.knowledge.set_status(verified.id, "verified", by=lan.id)

    child = await office.tickets.get(child.id)
    block = await office.doccontext.context_block(child, await office.stages.stage_def(child), kb=[verified])
    with capsys.disabled():
        print("\n----- Context block -----\n" + block + "\n-------------------------")
    assert block.startswith("# Context (worked out by the office")
    assert "## This ticket's `spec` rev 1 (approved), in full: you do not need to doc_get it again" in block and "AC-1.2 (từ chối)" in block
    assert f"- Parent: {epic.key} — Nâng hạn mức hoàn tiền" in block and "- This ticket is item T-1 of that breakdown." in block
    assert "breakdown says: Sau T-1 mới làm được T-2." in block
    assert f"{sibling.key} · Lý do từ chối" in block and "assigned to Minh" in block and "`design` rev 1 (not approved)" in block
    assert f'## Outline of {epic.key} `analysis` rev 1 (approved): headings and items only; the text is in doc_get(ticket="{epic.key}", key="analysis")' in block
    assert "- Hiện trạng: F-1 Hạn mức hiện là 5 lần mỗi ngày" in block and "REQ-1 Hạn mức hoàn tiền mỗi ngày là 9" in block
    assert "## Điều đã chốt" in block and f"- {epic.key}/Q-1: Khách VIP có hạn mức riêng không? → Không, dùng chung." in block
    assert f"- {sibling.key} `design` (C-1) also cites shop:src/shop/limits.py" in block
    knowledge = block[block.index("## Team knowledge") :]
    assert "A knowledge line is a pointer, not a source" in knowledge
    assert knowledge.index("[chưa xác nhận] code_note") < knowledge.index("[✓ đã xác nhận] fact"), "anchored to a cited file first, then by text"
    assert "(về file src/shop/limits.py)" in knowledge
    assert "## Not in this block: fetch it yourself" in block and len(block) < 6000 + doccontext.CONTEXT_CAP

    assert await office.doccontext.context_block(await office.tickets.create("Việc thường"), await office.stages.stage_def(child)) == ""


async def test_a_list_that_does_not_fit_says_how_many_are_left_and_how_to_get_them(office: Office, designed, monkeypatch) -> None:
    team, *_, epic, child = designed
    monkeypatch.setattr(doccontext, "SIBLINGS", 1)
    monkeypatch.setattr(doccontext, "OUTLINE_ITEMS", 2)
    for n in range(3):
        await office.tickets.create(f"Task {n}", project_id=epic.project_id, parent_id=epic.id)
    block = await office.doccontext.context_block(child, await office.stages.stage_def(child))
    assert f"… (còn 2 task: ticket_get {epic.key} liệt kê đủ)" in block
    assert f'… (còn 4 mục: doc_get(ticket="{epic.key}", key="analysis"))' in block
    assert doccontext.capped(["- a", "- b", "- c"], 2, "doc_get x") == ["- a", "- b", "- … (còn 1 mục: doc_get x)"]


# ---- grounded: what an item says is in what it cites (no model) ---------------------------------------------------------


def test_what_counts_as_a_name_and_a_number() -> None:
    names = ("refund_allowed", "LimitService.check", "MAX_PER_DAY", "createRefund", "src/shop/limits.py", "check()", "POST /v1/refunds/{refund_id}")
    assert all(is_name(n) for n in names)
    assert not any(is_name(n) for n in ("true", "refund", "Refund", "C-1", "AO-3/REQ-2", "9", "uv run pytest", "JSON"))
    assert [w for _, w in numbers_of("Tối đa 5 lần mỗi ngày, trong 30 ngày, khi count >= 3; xem C-2 và bản 1.")] == ["5 lần", "30 ngày", ">= 3"]
    assert glossary("## 2. Thuật ngữ\n| Thuật ngữ | Nghĩa |\n|---|---|\n| Hạn mức | Số lần hoàn tối đa mỗi ngày |\n| … | … |\n## 3. Yêu cầu\n| A | B |") == [
        ("Hạn mức", "Số lần hoàn tối đa mỗi ngày")
    ]


async def test_a_name_a_number_or_a_quote_that_is_not_in_the_source_is_found(office: Office, designed, monkeypatch, capsys) -> None:
    *_, epic, child = designed
    made_up = "Sửa `LimitService.check` gọi thêm `refund_limit_v2`, tối đa 7 lần mỗi ngày."
    await put(office, child, "design", design(child.key, epic.key, source=LIMIT_CHECK).replace("Từ 5 lên 9.", made_up))
    result = await check(office, child, "grounded")
    with capsys.disabled():
        print("\n----- grounded (failed) -----\n" + result.summary + "\n-----------------------------")
    assert result.status == "failed", result.summary
    assert "G-IDENT" in result.summary and "`refund_limit_v2` không có trong nguồn đã trích lẫn trong chỉ mục code: bỏ, sửa cho đúng tên, hoặc trích nguồn có nó" in result.summary
    assert "`LimitService.check`" not in result.summary, "it is in the cited lines"
    assert 'G-NUMBER' in result.summary and '"7 lần" không thấy trong đoạn nguồn đã trích của C-1' in result.summary

    new = made_up.replace("`refund_limit_v2`", "`refund_limit_v2` [mới]").replace("7 lần", "9 lần")  # 9 is what BR-1 of the spec says
    await put(office, child, "design", design(child.key, epic.key, source=LIMIT_CHECK).replace("Từ 5 lên 9.", new))
    result = await check(office, child, "grounded")
    assert result.status == "passed" and "tên, con số và dòng trích đều có trong nguồn" in result.summary, result.summary

    real, fake = "return self._count(customer_id)  <  MAX_PER_DAY", "return self._count(customer_id) <= MAX_PER_DAY"
    monkeypatch.setattr(DocContext, "_quotes", staticmethod(lambda item: [f'"{real}"'] if item.id == "C-1" else []))
    assert (await check(office, child, "grounded")).status == "passed", "a quote matches whatever its spacing"
    monkeypatch.setattr(DocContext, "_quotes", staticmethod(lambda item: [fake] if item.id == "C-1" else []))
    result = await check(office, child, "grounded")
    assert result.status == "failed" and "G-QUOTE" in result.summary and "trích dẫn không có trong nguồn" in result.summary and fake in result.summary


async def test_grounded_says_what_it_could_not_look_up(office: Office, designed) -> None:
    *_, epic, child = designed
    await put(office, child, "design", design(child.key, epic.key).replace("Nguồn: src/shop/limits.py:3\n", "").replace("### C-1:", "### C-1 [mới]:"))
    result = await check(office, child, "grounded")
    assert result.status == "skipped" and "không mục nào của tài liệu có nguồn ghim" in result.summary
    stage = await office.stages.stage_def(child)
    derived = office.stages.checks._derived(child, stage, ["outputs", "doc_rules", "crosscheck_clean"])
    assert derived == ["outputs", "doc_rules", "grounded", "impact_covered", "crosscheck_clean"], "no pack has to list them"
    analyze = pack_templates()["epic"].stage("analyze")
    assert office.stages.checks._derived(epic, analyze, ["doc_rules"]) == ["doc_rules", "grounded"], "an analysis changes no code"
    assert office.stages.checks._derived(child, stage, ["sources_read"]) == ["sources_read"]


# ---- impact_covered: a design deals with what depends on the code it changes -------------------------------------------


async def test_a_design_names_what_depends_on_the_code_it_changes(office: Office, designed) -> None:
    *_, epic, child = designed
    assert (await check(office, child, "impact_covered")).status == "failed", "the design cites code: the graph must have been asked"
    await did(office, child, "code_impact", {"targets": ["src/shop/limits.py:3"]})
    result = await check(office, child, "impact_covered")
    assert result.status == "skipped" and "chỉ mục code không có ký hiệu nào ở chỗ được trích" in result.summary, "a constant has no callers in the graph: said, not passed"

    await put(office, child, "design", design(child.key, epic.key, source=LIMIT_CHECK))
    await office.stages._open_run(child.id, "design")
    result = await check(office, child, "impact_covered")
    assert result.status == "failed" and "chưa lần nào hỏi đồ thị code" in result.summary and "code_impact" in result.summary
    await did(office, child, "code_refs", {"symbol": "LimitService.check"})
    result = await check(office, child, "impact_covered")
    assert result.status == "failed", result.summary
    wanted = "nơi gọi shop.refund.create_refund (shop:src/shop/refund.py:15) dùng tới chỗ design sửa nhưng design không nhắc tới: thêm vào mục Ảnh hưởng (xử lý bởi C-1"
    assert wanted in result.summary and "- endpoint " in result.summary

    names = [line.split(" ", 1)[1].rsplit(" (", 1)[0] for line in result.data["missing"]]
    table = "\n".join(f"| {name} | không ảnh hưởng vì chữ ký không đổi |" for name in names)
    await put(office, child, "design", design(child.key, epic.key, source=LIMIT_CHECK).replace("Không nơi nào khác dùng hằng số này.", table))
    result = await check(office, child, "impact_covered")
    assert result.status == "passed" and f"cả {len(names)} nơi phụ thuộc" in result.summary, result.summary

    await put(office, child, "design", design(child.key, epic.key).replace("Nguồn: src/shop/limits.py:3\n", "").replace("### C-1:", "### C-1 [mới]:"))
    assert (await check(office, child, "impact_covered")).status == "skipped"


# ---- after a rejection --------------------------------------------------------------------------------------------------


def build(stage, ticket, *, template, context: str = "", redo: str = "", reasons: list[dict] | None = None) -> prompt_pack.StagePrompt:
    return prompt_pack.build(
        actor=Actor(id="copilot", position_id="ai-engineer", name="Copilot"), ticket=ticket, template=template, stage=stage,
        stage_run=StageRun(id=1, ticket_id=ticket.id, stage=stage.id, attempt=2, started_at=""), language="Vietnamese", team_name=None, people=[],
        project_line="", dossier="", guidelines=[], memories=[], skills=[],
        wake=WakeRequest(id=1, actor_id="copilot", source=WakeSource.MANUAL, payload={"reasons": reasons or []}), resumed=False, answered=[],
        new_comments=[], changes=[], context=context, redo=redo,
    )  # fmt: skip


async def test_a_redo_starts_from_the_documents_the_comments_and_the_open_findings(office: Office, designed, capsys) -> None:
    *_, epic, child = designed
    await put(office, child, "design", design(child.key, epic.key, cover_all=False))  # AC-1.2 is not covered
    first = await office.stages.ensure_run(child)
    left = [{"id": "ab12cd34ef567890", "rule": "X-DESIGN-SPEC", "severity": "error", "doc": f"{child.key}/design", "item": "C-1", "message": "C-1 không trả lý do từ chối"}]
    await office.db.execute(
        "INSERT INTO doc_crosschecks(ticket_id, stage_run_id, input_hash, findings_json, created_at) VALUES (?, ?, 'h', ?, '2026-11-14')",
        (child.id, first.id, __import__("json").dumps(left)),
    )
    again = await office.stages._open_run(child.id, "design")
    stage = await office.stages.stage_def(child)
    reasons = [{"redo": "thiếu trường hợp lỗi", "comments": [{"doc": "design", "item": "C-1", "text": "nói rõ rollback"}]}]
    block = await office.doccontext.redo_block(child, stage, again, reasons)
    assert block.startswith("Revise, do not start over:") and "`design` rev 2 (not approved)" in block
    assert "Keep every item id" in block and "raise `Phiên bản` by one and add a Change log row" in block and "point by point" in block
    assert "- design · C-1: nói rõ rollback" in block
    assert "[ab12cd34] X-DESIGN-SPEC" in block and "C-1 không trả lý do từ chối" in block and "R-COVER-AC" in block and "AC-1.2" in block

    prompt = build(stage, child, template=pack_templates()["task-spec"], redo=block, reasons=reasons)
    with capsys.disabled():
        print("\n----- turn after a rejection -----\n" + prompt.turn + "\n----------------------------------")
    at = prompt.turn.index("The gate sent this stage back. Reviewer's note: thiếu trường hợp lỗi")
    assert prompt.turn.index("Revise, do not start over:") > at
    assert "Revise, do not start over" not in build(stage, child, template=pack_templates()["task-spec"], redo=block).turn, "only after a rejection"
    plain = await office.tickets.create("Việc thường")
    assert await office.doccontext.redo_block(plain, stage, again, reasons) == ""


# ---- the generic prompt does not fight the pack -------------------------------------------------------------------------


async def test_a_pack_stage_reads_code_through_the_index_and_writes_from_templates(office: Office, designed) -> None:
    *_, child = designed
    assert prompt_pack.LOOKUP in prompt_pack.BASE and prompt_pack.PROPORTION in prompt_pack.BASE and prompt_pack.ENVIRONMENTS_FROM in prompt_pack.BASE
    flow = pack_templates()["task-spec"]
    prompt = build(flow.stage("design"), child, template=flow, context="# Context (worked out by the office)\n- x")
    base = prompt.parts["base"]
    assert "the code (Read/Grep/Glob)" not in base and "not a template" not in base and "## Shared environments" not in base
    assert "code_pack to start" in base and "comes only from\ndoc_template(<type>)" in base and "## Work in proportion" in base and "## Safety" in base
    assert "# Context (worked out by the office)" in prompt.parts["stage"] and prompt.parts["context"].endswith("- x")
    assert "Lines marked [doc] are the documents this stage is written from" in prompt.system and "code_read, code_outline or Read for a file" in prompt.system

    code = default_templates()["code"]
    generic = build(code.stage("understand"), child, template=code).parts["base"]
    assert "the code (Read/Grep/Glob)" in generic and "not a template" in generic and "## Shared environments" in generic
    task = pack_templates()["task"]
    assert "## Shared environments" in build(task.stage("build"), child, template=task).parts["base"], "a stage that builds needs them"


# ---- memory that outlives the ticket ------------------------------------------------------------------------------------


async def test_an_approval_leaves_decisions_terms_and_facts_in_the_knowledge_base(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    saved = await office.doccontext.on_approved(child, SimpleNamespace(payload={"docs": [{"key": "design", "revision": 1}]}))
    (decision,) = [await office.knowledge.get(i) for i in saved]
    assert (decision.kind, decision.status, decision.ticket_id, decision.team_id) == ("decision", "draft", child.id, team.id)
    assert decision.title == "Giữ hạn mức là hằng số trong code" and decision.content.startswith("Chọn: B — chưa ai cần đổi lúc chạy")
    assert decision.anchor == {"paths": ["src/shop/limits.py"]} and decision.sources == [f"{child.key}/design bản 1 D-1"]

    table = "| Thuật ngữ | Nghĩa trong epic này |\n|---|---|\n| Hạn mức | Số lần hoàn tiền tối đa mỗi ngày của một khách |"
    await put(office, epic, "analysis", analysis(epic.key).replace("Không có từ mới.", table))
    await office.db.execute("UPDATE documents SET approved_revision = revision WHERE ticket_id = ?", (epic.id,))  # what the gate does
    approved = SimpleNamespace(payload={"docs": [{"key": "analysis", "revision": 2}]})
    items = [await office.knowledge.get(i) for i in await office.doccontext.on_approved(epic, approved)]
    assert [(i.kind, i.title) for i in items] == [("fact", "Thuật ngữ: Hạn mức"), ("code_note", "Hạn mức hiện là 5 lần mỗi ngày")]
    note = items[1]
    assert note.anchor["path"] == "src/shop/limits.py" and len(note.anchor["commit"]) == 40 and note.anchor["lines"] == "3" and note.status == "draft"
    assert any(s.startswith("shop:src/shop/limits.py:3@") for s in note.sources)
    assert [i.id for i in items] == await office.doccontext.on_approved(epic, approved), "approved again: the same items, not doubles"
    assert note.id in await office.knowledge.mark_stale(team_id=team.id, changed_files=["src/shop/limits.py"]), "stale when the file changes"

    block = await office.doccontext.context_block(child, await office.stages.stage_def(child))
    assert "[có thể đã cũ] code_note" in block and "Hạn mức hiện là 5 lần mỗi ngày" in block, "the next task that touches the file is told, with how far to trust it"
    assert f"- {child.key}/D-1" not in block and "D-1 (decision): Giữ hạn mức là hằng số trong code" not in block, "a design not approved yet settles nothing"
    await office.db.execute("UPDATE documents SET approved_revision = revision WHERE ticket_id = ? AND key = 'design'", (child.id,))
    block = await office.doccontext.context_block(child, await office.stages.stage_def(child))
    assert "- D-1 (decision): Giữ hạn mức là hằng số trong code → B — chưa ai cần đổi lúc chạy" in block
