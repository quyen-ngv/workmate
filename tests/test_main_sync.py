"""SI-21: before the build stage finishes, the office merges new commits of the main branch into the ticket's branch."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.dossier import parse_dossier, render_dossier
from actoroffice.domain.stage import default_templates
from actoroffice.domain.work import Project, ProjectAccess, Ticket
from actoroffice.services.checks import CheckResult
from actoroffice.tools.stage_tools import dossier_update

from .fakes import FakeRuntime
from .seed import seed_db
from .test_prompt_pack import _prompt

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]
SYNC_LINE = re.compile(r"(?m)^Đã cập nhật theo main tới ([0-9a-f]{7}) \(\d{4}-\d{2}-\d{2}\)$")
SKIP_REASON = "chưa cập nhật theo nhánh chính"


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    await o.install_default_positions(skills=False)
    yield o
    await o.close()


def git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *IDENTITY, *args], check=True, capture_output=True, text=True).stdout.strip()


def commit(path: Path, files: dict[str, str], message: str) -> str:
    for name, text in files.items():
        (path / name).write_text(text, encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", message)
    return git(path, "rev-parse", "HEAD")


async def at_build(office: Office, tmp_path: Path, *, profile: dict | None = None) -> tuple[Ticket, Project, Path, Path]:
    """A code ticket at stage build with its own worktree and one commit of its own: (ticket, project, repo, worktree)."""
    repo = tmp_path / "leave"
    project = await office.projects.create("Leave", path=str(repo), access=ProjectAccess.WORKTREE)
    commit(repo, {"shared.py": "a = 1\n"}, "base")
    if profile:
        project = await office.projects.configure(project.id, profile=profile)
    ticket = await office.tickets.create("Nghỉ phép", project_id=project.id)
    ws = await office.workspaces.realize(ticket, project, await office.org.find_position("writer"), ticket)
    await office.db.execute("UPDATE tickets SET stage = 'build', stage_template = 'code' WHERE id = ?", (ticket.id,))
    commit(ws.path, {"leave.py": "x = 1\n"}, "leave")
    return await office.tickets.get(ticket.id), project, repo, ws.path


async def run_checks(office: Office, ticket: Ticket, workspace: Path, *check_ids: str) -> list[CheckResult]:
    stage = (await office.stages.template_of(ticket)).stage("build")
    assert stage is not None
    srun = await office.stages.ensure_run(ticket)
    return await office.stages.checks.run(ticket, stage, srun, workspace=workspace, run_id=None, only=list(check_ids))


async def synced(office: Office, ticket: Ticket, workspace: Path) -> CheckResult:
    [result] = await run_checks(office, ticket, workspace, "main_synced")
    return result


def parents(path: Path) -> list[str]:
    return git(path, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]


async def dossier_tests(office: Office, ticket: Ticket) -> str:
    doc = await office.tickets.get_document(ticket.id, "dossier")
    return parse_dossier(doc.body if doc else "").get("tests", "")


# ---- AC-1, AC-2 ------------------------------------------------------------------------------------------------


async def test_ac1_merges_new_main_commit(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo, ws = await at_build(office, tmp_path)
    own = git(ws, "rev-parse", "HEAD")
    main_tip = commit(repo, {"holiday.py": "y = 2\n"}, "holiday on main")

    result = await synced(office, ticket, ws)

    assert result.status == "passed", result.summary
    assert parents(ws) == [own, main_tip]
    assert (ws / "holiday.py").read_text(encoding="utf-8") == "y = 2\n"
    assert git(ws, "log", "-1", "--format=%s") == f"Merge main into {git(ws, 'rev-parse', '--abbrev-ref', 'HEAD')} ({ticket.key})"
    assert result.data["merge_sha"] == git(ws, "rev-parse", "HEAD") and result.data["main_sha"] == main_tip
    assert git(repo, "rev-parse", "main") == main_tip  # main itself is only read


async def test_ac2_up_to_date_no_new_commit(office: Office, tmp_path: Path) -> None:
    ticket, _project, _repo, ws = await at_build(office, tmp_path)
    head = git(ws, "rev-parse", "HEAD")

    result = await synced(office, ticket, ws)

    assert result.status == "passed", result.summary
    assert git(ws, "rev-parse", "HEAD") == head
    assert "merge_sha" not in result.data


# ---- AC-3, AC-4: nothing changes when the office cannot merge -----------------------------------------------------


async def test_ac3_conflict_aborts_and_lists_files(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo, ws = await at_build(office, tmp_path)
    head = commit(ws, {"shared.py": "a = 'ticket'\n"}, "ticket edits shared")
    commit(repo, {"shared.py": "a = 'main'\n"}, "main edits shared")

    result = await synced(office, ticket, ws)

    assert result.status == "failed"
    assert "shared.py" in result.summary and "git merge main" in result.summary and "stage_complete" in result.summary
    assert git(ws, "rev-parse", "HEAD") == head
    assert subprocess.run(["git", "-C", str(ws), "rev-parse", "-q", "--verify", "MERGE_HEAD"], capture_output=True).returncode != 0
    assert git(ws, "status", "--porcelain") == ""


async def test_ac4_dirty_worktree_fails_untouched(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo, ws = await at_build(office, tmp_path)
    head = git(ws, "rev-parse", "HEAD")
    commit(repo, {"holiday.py": "y = 2\n"}, "holiday on main")
    (ws / "draft.py").write_text("wip\n", encoding="utf-8")

    result = await synced(office, ticket, ws)

    assert result.status == "failed" and "commit" in result.summary
    assert git(ws, "rev-parse", "HEAD") == head
    assert (ws / "draft.py").read_text(encoding="utf-8") == "wip\n" and not (ws / "holiday.py").exists()
    assert git(ws, "status", "--porcelain") == "?? draft.py"


# ---- AC-5, AC-6: the repo checks and the merge --------------------------------------------------------------------


def writer(target: Path, text: str = "ran") -> list[str]:
    """A project command that writes `text` (or the HEAD it ran on, for "HEAD") into target."""
    if text == "HEAD":
        code = f"import pathlib, subprocess; pathlib.Path(r'{target}').write_text(subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip())"
    else:
        code = f"import pathlib; pathlib.Path(r'{target}').write_text({text!r})"
    return [sys.executable, "-c", code]


async def test_ac5_repo_checks_skipped_after_failure(office: Office, tmp_path: Path) -> None:
    marks = {key: tmp_path / f"{key}.ran" for key in ("build", "test", "lint")}
    ticket, _project, repo, ws = await at_build(office, tmp_path, profile={key: writer(path) for key, path in marks.items()})
    commit(ws, {"shared.py": "a = 'ticket'\n"}, "ticket edits shared")
    commit(repo, {"shared.py": "a = 'main'\n"}, "main edits shared")

    results = await run_checks(office, ticket, ws, "main_synced", "repo:build", "repo:test", "repo:lint")

    assert [r.status for r in results] == ["failed", "skipped", "skipped", "skipped"]
    assert all(SKIP_REASON in r.summary for r in results[1:])
    assert not any(path.exists() for path in marks.values())

    commit(repo, {"shared.py": "a = 'ticket'\n"}, "main agrees")  # a second build attempt with no conflict runs them
    git(ws, "merge", "-q", "--no-edit", "main")
    results = await run_checks(office, ticket, ws, "main_synced", "repo:build", "repo:test", "repo:lint")
    assert [r.status for r in results] == ["passed"] * 4


async def test_ac6_repo_test_runs_on_merge_commit(office: Office, tmp_path: Path) -> None:
    seen = tmp_path / "head.txt"
    ticket, _project, repo, ws = await at_build(office, tmp_path, profile={"test": writer(seen, "HEAD")})
    commit(repo, {"holiday.py": "y = 2\n"}, "holiday on main")

    sync, test = await run_checks(office, ticket, ws, "main_synced", "repo:test")

    assert sync.status == "passed" and test.status == "passed", (sync.summary, test.summary)
    merge = git(ws, "rev-parse", "HEAD")
    assert sync.data["merge_sha"] == merge
    assert seen.read_text(encoding="utf-8") == merge
    assert test.data["head"] == merge and test.data["clean"] is True


# ---- AC-7 ---------------------------------------------------------------------------------------------------------


def test_ac7_main_synced_first_in_build() -> None:
    templates = default_templates()
    for name in ("code", "quick"):
        build = templates[name].stage("build")
        assert build is not None and build.checks[0] == "main_synced", name
    for name, template in templates.items():
        for stage in template.stages:
            if (name, stage.id) not in (("code", "build"), ("quick", "build")):
                assert "main_synced" not in stage.checks, (name, stage.id)


# ---- AC-8, AC-9, AC-10: timeline and dossier ------------------------------------------------------------------------


async def test_ac8_comment_and_dossier_line(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo, ws = await at_build(office, tmp_path)
    await office.tickets.put_document(ticket.id, "dossier", render_dossier({"tests": "pytest tests/test_leave.py"}), author="Copilot", base_revision=None)
    main_tip = commit(repo, {"holiday.py": "y = 2\n"}, "holiday on main")

    result = await synced(office, ticket, ws)

    assert result.status == "passed"
    merge = git(ws, "rev-parse", "HEAD")
    notes = [c.body for c in await office.tickets.comments(ticket.id) if "main" in c.body and main_tip[:7] in c.body and merge[:7] in c.body]
    assert len(notes) == 1
    section = await dossier_tests(office, ticket)
    assert section.startswith("pytest tests/test_leave.py")
    assert SYNC_LINE.findall(section) == [main_tip[:7]]


async def test_ac9_dossier_line_replaced(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo, ws = await at_build(office, tmp_path)
    first = commit(repo, {"holiday.py": "y = 2\n"}, "holiday on main")
    assert (await synced(office, ticket, ws)).status == "passed"
    assert SYNC_LINE.findall(await dossier_tests(office, ticket)) == [first[:7]]

    second = commit(repo, {"sick.py": "z = 3\n"}, "sick leave on main")
    assert (await synced(office, ticket, ws)).status == "passed"

    assert SYNC_LINE.findall(await dossier_tests(office, ticket)) == [second[:7]]
    revision = (await office.tickets.get_document(ticket.id, "dossier")).revision
    assert (await synced(office, ticket, ws)).status == "passed"  # nothing new: the dossier is not written again
    assert (await office.tickets.get_document(ticket.id, "dossier")).revision == revision


async def test_ac10_dossier_update_keeps_line(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo, ws = await at_build(office, tmp_path)
    tip = commit(repo, {"holiday.py": "y = 2\n"}, "holiday on main")
    assert (await synced(office, ticket, ws)).status == "passed"
    ctx = SimpleNamespace(office=office, ticket_id=ticket.id, actor=SimpleNamespace(name="Copilot"))

    await dossier_update({"section": "tests", "body": "12 test mới, ruff sạch", "sources": ["tests/test_leave.py"]}, ctx)  # type: ignore[arg-type]

    section = await dossier_tests(office, ticket)
    assert section.startswith("12 test mới, ruff sạch")
    assert section.splitlines()[-1] == SYNC_LINE.search(section).group(0)  # type: ignore[union-attr]
    assert SYNC_LINE.findall(section) == [tip[:7]]

    await dossier_update({"section": "context", "body": "bối cảnh"}, ctx)  # type: ignore[arg-type]
    await dossier_update({"section": "tests", "body": section}, ctx)  # the AI copied the line: still one
    assert SYNC_LINE.findall(await dossier_tests(office, ticket)) == [tip[:7]]


# ---- AC-11 ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("case", ["no_git", "no_main", "on_main"])
async def test_ac11_skipped_cases(office: Office, tmp_path: Path, case: str) -> None:
    ticket, project, repo, ws = await at_build(office, tmp_path)
    commit(repo, {"holiday.py": "y = 2\n"}, "holiday on main")
    target = ws
    if case == "no_git":
        target = tmp_path / "plain"
        target.mkdir()
    elif case == "no_main":
        trunk = project.model_copy(update={"main_branch": "trunk"})
        await office.db.execute("UPDATE projects SET config_json = ? WHERE id = ?", (trunk.model_dump_json(), project.id))
    else:
        target = repo
    before = {p: git(p, "rev-parse", "HEAD") for p in (repo, ws)}

    result = await synced(office, ticket, target)

    assert result.status == "skipped" and result.summary
    assert {p: git(p, "rev-parse", "HEAD") for p in (repo, ws)} == before
    assert git(ws, "status", "--porcelain") == "" and git(repo, "status", "--porcelain") == ""


# ---- AC-12 ------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("template", ["code", "quick"])
def test_ac12_build_prompt_mentions_main_synced(template: str) -> None:
    for office_runs_tests in (True, False):
        system = _prompt(template, office_runs_tests).system
        assert "main_synced" in system
        assert "merges the main branch into your branch" in system
        assert "git merge <main>" in system and "resolve the conflicts, commit, then call stage_complete again" in system
    assert "main_synced" not in _prompt(template, True, stage_id="pr").system
