"""Setup from the web: AI model, repos with test/build commands, and connections whose secrets never come back."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from actoroffice.web import team_api

from .test_web import HEADERS, client, login  # noqa: F401  (fixture re-export)

TG_TOKEN = "123456789:" + "A" * 35


def fake(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/rest/api/3/myself"):
        import base64

        user_token = base64.b64decode(request.headers["authorization"].split()[1]).decode()
        if user_token.endswith(":s3cret-jira"):
            return httpx.Response(200, json={"displayName": "Lan BA"})
    if "/workspaces/acme" in url:
        return httpx.Response(200, json={"slug": "acme"})
    if url.endswith("/getMe"):
        return httpx.Response(200, json={"ok": True, "result": {"username": "copilot_bot"}})
    return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})


@pytest.fixture(autouse=True)
def no_machine_token(monkeypatch, tmp_path):
    """This machine's own Telegram token (env or a .env in the repo) must not leak into the test."""
    from actoroffice.channels.telegram import ENV_KEYS

    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def mocked(monkeypatch):
    monkeypatch.setattr(team_api, "SETUP_TRANSPORT", httpx.MockTransport(fake))


def test_setup_status_and_ai_model(client, tmp_path):  # noqa: F811
    login(client)
    s = client.get("/api/setup").json()
    assert s["projects"] == [] and s["people"] == 0
    assert s["connections"] == {
        "telegram": {"configured": False, "running": False, "bound": False, "bot": None, "env_override": None},
        "jira": {"configured": False, "base_url": None, "email": None},
        "bitbucket": {"configured": False, "workspace": None},
    }
    assert client.post("/api/setup/ai", json={"model": "opus"}, headers=HEADERS).json()["model"] == "opus"
    assert client.get("/api/setup").json()["ai"]["model"] == "opus"
    assert yaml.safe_load((tmp_path / "home" / "config.yaml").read_text(encoding="utf-8"))["ai_model"] == "opus"


def test_project_from_the_web_with_commands_and_conventions(client, tmp_path):  # noqa: F811
    login(client)
    repo = tmp_path / "billing"
    repo.mkdir()
    (repo / "AGENTS.md").write_text("Luôn chạy test trước khi commit.", encoding="utf-8")
    import subprocess

    ident = ["-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), *ident, "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    assert client.post("/api/setup/project", json={"name": "x", "path": str(tmp_path)}, headers=HEADERS).status_code == 422  # not a git repo
    team = client.post("/api/teams", json={"name": "Billing"}, headers=HEADERS).json()
    r = client.post(
        "/api/setup/project",
        json={"name": "billing", "path": str(repo), "team_id": team["id"], "repo_url": "acme/billing", "test": "py -m pytest -q", "build": "", "lint": ""},
        headers=HEADERS,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["project"]["profile"] == {"test": ["py", "-m", "pytest", "-q"]} and len(body["imported_rules"]) == 1
    project = client.get("/api/setup").json()["projects"][0]
    assert project["team_id"] == team["id"] and project["test"] == "py -m pytest -q" and project["build"] == ""

    # edit: clearing a command removes it, a new one is added
    r = client.post(
        "/api/setup/project",
        json={"id": project["id"], "name": "billing", "path": str(repo), "team_id": team["id"], "test": "", "build": "mvn -q package"},
        headers=HEADERS,
    )
    assert r.json()["project"]["profile"] == {"build": ["mvn", "-q", "package"]}

    task = client.post("/api/tasks", json={"title": "Hoàn tiền", "team_id": team["id"], "project_id": project["id"]}, headers=HEADERS).json()
    assert task["project_id"] == project["id"]


def test_connections_are_verified_and_secrets_never_returned(client, tmp_path, mocked):  # noqa: F811
    login(client)
    bad = client.post("/api/setup/jira", json={"base_url": "https://acme.atlassian.net", "email": "x@acme.vn", "token": ""}, headers=HEADERS)
    assert bad.status_code == 422
    wrong = client.post("/api/setup/jira", json={"base_url": "https://acme.atlassian.net", "email": "x@acme.vn", "token": "wrong"}, headers=HEADERS)
    assert wrong.status_code == 422 and "Jira từ chối" in wrong.json()["detail"]
    assert not list((tmp_path / "home" / "secrets").glob("jira*"))
    ok = client.post("/api/setup/jira", json={"base_url": "https://acme.atlassian.net", "email": "lan@acme.vn", "token": "s3cret-jira"}, headers=HEADERS)
    assert ok.json() == {"ok": True, "user": "Lan BA"}
    assert client.post("/api/setup/bitbucket", json={"email": "lan@acme.vn", "token": "s3cret-bb", "workspace": "acme"}, headers=HEADERS).json() == {"ok": True}
    assert client.post("/api/setup/bitbucket", json={"email": "lan@acme.vn", "token": "x", "workspace": "other"}, headers=HEADERS).status_code == 422

    assert client.post("/api/setup/telegram", json={"token": "not-a-token"}, headers=HEADERS).status_code == 422
    r = client.post("/api/setup/telegram", json={"token": TG_TOKEN}, headers=HEADERS).json()
    assert r["bot"] == "copilot_bot" and "/start" in r["next"]
    assert (tmp_path / "home" / "secrets" / "telegram_token").read_text(encoding="utf-8") == TG_TOKEN

    text = json.dumps(client.get("/api/setup").json())
    for secret in ("s3cret-jira", "s3cret-bb", TG_TOKEN):
        assert secret not in text
    c = client.get("/api/setup").json()["connections"]
    assert c["jira"]["configured"] and c["jira"]["email"] == "lan@acme.vn" and c["bitbucket"]["workspace"] == "acme" and c["telegram"]["configured"]


def test_launchers_exist():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    assert "actoroffice serve" in (root / "run.bat").read_text(encoding="ascii")
    assert "actoroffice serve" in (root / "run.sh").read_text(encoding="ascii")


def test_telegram_can_be_turned_off_and_env_tokens_are_flagged(client, tmp_path, mocked, monkeypatch):  # noqa: F811
    login(client)
    client.post("/api/setup/telegram", json={"token": TG_TOKEN}, headers=HEADERS)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "987654321:" + "B" * 35)
    r = client.post("/api/setup/telegram", json={"token": TG_TOKEN}, headers=HEADERS).json()
    assert "TELEGRAM_BOT_TOKEN" in r["warning"]
    assert client.get("/api/setup").json()["connections"]["telegram"]["env_override"] == "TELEGRAM_BOT_TOKEN"
    off = client.post("/api/setup/telegram/off", headers=HEADERS).json()
    assert off["ok"] and "TELEGRAM_BOT_TOKEN" in off["warning"]
    assert not (tmp_path / "home" / "secrets" / "telegram_token").exists()
