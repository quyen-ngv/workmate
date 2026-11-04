"""Coverage for the repo (SI-32): pytest-cov under xdist, the coverage_changed_lines check on a real report,
the git-ignored report paths and `project set --profile` merging keys."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest

from actoroffice.app import Office
from actoroffice.cli_team import cmd_project
from actoroffice.config import Settings
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.work import ProjectAccess
from actoroffice.services.checks import check_coverage_changed_lines

from .fakes import FakeRuntime
from .seed import IDENT, build_repo, copy_repo, git, seed_db

ROOT = Path(__file__).resolve().parents[1]

CALC = """def base():
    return 0


def one():
    return 1


def two():
    return 2


def unused():
    return 3
"""

TEST = """import os
from pathlib import Path

from pkg.calc import {name}


def test_{name}():
    Path(os.environ["SEEN_DIR"], "{name}-" + os.environ.get("PYTEST_XDIST_WORKER", "main")).touch()
    assert {name}() == {value}
"""


@pytest.fixture(scope="module")
def sample_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, list[str]]:
    """A repo whose branch adds two functions and one test file each, tested with `-n 2 --cov` like the README says.
    Returns the repo and the "<test>-<worker>" marks the tests left. One run (~9s on Windows) for the module."""
    tmp_path = tmp_path_factory.mktemp("cov")
    repo = build_repo(tmp_path / "sample", {"pytest.ini": "[pytest]\npythonpath = src\n", "src/pkg/__init__.py": "", "src/pkg/calc.py": "def base():\n    return 0\n"})
    git(repo, "checkout", "-q", "-b", "t")
    (repo / "src" / "pkg" / "calc.py").write_text(CALC, encoding="utf-8")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_one.py").write_text(TEST.format(name="one", value=1), encoding="utf-8")
    (repo / "tests" / "test_two.py").write_text(TEST.format(name="two", value=2), encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, *IDENT, "commit", "-q", "-m", "one and two")
    seen = tmp_path / "seen"
    seen.mkdir()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COV_CORE_", "COVERAGE_", "PYTEST_"))}
    env["SEEN_DIR"] = str(seen)
    env["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"  # load only the two plugins under test: starts faster
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "xdist", "-p", "pytest_cov", "-p", "no:cacheprovider", "-n", "2", "--cov=pkg", "--cov-report=lcov:.cov/lcov.info", "tests"],
        cwd=repo, env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return repo, sorted(p.name for p in seen.iterdir())


def test_ac1_xdist_workers_are_combined_in_the_lcov_report(sample_run: tuple[Path, list[str]]) -> None:
    repo, seen = sample_run
    workers = {mark.split("-", 1)[1] for mark in seen}
    assert workers == {"gw0", "gw1"}, seen  # one test on each worker
    report = (repo / ".cov" / "lcov.info").read_text(encoding="utf-8")
    assert "SF:src/pkg/calc.py" in report.replace("\\", "/")
    hits = dict(line[3:].split(",")[:2] for line in report.splitlines() if line.startswith("DA:"))
    assert int(hits["6"]) > 0 and int(hits["10"]) > 0  # `return 1` (worker A) and `return 2` (worker B)
    assert int(hits["14"]) == 0  # `return 3` is never called


async def test_ac2_coverage_changed_lines_measures_a_real_report(sample_run: tuple[Path, list[str]]) -> None:
    repo, _ = sample_run
    project = SimpleNamespace(main_branch="main", profile={"coverage": {"kind": "lcov", "path": ".cov/lcov.info", "min": 80}})
    result = await check_coverage_changed_lines(SimpleNamespace(workspace=repo, project=project))
    # changed executable lines: three `def` (run at import), `return 1`, `return 2` covered; `return 3` not
    assert result.status == "passed", result.summary
    assert "83%" in result.summary and "(5/6" in result.summary
    project.profile["coverage"]["min"] = 90
    result = await check_coverage_changed_lines(SimpleNamespace(workspace=repo, project=project))
    assert result.status == "failed" and "83%" in result.summary


def test_ac4_coverage_files_are_git_ignored() -> None:
    paths = [".cov/lcov.info", ".coverage", ".coverage.gw0"]
    proc = subprocess.run(["git", "-C", str(ROOT), "check-ignore", *paths], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == paths


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home"), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    yield o
    await o.close()


def set_args(profile: str) -> argparse.Namespace:
    return argparse.Namespace(action="set", name="svc", team=None, kind=None, repo_url=None, profile=profile)


async def test_ac5_project_set_profile_merges_top_level_keys(office: Office, tmp_path: Path) -> None:
    repo = copy_repo(tmp_path / "svc")
    project = await office.projects.create("svc", path=str(repo), access=ProjectAccess.WORKTREE)
    await office.projects.configure(project.id, profile={"test": ["uv", "run", "pytest"], "lint": ["uv", "run", "ruff", "check"]})
    coverage = {"kind": "lcov", "path": ".cov/lcov.info", "min": 80}
    await cmd_project(office, set_args(json.dumps({"coverage": coverage})))
    profile = (await office.projects.get(project.id)).profile
    assert profile == {"test": ["uv", "run", "pytest"], "lint": ["uv", "run", "ruff", "check"], "coverage": coverage}
    await cmd_project(office, set_args('{"coverage": null}'))
    assert (await office.projects.get(project.id)).profile == {"test": ["uv", "run", "pytest"], "lint": ["uv", "run", "ruff", "check"]}
    with pytest.raises(OfficeError):
        await cmd_project(office, set_args('["coverage"]'))
