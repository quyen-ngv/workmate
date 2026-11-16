"""A project's own commands on a company network: the environment they get (proxy, package mirror, CA, the
project's own variables), which of them run without asking, the old `test_command`, `repo:setup`, and the form."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi import HTTPException

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.core import http as core_http
from actoroffice.domain.stage import default_templates
from actoroffice.domain.work import Project, split_command
from actoroffice.engine.gate import _profile_commands
from actoroffice.services import checks
from actoroffice.services.checks import CheckContext, ProfileError, check_profile, project_env
from actoroffice.services.docflow import pack_templates
from actoroffice.web import team_api

from .fakes import FakeRuntime
from .seed import copy_repo, seed_db
from .test_quality import build_ticket, repo_project, run_checks
from .test_web import HEADERS, client, login  # noqa: F401  (fixture re-export)

PY = sys.executable
PROXY_PASSWORD = "Pr0xy-Pa55word"


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    yield o
    await o.close()


def py(code: str) -> list[str]:
    return [PY, "-c", code]


# ---- A: the environment ------------------------------------------------------------------------------------------


async def test_proxy_mirror_and_tool_variables_reach_the_projects_commands(office: Office, monkeypatch) -> None:
    kept = {
        "HTTPS_PROXY": "http://proxy.acme.test:8080",
        "NO_PROXY": "localhost,.acme.test",
        "MAVEN_OPTS": "-Xmx1g",
        "NPM_CONFIG_REGISTRY": "https://nexus.acme.test/repository/npm/",
        "PIP_INDEX_URL": "https://nexus.acme.test/repository/pypi/simple",
        "UV_DEFAULT_INDEX": "https://nexus.acme.test/repository/pypi/simple",
        "GOPROXY": "https://nexus.acme.test/repository/go",
        "GRADLE_USER_HOME": "/opt/gradle-home",
        "CURL_CA_BUNDLE": "/etc/acme-ca.pem",
        "NUGET_PACKAGES": "/opt/nuget",
    }
    for name, value in kept.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("http_proxy", "http://proxy.acme.test:3128")  # curl and pip read the lower-case name
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-the-build")
    monkeypatch.setenv("ACTOROFFICE_SECRET_THING", "nope")
    env = checks.test_env(office)
    assert {name: env.get(name) for name in kept} == kept
    assert env.get("http_proxy" if sys.platform != "win32" else "HTTP_PROXY") == "http://proxy.acme.test:3128"
    assert "ANTHROPIC_API_KEY" not in env and "ACTOROFFICE_SECRET_THING" not in env
    assert env["CI"] == "1" and "PATH" in {k.upper() for k in env}


async def test_the_company_ca_is_given_to_node_and_python_tools_unless_the_machine_set_it(office: Office, monkeypatch, tmp_path) -> None:
    bundle = str(tmp_path / "acme-ca.pem")
    monkeypatch.setattr(core_http, "_ca_bundle", bundle)
    for name in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        monkeypatch.delenv(name, raising=False)
    env = checks.test_env(office)
    assert (env["NODE_EXTRA_CA_CERTS"], env["SSL_CERT_FILE"], env["REQUESTS_CA_BUNDLE"]) == (bundle, bundle, bundle)

    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/machine/node.pem")
    monkeypatch.setenv("SSL_CERT_FILE", "/machine/ssl.pem")
    env = checks.test_env(office)
    assert (env["NODE_EXTRA_CA_CERTS"], env["SSL_CERT_FILE"], env["REQUESTS_CA_BUNDLE"]) == ("/machine/node.pem", "/machine/ssl.pem", bundle)

    monkeypatch.setattr(core_http, "_ca_bundle", None)
    monkeypatch.delenv("SSL_CERT_FILE")
    env = checks.test_env(office)
    assert "SSL_CERT_FILE" not in env and "REQUESTS_CA_BUNDLE" not in env, "no bundle configured: nothing is invented"


@pytest.mark.parametrize("name", ["NEXUS_TOKEN", "npm_secret", "DB_PASSWORD", "SIGNING_KEY", "AWS_CREDENTIALS"])
def test_a_profile_variable_that_looks_like_a_secret_is_refused_when_saved(name: str) -> None:
    with pytest.raises(ProfileError) as refused:
        check_profile({"env": {"MAVEN_OPTS": "-Xmx2g", name: "v4lue-of-it"}})
    message = str(refused.value)
    assert name in message and "môi trường của máy" in message and "kết nối" in message and "v4lue-of-it" not in message


def test_profile_env_and_timeouts_must_be_plain() -> None:
    check_profile({"env": {"MAVEN_OPTS": "-Xmx2g", "SPRING_PROFILES_ACTIVE": "ci"}, "timeouts": {"test": 1800, "setup": 600}, "test": ["mvn", "test"]})
    for bad in ({"env": {"A": 1}}, {"env": {"A B": "x"}}, {"env": ["A=1"]}, {"env": {"GIT_CEILING_DIRECTORIES": "/"}}, {"timeouts": {"test": 0}}, {"timeouts": {"test": "x"}}):
        with pytest.raises(ProfileError):
            check_profile(bad)


async def test_project_env_adds_the_profiles_variables_and_never_a_stored_secret(office: Office) -> None:
    project = Project(id="p", name="svc", profile={"env": {"MAVEN_OPTS": "-Xmx2g", "NEXUS_TOKEN": "stored-before-the-rule", "GIT_CEILING_DIRECTORIES": "/"}})
    env = project_env(office, project)
    assert env["MAVEN_OPTS"] == "-Xmx2g" and "NEXUS_TOKEN" not in env
    assert env["GIT_CEILING_DIRECTORIES"] == str(office.settings.workspaces_dir.resolve())
    assert project_env(office, None) == checks.test_env(office)


async def test_a_proxy_password_never_shows_in_a_checks_output(office: Office, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HTTPS_PROXY", f"http://svc-build:{PROXY_PASSWORD}@proxy.acme.test:8080")
    show = "import os; print('proxy', os.environ['HTTPS_PROXY'], 'opts', os.environ['MAVEN_OPTS'], 'mirror', os.environ['ALL_PROXY'])"
    profile = {"test": py(show), "env": {"MAVEN_OPTS": "-Xmx2g", "ALL_PROXY": "socks5://bob:pw1@10.0.0.1:1080"}}
    project = await repo_project(office, tmp_path, profile=profile)
    ticket = await office.tickets.get(await build_ticket(office, project))
    stage = (await office.stages.template_of(ticket)).stage("build")
    srun = await office.stages.ensure_run(ticket)
    ctx = CheckContext(office, ticket, stage, srun, Path(project.path), project, "run_proxy")

    result = await office.stages.checks._repo("repo:test", ctx)

    assert result.status == "passed" and result.output_ref
    saved = (office.settings.runs_dir / "run_proxy" / result.output_ref).read_text(encoding="utf-8")
    assert "proxy.acme.test:8080" in saved and "opts -Xmx2g" in saved, "the command got the proxy and the project's variable"
    assert PROXY_PASSWORD not in saved and "bob:pw1" not in saved, saved


# ---- B: commands that run without asking -------------------------------------------------------------------------


def test_auto_allowed_commands_cover_every_form_and_never_a_setting(monkeypatch) -> None:
    profile = {
        "setup": "npm ci",  # a string
        "build": [["npm", "run", "lint:types"], ["npm", "run", "build"]],  # a list of commands
        "test": ["mvn", "-q", "test"],  # one argv
        "lint": ["ruff", "check"],
        "run": ["npm", "start"],
        "os": {"windows": {"test": ["mvn.cmd", "-q", "test"]}, "linux": {"test": ["./mvnw", "-q", "test"]}, "macos": {"test": ["./mvnw", "-q", "test"]}},
        "ports": ["8080", "5432"],
        "logs": ["target/app.log"],
        "test_glob": ["**/*Test.java"],
        "env": {"MAVEN_OPTS": "-Xmx2g"},
        "timeouts": {"test": 1800},
        "ui": {"start": ["npm", "run", "dev"], "url": "http://127.0.0.1:{port}"},
    }
    project = Project(id="p", name="svc", profile=profile)
    monkeypatch.setattr(checks, "_platform_key", lambda: "linux")
    assert _profile_commands(project) == ["npm ci", "npm run lint:types", "npm run build", "./mvnw -q test", "ruff check", "npm start"]
    monkeypatch.setattr(checks, "_platform_key", lambda: "windows")
    assert "mvn.cmd -q test" in _profile_commands(project) and "./mvnw -q test" not in _profile_commands(project)
    assert _profile_commands(None) == [] and _profile_commands(Project(id="p", name="svc")) == []


# ---- C: test_command ---------------------------------------------------------------------------------------------


def test_the_old_test_command_is_the_profiles_test_when_it_has_none() -> None:
    old = Project(id="p", name="svc", test_command='pytest -q "tests/unit tests"')
    assert old.profile["test"] == ["pytest", "-q", "tests/unit tests"]
    assert _profile_commands(old) == ["pytest -q tests/unit tests"]
    both = Project(id="p", name="svc", test_command="pytest -q", profile={"test": ["mvn", "test"], "lint": ["ruff"]})
    assert both.profile == {"test": ["mvn", "test"], "lint": ["ruff"]}, "the profile's own test wins"
    assert Project.model_validate_json(old.model_dump_json()).profile == old.profile
    assert "test" not in Project(id="p", name="svc", test_command="  ").profile


async def test_the_test_check_runs_the_old_test_command(office: Office, tmp_path) -> None:
    project = await repo_project(office, tmp_path)
    marker = tmp_path / "ran.txt"
    await office.projects.configure(project.id, test_command=f'"{PY}" -c "import pathlib; pathlib.Path(r\'{marker}\').write_text(\'1\')"')
    result, _ = await run_checks(office, await build_ticket(office, project), Path(project.path), "repo:test")
    assert result.status == "passed" and marker.exists()


# ---- D: repo:setup -----------------------------------------------------------------------------------------------


def test_setup_is_declared_right_before_build_in_the_code_flows() -> None:
    templates = default_templates()
    for name in ("code", "quick"):
        build = templates[name].stage("build").checks
        assert build.index("repo:setup") + 1 == build.index("repo:build"), name
    task = {s.id: s for s in pack_templates()["task"].stages}
    assert task["build"].checks.index("repo:setup") + 1 == task["build"].checks.index("repo:build"), "the pack's task flow reuses the code build"


async def test_setup_runs_before_build_with_its_own_timeout_and_is_skipped_when_absent(office: Office, tmp_path) -> None:
    fetched = "import pathlib; pathlib.Path('deps.marker').write_text('1')"
    needs = "import pathlib, sys; sys.exit(0 if pathlib.Path('deps.marker').exists() else 3)"
    project = await repo_project(office, tmp_path, profile={"setup": py(fetched), "build": py(needs)})
    ticket_id = await build_ticket(office, project)
    ticket = await office.tickets.get(ticket_id)
    stage = (await office.stages.template_of(ticket)).stage("build")
    srun = await office.stages.ensure_run(ticket)
    order = [c for c in stage.checks if c in ("repo:setup", "repo:build")]
    assert order == ["repo:setup", "repo:build"]
    results = await office.stages.checks.run(ticket, stage, srun, workspace=Path(project.path), run_id=None, only=order)
    assert [(r.id, r.status) for r in results] == [("repo:setup", "passed"), ("repo:build", "passed")], [r.summary for r in results]

    await office.projects.configure(project.id, profile={"setup": py("import time; time.sleep(30)"), "timeouts": {"setup": 1}})
    slow, _ = await run_checks(office, ticket_id, Path(project.path), "repo:setup")
    assert slow.status == "failed" and slow.data["exit"] == 124

    await office.projects.configure(project.id, profile={"build": py("print(1)")})
    absent, _ = await run_checks(office, ticket_id, Path(project.path), "repo:setup")
    assert absent.status == "skipped" and "setup" in absent.summary


# ---- E: the form -------------------------------------------------------------------------------------------------


def test_commands_of_a_form_field_one_per_line_with_quotes() -> None:
    assert team_api._argv("") is None and team_api._argv(" \n ") is None
    assert team_api._argv("mvn -q test") == ["mvn", "-q", "test"]
    assert team_api._argv('npm ci\n\n  npm run build -- --name "my app"\n') == [["npm", "ci"], ["npm", "run", "build", "--", "--name", "my app"]]
    assert team_api._argv("py -c 'print(1) # not a comment'") == ["py", "-c", "print(1) # not a comment"]
    if sys.platform == "win32":
        assert split_command(r'C:\tools\mvn.cmd -s "C:\Users\dev\.m2\settings nexus.xml" test') == [r"C:\tools\mvn.cmd", "-s", r"C:\Users\dev\.m2\settings nexus.xml", "test"]
    else:
        assert split_command(r"echo a\ b") == ["echo", "a b"]
    with pytest.raises(HTTPException) as bad:
        team_api._argv('mvn "test')
    assert bad.value.status_code == 422
    profile = {"build": [["npm", "ci"], ["npm", "run", "build", "--", "--name", "my app"]]}
    assert team_api._argv(team_api._cmd(profile, "build")) == profile["build"], "what the form shows saves back to the same"


def test_project_form_saves_setup_timeouts_env_and_several_commands(client, tmp_path):  # noqa: F811
    login(client)
    repo = copy_repo(tmp_path / "billing")
    base = {"name": "billing", "path": str(repo)}
    refused = client.post("/api/setup/project", json={**base, "env": {"NEXUS_TOKEN": "abc"}}, headers=HEADERS)
    assert refused.status_code == 422 and "NEXUS_TOKEN" in refused.text and client.get("/api/setup").json()["projects"] == [], "nothing saved"
    assert client.post("/api/setup/project", json={**base, "timeouts": {"test": 0}}, headers=HEADERS).status_code == 422

    sent = {
        **base,
        "test": "mvn -q test",
        "build": "mvn -q -DskipTests package\nnpm run build",
        "setup": "mvn -q dependency:go-offline",
        "timeouts": {"setup": 1200, "test": 1800},
        "env": {"MAVEN_OPTS": "-Xmx2g"},
        "test_glob": ["**/*Test.java"],
    }
    r = client.post("/api/setup/project", json=sent, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["project"]["profile"] == {
        "test": ["mvn", "-q", "test"],
        "build": [["mvn", "-q", "-DskipTests", "package"], ["npm", "run", "build"]],
        "setup": ["mvn", "-q", "dependency:go-offline"],
        "timeouts": {"setup": 1200, "test": 1800},
        "env": {"MAVEN_OPTS": "-Xmx2g"},
        "test_glob": ["**/*Test.java"],
    }
    shown = client.get("/api/setup").json()["projects"][0]
    assert shown["setup"] == "mvn -q dependency:go-offline" and shown["build"] == "mvn -q -DskipTests package\nnpm run build"

    # a client that does not know the new fields keeps them; an empty one removes it
    kept = client.post("/api/setup/project", json={**base, "id": shown["id"], "test": "mvn -q test"}, headers=HEADERS).json()["project"]["profile"]
    assert kept["setup"] == ["mvn", "-q", "dependency:go-offline"] and kept["env"] == {"MAVEN_OPTS": "-Xmx2g"} and "build" not in kept
    cleared = client.post("/api/setup/project", json={**base, "id": shown["id"], "test": "mvn -q test", "setup": "", "env": {}, "timeouts": {}}, headers=HEADERS)
    assert cleared.json()["project"]["profile"] == {"test": ["mvn", "-q", "test"], "test_glob": ["**/*Test.java"]}
