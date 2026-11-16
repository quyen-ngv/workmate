"""Outside MCP servers of a team: the typed config, secrets stored apart, the unattended tool list, API, executor."""

from __future__ import annotations

import contextlib
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.org import Position, PositionPolicy
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.team import McpServer, TeamSettings, check_mcp_name, validation_text
from actoroffice.runtimes.base import GateDecision
from actoroffice.runtimes.kiro_acp import _mcp_tool_name, acp_mcp_entry
from actoroffice.security.policy import Decision, PolicyEngine, ToolCallContext
from actoroffice.services.mcp_servers import McpServerError
from actoroffice.web.server import create_app

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db
from .test_adapters_generic import FAKE_MCP_SERVER
from .test_web import HEADERS, TOKEN, login

SECRET = "bbtoken-9f8e7d6c5b4a"
STDIO = {"type": "stdio", "command": "npx", "args": ["-y", "@acme/bitbucket-mcp"], "env": {"BITBUCKET_URL": "https://git.acme.example"},
         "secret_env": ["BITBUCKET_TOKEN"], "allowed_tools": ["get_*", "list_*"]}  # fmt: skip
HTTP = {"type": "http", "url": "https://mcp.acme.example/jira", "headers": {"X-Site": "acme"}, "secret_headers": ["Authorization"], "allowed_tools": ["search_*"]}


def refused(data: dict) -> str:
    with pytest.raises(ValidationError) as caught:
        McpServer.model_validate(data)
    return validation_text(caught.value)


# ---- 1. the model ---------------------------------------------------------------------------------------------


def test_valid_shapes() -> None:
    stdio = McpServer.model_validate(STDIO)
    assert stdio.type == "stdio" and stdio.secret_env == ["BITBUCKET_TOKEN"] and not stdio.disabled and stdio.timeout_s is None
    http = McpServer.model_validate(HTTP)
    assert http.url.startswith("https://") and http.secret_headers == ["Authorization"]
    assert McpServer.model_validate({"type": "sse", "url": "http://localhost:9000/sse"}).allowed_tools == []  # empty: nothing unattended
    assert McpServer.model_validate({**STDIO, "command": "C:\\Program Files\\nodejs\\npx.cmd", "allowed_tools": ["*"]}).allowed_tools == ["*"]


@pytest.mark.parametrize(
    ("data", "said"),
    [
        ({"type": "stdio"}, "command: cần chương trình"),
        ({"type": "stdio", "command": "npx -y @acme/mcp"}, "command: chỉ ghi tên chương trình"),
        ({"type": "stdio", "command": "npx", "args": "-y @acme/mcp"}, "args: phải là danh sách đối số"),
        ({"type": "stdio", "command": "bash", "args": ["-c", "npx x"]}, "command: không chạy qua shell"),
        ({"type": "stdio", "command": "C:\\Windows\\System32\\cmd.exe", "args": ["/c", "npx"]}, "command: không chạy qua shell"),
        ({"type": "stdio", "command": "powershell"}, "command: không chạy qua shell"),
        ({"type": "stdio", "command": "npx", "env": {"BITBUCKET_TOKEN": "x"}}, "env: «BITBUCKET_TOKEN» trông như bí mật"),
        ({"type": "stdio", "command": "npx", "env": {"API_KEY": "x"}}, "secret_env"),
        ({"type": "stdio", "command": "npx", "env": {"AUTH": "Bearer abcdefgh12345"}}, "env: «AUTH» trông như bí mật"),
        ({"type": "stdio", "command": "npx", "env": {"A": "1"}, "secret_env": ["A"]}, "secret_env: «A» đã có giá trị thường"),
        ({"type": "stdio", "command": "npx", "url": "https://x.example"}, "chỉ dùng với type http"),
        ({"type": "http"}, "url: cần địa chỉ"),
        ({"type": "http", "url": "http://mcp.acme.example/x"}, "url: phải là https"),
        ({"type": "http", "url": "https://user:pw@mcp.acme.example/x"}, "url: không được chứa tài khoản"),
        ({"type": "http", "url": "https://x.example", "headers": {"Authorization": "Bearer t"}}, "headers: «Authorization» trông như bí mật"),
        ({"type": "http", "url": "https://x.example", "headers": {"Cookie": "a=b"}}, "secret_headers"),
        ({"type": "http", "url": "https://x.example", "headers": {"X-Api-Password": "p"}}, "headers: «X-Api-Password»"),
        ({"type": "http", "url": "https://x.example", "command": "npx"}, "chỉ dùng với type stdio"),
        ({"type": "pipe", "command": "x"}, "type"),
        ({**STDIO, "allowed_tools": ["get_*, list_*"]}, "allowed_tools: mẫu"),
        ({**STDIO, "timeout_s": 0}, "timeout_s"),
        ({**STDIO, "cwd": "/tmp"}, "cwd: không có trường này"),
    ],
)
def test_validation_names_the_field(data: dict, said: str) -> None:
    assert said in refused(data)


def test_server_names() -> None:
    assert check_mcp_name("bitbucket") == "bitbucket" and check_mcp_name("jira_dc-2")
    for bad in ("", "Bitbucket", "-x", "a b", "x" * 33, "a/b"):
        with pytest.raises(ValueError, match="tên MCP server"):
            check_mcp_name(bad)
    with pytest.raises(ValueError, match="dành riêng"):
        check_mcp_name("office")
    with pytest.raises(ValidationError, match="dành riêng"):
        TeamSettings.model_validate({"mcp_servers": {"office": STDIO}})
    with pytest.raises(ValidationError, match="trông như bí mật"):  # the strict rules apply to settings a person sends
        TeamSettings.model_validate({"mcp_servers": {"jira": {"type": "http", "url": "https://x.example", "headers": {"Authorization": "Bearer t"}}}})


def test_old_free_shape_still_loads() -> None:
    old = {
        "mcp_servers": {
            "Rovo": {"type": "http", "url": "https://mcp.atlassian.com/v1/sse", "headers": {"Authorization": "Bearer old-inline-token", "X-Site": "acme"}},
            "notes": {"command": "npx", "args": ["-y", "notes-mcp"], "env": {"NOTES_TOKEN": "tok-123456", "PORT": 8080}, "timeout": 30000, "autoApprove": ["x"]},
            "plain": {"command": "uvx", "args": ["plain-mcp"]},
            "streamable": {"type": "streamable-http", "url": "https://x.example/mcp", "command": ""},
            "broken": {"args": ["x"]},
            "shelled": {"command": "cmd", "args": ["/c", "npx", "x"]},
            "junk": "not a dict",
        }
    }
    with pytest.raises(ValidationError):  # as new input it is refused …
        TeamSettings.model_validate(old)
    servers = TeamSettings.model_validate_json(json.dumps(old), context={"stored": True}).mcp_servers  # … as stored settings it loads
    rovo, notes = servers["Rovo"], servers["notes"]
    assert rovo.legacy_inline_secret and not rovo.invalid and rovo.headers["Authorization"] == "Bearer old-inline-token"
    assert notes.type == "stdio" and notes.legacy_inline_secret and notes.env == {"NOTES_TOKEN": "tok-123456", "PORT": "8080"}
    assert notes.allowed_tools == []  # nothing of the old entry makes a tool run unattended
    assert not servers["plain"].legacy_inline_secret and not servers["plain"].invalid
    assert servers["streamable"].type == "http"
    assert servers["broken"].invalid and servers["broken"].disabled and "command" in servers["broken"].invalid
    assert "shell" in servers["shelled"].invalid and "không phải" in servers["junk"].invalid
    # stored again and read again: the same
    stored = TeamSettings().model_copy(update={"mcp_servers": servers}).model_dump_json()
    again = TeamSettings.model_validate_json(stored, context={"stored": True}).mcp_servers
    assert again["Rovo"] == rovo and again["notes"] == notes and again["broken"].invalid


# ---- 4. approval ----------------------------------------------------------------------------------------------


@pytest.fixture
def engine(tmp_path) -> PolicyEngine:
    return PolicyEngine(protected_dirs=[tmp_path / "appdata"])


def ctx(allowed: dict[str, list[str]], *, tainted: bool = False, **policy) -> ToolCallContext:
    position = Position(id="p", name="dev", title="Dev", policy=PositionPolicy(**policy))
    return ToolCallContext(position=position, workspace=None, tainted=tainted, mcp_allowed=allowed)


def test_unattended_tools(engine: PolicyEngine) -> None:
    c = ctx({"bitbucket": ["get_*", "list_*", "whoami"], "jira": [], "jira__dc": ["read_?"], "all": ["*"]})
    hit = engine.decide("mcp__bitbucket__get_pull_request", {"id": 1}, c)
    assert hit.decision == Decision.ALLOW and hit.taints  # runs unattended; what it returns is outside content
    assert engine.decide("mcp__bitbucket__whoami", {}, c).decision == Decision.ALLOW
    for asks in (
        "mcp__bitbucket__merge_pull_request",  # no match
        "mcp__bitbucket__GET_pull_request",  # case-sensitive
        "mcp__bitbucket__forget_x",  # the glob is on the whole bare name
        "mcp__bitbucket__",  # no tool name
        "mcp__jira__get_issue",  # empty list: nothing runs unattended
        "mcp__github__get_file",  # a server that is not this run's
        "mcp__jira__dc__get_issue",  # the longer server name owns it; its list does not match
        "mcp__jira__dc__read_10",
    ):
        verdict = engine.decide(asks, {}, c)
        assert verdict.decision == Decision.ASK and not verdict.taints, asks
    assert engine.decide("mcp__jira__dc__read_1", {}, c).decision == Decision.ALLOW
    assert engine.decide("mcp__all__delete_everything", {}, c).decision == Decision.ALLOW  # `*`: only when a person typed it
    assert engine.decide("mcp__bitbucket__get_x", {}, ctx({})).decision == Decision.ASK  # no config handed over: today's rule


def test_unattended_with_taint_position_rules_and_the_office_server(engine: PolicyEngine) -> None:
    allowed = {"bitbucket": ["get_*"], "office": ["*"]}
    # a tainted session still reads; acting outside still asks
    assert engine.decide("mcp__bitbucket__get_x", {}, ctx(allowed, tainted=True, external="allow")).decision == Decision.ALLOW
    assert engine.decide("mcp__bitbucket__create_pr", {}, ctx(allowed, tainted=True, external="allow")).decision == Decision.ASK
    assert engine.decide("mcp__bitbucket__create_pr", {}, ctx(allowed, external="allow")).decision == Decision.ALLOW  # as before
    # a position that denies outside tools is never overridden
    assert engine.decide("mcp__bitbucket__get_x", {}, ctx(allowed, external="deny")).decision == Decision.DENY
    # the office's own server keeps its own rules whatever a list under its name says
    own = engine.decide("mcp__office__ticket_get", {}, ctx(allowed))
    assert own.decision == Decision.ALLOW and not own.taints
    assert engine.decide("mcp__office__pr_create", {}, ctx(allowed, tainted=True), external=True).decision == Decision.ASK


def test_kiro_names_a_team_tool_like_claude() -> None:
    servers = {"bitbucket": {}, "jira": {}}
    meta = {"_meta": {"kiro": {"mcpTool": {"serverName": "bitbucket", "toolName": "get_pr"}}}}
    assert _mcp_tool_name({**meta, "title": "whatever"}, servers) == "mcp__bitbucket__get_pr"
    assert _mcp_tool_name({"_meta": {"kiro": {"mcpTool": {"serverName": "bitbucket"}}}, "title": "bitbucket_get_pr"}, servers) == "mcp__bitbucket__get_pr"
    assert _mcp_tool_name({"name": "@jira/search_issues"}, servers) == "mcp__jira__search_issues"
    assert _mcp_tool_name({"_meta": {"kiro": {"mcpTool": {"serverName": "other", "toolName": "x"}}}}, servers) is None
    assert _mcp_tool_name({"name": "execute_bash", "kind": "execute"}, servers) is None


# ---- 2, 3. secrets, the resolved shape, the executor ----------------------------------------------------------


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


async def test_secrets_are_stored_apart(office: Office) -> None:
    svc = office.mcp_servers
    team = await office.teams.create_team("Billing")
    team = await svc.save(team, "bitbucket", STDIO, {"BITBUCKET_TOKEN": SECRET})
    path = svc.secret_path(team.id, "bitbucket")
    assert path == office.settings.data_dir / "secrets" / f"mcp-{team.id}-bitbucket.json"
    assert json.loads(path.read_text(encoding="utf-8")) == {"BITBUCKET_TOKEN": SECRET}
    row = await office.db.fetchone("SELECT settings_json FROM teams WHERE id = ?", (team.id,))
    assert SECRET not in row["settings_json"] and "BITBUCKET_TOKEN" in row["settings_json"]
    [view] = svc.list(team)
    assert view["name"] == "bitbucket" and view["state"] == "on" and view["secret_env"] == {"BITBUCKET_TOKEN": True}
    assert SECRET not in json.dumps(view) and office.redact(f"x {SECRET} y") != f"x {SECRET} y"

    resolved = svc.resolved(team)
    assert resolved.skipped == [] and resolved.allowed == {"bitbucket": ["get_*", "list_*"]}
    assert resolved.servers == {
        "bitbucket": {"type": "stdio", "command": "npx", "args": ["-y", "@acme/bitbucket-mcp"], "env": {"BITBUCKET_URL": "https://git.acme.example", "BITBUCKET_TOKEN": SECRET}}
    }
    # both runtimes take that shape as it is
    assert acp_mcp_entry("bitbucket", resolved.servers["bitbucket"])["env"] == [
        {"name": "BITBUCKET_URL", "value": "https://git.acme.example"},
        {"name": "BITBUCKET_TOKEN", "value": SECRET},
    ]

    # an empty value keeps the stored secret; a new name without a value is missing
    team = await svc.save(team, "bitbucket", {**STDIO, "secret_env": ["BITBUCKET_TOKEN", "BITBUCKET_USER"]}, {"BITBUCKET_TOKEN": "", "BITBUCKET_USER": None})
    assert svc.list(team)[0]["secret_env"] == {"BITBUCKET_TOKEN": True, "BITBUCKET_USER": False} and svc.list(team)[0]["state"] == "missing_secret"
    resolved = svc.resolved(team)
    assert resolved.servers == {} and resolved.skipped == ["MCP 'bitbucket' bỏ qua: thiếu bí mật BITBUCKET_USER"]
    # a name taken off the list: its value is forgotten
    team = await svc.save(team, "bitbucket", {**STDIO, "secret_env": ["BITBUCKET_USER"]}, {"BITBUCKET_USER": "lan-user"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"BITBUCKET_USER": "lan-user"}

    team = await svc.save(team, "jira", HTTP, {"Authorization": f"Bearer {SECRET}"})
    assert svc.resolved(team).servers["jira"] == {"type": "http", "url": HTTP["url"], "headers": {"X-Site": "acme", "Authorization": f"Bearer {SECRET}"}}
    team = await svc.save(team, "jira", {**HTTP, "disabled": True})
    assert "jira" not in svc.resolved(team).servers and svc.resolved(team).skipped == []  # switched off: nothing to say
    assert next(s for s in svc.list(team) if s["name"] == "jira")["state"] == "off"

    team = await svc.remove(team, "bitbucket")
    assert not path.exists() and list(team.settings.mcp_servers) == ["jira"]


async def test_save_refuses_what_is_invalid(office: Office) -> None:
    svc = office.mcp_servers
    team = await office.teams.create_team("Billing")
    for name, server, secrets, said in [
        ("office", STDIO, None, "dành riêng"),
        ("Bit Bucket", STDIO, None, "tên MCP server"),
        ("bitbucket", {**STDIO, "env": {"BITBUCKET_TOKEN": SECRET}, "secret_env": []}, None, "env: «BITBUCKET_TOKEN» trông như bí mật"),
        ("bitbucket", {**STDIO, "env": {"BITBUCKET_TOKEN": SECRET}, "secret_env": [], "legacy_inline_secret": True}, None, "trông như bí mật"),
        ("bitbucket", STDIO, {"OTHER": "x"}, "secrets: «OTHER» chưa được khai"),
        ("bitbucket", {"type": "stdio", "command": "sh", "args": ["-c", "x"]}, None, "không chạy qua shell"),
    ]:
        with pytest.raises(McpServerError, match=said):
            await svc.save(team, name, server, secrets)
    assert (await office.teams.get_team(team.id)).settings.mcp_servers == {}
    assert not (office.settings.data_dir / "secrets").exists() or not list((office.settings.data_dir / "secrets").glob("mcp-*"))


async def stage_ticket(office: Office, team_id: str) -> int:
    ticket = await office.tickets.create("Tổng tiền", description="x")
    await office.db.execute("UPDATE tickets SET stage = 'build', stage_template = 'code', team_id = ? WHERE id = ?", (team_id, ticket.id))
    return ticket.id


async def test_executor_hands_the_resolved_servers_to_the_runtime(office: Office, runtime: FakeRuntime) -> None:
    svc = office.mcp_servers
    team = await office.teams.create_team("Billing")
    team = await svc.save(team, "bitbucket", STDIO, {"BITBUCKET_TOKEN": SECRET})
    team = await svc.save(team, "jira", HTTP)  # its secret was never entered
    team = await svc.save(team, "wiki", {"type": "http", "url": "https://wiki.acme.example/mcp", "disabled": True})
    ticket_id = await stage_ticket(office, team.id)
    seen: dict[str, object] = {}

    async def work(ctx: TurnCtx) -> TurnResult:
        seen["servers"] = dict(ctx.spec.mcp_servers)
        seen["run"] = ctx.spec.run_id
        gate = office.gates[ctx.spec.run_id]
        seen["tainted_before"] = gate.tainted
        seen["read"] = await ctx.tool("mcp__bitbucket__get_pull_request", {"id": 7})
        seen["tainted_after"] = gate.tainted
        seen["write"] = await ctx.tool("mcp__bitbucket__merge_pull_request", {"id": 7})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s1")

    runtime.scripts.append(work)
    await office.tickets.assign(ticket_id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=30)

    assert seen["servers"] == {
        "bitbucket": {"type": "stdio", "command": "npx", "args": ["-y", "@acme/bitbucket-mcp"], "env": {"BITBUCKET_URL": "https://git.acme.example", "BITBUCKET_TOKEN": SECRET}}
    }
    assert seen["read"] == GateDecision.ALLOW and seen["tainted_before"] is False and seen["tainted_after"] is True
    assert seen["write"] == GateDecision.DEFER  # not on the list: a person is asked, as before
    events = await office.runs.events(str(seen["run"]))
    skipped = [e for e in events if e["kind"] == "health" and "MCP" in json.dumps(e, ensure_ascii=False)]
    assert len(skipped) == 1 and "MCP 'jira' bỏ qua: thiếu bí mật Authorization" in json.dumps(skipped[0], ensure_ascii=False)
    assert SECRET not in json.dumps(events, ensure_ascii=False, default=str)


async def test_old_stored_entry_keeps_working_and_is_masked(office: Office) -> None:
    team = await office.teams.create_team("Billing")
    old = {"mcp_servers": {"rovo": {"type": "http", "url": "https://mcp.atlassian.com/v1/sse", "headers": {"Authorization": f"Bearer {SECRET}", "X-Site": "acme"}}}}
    await office.db.execute("UPDATE teams SET settings_json = ? WHERE id = ?", (json.dumps(old), team.id))
    team = await office.teams.get_team(team.id)
    resolved = office.mcp_servers.resolved(team)
    assert resolved.servers == {"rovo": {"type": "http", "url": "https://mcp.atlassian.com/v1/sse", "headers": {"Authorization": f"Bearer {SECRET}", "X-Site": "acme"}}}
    assert resolved.allowed == {"rovo": []} and resolved.skipped == []
    [view] = office.mcp_servers.list(team)
    assert view["legacy_inline_secret"] and view["state"] == "on" and view["headers"]["X-Site"] == "acme"
    assert SECRET not in json.dumps(view) and SECRET not in office.redact(f"Bearer {SECRET}")
    # re-entered through the service: the secret leaves the settings
    team = await office.mcp_servers.save(team, "rovo", {"type": "http", "url": "https://mcp.atlassian.com/v1/sse", "secret_headers": ["Authorization"]}, {"Authorization": f"Bearer {SECRET}"})
    row = await office.db.fetchone("SELECT settings_json FROM teams WHERE id = ?", (team.id,))
    assert SECRET not in row["settings_json"] and not team.settings.mcp_servers["rovo"].legacy_inline_secret
    assert office.mcp_servers.resolved(team).servers["rovo"]["headers"] == {"Authorization": f"Bearer {SECRET}"}


# ---- 5. API ---------------------------------------------------------------------------------------------------


@pytest.fixture
def office_client(tmp_path):
    office = Office(Settings(data_dir=tmp_path / "home"), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        seed_db(office.settings.db_path)
        await office.open()
        yield
        await office.db.close()

    app.router.lifespan_context = lifespan
    with TestClient(app, base_url="http://localhost:8765") as c:
        login(c)
        yield office, c


def test_api_round_trip(office_client) -> None:
    office, c = office_client
    team = c.post("/api/teams", json={"name": "Billing"}, headers=HEADERS).json()["id"]
    base = f"/api/teams/{team}/mcp"
    first = c.get(base).json()
    assert first["servers"] == [] and first["default_allowed_tools"] == ["get_*", "list_*", "search_*", "read_*", "find_*"]

    r = c.put(f"{base}/bitbucket", json={**STDIO, "secrets": {"BITBUCKET_TOKEN": SECRET}}, headers=HEADERS)
    assert r.status_code == 200 and SECRET not in r.text
    [server] = r.json()["servers"]
    assert server["name"] == "bitbucket" and server["state"] == "on" and server["secret_env"] == {"BITBUCKET_TOKEN": True}
    assert server["allowed_tools"] == ["get_*", "list_*"]
    for path in (base, "/api/people"):
        assert SECRET not in c.get(path).text
    assert c.get("/api/people").json()["teams"][0]["settings"]["mcp_servers"]["bitbucket"]["secret_env"] == {"BITBUCKET_TOKEN": True}

    # what GET shows can be sent back; an empty secret keeps the stored one
    edited = {**server, "allowed_tools": ["get_*"], "secrets": {"BITBUCKET_TOKEN": ""}}
    assert c.put(f"{base}/bitbucket", json=edited, headers=HEADERS).json()["servers"][0]["allowed_tools"] == ["get_*"]
    assert json.loads(office.mcp_servers.secret_path(team, "bitbucket").read_text(encoding="utf-8")) == {"BITBUCKET_TOKEN": SECRET}

    bad = c.put(f"{base}/jira", json={"type": "http", "url": "http://jira.acme.example/mcp"}, headers=HEADERS)
    assert bad.status_code == 422 and "url: phải là https" in bad.json()["error"]
    bad = c.put(f"{base}/jira", json={"type": "http", "url": "https://x.example", "headers": {"Authorization": "Bearer abc"}}, headers=HEADERS)
    assert bad.status_code == 422 and "secret_headers" in bad.json()["error"]
    assert c.put(f"{base}/office", json=STDIO, headers=HEADERS).status_code == 422
    assert c.put("/api/teams/nope/mcp/x", json=STDIO, headers=HEADERS).status_code == 404

    # editing the team itself neither drops nor rewrites its servers
    kept = c.put(f"/api/teams/{team}", json={"name": "Billing 2", "settings": {"model": "kiro"}}, headers=HEADERS)
    assert kept.status_code == 200 and list(kept.json()["settings"]["mcp_servers"]) == ["bitbucket"] and SECRET not in kept.text

    assert c.delete(f"{base}/bitbucket", headers=HEADERS).json() == {"servers": []}
    assert c.delete(f"{base}/bitbucket", headers=HEADERS).status_code == 404
    assert not office.mcp_servers.secret_path(team, "bitbucket").exists()


def test_api_test_endpoint_lists_the_tools_of_a_real_stdio_server(office_client, tmp_path: Path) -> None:
    office, c = office_client
    team = c.post("/api/teams", json={"name": "Billing"}, headers=HEADERS).json()["id"]
    base = f"/api/teams/{team}/mcp"
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE_MCP_SERVER, encoding="utf-8")
    env = {"FAKE_PID_FILE": str(tmp_path / "pids.txt"), "FAKE_TOOLS": "get_page,find_pages,delete_page,Get_upper"}
    notes = {"type": "stdio", "command": sys.executable, "args": [str(script)], "env": env, "secret_env": ["NOTES_TOKEN"], "allowed_tools": ["get_*", "find_*"], "timeout_s": 20}

    assert c.put(f"{base}/notes", json=notes, headers=HEADERS).status_code == 200
    missing = c.post(f"{base}/notes/test", headers=HEADERS).json()
    assert missing["ok"] is False and "Thiếu bí mật NOTES_TOKEN" in missing["error"]

    c.put(f"{base}/notes", json={**notes, "secrets": {"NOTES_TOKEN": SECRET}}, headers=HEADERS)
    tried = c.post(f"{base}/notes/test", headers=HEADERS)
    assert tried.status_code == 200 and SECRET not in tried.text
    assert tried.json() == {
        "ok": True,
        "tools": ["Get_upper", "delete_page", "find_pages", "get_page"],
        "unattended": ["find_pages", "get_page"],
        "asks": ["Get_upper", "delete_page"],
    }

    c.put(f"{base}/gone", json={"type": "stdio", "command": str(tmp_path / "no-such-program"), "timeout_s": 5}, headers=HEADERS)
    failed = c.post(f"{base}/gone/test", headers=HEADERS).json()
    assert failed["ok"] is False and "không" in failed["error"]
    assert c.post(f"{base}/nope/test", headers=HEADERS).status_code == 404
