"""SI-14: ruff is a dev dependency, configured for this code base, clean, and runnable as the repo:lint check."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import tomllib
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.work import ProjectAccess
from actoroffice.engine.gate import ENV_DUMP
from actoroffice.services.projects import ProjectError

from .fakes import FakeRuntime
from .seed import seed_db
from .test_quality import build_ticket, run_checks

ROOT = Path(__file__).resolve().parents[1]
LINT = "uv run ruff check src tests"


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    yield o
    await o.close()


def ruff() -> str:
    found = shutil.which("ruff", path=str(Path(sys.executable).parent))
    assert found, "ruff is not installed next to the test interpreter (uv sync installs the dev group)"
    return found


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_ac1_ruff_is_a_locked_dev_dependency() -> None:
    assert any(re.match(r"ruff\b", dep) for dep in pyproject()["dependency-groups"]["dev"])
    assert re.search(r'(?m)^name = "ruff"$', (ROOT / "uv.lock").read_text(encoding="utf-8"))
    version = subprocess.run([ruff(), "--version"], capture_output=True, text=True)
    assert version.returncode == 0 and version.stdout.startswith("ruff ")


def test_ac2_ruff_config_matches_the_agreed_rules() -> None:
    config = pyproject()["tool"]["ruff"]
    assert config["line-length"] == 200
    assert config["lint"]["select"] == ["E", "W", "F", "I", "B", "SIM", "C4", "UP"]
    assert config["lint"]["per-file-ignores"] == {"tests/*": ["F811"]}
    assert "format" not in config


def test_ac3_ruff_check_src_and_tests_is_clean() -> None:
    result = subprocess.run([ruff(), "check", "src", "tests"], cwd=ROOT, capture_output=True, encoding="utf-8")
    assert result.returncode == 0, result.stdout[-3000:]


def test_ac4_split_env_dump_regex_is_the_same_pattern() -> None:
    before = (
        r"^\s*(env|printenv|set|export\s+-p|declare\s+-x|Get-ChildItem\s+env:|gci\s+env:|dir\s+env:)\s*(\||$)"
        r"|\becho\s+\$\w*(DSN|TOKEN|SECRET|PASSWORD|KEY)\b|\bcat\s+/proc/(self|\d+)/environ"
    )
    assert ENV_DUMP.pattern == before and ENV_DUMP.flags & re.IGNORECASE
    assert ENV_DUMP.search("printenv | grep X") and ENV_DUMP.search("echo $DB_PASSWORD") and not ENV_DUMP.search("echo hello")


async def test_ac4_loki_query_keeps_its_logql(office) -> None:
    seen: list[str] = []

    def loki(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["query"])
        return httpx.Response(200, json={"data": {"result": []}})

    office.logs.transport = httpx.MockTransport(loki)
    resource = await office.access.add_resource("app-logs", "logs", env="local", config={"store": "loki", "url": "http://loki.local", "service_label": "svc"})
    until = datetime.now(UTC)
    await office.logs.fetch(resource, service="orders", query='bo"om', since=until - timedelta(hours=1), until=until, level="ERROR")
    assert seen == ['{svc="orders"} |= "boom" |~ "(?i)ERROR"']


# The .py files SI-14 changed (git diff 1665252...88b9166): each must have had a finding under the new config.
BASE = "1665252"
SI14_FILES = {
    "src/actoroffice/app.py", "src/actoroffice/channels/telegram.py", "src/actoroffice/engine/executor.py",
    "src/actoroffice/engine/gate.py", "src/actoroffice/engine/scheduler.py", "src/actoroffice/services/logs.py",
    "src/actoroffice/services/org.py", "src/actoroffice/tools/chat_tools.py", "src/actoroffice/tools/infra_tools.py",
    "src/actoroffice/tools/office_tools.py", "src/actoroffice/tools/stage_tools.py",
    "tests/fake_acp_agent.py", "tests/test_atlassian.py", "tests/test_chat.py", "tests/test_enterprise.py",
    "tests/test_flow_fixes.py", "tests/test_infra_tools.py", "tests/test_jira.py", "tests/test_knowledge.py",
    "tests/test_permission_modes.py", "tests/test_permissions_projects.py", "tests/test_quality.py", "tests/test_quick_workflow.py",
}


def test_ac5_every_file_changed_for_lint_had_a_finding_at_the_base_commit(tmp_path) -> None:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", BASE, *sorted(SI14_FILES)], capture_output=True)
    assert archive.returncode == 0, f"base commit {BASE} is not in this clone: {archive.stderr.decode(errors='replace')[:300]}"
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(tmp_path, filter="data")
    shutil.copy(ROOT / "pyproject.toml", tmp_path / "pyproject.toml")
    result = subprocess.run([ruff(), "check", "src", "tests", "--output-format", "json", "--no-cache"], cwd=tmp_path, capture_output=True, encoding="utf-8")
    flagged = {Path(f["filename"]).relative_to(tmp_path).as_posix() for f in json.loads(result.stdout)}
    assert SI14_FILES - flagged == set()


def test_ac6_every_noqa_names_its_rule() -> None:
    bare = [
        f"{path.relative_to(ROOT)}:{no}"
        for folder in ("src", "tests")
        for path in (ROOT / folder).rglob("*.py")
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.search(r"#\s*noqa(?!:\s*[A-Z]+\d+)", line, re.IGNORECASE)
    ]
    assert bare == []


def test_ac7_readme_documents_the_lint_command() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    lint = readme.split("### Lint", 1)
    assert len(lint) == 2 and LINT in lint[1]


async def test_ac8_repo_lint_check_passes_on_this_repo(office) -> None:
    # No project path: the checkout may hold the office data dir (TEMP inside an office worktree), which
    # projects._validated rightly refuses; the check runs on ROOT through the workspace argument.
    project = await office.projects.create("actor-office", path=None, access=ProjectAccess.READ_ONLY)
    # The profile command is `uv run ruff check src tests`; the test calls the same ruff directly (no nested uv).
    await office.projects.configure(project.id, profile={"lint": [ruff(), *LINT.split()[3:]]})
    result, _ = await run_checks(office, await build_ticket(office, project), ROOT, "repo:lint")
    assert result.status == "passed", result.summary


async def test_ac8_repo_lint_check_fails_on_a_violation(office, tmp_path) -> None:
    repo = tmp_path / "svc"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    (repo / "src" / "app.py").write_text("import os\n\n\ndef total(x):\n    return x\n", encoding="utf-8")
    project = await office.projects.create("svc", path=str(repo), access=ProjectAccess.READ_ONLY)
    await office.projects.configure(project.id, profile={"lint": [ruff(), *LINT.split()[3:]]})
    result, _ = await run_checks(office, await build_ticket(office, project), repo, "repo:lint")
    assert result.status == "failed" and result.data["exit"] == 1


async def test_si37_ac1_lint_check_runs_on_a_repo_that_holds_the_data_dir(tmp_path) -> None:
    # Like a run of the office: TEMP (so tmp_path and the data dir) inside the checkout under test
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "tests").mkdir()
    shutil.copy(ROOT / "pyproject.toml", repo / "pyproject.toml")
    (repo / "src" / "app.py").write_text("def total(x):\n    return x\n", encoding="utf-8")
    o = Office(Settings(data_dir=repo / ".ao-tmp" / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    try:
        with pytest.raises(ProjectError, match="dữ liệu của văn phòng"):  # _validated still refuses it
            await o.projects.create("svc", path=str(repo), access=ProjectAccess.READ_ONLY)
        project = await o.projects.create("svc", path=None, access=ProjectAccess.READ_ONLY)
        await o.projects.configure(project.id, profile={"lint": [ruff(), *LINT.split()[3:]]})
        result, _ = await run_checks(o, await build_ticket(o, project), repo, "repo:lint")
        assert result.status == "passed", result.summary
    finally:
        await o.close()


@pytest.mark.parametrize("missing", [{}, {"test": "pytest"}])
async def test_ac8_without_a_lint_command_the_check_is_skipped(office, missing) -> None:
    project = await office.projects.create("bare", path=None, access=ProjectAccess.READ_ONLY)
    await office.projects.configure(project.id, profile=missing)
    result, _ = await run_checks(office, await build_ticket(office, project), ROOT, "repo:lint")
    assert result.status == "skipped"
