"""SI-19: the suite runs in parallel by default, and the shortcuts that make it fast keep tests as strict as before."""

from __future__ import annotations

import ast
import re
import ssl
import tomllib
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

import aiosqlite
import pytest
import yaml

from actoroffice.app import position_template
from actoroffice.channels.telegram import BotApi
from actoroffice.core.http import ssl_context
from actoroffice.domain.work import ProjectAccess
from actoroffice.security.redact import Redactor
from actoroffice.services import projects
from actoroffice.services.access import HttpEnvelope
from actoroffice.store import Database

from . import seed

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
FILES = {"src/app.py": "def total(x):\n    return x\n", "README.md": "svc\n"}


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_ac1_xdist_is_a_locked_dev_dependency_and_on_by_default() -> None:
    assert any(re.match(r"pytest-xdist\b", dep) for dep in pyproject()["dependency-groups"]["dev"])
    assert re.search(r'(?m)^name = "pytest-xdist"\nversion = "\d+\.\d+', (ROOT / "uv.lock").read_text(encoding="utf-8"))
    options = pyproject()["tool"]["pytest"]["ini_options"]
    addopts = options["addopts"]
    assert "-n auto" in (addopts if isinstance(addopts, str) else " ".join(addopts))
    assert options["tmp_path_retention_policy"] == "failed"  # old basetemps are not swept at every session start


# AC-2: a static scan of tests/, so a later test cannot quietly break isolation between xdist workers.
ENV_WRITES = {"chdir", "putenv", "unsetenv"}
SERVERS = {"bind", "start_server", "create_server", "serve", "listen"}
OUTSIDE = re.compile(r"^(/(tmp|home|var|etc|Users)\b|[A-Za-z]:[\\/])")
WRITES = {"write_text", "write_bytes", "mkdir", "touch", "copytree", "copyfile", "copy", "copy2", "rmtree", "unlink"}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        return f"{_dotted(node.func)}()"
    return ""


def _isolation_problems(source: str, name: str) -> list[str]:
    problems: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign | ast.AugAssign | ast.Delete):
            targets = node.targets if isinstance(node, ast.Assign | ast.Delete) else [node.target]
            problems += [f"{name}:{node.lineno} os.environ[...] changed directly" for t in targets if isinstance(t, ast.Subscript) and _dotted(t.value) == "os.environ"]
        if not isinstance(node, ast.Call):
            continue
        func = _dotted(node.func)
        last = func.rsplit(".", 1)[-1]
        if func.startswith("os.") and last in ENV_WRITES or func.startswith("os.environ.") and last in {"update", "setdefault", "pop", "clear", "__setitem__"}:
            problems.append(f"{name}:{node.lineno} {func} outside monkeypatch")
        if func.startswith("tempfile."):
            problems.append(f"{name}:{node.lineno} {func} writes outside tmp_path")
        if last in SERVERS or last == "run" and func.startswith("uvicorn"):
            ports = [kw.value for kw in node.keywords if kw.arg == "port"] + [a for a in node.args[1:] if isinstance(a, ast.Constant) and isinstance(a.value, int)]
            problems += [f"{name}:{node.lineno} {func} on fixed port {p.value}" for p in ports if isinstance(p, ast.Constant) and p.value != 0]
        if last in WRITES or func == "open":
            text = ast.unparse(node)
            literals = [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            if any(OUTSIDE.match(s) for s in literals) or re.search(r"Path\.(home|cwd)\(\)|expanduser", text):
                problems.append(f"{name}:{node.lineno} writes to a fixed path: {text[:80]}")
    return problems


def test_ac2_tests_only_bind_port_zero_and_patch_env_via_monkeypatch() -> None:
    problems = [p for f in sorted(TESTS.glob("*.py")) for p in _isolation_problems(f.read_text(encoding="utf-8"), f.name)]
    assert problems == []


def test_ac2_the_scan_finds_each_kind_of_shared_state() -> None:
    bad = (
        "import os, tempfile\n"
        "os.environ['X'] = '1'\n"
        "os.environ.update(Y='2')\n"
        "os.chdir('..')\n"
        "tempfile.mkdtemp()\n"
        "await asyncio.start_server(h, '127.0.0.1', 8765)\n"
        "uvicorn.run(app, port=8000)\n"
        "Path('C:/build/out.txt').write_text('x')\n"
        "(Path.home() / 'x').mkdir()\n"
    )
    assert len(_isolation_problems(bad, "bad.py")) == 8
    good = (
        "monkeypatch.setenv('X', '1')\n"
        "monkeypatch.chdir(tmp_path)\n"
        "await asyncio.start_server(h, '127.0.0.1', 0)\n"
        "(tmp_path / 'a').write_text('x')\n"
        "x = os.environ.get('AO_OUTPUT_DIR')\n"
        "engine.decide('Read', {'file_path': str(Path.home() / '.ssh')})\n"
    )
    assert _isolation_problems(good, "good.py") == []


async def _schema(path: Path) -> dict:
    async with aiosqlite.connect(path) as conn:
        master = await (await conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name")).fetchall()
        version = (await (await conn.execute("PRAGMA user_version")).fetchone())[0]
        migrations = [r[0] for r in await (await conn.execute("SELECT version FROM schema_migrations ORDER BY version")).fetchall()]
    return {"master": master, "user_version": version, "migrations": migrations}


async def test_ac3_seeded_db_matches_a_freshly_migrated_db(tmp_path) -> None:
    fresh = Database(tmp_path / "fresh" / "office.db")
    await fresh.open()
    await fresh.close()
    seeded = Database(seed.seed_db(tmp_path / "seeded" / "office.db"))
    await seeded.open()  # runs _migrate for real: every migration is already recorded
    await seeded.close()

    expected = await _schema(tmp_path / "fresh" / "office.db")
    assert expected["migrations"] and any(row[0] == "table" for row in expected["master"])
    assert await _schema(tmp_path / "seeded" / "office.db") == expected


async def test_ac3_each_seeded_db_is_its_own_file(tmp_path) -> None:
    first = Database(seed.seed_db(tmp_path / "a.db"))
    await first.open()
    await first.execute("INSERT INTO schema_migrations(version, applied_at) VALUES ('zz-test', 'now')")
    await first.close()
    second = Database(seed.seed_db(tmp_path / "b.db"))
    await second.open()
    assert await second.fetchone("SELECT 1 FROM schema_migrations WHERE version = 'zz-test'") is None
    await second.close()
    assert "zz-test" not in (await _schema(seed.template_db()))["migrations"]
    seed.seed_db(tmp_path / "a.db")  # an existing DB is kept, as on a restart
    assert "zz-test" in (await _schema(tmp_path / "a.db"))["migrations"]


def test_ac4_copied_repo_matches_a_fresh_repo_and_is_independent(tmp_path) -> None:
    fresh = seed.build_repo(tmp_path / "fresh", FILES)
    copy = seed.copy_repo(tmp_path / "copy", FILES)
    other = seed.copy_repo(tmp_path / "other", FILES)
    template = seed.template_repo(FILES)

    for repo in (copy, other):
        assert seed.git(repo, "branch", "--show-current") == "main"
        assert seed.git(repo, "ls-tree", "-r", "HEAD") == seed.git(fresh, "ls-tree", "-r", "HEAD")
        assert seed.git(repo, "log", "--format=%T %s %an <%ae>") == seed.git(fresh, "log", "--format=%T %s %an <%ae>")
        assert seed.git(repo, "status", "--porcelain") == ""
        assert (repo / "src" / "app.py").read_text(encoding="utf-8") == FILES["src/app.py"]

    head = seed.git(template, "rev-parse", "HEAD")
    (copy / "src" / "app.py").write_text("def total(x):\n    return 2 * x\n", encoding="utf-8")
    seed.git(copy, "add", "-A")
    seed.git(copy, *seed.IDENT, "commit", "-q", "-m", "change")
    seed.git(copy, "switch", "-q", "-c", "t/x")
    assert seed.git(template, "rev-parse", "HEAD") == head == seed.git(other, "rev-parse", "HEAD")
    assert seed.git(template, "status", "--porcelain") == "" and seed.git(template, "branch", "--format=%(refname:short)") == "main"
    assert (template / "src" / "app.py").read_text(encoding="utf-8") == FILES["src/app.py"]


def test_ac4_an_empty_repo_copy_has_one_empty_commit(tmp_path) -> None:
    repo = seed.copy_repo(tmp_path / "empty")
    assert seed.git(repo, "ls-tree", "-r", "HEAD") == "" and seed.git(repo, "log", "--format=%s") == "init"
    assert seed.git(repo, "status", "--porcelain") == ""


def test_position_templates_are_parsed_once_and_copied() -> None:
    text = (files("actoroffice.templates.positions") / "engineer.yaml").read_text(encoding="utf-8")
    first = position_template(text)
    assert first == yaml.safe_load(text)
    first["name"] = "changed"
    first.setdefault("tools", []).append("x")
    assert position_template(text) == yaml.safe_load(text)  # a caller's changes never reach the next caller
    assert position_template("name: other\n") == {"name": "other"}


def test_a_worktree_project_asks_git_once_for_an_existing_repo(tmp_path, monkeypatch) -> None:
    calls: list[Path] = []
    real = projects.git_branch
    monkeypatch.setattr(projects, "git_branch", lambda folder: calls.append(folder) or real(folder))
    service = projects.ProjectService(SimpleNamespace(), SimpleNamespace(), forbidden=[])

    repo = seed.copy_repo(tmp_path / "repo")
    assert service._validated(str(repo), ProjectAccess.WORKTREE)["main_branch"] == "main" and len(calls) == 1
    calls.clear()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert service._validated(str(empty), ProjectAccess.WORKTREE)["main_branch"] == "main" and len(calls) == 2  # after git init
    calls.clear()
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain" / "a.txt").write_text("x", encoding="utf-8")
    with pytest.raises(projects.ProjectError):
        service._validated(str(tmp_path / "plain"), ProjectAccess.WORKTREE)
    assert len(calls) == 1


def _client_ssl(client) -> ssl.SSLContext:
    return client._transport._pool._ssl_context


def test_ac5_office_http_clients_keep_tls_verification(tmp_path) -> None:
    office = SimpleNamespace(settings=SimpleNamespace(data_dir=tmp_path), redact=Redactor())
    resource = SimpleNamespace(name="payments", config={"base_url": "https://payments.example"})
    envelope = HttpEnvelope(office, SimpleNamespace(), resource)
    bot = BotApi("123:abc")

    shared = ssl_context()
    assert ssl_context() is shared
    for ctx in (_client_ssl(envelope.client), _client_ssl(bot._client)):
        assert ctx is shared
        assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname is True
        assert ctx.get_ca_certs() or ctx.cert_store_stats()["x509_ca"] > 0  # trusted roots are loaded
