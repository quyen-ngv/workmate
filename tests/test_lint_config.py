"""SI-14: ruff is a dev dependency, configured for this code base, clean, and runnable as the repo:lint check."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
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

from .fakes import FakeRuntime
from .test_quality import build_ticket, run_checks

ROOT = Path(__file__).resolve().parents[1]
LINT = "uv run ruff check src tests"


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
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
    result = subprocess.run([ruff(), "check", "src", "tests"], cwd=ROOT, capture_output=True, text=True)
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
    project = await office.projects.create("actor-office", path=str(ROOT), access=ProjectAccess.READ_ONLY)
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


@pytest.mark.parametrize("missing", [{}, {"test": "pytest"}])
async def test_ac8_without_a_lint_command_the_check_is_skipped(office, missing) -> None:
    project = await office.projects.create("bare", path=str(ROOT), access=ProjectAccess.READ_ONLY)
    await office.projects.configure(project.id, profile=missing)
    result, _ = await run_checks(office, await build_ticket(office, project), ROOT, "repo:lint")
    assert result.status == "skipped"
