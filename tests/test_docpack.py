"""A unit's own standard pack: set in the office config, checked before it is used, tried on existing documents."""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from actoroffice.__main__ import cmd_pack
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.doccore import PackError
from actoroffice.services import docflow

from .fakes import FakeRuntime
from .seed import seed_db
from .test_docflow import analysis, design, put, spec

STRICT = """name: acme
version: 2
extends: default
roles: [ba, dev, lead, architect]
rules:
  R-NO-ORPHAN: {type: traces_to, each: design.C, to: [spec.AC, spec.BR], unless_tag: technical, severity: error}
gates:
  design: {roles: [architect], fallback: null, submit_by: [dev]}
"""


@pytest.fixture(autouse=True)
def builtin_pack_afterwards():
    yield
    docflow.configure(None)


def write_pack(folder: Path, text: str) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "pack.yaml").write_text(text, encoding="utf-8")
    return folder


async def open_office(tmp_path: Path, **settings) -> Office:
    office = Office(Settings(data_dir=tmp_path / "home", ai_review=False, **settings), runtime=FakeRuntime(scripts=[]))
    seed_db(office.settings.db_path)
    await office.open()
    return office


async def test_a_units_pack_changes_rules_and_gates_without_code(tmp_path: Path) -> None:
    write_pack(tmp_path / "home" / "packs" / "acme", STRICT)
    office = await open_office(tmp_path, doc_pack="acme")
    try:
        assert office.docs.pack.name == "acme" and office.docs.pack.rules["R-NO-ORPHAN"].severity == "error"
        assert office.docs.pack.rules["R-COVER-AC"].severity == "error", "what the unit did not change comes from the built-in pack"
        ticket = await office.tickets.create("x")
        gate = await office.teams.gate_policy(ticket, "design")
        assert gate.roles == ["architect"] and gate.submit_by == ["dev"]
        assert [s.id for s in docflow.pack_templates()["epic"].stages] == ["analyze", "breakdown", "close"]
    finally:
        await office.close()
    docflow.configure(None)
    assert docflow.default_pack().name == "default" and docflow.pack_gates()["design"].roles == ["lead"]


async def test_a_pack_that_is_not_valid_stops_the_office_and_names_every_problem(tmp_path: Path) -> None:
    bad = STRICT.replace("roles: [architect]", "roles: [cto]") + "doc_types:\n  memo: {template: templates/memo.md, sections: [A], items: [ZZ]}\n"
    path = write_pack(tmp_path / "bad", bad)
    with pytest.raises(PackError) as err:
        Office(Settings(data_dir=tmp_path / "home", doc_pack=str(path)), runtime=FakeRuntime(scripts=[]))
    text = " | ".join(err.value.problems)
    assert "cto" in text and "ZZ" in text
    assert docflow.default_pack().name == "default", "the pack in use stays as it was"


async def test_pack_check_command(tmp_path: Path, capsys) -> None:
    office = await open_office(tmp_path)
    try:
        epic = await office.tickets.create("Nâng hạn mức")
        await office.db.execute("UPDATE tickets SET stage_template = 'epic', stage = 'breakdown' WHERE id = ?", (epic.id,))
        epic = await office.tickets.get(epic.id)
        await put(office, epic, "analysis", analysis(epic.key))
        child = await office.tickets.create("Task", parent_id=epic.id)
        await office.db.execute("UPDATE tickets SET stage_template = 'task-spec', stage = 'design' WHERE id = ?", (child.id,))
        child = await office.tickets.get(child.id)
        await put(office, child, "spec", spec(epic.key, "T-1"))
        orphan = design(child.key, epic.key).replace("## 3. Mô hình dữ liệu", "### C-9 [mới]: Dọn code\nKhông liên quan AC nào.\n\n## 3. Mô hình dữ liệu")
        await put(office, child, "design", orphan)

        await cmd_pack(office, argparse.Namespace(path=None, against=None))
        assert "Pack 'default' v1 hợp lệ" in capsys.readouterr().out
        await cmd_pack(office, argparse.Namespace(path=None, against=child.key))
        out = capsys.readouterr().out
        assert "0 lỗi, 1 cảnh báo" in out and "R-NO-ORPHAN" in out and "C-9" in out, "the built-in pack only warns about a change that serves no AC"

        strict = write_pack(tmp_path / "acme", STRICT)
        with pytest.raises(SystemExit) as stop:
            await cmd_pack(office, argparse.Namespace(path=str(strict), against=child.key))
        out = capsys.readouterr().out
        assert stop.value.code == 2 and "Pack 'acme' v2 hợp lệ" in out and "1 lỗi, 0 cảnh báo" in out and "[LỖI] R-NO-ORPHAN" in out

        with pytest.raises(SystemExit) as stop:
            await cmd_pack(office, argparse.Namespace(path=str(write_pack(tmp_path / "bad", STRICT.replace("[architect]", "[cto]"))), against=None))
        assert stop.value.code == 1 and "Pack không hợp lệ" in capsys.readouterr().out
        assert office.docs.pack.name == "default", "checking a pack never switches the office to it"
    finally:
        await office.close()
