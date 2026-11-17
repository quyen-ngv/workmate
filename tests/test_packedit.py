"""People see and change the document standard from the web: a pack of their own that extends the one in use."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from actoroffice.services import packedit
from actoroffice.services.docflow import pack_at, pack_path

from .test_docweb import web  # noqa: F401  (fixture)
from .test_web import HEADERS


def test_the_built_in_pack_is_read_whole_and_never_written(tmp_path: Path) -> None:
    v = packedit.view("default", tmp_path)
    assert not v["editable"] and v["yaml"] == ""
    analysis = next(d for d in v["doc_types"] if d["id"] == "analysis")
    assert analysis["template"].startswith("# Phân tích") and "REQ" in analysis["items"]
    assert "common" in v["instructions"] and v["reviews"]["Q-SPEC"]["doc"] == "spec"
    with pytest.raises(packedit.PackEditError, match="khác 'default'"):
        packedit.save("default", tmp_path, instructions={"common": "x"})


def test_a_pack_of_ones_own_holds_only_what_differs(tmp_path: Path) -> None:
    base = packedit.view("default", tmp_path)
    spec_template = next(d for d in base["doc_types"] if d["id"] == "spec")["template"]
    pack = packedit.save(
        "pay", tmp_path,
        templates={"spec": spec_template.replace("## 1. Mục tiêu", "## 1. Mục tiêu\nGhi cả KPI."), "design": next(d for d in base["doc_types"] if d["id"] == "design")["template"]},
        instructions={"common": base["instructions"]["common"], "ask": "Hỏi BA bằng tiếng Việt, tối đa 3 câu mỗi lần."},
        reviews={"Q-SPEC": [*base["reviews"]["Q-SPEC"]["rubric"], "Every AC names the customer tier it applies to."]},
    )
    own = yaml.safe_load((tmp_path / "packs" / "pay" / "pack.yaml").read_text(encoding="utf-8"))
    assert own["extends"] == "default" and list(own["doc_types"]) == ["spec"], "an unchanged template is not copied"
    assert list(own["instructions"]) == ["ask"] and own["reviews"]["Q-SPEC"]["rubric"][-1].startswith("Every AC names")
    assert "Ghi cả KPI." in pack.template_text("spec") and "Ghi cả KPI." not in pack_at(None).template_text("spec")
    assert pack.instructions["ask"].startswith("Hỏi BA bằng tiếng Việt") and pack.rules == pack_at(None).rules

    v = packedit.view("pay", tmp_path)
    assert v["editable"] and v["extends"] == "default" and "Ghi cả KPI." in next(d for d in v["doc_types"] if d["id"] == "spec")["template"]


def test_a_save_that_breaks_the_pack_is_undone(tmp_path: Path) -> None:
    packedit.save("pay", tmp_path, instructions={"ask": "Hỏi ngắn."})
    before = (tmp_path / "packs" / "pay" / "pack.yaml").read_text(encoding="utf-8")
    broken = before + "rules:\n  R-X: {type: cover, each: spec.ZZ}\n"
    with pytest.raises(packedit.PackEditError, match="không hợp lệ") as err:
        packedit.save("pay", tmp_path, yaml_text=broken)
    assert "rules.R-X" in str(err.value)
    assert (tmp_path / "packs" / "pay" / "pack.yaml").read_text(encoding="utf-8") == before
    assert pack_at(pack_path("pay", tmp_path)).instructions["ask"].startswith("Hỏi ngắn.")


def test_the_standard_page_creates_a_pack_for_a_layer_and_edits_it(web) -> None:  # noqa: F811
    office, c, team, _people, _project = web
    view = c.get("/api/doc-pack?name=default").json()
    assert not view["editable"]
    made = c.put("/api/doc-pack", json={"name": "pay-team", "base": "default"}, headers=HEADERS).json()
    assert made["editable"] and made["extends"] == "default"
    assert c.put("/api/doc-profile", json={"scope": f"team:{team.id}", "pack": "pay-team"}, headers=HEADERS).status_code == 200
    saved = c.put("/api/doc-pack", json={"name": "pay-team", "instructions": {"ask": "Hỏi ngắn gọn."}}, headers=HEADERS).json()
    assert saved["instructions"]["ask"].startswith("Hỏi ngắn gọn.")
    bad = c.put("/api/doc-pack", json={"name": "pay-team", "yaml": "rules: [1, 2]"}, headers=HEADERS)
    assert bad.status_code == 400 and "không hợp lệ" in bad.json()["error"]
    assert c.get(f"/api/doc-profile?scope=team:{team.id}").json()["profile"]["pack"] == "pay-team"
