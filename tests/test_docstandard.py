"""The document standard: what the default pack asks of a document, and the holes the parser and the rules must not
have (a document the machine cannot read must fail, not pass with nothing checked)."""

from __future__ import annotations

import pytest

from actoroffice.doccore import DocReview, Evidence, PackError, build_pack, load_pack, parse, parse_sources, run_rules

from .test_doccore import ANALYSIS, DESIGN, PACK, SPEC, docs, of, raw_default

LOG = "\n## Change log\n| Phiên bản | Ngày | Thay đổi | Người |\n|---|---|---|---|\n| 1 | x | y | z |\n"


def codes(doc) -> set[tuple[str, str | None]]:
    return {(p.code, p.item) for p in doc.problems}


# --- A6: a document without readable items does not pass ---------------------------------------------------------


def test_items_in_a_table_or_a_checkbox_are_reported_and_an_empty_document_fails():
    body = "# D\nPhiên bản: 1\n## Thay đổi\n- [ ] C-3 việc dạng checkbox\n  Truy vết: AC-1.1\n\n| C-4 | trong bảng | Truy vết: AC-1.1 |\n" + LOG
    design = parse(body, "design", PACK)
    assert design.items == []
    assert {("unparsed_id", "C-3"), ("unparsed_id", "C-4")} <= codes(design)
    assert all("bảng/checkbox" in p.message and "viết thành mục riêng" in p.message for p in design.problems if p.code == "unparsed_id")
    found = run_rules(PACK, docs(design=body), only_docs=["T-12/design"])
    [empty] = of(found, "R-HAS-ITEMS")
    assert (empty.doc, empty.item, empty.severity) == ("T-12/design", None, "error")
    assert "C" in empty.message and "### C-1" in empty.message
    assert {f.item for f in of(found, "R-TEMPLATE")} >= {"C-3", "C-4"}


def test_a_table_that_only_sums_up_items_and_the_change_log_are_left_alone():
    assert of(run_rules(PACK, docs()), "R-HAS-ITEMS") == []
    body = DESIGN.replace("| AC / BR | C |", "| AC / BR | C |\n| AC-1.1 | C-1 |\n- [x] kiểm lại C-2").replace(
        "| Phiên bản | Ngày | Thay đổi | Người |", "| Phiên bản | Ngày | Thay đổi | Người |\n| 2 | x | bỏ C-9 | z |"
    )
    assert not [p for p in parse(body, "design", PACK).problems if p.code == "unparsed_id"]
    # An id of another ticket is not this document's item.
    spec = SPEC.replace("## Phụ thuộc", "## Phụ thuộc\n| EP-4/A-9 | giả định của epic |")
    assert not [p for p in parse(spec, "spec", PACK).problems if p.code == "unparsed_id"]


@pytest.mark.parametrize(
    ("doctype", "kind", "sample"),
    [("analysis", "REQ", ANALYSIS), ("spec", "AC", SPEC), ("design", "C", DESIGN)],
)
def test_each_document_type_needs_its_main_items(doctype, kind, sample):
    lines = [line for line in sample.splitlines() if not line.lstrip("#- ").startswith(f"{kind}-")]
    found = of(run_rules(PACK, {f"X-1/{doctype}": parse("\n".join(lines), doctype, PACK)}), "R-HAS-ITEMS")
    assert [f.doc for f in found] == [f"X-1/{doctype}"]
    breakdown = parse("# B\nPhiên bản: 1\n## Danh sách task\n- [ ] T-1 việc một\n", "breakdown", PACK)
    assert [f.doc for f in of(run_rules(PACK, {"X-1/breakdown": breakdown}), "R-HAS-ITEMS")] == ["X-1/breakdown"]


def test_min_items_takes_docs_and_kinds_and_needs_one_of_the_two_forms():
    data = raw_default()
    data["rules"]["R-HAS-ITEMS"] = {"type": "min_items", "docs": ["spec"], "kinds": ["AC", "BR"], "min": 4}
    [f] = of(run_rules(build_pack(data), docs()), "R-HAS-ITEMS")
    assert f.doc == "T-12/spec" and "có 3 mục AC/BR, cần ít nhất 4" in f.message
    data["rules"]["R-HAS-ITEMS"] = {"type": "min_items", "docs": ["spec"]}
    with pytest.raises(PackError, match="R-HAS-ITEMS"):
        build_pack(data)


# --- A7: an item heading ends "Ngoài phạm vi" --------------------------------------------------------------------


def test_an_item_heading_after_out_of_scope_is_in_scope_again():
    grown = ANALYSIS.replace("## 3. Hiện trạng", "### REQ-4: Yêu cầu thêm khi epic lớn lên\nPhải làm.\nNguồn: EP-4\n\n## 3. Hiện trạng")
    analysis = parse(grown, "analysis", PACK)
    assert analysis.item("REQ-3").section == "Ngoài phạm vi"  # a list line directly under the sub-heading
    assert analysis.item("REQ-4").section == "Yêu cầu"
    assert analysis.item("F-1").section == "Hiện trạng"
    [f] = of(run_rules(PACK, docs(analysis=grown)), "R-COVER-REQ")
    assert f.item == "EP-4/REQ-4"


# --- A8: a source nobody can open is not evidence ----------------------------------------------------------------


def test_a_source_that_cannot_be_opened_is_not_evidence():
    said = ANALYSIS.replace("Nguồn: src/pay/limit.py:40-88", "Nguồn: theo trao đổi với anh Nam")
    [f] = of(run_rules(PACK, docs(analysis=said)), "R-EVIDENCE")
    assert (f.doc, f.item) == ("EP-4/analysis", "F-1")
    assert "không phải đường dẫn, mã ticket hay URL" in f.message and "theo trao đổi với anh Nam" in f.message
    # One openable source next to it is enough.
    assert of(run_rules(PACK, docs(analysis=said.replace("anh Nam", "anh Nam; src/pay/limit.py:40-88"))), "R-EVIDENCE") == []


def test_the_new_tag_excuses_only_the_also_selectors():
    tagged = ANALYSIS.replace("### F-1: Hạn mức đang tính theo tháng\nNguồn: src/pay/limit.py:40-88", "### F-1 [mới]: Hạn mức đang tính theo tháng")
    found = of(run_rules(PACK, docs(analysis=tagged)), "R-EVIDENCE")
    assert [(f.doc, f.item) for f in found] == [("EP-4/analysis", "F-1")]
    assert "[mới]" not in found[0].message  # the tag is not offered as a way out for a fact
    assert of(run_rules(PACK, docs()), "R-EVIDENCE") == []  # C-2 [mới] has no source and is fine


def test_sources_split_on_comma_semicolon_and_va():
    got = parse_sources("a/b.py:1-2, c/d.py:3 và e/f.py:9; PAY-120")
    assert [(s.kind, s.ref, s.locator) for s in got] == [("file", "a/b.py", ":1-2"), ("file", "c/d.py", ":3"), ("file", "e/f.py", ":9"), ("ticket", "PAY-120", "")]


# --- A9b: a stale finding is about one change --------------------------------------------------------------------


def test_the_stale_finding_changes_when_the_code_changes_again():
    def stale(now: str):
        record = Evidence(doc="T-12/design", item="C-1", source="src/pay/limit.py:40-88", provider="local_git", category="code", content_hash="h1", current_hash=now)
        [f] = of(run_rules(PACK, docs(), evidence=[record]), "R-FRESH")
        return f

    assert stale("h2").id == stale("h2").id
    assert stale("h2").id != stale("h3").id
    assert "h3" in stale("h3").message


# --- A13: a bare id of a kind that lives on the parent -----------------------------------------------------------


def test_a_bare_id_of_the_parent_resolves_and_an_unknown_one_is_reported_with_the_way_to_write_it():
    unknown = DESIGN.replace("Truy vết: AC-1.2", "Truy vết: AC-1.2, REQ-77")
    [f] = of(run_rules(PACK, docs(design=unknown)), "R-TRACE-VALID")
    assert (f.doc, f.item) == ("T-12/design", "C-2")
    assert "REQ-77" in f.message and "EP-4/REQ-77" in f.message and "EP-4/analysis" in f.message
    # Written bare, a requirement that exists on the parent is that requirement: it counts for coverage.
    bare = SPEC.replace("EP-4/REQ-", "REQ-").replace("EP-4/F-1", "F-1")
    assert run_rules(PACK, docs(spec=bare)) == []
    # A kind the ticket's own documents hold is never looked for elsewhere.
    [f] = of(run_rules(PACK, docs(design=DESIGN.replace("Truy vết: AC-1.2", "Truy vết: AC-9.9"))), "R-TRACE-VALID")
    assert "AC-9.9" in f.message and "EP-4/" not in f.message
    # Nothing in the set could hold it: cannot be judged.
    assert run_rules(PACK, {"T-12/design": parse(unknown, "design", PACK)}, only_docs=[]) == []


# --- A16: nothing inside a code block is read --------------------------------------------------------------------


def test_keyword_lines_inside_a_code_block_do_not_count():
    hidden = ANALYSIS.replace("Trả lời: UTC+7", "```\nTrả lời: UTC+7\nNguồn: src/a.py:1\nTruy vết: REQ-1\n```")
    question = parse(hidden, "analysis", PACK).item("Q-1")
    assert (question.answer, question.sources, question.traces) == ("", [], [])
    assert "Trả lời: UTC+7" in question.body  # still part of the item's text
    assert [f.item for f in of(run_rules(PACK, docs(analysis=hidden)), "R-OPEN-Q")] == ["Q-1"]


# --- B1: instructions are written once ---------------------------------------------------------------------------


def test_stage_instructions_are_built_from_named_blocks():
    raw = raw_default()
    assert "instructions" not in raw["flows"]["task"][0]
    assert raw["flows"]["task"][0]["instructions_from"] == ["design", "common"]
    common, design = PACK.instructions["common"].strip(), PACK.instructions["design"].strip()
    for flow in ("task", "task-spec"):
        stage = PACK.flows[flow][0]
        assert stage.instructions == f"{design}\n{common}\n"
        assert stage.instructions_from == []
    for stage in PACK.flows["epic"][:2]:
        assert stage.instructions.rstrip().endswith(common)
        assert PACK.instructions["ask"].strip() in stage.instructions
    assert sum(text.count("Machine-read lines") for text in PACK.instructions.values()) == 1
    # Dumped and built again, the text is the same: the blocks are not added twice.
    again = build_pack(PACK.model_dump(by_alias=True))
    assert again.flows["task"][0].instructions == PACK.flows["task"][0].instructions


def test_a_unit_changes_one_block_and_an_unknown_block_is_refused(tmp_path):
    (tmp_path / "pack.yaml").write_text("name: acme\nextends: default\ninstructions:\n  common: Write in English.\n", encoding="utf-8")
    pack = load_pack(tmp_path)
    assert pack.flows["task"][0].instructions == PACK.instructions["design"].strip() + "\nWrite in English.\n"
    data = raw_default()
    data["flows"]["task"][0]["instructions_from"] = ["design", "nowhere"]
    with pytest.raises(PackError) as err:
        build_pack(data)
    assert any(p.startswith("flows.task[0].instructions_from:") and "nowhere" in p for p in err.value.problems)


def test_what_the_instructions_ask_for():
    design = PACK.flows["task"][0].instructions
    for needed in (
        "doc_get", "ticket_get", "kb_search", "code_pack", "code_impact", "ask_human", "Chọn bởi", "Phương án", '"assume" mode', "Ảnh hưởng",
        "never renumber", "[mới]", "State only what a tool result or a document shows", "Trích:", "Thuật ngữ", "doc_check",
    ):  # fmt: skip
        assert needed in design, needed
    assert design.index("Read first") < design.index("Brainstorm") < design.index("Write the document `design`")
    analyze, breakdown = (" ".join(s.instructions.split()) for s in PACK.flows["epic"][:2])
    assert "Ask before you assume" in analyze and "Ask before you assume" in breakdown
    assert "propose the split" in breakdown
    assert "Never write an identifier, path, endpoint, table or number" in analyze
    assert all(len(line) <= 200 for text in PACK.instructions.values() for line in text.splitlines())


# --- B4: open questions everywhere, decisions that show the choice -----------------------------------------------


def test_an_open_question_in_a_spec_or_a_design_blocks():
    spec = SPEC.replace("## Câu hỏi mở", "## Câu hỏi mở\n### Q-1: Áp cho khách doanh nghiệp không?")
    design = DESIGN.replace("## Câu hỏi mở", "## Câu hỏi mở\n- Q-1: Có cần cờ bật tắt?\n- Q-2: Giữ API cũ?\n  Trả lời: Có — Minh")
    found = of(run_rules(PACK, docs(spec=spec, design=design)), "R-OPEN-Q")
    assert {(f.doc, f.item, f.severity) for f in found} == {("T-12/spec", "Q-1", "error"), ("T-12/design", "Q-1", "error")}


def test_a_decision_shows_its_options_the_choice_and_who_chose():
    assert [f for f in run_rules(PACK, docs()) if f.rule.startswith("R-D-")] == []
    decision = parse(DESIGN, "design", PACK).item("D-1")
    assert decision.lines == {"option": ["giữ bảng cũ, thêm cột", "bảng mới"], "choice": ["A — ít rủi ro"], "chosen_by": ["Minh"]}
    one = DESIGN.replace("- Phương án B: bảng mới\n", "").replace("Chọn bởi: Minh\n", "")
    found = {f.rule: f for f in run_rules(PACK, docs(design=one)) if f.rule.startswith("R-D-")}
    assert set(found) == {"R-D-OPTIONS", "R-D-CHOSEN-BY"}  # "Chọn bởi:" is not "Chọn:" and the other way round
    assert found["R-D-OPTIONS"].item == "D-1" and "có 1 dòng" in found["R-D-OPTIONS"].message and "Phương án" in found["R-D-OPTIONS"].message
    bare = DESIGN.replace("- Phương án A: giữ bảng cũ, thêm cột\n- Phương án B: bảng mới\nChọn: A — ít rủi ro\nChọn bởi: Minh\n", "- Phương án: A hoặc B. Chọn A.\n")
    assert {f.rule for f in run_rules(PACK, docs(design=bare)) if f.rule.startswith("R-D-")} == {"R-D-OPTIONS", "R-D-CHOICE", "R-D-CHOSEN-BY"}


def test_has_line_is_generic_and_checked_when_the_pack_loads():
    data = raw_default()
    data["syntax"]["lines"]["owner"] = ["Người chịu trách nhiệm"]
    data["rules"]["R-BR-OWNER"] = {"type": "has_line", "kinds": ["BR"], "line": "owner", "severity": "warn"}
    [f] = of(run_rules(build_pack(data), docs()), "R-BR-OWNER")
    assert (f.doc, f.item, f.severity) == ("T-12/spec", "BR-1", "warn")
    data["rules"]["R-BR-OWNER"]["line"] = "nobody"
    with pytest.raises(PackError) as err:
        build_pack(data)
    assert any(p.startswith("rules.R-BR-OWNER.line:") for p in err.value.problems)


# --- B5, B6: quality reviews and the analysis cross-check --------------------------------------------------------


def test_quality_reviews_of_the_default_pack():
    assert {k: (r.doc, r.at, r.severity) for k, r in PACK.reviews.items()} == {
        "Q-ANALYSIS": ("analysis", ["analysis"], "error"),
        "Q-SPEC": ("spec", ["business"], "error"),
        "Q-DESIGN": ("design", ["design"], "error"),
    }
    assert all(isinstance(r, DocReview) and len(r.rubric) >= 4 and all(line.strip() for line in r.rubric) for r in PACK.reviews.values())
    assert DocReview(doc="spec", rubric=["One thing."]).severity == "error"


@pytest.mark.parametrize(
    ("change", "location"),
    [
        (lambda d: d["reviews"]["Q-SPEC"].update(doc="memo"), "reviews.Q-SPEC.doc"),
        (lambda d: d["reviews"]["Q-SPEC"].update(rubric=[]), "reviews.Q-SPEC.rubric"),
        (lambda d: d["reviews"]["Q-SPEC"].update(severity="fatal"), "reviews.Q-SPEC.severity"),
        (lambda d: d["reviews"]["Q-SPEC"].update(colour="blue"), "reviews.Q-SPEC.colour"),
    ],
)
def test_a_bad_review_is_rejected_with_its_location(change, location):
    data = raw_default()
    change(data)
    with pytest.raises(PackError) as err:
        build_pack(data)
    assert any(p.startswith(location + ":") for p in err.value.problems), err.value.problems


def test_the_analysis_is_cross_checked_against_the_code():
    check = PACK.crosschecks["X-ANALYSIS-CODE"]
    assert (check.left, check.right, check.at) == ("analysis", "evidence", ["analysis"])
    assert "crosscheck_clean" in PACK.flows["epic"][0].checks


# --- B7: the templates -------------------------------------------------------------------------------------------


def test_templates_hold_the_new_sections_and_follow_their_own_rules():
    assert PACK.version >= 2
    wanted = {
        "analysis": ["Thuật ngữ"],
        "spec": ["Thuật ngữ", "Ngoài phạm vi", "Câu hỏi mở"],
        "design": ["Ảnh hưởng", "Phi chức năng", "Triển khai", "Câu hỏi mở"],
    }
    for doctype, sections in wanted.items():
        doc = parse(PACK.template_text(doctype), doctype, PACK)
        assert set(sections) <= set(PACK.doc_types[doctype].sections)
        assert all(any(s.startswith(name) for s in doc.sections) for name in sections)
    assert {"Q"} <= set(PACK.doc_types["spec"].items) & set(PACK.doc_types["design"].items)
    design = parse(PACK.template_text("design"), "design", PACK)
    assert "| AC | Loại test" in PACK.template_text("design") and "| Nơi bị chạm | Loại" in PACK.template_text("design")
    rules = {f.rule for f in run_rules(PACK, {"design": design})}
    assert not rules & {"R-TEMPLATE", "R-HAS-ITEMS", "R-EVIDENCE", "R-OPEN-Q", "R-D-OPTIONS", "R-D-CHOICE", "R-D-CHOSEN-BY"}
    assert design.item("C-1").quotes and parse(PACK.template_text("analysis"), "analysis", PACK).item("F-1").quotes


# --- G1: a requirement says where it comes from ------------------------------------------------------------------


def test_a_requirement_needs_a_source():
    assert of(run_rules(PACK, docs()), "R-EVIDENCE") == []
    [f] = of(run_rules(PACK, docs(analysis=ANALYSIS.replace("Khách không chuyển quá hạn mức ngày.\nNguồn: EP-4", "Khách không chuyển quá hạn mức ngày."))), "R-EVIDENCE")
    assert (f.doc, f.item) == ("EP-4/analysis", "REQ-1")
    said = ANALYSIS.replace("### REQ-2: Báo cáo vượt hạn mức\nNguồn: Q-1", "### REQ-2: Báo cáo vượt hạn mức\nNguồn: chị Lan nói")
    assert [f.item for f in of(run_rules(PACK, docs(analysis=said)), "R-EVIDENCE")] == ["REQ-2"]


def test_a_requirement_the_ba_stated_cites_an_answered_question():
    analysis = parse(ANALYSIS, "analysis", PACK)
    assert [(s.kind, s.ref) for s in analysis.item("REQ-2").sources] == [("item", "Q-1")]
    assert [(s.kind, s.ref) for s in analysis.item("REQ-1").sources] == [("ticket", "EP-4")]  # a ticket key stays a ticket
    [f] = of(run_rules(PACK, docs(analysis=ANALYSIS.replace("Trả lời: UTC+7", ""))), "R-EVIDENCE")
    assert f.item == "REQ-2" and "Q-1" in f.message and "Trả lời" in f.message
    [f] = of(run_rules(PACK, docs(analysis=ANALYSIS.replace("Nguồn: Q-1", "Nguồn: Q-9"))), "R-EVIDENCE")
    assert f.item == "REQ-2" and "Q-9" in f.message
    # With the ticket in front it is an item of that ticket; one that is not in the set cannot be judged.
    [source] = parse_sources("EP-4/Q-1", PACK)
    assert (source.kind, source.ref) == ("item", "EP-4/Q-1")
    assert of(run_rules(PACK, docs(spec=SPEC.replace("Nguồn: src/pay/limit.py:50-60", "Nguồn: EP-4/Q-1"))), "R-EVIDENCE") == []
    assert of(run_rules(PACK, docs(spec=SPEC.replace("Nguồn: src/pay/limit.py:50-60", "Nguồn: ZZ-1/Q-9"))), "R-EVIDENCE") == []
    assert [s.kind for s in parse_sources("Q-1")] == ["ticket"]  # without a pack nothing is known about kinds


# --- G2: quotes are carried ------------------------------------------------------------------------------------


def test_quotes_are_stored_on_the_item():
    body = ANALYSIS.replace("Nguồn: src/pay/limit.py:40-88", 'Nguồn: src/pay/limit.py:40-88\nTrích: `LIMIT_PERIOD = "month"`\nTrích: "hạn mức theo tháng", “mỗi khách”\nTrích: không có dấu nháy')
    fact = parse(body, "analysis", PACK).item("F-1")
    assert fact.quotes == ['LIMIT_PERIOD = "month"', "hạn mức theo tháng", "mỗi khách", "không có dấu nháy"]
    assert [s.raw for s in fact.sources] == ["src/pay/limit.py:40-88"]
    assert parse(ANALYSIS, "analysis", PACK).item("F-1").quotes == []
    data = raw_default()
    data["syntax"]["quote"] = ["Quote"]
    assert parse("### F-1: x\nQuote: `a = 1`\n", "analysis", build_pack(data)).item("F-1").quotes == ["a = 1"]
    data["syntax"]["quote"] = []
    with pytest.raises(PackError, match="syntax.quote"):
        build_pack(data)
