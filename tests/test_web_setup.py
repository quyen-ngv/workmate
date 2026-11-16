"""Setup from the web: AI model, repos with test/build commands, and connections whose secrets never come back."""

from __future__ import annotations

import json

import httpx
import pytest
import yaml

from actoroffice.web import team_api

from .seed import copy_repo, seed_db
from .test_telegram import GROUP, FakeBot
from .test_web import HEADERS, client, login  # noqa: F401  (fixture re-export)

TG_TOKEN = "123456789:" + "A" * 35


def fake(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
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
    assert s["connections"] == {"telegram": {"configured": False, "running": False, "bound": False, "bot": None, "env_override": None}}
    assert s["integrations"] == [], "outside systems come from the connections"
    assert client.post("/api/setup/ai", json={"model": "opus"}, headers=HEADERS).json()["model"] == "opus"
    assert client.get("/api/setup").json()["ai"]["model"] == "opus"
    assert yaml.safe_load((tmp_path / "home" / "config.yaml").read_text(encoding="utf-8"))["ai_model"] == "opus"


def test_project_from_the_web_with_commands_and_conventions(client, tmp_path):  # noqa: F811
    login(client)
    repo = copy_repo(tmp_path / "billing")  # an empty commit on main
    (repo / "AGENTS.md").write_text("Luôn chạy test trước khi commit.", encoding="utf-8")
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
    # the forms from before connections are gone: 410, pointing to Kết nối, and nothing is saved
    for path, body in (
        ("/api/setup/jira", {"base_url": "https://acme.atlassian.net", "email": "lan@acme.vn", "token": "s3cret-jira"}),
        ("/api/setup/bitbucket", {"email": "lan@acme.vn", "token": "s3cret-bb", "workspace": "acme"}),
        ("/api/setup/jira", {}),
    ):
        gone = client.post(path, json=body, headers=HEADERS)
        assert gone.status_code == 410 and "Kết nối" in gone.json()["detail"] and "connections add" in gone.json()["detail"]
    assert not (tmp_path / "home" / "secrets").exists() or not list((tmp_path / "home" / "secrets").glob("*.json"))

    # a connection made on the Kết nối page is what the setup page lists
    saved = client.post(
        "/api/connections",
        json={"id": "jira-main", "use": "jira_cloud", "config": {"site": "https://acme.atlassian.net"}, "secret": {"email": "lan@acme.vn", "token": "s3cret-jira"}},
        headers=HEADERS,
    )
    assert saved.status_code == 200, saved.text

    assert client.post("/api/setup/telegram", json={"token": "not-a-token"}, headers=HEADERS).status_code == 422
    r = client.post("/api/setup/telegram", json={"token": TG_TOKEN}, headers=HEADERS).json()
    assert r["bot"] == "copilot_bot" and "/start" in r["next"]
    assert (tmp_path / "home" / "secrets" / "telegram_token").read_text(encoding="utf-8") == TG_TOKEN

    text = json.dumps(client.get("/api/setup").json())
    for secret in ("s3cret-jira", "s3cret-bb", TG_TOKEN, "lan@acme.vn", "acme.atlassian.net"):
        assert secret not in text
    setup = client.get("/api/setup").json()
    assert setup["connections"]["telegram"]["configured"] and set(setup["connections"]) == {"telegram"}
    assert [(a["name"], a["title"], [(c["id"], c["scope"], c["enabled"], c["write"]) for c in a["connections"]]) for a in setup["integrations"]] == [
        ("jira_cloud", "Jira Cloud", [("jira-main", "office", True, False)])
    ]


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


@pytest.fixture
def bound(tmp_path):
    """The web app with a running bridge (a fake bot) bound to GROUP."""
    import contextlib

    from fastapi.testclient import TestClient

    from actoroffice.app import Office
    from actoroffice.channels.telegram import TelegramBridge
    from actoroffice.config import Settings
    from actoroffice.web.server import create_app

    from .fakes import FakeRuntime
    from .test_web import TOKEN

    office = Office(Settings(data_dir=tmp_path / "bound"), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)
    bot = FakeBot()

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        seed_db(office.settings.db_path)
        await office.open()
        office.telegram = TelegramBridge(office, bot)
        office.telegram.chat_id = GROUP
        await office.telegram._set_state("chat_id", str(GROUP))
        yield
        office.telegram = None
        await office.db.close()

    app.router.lifespan_context = lifespan
    with TestClient(app, base_url="http://localhost:8765") as c:
        login(c)
        yield c, office, bot


def tg_status(c) -> dict:
    t = c.get("/api/setup").json()["connections"]["telegram"]
    return {"running": t["running"], "bound": t["bound"]}


def test_ac1_setup_reports_bound_for_switch_button(client, bound):  # noqa: F811
    login(client)
    assert tg_status(client) == {"running": False, "bound": False}  # no Telegram: no switch button
    c, _office, _bot = bound
    assert tg_status(c) == {"running": True, "bound": True}  # the only case with the button
    c.post("/api/setup/telegram/unbind", headers=HEADERS)
    assert tg_status(c) == {"running": True, "bound": False}  # waiting for /start: no button


def test_ac2_switch_button_asks_before_calling_the_api():
    """No JS test harness in the repo: guard the page's source (button only when bound, confirm before the request)."""
    from pathlib import Path

    js = (Path(team_api.__file__).parent / "static" / "team" / "team.js").read_text(encoding="utf-8")
    assert '${c.telegram.running && c.telegram.bound ? `<button class="btn" data-telegram-unbind>Chuyển sang group mới</button>` : ""}' in js
    handler = js[js.index("ds.telegramUnbind !== undefined") :]
    handler = handler[: handler.index("pageSetup()")]
    assert handler.index("if (!confirm(") < handler.index('api("/api/setup/telegram/unbind"')
    assert "return;" in handler[handler.index("if (!confirm(") : handler.index('api("/api/setup/telegram/unbind"')]


def test_ac3_setup_unbind_endpoint(bound):
    c, office, bot = bound
    r = c.post("/api/setup/telegram/unbind", headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] and "/start" in r.json()["next"]
    assert [p["chat_id"] for p in bot.sent()] == [GROUP]  # one notice in the old group
    assert office.telegram.chat_id is None and tg_status(c)["bound"] is False


def test_ac8_setup_unbind_conflicts_when_not_running_or_unbound(client, bound):  # noqa: F811
    login(client)
    r = client.post("/api/setup/telegram/unbind", headers=HEADERS)
    assert r.status_code == 409 and r.json()["detail"] == "Telegram chưa chạy"
    assert tg_status(client) == {"running": False, "bound": False}
    c, _office, bot = bound
    c.post("/api/setup/telegram/unbind", headers=HEADERS)
    sent = len(bot.calls)
    r = c.post("/api/setup/telegram/unbind", headers=HEADERS)
    assert r.status_code == 409 and r.json()["detail"] == "Bot chưa gắn group nào"
    assert len(bot.calls) == sent and tg_status(c) == {"running": True, "bound": False}
