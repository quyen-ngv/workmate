"""SI-28: each ticket run gets its own temp folder (TMPDIR/TEMP/TMP) inside the workspace, so temp files need no click."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.org import Position, PositionPolicy
from actoroffice.engine.workspace import ticket_tmp
from actoroffice.security import shell
from actoroffice.security.policy import Decision, PolicyEngine, ToolCallContext
from actoroffice.security.shell import TICKET_TMP

from .fakes import FakeRuntime
from .seed import copy_repo, seed_db
from .test_prompt_pack import _stage

TMP_VARS = ("TMPDIR", "TEMP", "TMP")


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, corrective_turns=0), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    yield o
    await o.close()


async def test_ac1_ticket_run_env_points_temp_vars_to_the_ticket_tmp(office, runtime):
    pos = await office.org.find_position("writer")
    actor = await office.org.hire(pos.id, "Lan")
    await office.tickets.create("Write intro", assignee_actor_id=actor.id)
    await office.scheduler.wait_idle(timeout=10)
    spec = runtime.specs[0]
    tmp = Path(spec.cwd) / TICKET_TMP
    assert {name: spec.env[name] for name in TMP_VARS} == dict.fromkeys(TMP_VARS, str(tmp))
    assert tmp.is_dir()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout


async def test_ac4_ticket_tmp_is_excluded_from_git_once(tmp_path):
    repo = copy_repo(tmp_path / "repo", {"a.txt": "a\n"})
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", "-b", "t/x", str(worktree))
    env = await ticket_tmp(worktree)
    (Path(env["TMPDIR"]) / "x.txt").write_text("x", encoding="utf-8")
    assert _git(worktree, "status", "--porcelain") == ""
    await ticket_tmp(worktree)
    exclude = Path(_git(worktree, "rev-parse", "--path-format=absolute", "--git-path", "info/exclude").strip())
    assert exclude.read_text(encoding="utf-8").splitlines().count(f"{TICKET_TMP}/") == 1


async def test_ac4_plain_folder_gets_a_tmp_without_git(tmp_path):
    env = await ticket_tmp(tmp_path)
    assert Path(env["TEMP"]).is_dir() and not (tmp_path / ".git").exists()


@pytest.fixture
def ws(tmp_path) -> Path:
    path = tmp_path / "ws"
    path.mkdir()
    return path.resolve()


def _decide(ws: Path, command: str, tool: str = "Bash") -> Decision:
    ctx = ToolCallContext(position=Position(id="p", name="dev", title="Dev", policy=PositionPolicy(bash_allow=["uv run *"])), workspace=ws)
    return PolicyEngine(protected_dirs=[]).decide(tool, {"command": command}, ctx).decision


@pytest.mark.parametrize(
    "command",
    [
        "uv run main.py > $TMPDIR/out.txt",
        'uv run main.py > "${TMPDIR}/out.txt"',
        "uv run main.py 2> $TEMP/err.txt",
        "uv run main.py > $TMP",
        "mkdir -p $TMPDIR/run",
        "cp a.txt $TMPDIR/b.txt && uv run main.py $TMPDIR/b.txt",
    ],
)
def test_ac2_writing_to_the_ticket_tmp_needs_no_click(ws, command):
    assert _decide(ws, command) == Decision.ALLOW


def test_ac2_absolute_path_and_powershell_need_no_click(ws):
    assert _decide(ws, f"uv run main.py > {(ws / TICKET_TMP / 'x.txt').as_posix()}") == Decision.ALLOW
    assert _decide(ws, "uv run main.py > $env:TEMP/x.txt", tool="PowerShell") == Decision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "uv run main.py > /tmp/x.txt",
        # the line sets or drops the variable itself: its value is no longer the ticket's folder
        "TMPDIR=/etc uv run main.py > $TMPDIR/x",
        "export TMPDIR=/etc && uv run main.py > $TMPDIR/x",
        "unset TMPDIR; uv run main.py > $TMPDIR/x",
        "read TMP && uv run main.py > $TMP/x",
        "uv run main.py > ${TMPDIR:=/etc}/x",
        # bash leaves these as text ($TMPDIR literally), not the ticket's folder
        "uv run main.py > '$TMPDIR'/x",
        "uv run main.py > \\$TMPDIR/x",
        "uv run main.py > $'$TMPDIR'/x",
        # another variable, or a path that climbs out of the folder
        "uv run main.py > $TMPDIRX/x",
        "uv run main.py > $TMPDIR/../../x",
        "mkdir $TMPDIR/../../x",
        # a comment or a nested shell does not hide a reassignment
        "uv run main.py > $TMPDIR/x # TMPDIR=/z",
        "bash -c 'uv run main.py > $TMPDIR/x'",
    ],
)
def test_ac3_writing_outside_or_with_a_changed_variable_still_asks(ws, command):
    assert _decide(ws, command) == Decision.ASK


@pytest.mark.parametrize(
    ("command", "dialect"),
    [
        # a name built at run time (checked on Git Bash: each sets TMPDIR)
        ("printf -v TM''PDIR /etc; uv run main.py > $TMPDIR/x", "sh"),
        ("printf -v TM\\PDIR /etc; uv run main.py > $TMPDIR/x", "sh"),
        ("n=TM; n+=PDIR; printf -v $n /etc; uv run main.py > $TMPDIR/x", "sh"),
        ("command -p printf -v TMPDIR /etc; uv run main.py > $TMPDIR/x", "sh"),
        ("read -r TM\\PDIR < a.txt; uv run main.py > $TMPDIR/x", "sh"),
        ("declare TM''PDIR=/etc; uv run main.py > $TMPDIR/x", "sh"),
        ("eval TM''PDIR=/etc; uv run main.py > $TMPDIR/x", "sh"),
        ("wait -p TM''PDIR; uv run main.py > $TMPDIR/x", "sh"),
        ("exec {TM''PDIR}>a.txt; uv run main.py > $TMPDIR/x", "sh"),
        # a sourced file or a nested shell may set it
        ("source env.sh; uv run main.py > $TMPDIR/x", "sh"),
        (". ./env.sh; uv run main.py > $TMPDIR/x", "sh"),
        ("sh -c 'TMPDIR=/etc'; uv run main.py > $TMPDIR/x", "sh"),
        # quotes inside ${…}, substitution, multi-line, backslash-newline
        ("uv run main.py > ${TM''PDIR}/x", "sh"),
        ("uv run main.py > $(echo $TMPDIR)/x", "sh"),
        ("uv run main.py > `echo /etc`/$TMPDIR", "sh"),
        ("TMPDIR=/etc\nuv run main.py > $TMPDIR/x", "sh"),
        ("export \\\nTMPDIR=/etc; uv run main.py > $TMPDIR/x", "sh"),
        ("uv run main.py > $TMPDIR/$HOME/x", "sh"),
        # PowerShell: compound assignment, the env: drive, the bash name
        ("$env:TEMP+='/../..'; uv run main.py > $env:TEMP/x", "ps"),
        ("$env:TEMP ??= 'C:/'; uv run main.py > $env:TEMP/x", "ps"),
        ("$env:TEMP = 'C:/'; uv run main.py > $env:TEMP/x", "ps"),
        ("Set-Item env:TE''MP C:/; uv run main.py > $env:TEMP/x", "ps"),
        ("Set-Location e''nv:; uv run main.py > $env:TEMP/x", "ps"),
        ("uv run main.py > $TMPDIR/x", "ps"),
    ],
)
def test_ac3_a_line_that_may_change_the_variable_still_asks(ws, command, dialect):
    parts = shell.split(command, dialect)
    assert parts is None or shell.tmp_dir(command, parts, ws, dialect) is None
    assert _decide(ws, command, tool="PowerShell" if dialect == "ps" else "Bash") == Decision.ASK


def test_ac5_build_guides_say_use_tmpdir_not_tmp():
    for template_name in ("code", "quick"):
        assert "Temp files: use $TMPDIR" in _stage(template_name, "build") and "not /tmp" in _stage(template_name, "build")
        assert "$TMPDIR" not in _stage(template_name, "pr")
