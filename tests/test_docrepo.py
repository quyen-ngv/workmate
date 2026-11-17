"""The documents repository: every document of the office as a file in a git folder, kept in step both ways, and
searchable by people and by the AI."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.docrepo import layout
from actoroffice.docrepo.chunks import chunks

from .seed import seed_db
from .test_docflow import design, designed, put, runtime, setup  # noqa: F401  (fixtures)

WHO = ["-c", "user.name=Hà", "-c", "user.email=ha@example.test"]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False, docs_repo="docs"), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


def find(root: Path, name: str, under: str = "") -> Path:
    (path,) = [p for p in (root / under).rglob(name) if ".git" not in p.parts]
    return path


# ---- pure ---------------------------------------------------------------------------------------------------------------


def test_folders_are_named_after_the_key_and_a_plain_slug() -> None:
    epic, task = layout.Node("AO-12", "Nâng hạn mức hoàn tiền/ngày", True), layout.Node("AO-13", "Lý do từ chối")
    assert layout.ticket_dir([epic, task]) == "epics/AO-12-nang-han-muc-hoan-tien-ngay/stories/AO-13-ly-do-tu-choi"
    assert layout.ticket_dir([layout.Node("AO-5", "Đổi màu nút")]) == "tickets/AO-5-doi-mau-nut"
    assert layout.relative("epics/AO-12-x/stories/AO-13-y", "epics/AO-12-x") == "../.."
    meta, body = layout.split(layout.render({"ticket": "AO-1", "revision": 3}, "# Hi\n\nbody\n"))
    assert meta == {"ticket": "AO-1", "revision": 3} and body == "# Hi\n\nbody\n"
    assert layout.split("no front matter\n") == ({}, "no front matter\n")


def test_a_file_is_cut_at_its_headings_with_the_line_each_piece_starts_on() -> None:
    text = layout.render({"x": 1}, "# Design\nintro\n## Thay đổi\n### C-1: hằng số\nTừ 5 lên 9.\n```\n# not a heading\n```\n## Rủi ro\nKhông có.")
    pieces = {c.heading: c for c in chunks(text)}
    assert list(pieces) == ["Design", "Design › Thay đổi", "Design › Thay đổi › C-1: hằng số", "Design › Rủi ro"]
    lines = text.split("\n")
    assert lines[pieces["Design › Thay đổi › C-1: hằng số"].line - 1] == "### C-1: hằng số"
    assert "# not a heading" in pieces["Design › Thay đổi › C-1: hằng số"].text


# ---- office → folder ---------------------------------------------------------------------------------------------------


async def test_the_office_writes_a_tree_of_epics_stories_and_documents_and_commits_it(office: Office, designed) -> None:
    team, lan, minh, hung, repo, epic, child = designed
    done = await office.docrepo.sync(full=True)
    root = office.docrepo.root
    assert done["commits"] and done["error"] == ""
    epic_dir = root / "epics" / layout.folder(epic.key, epic.title)
    story = epic_dir / "stories" / layout.folder(child.key, child.title)
    assert (epic_dir / "analysis.md").is_file() and (story / "spec.md").is_file() and (story / "design.md").is_file()
    meta, body = layout.split((story / "spec.md").read_text(encoding="utf-8"))
    assert (meta["ticket"], meta["doc"], meta["revision"], meta["state"]) == (child.key, "spec", 1, "approved") and body.startswith("# Nghiệp vụ")
    card = (story / "README.md").read_text(encoding="utf-8")
    assert f"Thuộc: [{epic.key} · {epic.title}](../../README.md)" in card and "| [design](design.md) | 1 | chưa duyệt |" in card
    assert f"[{child.key} · {child.title}](stories/{story.name}/README.md)" in (epic_dir / "README.md").read_text(encoding="utf-8")
    assert f"[{epic.key} · {epic.title}](epics/{epic_dir.name}/README.md)" in (root / "README.md").read_text(encoding="utf-8")
    assert "Phụ thuộc: <ID>" in (root / "AGENTS.md").read_text(encoding="utf-8")
    assert git(root, "status", "--porcelain") == "" and git(root, "log", "--format=%an") == "ActorOffice"

    # nothing changed: nothing to commit
    assert (await office.docrepo.sync())["commits"] == []
    # the office saves a new revision: the file follows, one commit says what changed
    await put(office, child, "design", design(child.key, epic.key).replace("Từ 5 lên 9.", "Từ 5 lên 10."))
    office.docrepo.touch(child.id)
    await office.docrepo.sync()
    assert "Từ 5 lên 10." in (story / "design.md").read_text(encoding="utf-8")
    assert git(root, "log", "-1", "--format=%s") == f"{child.key} design bản 2 (Copilot)"


async def test_the_files_follow_the_office_events(office: Office, designed) -> None:
    *_, epic, child = designed
    await office.docrepo.sync(full=True)
    office.docrepo.on_event("ticket.document", {"ticket_id": child.id, "key": "design", "revision": 2})
    assert child.id in office.docrepo._tickets
    office.docrepo.on_event("interaction.resolved", {"ticket_id": child.id})
    assert office.docrepo._urgent
    office.docrepo.on_event("kb.changed", {"item": {"id": 7}})
    assert 7 in office.docrepo._kb


# ---- folder → office ---------------------------------------------------------------------------------------------------


async def test_an_edit_in_the_folder_becomes_a_revision_and_an_approved_document_must_be_approved_again(office: Office, designed) -> None:
    *_, epic, child = designed
    await office.docrepo.sync(full=True)
    root = office.docrepo.root
    spec_file = find(root, "spec.md", "epics")
    text = spec_file.read_text(encoding="utf-8")
    spec_file.write_text(text.replace("Áp cho mọi khách.", "Áp cho mọi khách, trừ khách VIP."), encoding="utf-8")
    await office.docrepo.sync()

    doc = await office.tickets.get_document(child.id, "spec")
    assert doc.revision == 2 and doc.approved_revision == 1 and "trừ khách VIP" in doc.body and doc.updated_by == "git:kho tài liệu"
    assert not doc.body.startswith("---"), "the head block is the office's, not part of the document"
    comments = [c.body for c in await office.tickets.comments(child.id)]
    assert any("được sửa trong kho tài liệu" in c and "phải được duyệt lại" in c for c in comments)
    meta, _ = layout.split(spec_file.read_text(encoding="utf-8"))
    assert (meta["revision"], meta["state"]) == (2, "changed")
    assert git(root, "status", "--porcelain") == ""


async def test_both_sides_changed_the_same_document_their_edits_are_merged(office: Office, designed) -> None:
    *_, epic, child = designed
    await office.docrepo.sync(full=True)
    design_file = find(office.docrepo.root, "design.md", "epics")
    # the office saves revision 2 (another section) while the file still holds revision 1, edited by hand
    await put(office, child, "design", design(child.key, epic.key).replace("Không có.\n\n## 8.", "Bộ đếm có thể lệch múi giờ.\n\n## 8."))
    text = design_file.read_text(encoding="utf-8")
    design_file.write_text(text.replace("Từ 5 lên 9.", "Từ 5 lên 9 (hằng số MAX_PER_DAY)."), encoding="utf-8")
    await office.docrepo.sync()
    doc = await office.tickets.get_document(child.id, "design")
    assert doc.revision == 3 and "hằng số MAX_PER_DAY" in doc.body and "Bộ đếm có thể lệch múi giờ." in doc.body
    assert "Bộ đếm có thể lệch múi giờ." in design_file.read_text(encoding="utf-8")


async def test_a_new_file_in_a_ticket_folder_is_a_new_document_and_the_library_is_searchable(office: Office, designed) -> None:
    *_, epic, child = designed
    await office.docrepo.sync(full=True)
    root = office.docrepo.root
    story = find(root, "design.md", "epics").parent
    (story / "notes.md").write_text("# Ghi chú họp\n\nĐã thống nhất giữ hằng số.\n", encoding="utf-8")
    library = root / "knowledge" / "library"
    library.mkdir(parents=True)
    (library / "quy-trinh-hoan-tien.md").write_text("# Quy trình hoàn tiền\n\n## Đối soát\nĐối soát cuối ngày với ngân hàng lúc 23:00.\n", encoding="utf-8")
    await office.docrepo.sync()
    assert (await office.tickets.get_document(child.id, "notes")).body.startswith("# Ghi chú họp")

    hits = await office.docrepo.search("đối soát ngân hàng")
    assert hits[0]["path"] == "knowledge/library/quy-trinh-hoan-tien.md" and hits[0]["heading"] == "Quy trình hoàn tiền › Đối soát" and hits[0]["line"] == 3
    assert any(h["path"].endswith("design.md") for h in await office.docrepo.search("hang so han muc")), "accents are ignored"
    first = (await office.docrepo.search("Nâng hạn mức"))[0]
    assert not first["path"].endswith("README.md"), "a document comes before the cards the office generates from it"
    grep = await office.docrepo.grep(r"AC-1\.1")
    assert {h["path"].rsplit("/", 1)[-1] for h in grep} >= {"spec.md", "design.md"}
    section = await office.docrepo.read(str(story.relative_to(root) / "design.md"), heading="C-1")
    assert section["text"].startswith("### C-1: Đổi hằng số hạn mức") and "C-2" not in section["text"]
    assert "Quy trình hoàn tiền" in (root / "knowledge" / "README.md").read_text(encoding="utf-8")
    with pytest.raises(Exception, match="ngoài kho"):
        await office.docrepo.read("../home/config.yaml")


async def test_knowledge_items_are_files_and_an_edit_there_is_confirmed_knowledge(office: Office, designed) -> None:
    team, *_ = designed
    item = await office.knowledge.note(kind="decision", title="Hạn mức là hằng số", content="Giữ trong code.", by="Copilot", team_id=team.id)
    office.docrepo.touch(kb_id=item.id)
    await office.docrepo.sync()
    path = office.docrepo.root / layout.kb_path("decision", item.id, item.title)
    assert path.is_file() and "nháp" in (office.docrepo.root / "knowledge" / "README.md").read_text(encoding="utf-8")
    path.write_text(path.read_text(encoding="utf-8").replace("Giữ trong code.", "Giữ trong code; đổi khi có yêu cầu cấu hình."), encoding="utf-8")
    await office.docrepo.sync()
    again = await office.knowledge.get(item.id)
    assert again.content == "Giữ trong code; đổi khi có yêu cầu cấu hình." and again.status == "verified"

    # a file somebody adds to a knowledge folder becomes a confirmed item
    (office.docrepo.root / "knowledge" / "facts").mkdir(parents=True, exist_ok=True)
    (office.docrepo.root / "knowledge" / "facts" / "vip.md").write_text("# Khách VIP\n\nKhách VIP được hoàn trong 60 ngày.\n", encoding="utf-8")
    await office.docrepo.sync()
    (fact,) = [i for i in await office.knowledge.list(kind="fact") if i.title == "Khách VIP"]
    assert fact.status == "verified" and fact.content == "Khách VIP được hoàn trong 60 ngày."
    assert not (office.docrepo.root / "knowledge" / "facts" / "vip.md").exists() and layout.kb_path("fact", fact.id, fact.title).endswith("khach-vip.md")


# ---- a remote ----------------------------------------------------------------------------------------------------------


async def test_the_folder_is_pushed_and_a_change_pushed_by_somebody_else_comes_back_as_a_revision(office: Office, designed, tmp_path) -> None:
    *_, epic, child = designed
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    office.settings.docs_remote = str(remote)
    assert (await office.docrepo.sync(full=True))["error"] == ""
    clone = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(remote), str(clone)], check=True)
    spec_file = find(clone, "spec.md", "epics")
    spec_file.write_text(spec_file.read_text(encoding="utf-8").replace("Áp cho mọi khách.", "Áp cho khách thường."), encoding="utf-8")
    git(clone, *WHO, "commit", "-qam", "BA sửa BR-1")
    git(clone, "push", "-q", "origin", "main")

    done = await office.docrepo.sync()
    assert done["error"] == ""
    doc = await office.tickets.get_document(child.id, "spec")
    assert doc.revision == 2 and "Áp cho khách thường." in doc.body and doc.updated_by == "git:Hà"
    git(clone, "pull", "-q")
    meta, _ = layout.split(find(clone, "spec.md", "epics").read_text(encoding="utf-8"))
    assert meta["revision"] == 2, "the office's own record of the revision went back to the remote"


async def test_an_outside_edit_that_cannot_be_taken_in_now_is_kept_and_taken_in_later(office: Office, designed, monkeypatch) -> None:
    *_, epic, child = designed
    await office.docrepo.sync(full=True)
    spec_file = find(office.docrepo.root, "spec.md", "epics")
    spec_file.write_text(spec_file.read_text(encoding="utf-8").replace("Áp cho mọi khách.", "Áp cho khách thường."), encoding="utf-8")
    real = office.tickets.put_document

    async def busy(*args, **kwargs):
        from actoroffice.domain.errors import Conflict

        raise Conflict("Document 'spec' is at revision 2, not 1")

    monkeypatch.setattr(office.tickets, "put_document", busy)
    await office.docrepo.sync()
    assert "Áp cho khách thường." in spec_file.read_text(encoding="utf-8"), "the edit is not written over"
    assert "spec.md" in office.docrepo.last_error and (await office.tickets.get_document(child.id, "spec")).revision == 1

    monkeypatch.setattr(office.tickets, "put_document", real)
    await office.docrepo.sync()
    doc = await office.tickets.get_document(child.id, "spec")
    assert doc.revision == 2 and "Áp cho khách thường." in doc.body and office.docrepo.last_error == ""
