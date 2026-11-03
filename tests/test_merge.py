"""SI-20: the "Merge into <main>" button for repos without a code host: test the merge result, fast-forward main, pass the gate."""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import pkgutil
import shutil
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.errors import Conflict
from actoroffice.domain.work import ProjectAccess, Ticket
from actoroffice.engine.workspace import branch_name
from actoroffice.services.merges import MergeError
from actoroffice.web.server import create_app

from .fakes import FakeRuntime
from .seed import seed_db
from .test_web import HEADERS, TOKEN, login

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")

IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]

# The project's test command: records where it ran, optionally waits for a flag file, then checks files exist.
CHECK = """\
import os, pathlib, sys, time
log, need, flag = sys.argv[1], sys.argv[2], sys.argv[3]
pathlib.Path(log).write_text(os.getcwd(), encoding="utf-8")
if flag != "-":
    pathlib.Path(flag + ".started").write_text("1")
    while not os.path.exists(flag):
        time.sleep(0.05)
missing = [f for f in need.split(",") if f and not os.path.exists(f)]
if missing:
    print("missing files:", missing)
    sys.exit(1)
print("all good")
"""


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


async def at_pr(office: Office, tmp_path: Path, *, title: str = "Nghỉ phép") -> tuple[Ticket, Path, Path]:
    """A root ticket of a worktree project at stage pr, the AI done with the stage: (root, project folder, worktree)."""
    repo = tmp_path / "leave"
    project = await office.projects.create("Leave", path=str(repo), access=ProjectAccess.WORKTREE)
    root = await office.tickets.create(title, project_id=project.id)
    ws = await office.workspaces.realize(root, project, await office.org.find_position("writer"), root)
    await office.db.execute("UPDATE tickets SET stage = 'pr', stage_template = 'code' WHERE id = ?", (root.id,))
    await office.db.execute(
        "INSERT INTO stage_runs(ticket_id, stage, attempt, started_at, finished_at, outcome, checks_json) VALUES (?, 'pr', 1, ?, ?, 'completed', '[]')",
        (root.id, "2026-11-03T00:00:00Z", "2026-11-03T01:00:00Z"),
    )
    return await office.tickets.get(root.id), repo, ws.path


async def use_check(office: Office, tmp_path: Path, need: str, *, flag: str = "-", test_command: str | None = None) -> Path:
    script = tmp_path / "check.py"
    script.write_text(CHECK, encoding="utf-8")
    log = tmp_path / "cwd.log"
    project = await office.projects.find("Leave")
    assert project
    await office.projects.configure(project.id, profile={"test": [sys.executable, str(script), str(log), need, flag]}, test_command=test_command)
    return log


async def comments(office: Office, ticket: Ticket) -> str:
    return "\n".join(c.body for c in await office.tickets.comments(ticket.id))


async def wait_for(path: Path) -> None:
    for _ in range(400):
        if path.exists():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


# ---- AC-1 ----------------------------------------------------------------------------------------------------


async def test_ac1_status_available_only_for_root_without_code_host(office: Office, tmp_path: Path, monkeypatch) -> None:
    root, _repo, ws = await at_pr(office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    panel = await office.merges.panel(root.key)
    assert panel["available"] and panel["ready"] and not panel["running"]
    assert panel["branch"] == branch_name(root) and panel["main"] == "main"

    await office.db.execute("UPDATE stage_runs SET finished_at = NULL, outcome = NULL WHERE ticket_id = ?", (root.id,))
    waiting = await office.merges.panel(root.key)
    assert waiting["available"] and not waiting["ready"] and "AI chưa xong" in waiting["reason"]
    with pytest.raises(MergeError, match="AI chưa xong"):
        await office.merges.start(root.key, by="user")
    await office.db.execute("UPDATE stage_runs SET finished_at = '2026-11-03T01:00:00Z', outcome = 'completed' WHERE ticket_id = ?", (root.id,))

    child = await office.tickets.create("Con", project_id=root.project_id, parent_id=root.id)
    await office.db.execute("UPDATE tickets SET stage = 'pr', stage_template = 'code' WHERE id = ?", (child.id,))
    assert (await office.merges.panel(child.key))["available"] is False

    monkeypatch.setattr(office.atlassian, "code_host", lambda: object())
    assert (await office.merges.panel(root.key))["available"] is False
    monkeypatch.undo()

    await office.db.execute("UPDATE tickets SET pr_url = 'https://example.org/pr/1' WHERE id = ?", (root.id,))
    assert (await office.merges.panel(root.key))["available"] is False
    with pytest.raises(MergeError):
        await office.merges.start(root.key, by="user")


# ---- AC-2 ----------------------------------------------------------------------------------------------------


async def test_ac2_merge_tests_the_merge_result_and_passes_the_gate(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    tip = commit(ws, {"leave.py": "def balance():\n    return 12\n"}, "leave")
    m0 = commit(repo, {"holiday.py": "DAYS = 11\n"}, "main moved on")
    log = await use_check(office, tmp_path, "leave.py,holiday.py")

    assert await office.merges.start(root.key, by="user") == {"started": True, "root": root.key}
    await office.merges.wait(root.key)

    head = git(repo, "rev-parse", "main")
    assert git(repo, "rev-list", "--parents", "-n", "1", head).split()[1:] == [m0, tip]
    ran_in = Path(log.read_text(encoding="utf-8"))
    assert ran_in.resolve() != repo.resolve() and ".merge-" in ran_in.name
    assert not ran_in.exists()  # the scratch worktree is gone
    assert git(repo, "status", "--porcelain") == ""
    products = [p for p in await office.tickets.work_products(root.id) if p["kind"] == "merge"]
    assert products and head.startswith(products[-1]["ref"])
    audit = await office.db.fetchall("SELECT outcome FROM audit_log WHERE action = 'git:merge'")
    assert [a["outcome"] for a in audit] == ["merged"]
    text = await comments(office, root)
    assert "Đã merge" in text and "check.py" in text and "🔓 merge" in text
    assert (await office.tickets.get(root.id)).stage != "pr"


# ---- AC-3 ----------------------------------------------------------------------------------------------------


async def test_ac3_failing_test_on_merge_result_keeps_main(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    m0 = commit(repo, {"holiday.py": "DAYS = 11\n"}, "main moved on")
    # passes on the branch tip (no holiday.py there), fails once main's commit is in
    script = tmp_path / "nohol.py"
    script.write_text("import os, sys\nprint('holiday.py breaks leave')\nsys.exit(1 if os.path.exists('holiday.py') else 0)\n", encoding="utf-8")
    project = await office.projects.find("Leave")
    assert project
    await office.projects.configure(project.id, profile={"test": [sys.executable, str(script)]})
    assert subprocess.run([sys.executable, str(script)], cwd=ws).returncode == 0

    await office.merges.start(root.key, by="user")
    await office.merges.wait(root.key)

    assert git(repo, "rev-parse", "main") == m0
    assert git(repo, "status", "--porcelain") == ""
    assert not (office.settings.workspaces_dir / f".merge-{root.key}").exists()
    text = await comments(office, root)
    assert "nohol.py" in text and "holiday.py breaks leave" in text
    assert (await office.tickets.get(root.id)).stage == "pr"
    audit = await office.db.fetchall("SELECT outcome FROM audit_log WHERE action = 'git:merge'")
    assert [a["outcome"] for a in audit] == ["failed"]
    assert (await office.merges.panel(root.key))["ready"]  # the button is back for another try


# ---- AC-4 ----------------------------------------------------------------------------------------------------


async def test_ac4_conflict_keeps_main_and_names_files(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    commit(repo, {"leave.py": "x = 0\n"}, "base")
    git(ws, "merge", "-q", "main")
    commit(ws, {"leave.py": "x = 1\n"}, "branch")
    m0 = commit(repo, {"leave.py": "x = 2\n"}, "main")

    await office.merges.start(root.key, by="user")
    await office.merges.wait(root.key)

    assert git(repo, "rev-parse", "main") == m0
    assert not (office.settings.workspaces_dir / f".merge-{root.key}").exists()
    text = await comments(office, root)
    assert "xung đột" in text and "leave.py" in text
    assert (await office.tickets.get(root.id)).stage == "pr"


# ---- AC-5 ----------------------------------------------------------------------------------------------------


async def test_ac5_main_moved_during_test_is_cancelled(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    flag = tmp_path / "go"
    await use_check(office, tmp_path, "leave.py", flag=str(flag))

    await office.merges.start(root.key, by="user")
    await wait_for(Path(f"{flag}.started"))
    moved = commit(repo, {"other.py": "y = 1\n"}, "someone else")
    flag.write_text("1")
    await office.merges.wait(root.key)

    assert git(repo, "rev-parse", "main") == moved
    assert "nhánh chính đã đổi trong lúc test" in (await comments(office, root)).lower()
    assert (await office.tickets.get(root.id)).stage == "pr"


# ---- AC-6 ----------------------------------------------------------------------------------------------------


async def test_ac6_profile_test_wins_over_test_command(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    other = tmp_path / "test_command_ran"
    shell = f'"{sys.executable}" -c "import pathlib; pathlib.Path(r\'{other}\').write_text(\'1\')"'
    log = await use_check(office, tmp_path, "leave.py", test_command=shell)

    await office.merges.start(root.key, by="user")
    await office.merges.wait(root.key)

    assert log.exists() and not other.exists()
    assert (await office.tickets.get(root.id)).stage != "pr"


async def test_ac6_test_command_runs_when_profile_has_none(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    marker = tmp_path / "test_command_ran"
    project = await office.projects.find("Leave")
    assert project
    shell = f'"{sys.executable}" -c "import pathlib; pathlib.Path(r\'{marker}\').write_text(\'1\')"'
    await office.projects.configure(project.id, test_command=shell)

    result = await office.merges.merge(root.key, by="user")
    assert result["merged"] and marker.exists()


async def test_ac6_merge_without_test_command_says_untested(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    tip = commit(ws, {"leave.py": "x = 1\n"}, "leave")

    await office.merges.start(root.key, by="user")
    await office.merges.wait(root.key)

    assert tip in git(repo, "rev-list", "main")
    assert "không có lệnh test" in await comments(office, root)
    assert (await office.tickets.get(root.id)).stage != "pr"


# ---- AC-7 ----------------------------------------------------------------------------------------------------


@pytest.fixture
def office_client(tmp_path):
    office = Office(Settings(data_dir=tmp_path / "home"), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        seed_db(office.settings.db_path)
        await office.open()
        await office.install_default_positions(skills=False)
        yield
        await office.merges.close()
        await office.db.close()

    app.router.lifespan_context = lifespan
    with TestClient(app, base_url="http://localhost:8765") as c:
        yield office, c


def test_ac7_second_start_is_rejected_while_running(office_client, tmp_path: Path) -> None:
    office, c = office_client
    login(c)
    root, repo, ws = c.portal.call(at_pr, office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    flag = tmp_path / "go"
    c.portal.call(lambda: use_check(office, tmp_path, "leave.py", flag=str(flag)))

    stage = c.get(f"/api/tickets/{root.key}/stage").json()
    assert stage["merge"]["available"] and stage["merge"]["ready"]  # AC-1 through the API the pane uses

    first = c.post(f"/api/tickets/{root.key}/merge/start", headers=HEADERS)
    assert first.status_code == 200, first.text
    second = c.post(f"/api/tickets/{root.key}/merge/start", headers=HEADERS)
    assert second.status_code == 409
    running = c.get(f"/api/tickets/{root.key}/stage").json()["merge"]
    assert running["running"] and not running["ready"]

    flag.write_text("1")
    c.portal.call(office.merges.wait, root.key)
    assert git(repo, "rev-list", "--merges", "--count", "main") == "1"


async def test_ac7_start_twice_in_process_raises_conflict(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    flag = tmp_path / "go"
    await use_check(office, tmp_path, "leave.py", flag=str(flag))
    await office.merges.start(root.key, by="user")
    with pytest.raises(Conflict):
        await office.merges.start(root.key, by="user")
    with pytest.raises(Conflict):
        await office.merges.merge(root.key, by="user")  # the old synchronous API waits its turn too
    flag.write_text("1")
    await office.merges.wait(root.key)
    assert git(repo, "rev-list", "--merges", "--count", "main") == "1"


# ---- AC-8 ----------------------------------------------------------------------------------------------------


def test_ac8_no_ai_tool_can_merge() -> None:
    import actoroffice.tools as tools_pkg
    from actoroffice.tools.registry import ToolDef

    names: list[str] = []
    for info in pkgutil.iter_modules(tools_pkg.__path__):
        module = importlib.import_module(f"actoroffice.tools.{info.name}")
        source = Path(module.__file__ or "").read_text(encoding="utf-8")
        assert "merges." not in source and "MergeService" not in source, info.name
        for value in vars(module).values():
            if isinstance(value, list):
                names += [t.name for t in value if isinstance(t, ToolDef)]
    assert names and not [n for n in names if "merge" in n.lower()]


# ---- checks before merging, stale knowledge ------------------------------------------------------------------


async def test_dirty_project_folder_is_refused_before_starting(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    commit(repo, {"readme.md": "a\n"}, "readme")
    (repo / "readme.md").write_text("changed\n", encoding="utf-8")
    with pytest.raises(MergeError, match="chưa commit"):
        await office.merges.start(root.key, by="user")
    assert not (await office.merges.panel(root.key))["running"]
    text = await comments(office, root)
    assert "Chưa merge được" in text and "chưa commit" in text  # BR-7: the reason is on the timeline
    audit = await office.db.fetchall("SELECT outcome, detail_json FROM audit_log WHERE action = 'git:merge'")
    assert [a["outcome"] for a in audit] == ["failed"] and "chưa commit" in audit[0]["detail_json"]


async def test_failure_after_main_moved_is_not_reported_as_main_unchanged(office: Office, tmp_path: Path, monkeypatch) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    tip = commit(ws, {"leave.py": "x = 1\n"}, "leave")

    async def broken(*_a, **_k) -> None:
        raise RuntimeError("db is gone")

    monkeypatch.setattr(office.tickets, "add_work_product", broken)
    await office.merges.start(root.key, by="user")
    await office.merges.wait(root.key)

    assert tip in git(repo, "rev-list", "main")
    text = await comments(office, root)
    assert "chưa ghi được vào ticket" in text and "nhánh chính giữ nguyên" not in text
    assert (await office.tickets.get(root.id)).stage != "pr"  # main holds the merge: the gate still passes


async def test_gate_failure_after_merge_is_reported(office: Office, tmp_path: Path, monkeypatch) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    tip = commit(ws, {"leave.py": "x = 1\n"}, "leave")

    async def broken(*_a, **_k) -> None:
        raise RuntimeError("gate broke")

    monkeypatch.setattr(office.stages, "external_passed", broken)
    await office.merges.start(root.key, by="user")
    await office.merges.wait(root.key)

    assert tip in git(repo, "rev-list", "main")
    text = await comments(office, root)
    assert "chưa qua được cổng merge" in text and "PR đã merge" in text
    assert not (await office.merges.panel(root.key))["running"]


async def test_leftover_scratch_worktree_is_cleaned_up(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    tip = commit(ws, {"leave.py": "x = 1\n"}, "leave")
    scratch = office.settings.workspaces_dir / f".merge-{root.key}"
    git(repo, "worktree", "add", "-q", "--detach", str(scratch), "main")  # an office that stopped mid-merge
    result = await office.merges.merge(root.key, by="user")
    assert result["merged"] and tip in git(repo, "rev-list", "main") and not scratch.exists()


async def test_knowledge_goes_stale_after_a_button_merge(office: Office, tmp_path: Path) -> None:
    root, repo, ws = await at_pr(office, tmp_path)
    note = await office.knowledge.note(kind="code_note", title="Số ngày phép", content="leave.py", by="Copilot", anchor={"path": "leave.py"})
    commit(ws, {"leave.py": "x = 1\n"}, "leave")
    commit(repo, {"holiday.py": "DAYS = 11\n"}, "main moved on")
    await office.merges.start(root.key, by="user")
    await office.merges.wait(root.key)
    assert (await office.knowledge.get(note.id)).status == "stale"
