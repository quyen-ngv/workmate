"""Doc Core (docs/specs/2026-11-13-doc-core.md, steps 1-3): pack, parser, trace graph and the rule primitives."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest
import yaml

from actoroffice.doccore import (
    Approved,
    Baseline,
    Evidence,
    MatchState,
    PackError,
    TraceGraph,
    build_pack,
    impact,
    load_default,
    load_pack,
    match_state,
    parse,
    parse_sources,
    run_rules,
)
from actoroffice.doccore.pack import DEFAULT_DIR

PACK = load_default()

ANALYSIS = """\
# Phân tích — EP-4 Hạn mức
Phiên bản: 2 · Cập nhật: 2026-11-13

## 1. Phạm vi
- Trong phạm vi: hạn mức chuyển tiền.

## 2. Yêu cầu
### REQ-1: Hạn mức theo ngày
Khách không chuyển quá hạn mức ngày.

### REQ-2: Báo cáo vượt hạn mức

### Ngoài phạm vi
- REQ-3: Xuất PDF — lý do: để đợt sau

## 3. Hiện trạng
### F-1: Hạn mức đang tính theo tháng
Nguồn: src/pay/limit.py:40-88

## 4. Mâu thuẫn và thiếu
## 5. Câu hỏi mở
### Q-1: Tính theo múi giờ nào?
Trả lời: UTC+7

## 6. Giả định
- A-1: Chỉ áp dụng cho khách cá nhân

## 7. Change log
| Phiên bản | Ngày | Thay đổi | Người |
"""

SPEC = """\
# Nghiệp vụ — T-12
Phiên bản: 1

## Mục tiêu
## User stories
### US-1: Chặn giao dịch vượt hạn mức
Là khách, tôi muốn biết khi vượt hạn mức.

- AC-1.1 (happy path) — Given còn hạn mức When chuyển Then thành công
  Truy vết: EP-4/REQ-1
- AC-1.2 [review] — Given vượt hạn mức When chuyển Then báo lỗi
  Truy vết: EP-4/REQ-1, EP-4/REQ-2

## Quy tắc nghiệp vụ
### BR-1: Hạn mức tính theo ngày
Truy vết: EP-4/REQ-1
Thay đổi: EP-4/F-1
Nguồn: src/pay/limit.py:50-60

## Thay đổi so với hiện trạng
## Phụ thuộc
## Giả định
## Change log
| Phiên bản | Ngày | Thay đổi | Người |
"""

DESIGN = """\
# Design — T-12
Phiên bản: 1

## 1. Bối cảnh
## 2. Thay đổi
### C-1: Đổi cách tính hạn mức
Truy vết: AC-1.1, BR-1
Thay đổi: EP-4/F-1
Nguồn: src/pay/limit.py:40-88

### C-2 [mới]: Màn hình báo lỗi
Truy vết: AC-1.2

### C-3 [kỹ thuật]: tách module vì file quá dài
Nguồn: repo:src/pay/old.py:1-30

## 3. Mô hình dữ liệu
## 4. Giao diện
## 5. Quyết định kỹ thuật
### D-1: Giữ bảng cũ
## 6. Kiểm thử
## 7. Rủi ro
## 8. Truy vết
| AC / BR | C |
## 9. Các bước làm
1. Sửa limit.py (C-1)
## 10. Change log
| Phiên bản | Ngày | Thay đổi | Người |
"""


def docs(analysis: str = ANALYSIS, spec: str = SPEC, design: str = DESIGN) -> dict:
    return {"EP-4/analysis": parse(analysis, "analysis", PACK), "T-12/spec": parse(spec, "spec", PACK), "T-12/design": parse(design, "design", PACK)}


def of(findings: list, rule: str) -> list:
    return [f for f in findings if f.rule == rule]


def raw_default() -> dict:
    return yaml.safe_load((DEFAULT_DIR / "pack.yaml").read_text(encoding="utf-8"))


# --- pack ------------------------------------------------------------------------------------------------------


def test_default_pack_loads():
    assert PACK.name == "default"
    assert {"analysis", "spec", "design", "breakdown"} <= set(PACK.doc_types)
    assert PACK.doc_types["design"].based_on == ["spec", "analysis"]
    assert {r.type for r in PACK.rules.values()} == {"template", "cover", "traces_to", "evidence", "fresh", "baseline", "answered", "declared_change"}
    assert PACK.rules["R-COVER-AC"].each == ["spec.AC", "spec.BR"]
    assert PACK.rules["R-COVER-AC"].by == ["design.C"]
    assert PACK.rules["R-TEMPLATE"].docs == ["*"]
    breakdown = PACK.flows["epic"][1]
    assert breakdown.spawn.flows == ["task", "task-spec"]
    assert breakdown.spawn.copy_docs == ["spec"]
    assert PACK.gates["design"].submit_by == ["dev"]
    assert PACK.gates["design"].fallback is None
    assert PACK.kind_of("EP-4/AC-1.2") == "AC"
    assert PACK.kind_of("PAY-120") is None


@pytest.mark.parametrize(
    ("change", "location"),
    [
        (lambda d: d["doc_types"]["spec"]["items"].append("ZZ"), "doc_types.spec.items"),
        (lambda d: d["gates"]["design"]["roles"].append("ceo"), "gates.design.roles"),
        (lambda d: d["doc_types"]["design"]["based_on"].append("contract"), "doc_types.design.based_on"),
        (lambda d: d["rules"].update({"R-X": {"type": "magic"}}), "rules.R-X.type"),
        (lambda d: d["rules"]["R-COVER-AC"].update(each="spec/AC"), "rules.R-COVER-AC.each"),
        (lambda d: d["rules"]["R-COVER-AC"].update(by="design.REQ"), "rules.R-COVER-AC.by"),
        (lambda d: d["rules"]["R-NO-ORPHAN"].update(unless_tag="draft"), "rules.R-NO-ORPHAN.unless_tag"),
        (lambda d: d["rules"]["R-OPEN-Q"].pop("kinds"), "rules.R-OPEN-Q.kinds"),
        (lambda d: d["rules"]["R-FRESH"].update(severity="fatal"), "rules.R-FRESH.severity"),
        (lambda d: d["on_change"]["evidence_stale"].update(after_approval="panic"), "on_change.evidence_stale.after_approval"),
        (lambda d: d["flows"]["epic"][1]["spawn"]["flows"].append("subtask"), "flows.epic[1].spawn.flows"),
        (lambda d: d["item_kinds"]["AC"].update(pattern="AC-(\\d+"), "item_kinds.AC.pattern"),
        (lambda d: d.update(colour="blue"), "colour"),
    ],
)
def test_bad_pack_is_rejected_with_its_location(change, location):
    data = raw_default()
    change(data)
    with pytest.raises(PackError) as err:
        build_pack(data)
    assert any(p.startswith(location + ":") for p in err.value.problems), err.value.problems
    assert location in str(err.value)


def test_pack_error_lists_every_problem():
    data = raw_default()
    data["doc_types"]["spec"]["items"].append("ZZ")
    data["gates"]["design"]["roles"].append("ceo")
    data["doc_types"]["design"]["based_on"].append("contract")
    with pytest.raises(PackError) as err:
        build_pack(data)
    assert len(err.value.problems) == 3


def test_extends_overrides_one_rule_and_keeps_the_rest(tmp_path):
    folder = tmp_path / "acme"
    folder.mkdir()
    (folder / "pack.yaml").write_text("name: acme\nextends: default\nroles: [ba, dev, lead, qa]\nrules:\n  R-NO-ORPHAN: {severity: error}\n", encoding="utf-8")
    pack = load_pack(folder)
    assert pack.name == "acme"
    assert pack.rules["R-NO-ORPHAN"].severity == "error"
    assert pack.rules["R-NO-ORPHAN"].type == "traces_to"  # the rest of the rule comes from the parent
    assert pack.rules["R-COVER-AC"].severity == PACK.rules["R-COVER-AC"].severity
    assert pack.roles == ["ba", "dev", "lead", "qa"]  # a list is replaced, not merged
    assert "Phiên bản" in pack.template_text("design")  # templates it did not replace are the parent's


def test_extends_missing_parent_and_missing_file(tmp_path):
    (tmp_path / "pack.yaml").write_text("name: x\nextends: nowhere\n", encoding="utf-8")
    with pytest.raises(PackError, match="pack.yaml"):
        load_pack(tmp_path)
    with pytest.raises(PackError):
        load_pack(tmp_path / "missing")


# --- parse -----------------------------------------------------------------------------------------------------


def test_parse_items_sections_and_links():
    spec = parse(SPEC, "spec", PACK)
    assert [i.id for i in spec.items] == ["US-1", "AC-1.1", "AC-1.2", "BR-1"]
    assert spec.version_line
    assert spec.problems == []
    assert "User stories" in spec.sections and "Change log" in spec.sections
    ac = spec.item("AC-1.2")
    assert (ac.kind, ac.section, ac.tags) == ("AC", "User stories", ["review"])
    assert ac.title == "Given vượt hạn mức When chuyển Then báo lỗi"
    assert ac.traces == ["EP-4/REQ-1", "EP-4/REQ-2"]
    br = spec.item("BR-1")
    assert br.title == "Hạn mức tính theo ngày"
    assert br.changes == ["EP-4/F-1"]
    assert [(s.ref, s.locator) for s in br.sources] == [("src/pay/limit.py", ":50-60")]
    # A user story does not swallow its acceptance criteria.
    assert "AC-1.1" not in spec.item("US-1").body


def test_parse_tags_answers_and_sections():
    analysis = parse(ANALYSIS, "analysis", PACK)
    assert analysis.problems == []
    assert analysis.item("REQ-3").section == "Ngoài phạm vi"
    assert analysis.item("REQ-1").section == "Yêu cầu"
    assert analysis.item("Q-1").answer == "UTC+7"
    design = parse(DESIGN, "design", PACK)
    assert design.item("C-2").tags == ["new"]
    assert design.item("C-3").tags == ["technical"]
    assert design.item("C-3").title == "tách module vì file quá dài"
    assert design.item("C-3").sources[0].repo == "repo"
    assert design.item("C-1").traces == ["AC-1.1", "BR-1"]


@pytest.mark.parametrize(
    ("text", "kind", "ref", "locator", "repo"),
    [
        ("src/a.py:40-88", "file", "src/a.py", ":40-88", ""),
        ("repo:src/a.py:40-88", "file", "src/a.py", ":40-88", "repo"),
        ("docs/x.md §2.1", "file", "docs/x.md", "§2.1", ""),
        ("src/a.py#L10-L20", "file", "src/a.py", "#L10-L20", ""),
        ("`src/a.py`", "file", "src/a.py", "", ""),
        ("PAY-120", "ticket", "PAY-120", "", ""),
        ("https://wiki.example.com/x/123", "url", "https://wiki.example.com/x/123", "", ""),
        ("https://wiki.example.com/x/123 §2.1", "url", "https://wiki.example.com/x/123", "§2.1", ""),
        ("hỏi anh Nam", "other", "hỏi anh Nam", "", ""),
    ],
)
def test_parse_source_forms(text, kind, ref, locator, repo):
    [source] = parse_sources(text)
    assert (source.kind, source.ref, source.locator, source.repo) == (kind, ref, locator, repo)
    assert source.raw == text.strip("`")


def test_parse_several_sources_on_one_line():
    doc = parse("### F-1: x\nNguồn: src/a.py:1-2, PAY-120; https://h/p?a=1,2\n", "analysis", PACK)
    assert [s.kind for s in doc.item("F-1").sources] == ["file", "ticket", "url"]
    assert doc.item("F-1").sources[0].lines() == (1, 2)


def test_parse_ignores_ids_in_code_fences_and_other_kinds():
    doc = parse("## Thay đổi\n```\n### C-9: không phải mục\n```\n- AC-1.1 → C-1\n### C-1: thật\n", "design", PACK)
    assert [i.id for i in doc.items] == ["C-1"]


def test_parse_reports_structure_problems():
    doc = parse("# Design\n## Bối cảnh\n### C-1: a\n### C-1: b\n", "design", PACK)
    codes = {(p.code, p.item or p.detail) for p in doc.problems}
    assert ("duplicate_id", "C-1") in codes
    assert ("no_version", "") in codes
    assert ("missing_section", "Change log") in codes
    assert ("missing_section", "Bối cảnh") not in codes
    spec = parse("## Change log\nchưa có bảng\n- AC-3 — Given a When b Then c\n- BR-x: sai\n", "spec", PACK)
    assert {(p.code, p.item) for p in spec.problems if p.item} == {("bad_id", "AC-3"), ("bad_id", "BR-x")}
    assert any(p.code == "no_changelog" for p in spec.problems)
    assert all(p.message for p in spec.problems)


def test_item_hash_ignores_whitespace_only():
    a = parse("### C-1: Đổi cách tính\nMô tả  dài\n", "design", PACK).item("C-1")
    b = parse("### C-1:  Đổi cách tính\n\nMô tả dài\n", "design", PACK).item("C-1")
    c = parse("### C-1: Đổi cách tính\nMô tả khác\n", "design", PACK).item("C-1")
    assert a.hash == b.hash != c.hash


# --- rules -----------------------------------------------------------------------------------------------------


def test_clean_set_has_no_findings_and_ids_are_stable():
    assert run_rules(PACK, docs()) == []
    broken = docs(design=DESIGN.replace("Truy vết: AC-1.2", ""))
    first, second = run_rules(PACK, broken), run_rules(PACK, broken)
    assert first and [f.id for f in first] == [f.id for f in second]
    assert len({f.id for f in first}) == len(first)


def test_rule_template():
    assert of(run_rules(PACK, docs()), "R-TEMPLATE") == []
    found = of(run_rules(PACK, docs(spec=SPEC.replace("Phiên bản: 1", "").replace("## Phụ thuộc", ""))), "R-TEMPLATE")
    assert {f.doc for f in found} == {"T-12/spec"}
    assert len(found) == 2 and all(f.severity == "error" and f.message for f in found)
    assert any("Phụ thuộc" in f.message for f in found)


def test_rule_cover():
    assert of(run_rules(PACK, docs()), "R-COVER-AC") == []
    [f] = of(run_rules(PACK, docs(design=DESIGN.replace("Truy vết: AC-1.2", ""))), "R-COVER-AC")
    assert (f.doc, f.item, f.severity) == ("T-12/design", "AC-1.2", "error")
    assert "AC-1.2" in f.message and "Truy vết" in f.message
    # Cross-ticket: REQ-2 loses its only AC; REQ-3 is out of scope and never needs one.
    [f] = of(run_rules(PACK, docs(spec=SPEC.replace(", EP-4/REQ-2", ""))), "R-COVER-REQ")
    assert (f.doc, f.item) == ("T-12/spec", "EP-4/REQ-2")
    assert "Ngoài phạm vi" in f.message


def test_rule_traces_to():
    assert of(run_rules(PACK, docs()), "R-NO-ORPHAN") == []
    [f] = of(run_rules(PACK, docs(design=DESIGN.replace("[kỹ thuật]", ""))), "R-NO-ORPHAN")
    assert (f.doc, f.item, f.severity) == ("T-12/design", "C-3", "warn")
    assert "kỹ thuật" in f.message


def test_rule_trace_valid_is_always_on():
    pack = PACK.model_copy(update={"rules": {}})
    assert run_rules(pack, docs()) == []
    found = run_rules(pack, docs(design=DESIGN.replace("Truy vết: AC-1.2", "Truy vết: AC-9.9, EP-4/REQ-7\nThay đổi: XX-1")))
    assert [(f.rule, f.item) for f in found] == [("R-TRACE-VALID", "C-2")] * 3
    assert {r for f in found for r in ("AC-9.9", "EP-4/REQ-7", "XX-1") if r in f.message} == {"AC-9.9", "EP-4/REQ-7", "XX-1"}
    # The document that would hold the id was not given: cannot be judged, so not failed.
    alone = {"T-12/design": parse(DESIGN, "design", PACK)}
    assert run_rules(pack, alone) == []


def evidence(**over) -> Evidence:
    base = {"doc": "T-12/design", "item": "C-1", "source": "src/pay/limit.py:40-88", "provider": "local_git", "category": "code", "content_hash": "h1", "current_hash": "h1"}
    return Evidence(**{**base, **over})


def test_rule_evidence():
    assert of(run_rules(PACK, docs(), evidence=[evidence()]), "R-EVIDENCE") == []
    # F needs a source; C needs one unless it is tagged [mới].
    found = of(run_rules(PACK, docs(analysis=ANALYSIS.replace("Nguồn: src/pay/limit.py:40-88", ""), design=DESIGN.replace("[mới]", ""))), "R-EVIDENCE")
    assert {(f.doc, f.item) for f in found} == {("EP-4/analysis", "F-1"), ("T-12/design", "C-2")}
    assert all("Nguồn" in f.message for f in found)
    # A cited source that could not be resolved.
    [f] = of(run_rules(PACK, docs(), evidence=[evidence(error="file không tồn tại")]), "R-EVIDENCE")
    assert (f.doc, f.item) == ("T-12/design", "C-1")
    assert "src/pay/limit.py:40-88" in f.message and "file không tồn tại" in f.message


def test_rule_fresh():
    assert of(run_rules(PACK, docs(), evidence=[evidence()]), "R-FRESH") == []
    [f] = of(run_rules(PACK, docs(), evidence=[evidence(current_hash="h2")]), "R-FRESH")
    assert (f.doc, f.item, f.severity) == ("T-12/design", "C-1", "error")
    assert f.message
    # The pack only watches code providers.
    assert of(run_rules(PACK, docs(), evidence=[evidence(current_hash="h2", category="docs", provider="jira")]), "R-FRESH") == []


def test_rule_answered():
    assert of(run_rules(PACK, docs()), "R-OPEN-Q") == []
    [f] = of(run_rules(PACK, docs(analysis=ANALYSIS.replace("Trả lời: UTC+7", ""))), "R-OPEN-Q")
    assert (f.doc, f.item) == ("EP-4/analysis", "Q-1")
    assert "Trả lời" in f.message


def test_rule_declared_change():
    assert of(run_rules(PACK, docs()), "R-DECLARED") == []
    found = of(run_rules(PACK, docs(spec=SPEC.replace("Thay đổi: EP-4/F-1\n", ""), design=DESIGN.replace("Thay đổi: EP-4/F-1\n", ""))), "R-DECLARED")
    assert {(f.doc, f.item, f.severity) for f in found} == {("T-12/spec", "BR-1", "warn"), ("T-12/design", "C-1", "warn")}
    assert all("EP-4/F-1" in f.message for f in found)
    # Lines that do not overlap the fact's are not the same place.
    assert of(run_rules(PACK, docs(spec=SPEC.replace("Thay đổi: EP-4/F-1\nNguồn: src/pay/limit.py:50-60", "Nguồn: src/pay/limit.py:100-120"))), "R-DECLARED") == []


SPEC_V2_TRACED = SPEC.replace("Given còn hạn mức", "Given còn hạn mức trong ngày")  # AC-1.1, which C-1 traces to
SPEC_V2_UNTRACED = SPEC.replace("Là khách, tôi muốn biết", "Là khách hàng, tôi muốn biết")  # US-1, which nobody traces to


def approved_spec(new_text: str) -> dict:
    changed = impact(parse(SPEC, "spec", PACK), parse(new_text, "spec", PACK))
    return {"T-12/spec": Approved(revision=2, changed_since={1: sorted(changed)})}


def test_rule_baseline():
    base = {"T-12/design": Baseline(docs={"T-12/spec": 1})}
    assert of(run_rules(PACK, docs(), baseline=base, approved={"T-12/spec": Approved(revision=1)}), "R-BASELINE") == []
    assert of(run_rules(PACK, docs(), baseline=base, approved=approved_spec(SPEC_V2_UNTRACED)), "R-BASELINE") == []
    [f] = of(run_rules(PACK, docs(), baseline=base, approved=approved_spec(SPEC_V2_TRACED)), "R-BASELINE")
    assert (f.doc, f.item, f.severity) == ("T-12/design", "C-1", "error")
    assert "AC-1.1" in f.message and "bản 1" in f.message and "2" in f.message
    # Nobody said what changed: treated as "everything may have".
    [f] = of(run_rules(PACK, docs(), baseline=base, approved={"T-12/spec": Approved(revision=3)}), "R-BASELINE")
    assert (f.doc, f.item) == ("T-12/design", None)


def test_only_docs_and_skipped_rules():
    broken = docs(spec=SPEC.replace("Phiên bản: 1", ""), design=DESIGN.replace("Truy vết: AC-1.2", ""))
    assert {f.doc for f in run_rules(PACK, broken)} == {"T-12/spec", "T-12/design"}
    assert {f.doc for f in run_rules(PACK, broken, only_docs=["T-12/design"])} == {"T-12/design"}
    # No design given: the rules that need it are skipped, not failed.
    without = {k: v for k, v in docs().items() if k != "T-12/design"}
    assert run_rules(PACK, without) == []


# --- trace and match state -------------------------------------------------------------------------------------


def test_impact_add_remove_change():
    old = parse("### C-1: a\n### C-2: b\n### C-3: c\n", "design", PACK)
    new = parse("### C-1: a\n### C-2: b đã sửa\n### C-4: d\n", "design", PACK)
    assert impact(old, new) == {"C-2", "C-3", "C-4"}
    assert impact(old, old) == set()
    assert impact(None, old) == {"C-1", "C-2", "C-3"}


def test_trace_graph():
    graph = TraceGraph(docs())
    assert graph.tracing_to("T-12/spec", "AC-1.1", in_doc="T-12/design") == ["C-1"]
    assert graph.tracing_to("EP-4/analysis", "REQ-1", in_doc="T-12/spec") == ["AC-1.1", "AC-1.2", "BR-1"]
    assert graph.tracing_to("EP-4/analysis", "F-1") == ["T-12/spec#BR-1", "T-12/design#C-1"]
    assert graph.affected("T-12/design", "T-12/spec", {"AC-1.1", "US-1"}) == {"C-1": ["AC-1.1"]}
    assert graph.affected("T-12/design", "T-12/spec", {"US-1"}) == {}


def test_match_state_follows_only_traced_items():
    design = docs()["T-12/design"]
    base = Baseline(docs={"T-12/spec": 1})
    assert match_state(PACK, "T-12/design", design, None) == MatchState.CHUA_KIEM
    assert match_state(PACK, "T-12/design", design, [], base, {"T-12/spec": Approved(revision=1)}) == MatchState.KHOP
    # A typo fixed in an item nobody traces to calls nobody back; an edit of a traced item does.
    assert match_state(PACK, "T-12/design", design, [], base, approved_spec(SPEC_V2_UNTRACED)) == MatchState.KHOP
    assert match_state(PACK, "T-12/design", design, [], base, approved_spec(SPEC_V2_TRACED)) == MatchState.LECH
    assert match_state(PACK, "T-12/design", design, [], base, evidence=[evidence(current_hash="h2")]) == MatchState.CU
    assert match_state(PACK, "T-12/design", design, [], base, evidence=[evidence()]) == MatchState.KHOP
    errors = run_rules(PACK, docs(design=DESIGN.replace("Truy vết: AC-1.2", "")))
    assert match_state(PACK, "T-12/design", design, errors) == MatchState.LOI
    warnings = run_rules(PACK, docs(design=DESIGN.replace("[kỹ thuật]", "")))
    assert warnings and match_state(PACK, "T-12/design", design, warnings) == MatchState.KHOP
    accepted = [f.model_copy(update={"status": "accepted"}) for f in errors]
    assert match_state(PACK, "T-12/design", design, accepted) == MatchState.KHOP


# --- shipped templates and purity ------------------------------------------------------------------------------


@pytest.mark.parametrize("doctype", sorted(PACK.doc_types))
def test_shipped_template_passes_the_template_rule(doctype):
    doc = parse(PACK.template_text(doctype), doctype, PACK)
    assert doc.problems == []
    assert doc.items, "a template shows at least one item of its kinds"
    assert of(run_rules(PACK, {doctype: doc}), "R-TEMPLATE") == []


def test_doccore_imports_only_stdlib_pydantic_yaml():
    package = Path(sys.modules["actoroffice.doccore"].__file__).parent
    allowed = set(sys.stdlib_module_names) | {"pydantic", "yaml"}
    bad = []
    files = sorted(package.rglob("*.py"))
    assert len(files) >= 6
    for file in files:
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level == 1:  # a sibling module of doccore
                    continue
                names = [node.module or ""] if node.level == 0 else [f"<relative import above doccore: {'.' * node.level}{node.module or ''}>"]
            else:
                continue
            bad += [f"{file.name}: {name}" for name in names if name.split(".")[0] not in allowed]
    assert bad == []
