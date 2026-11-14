"""The office as a read-only MCP server for an IDE (MCP role (c)): off by default, a personal token per person,
only tools that read, and nothing of a team the person is not in."""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from typing import Any

import pytest

from actoroffice.adapters import registry
from actoroffice.ports.connection import ConnectionSpec
from actoroffice.web import mcp_api

from . import test_sourcehub as wiki
from .test_docflow import analysis, put
from .test_docweb import web  # noqa: F401  (fixture)
from .test_web import HEADERS

READ_ONLY = {
    "repo_list", "code_search", "code_outline", "code_symbol", "code_refs", "code_read", "code_impact", "code_pack", "service_map",
    "source_get", "source_search", "ticket_docs", "ticket_doc",
}  # fmt: skip
SECRET_IN_CODE = "MAX_PER_DAY"  # only in the sample repository of team Pay
SECRET_IN_DOC = "Hạn mức hiện là 5 lần mỗi ngày"  # only in the analysis of the team's ticket


def rpc(c, token: str | None, method: str, params: dict[str, Any] | None = None, *, msg_id: int = 1):
    """One MCP request the way an IDE sends it: a bearer token, no cookie needed, no `X-Requested-With`."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return c.post("/mcp", json={"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params or {}}, headers=headers)


def call(c, token: str, name: str, **arguments: Any) -> tuple[bool, str]:
    """A tool call that the server answered: (is it an error, its text)."""
    r = rpc(c, token, "tools/call", {"name": name, "arguments": arguments})
    assert r.status_code == 200 and "result" in r.json(), r.text
    result = r.json()["result"]
    assert [part["type"] for part in result["content"]] == ["text"]
    return result["isError"], result["content"][0]["text"]


def new_token(c, user_id: str, name: str = "laptop", team_id: str | None = None) -> dict[str, Any]:
    r = c.post("/api/mcp/tokens", json={"user_id": user_id, "name": name, "team_id": team_id}, headers=HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


def audit(env) -> list[tuple[str, str, dict[str, Any]]]:
    async def rows():
        return await env.office.db.fetchall("SELECT target, outcome, detail_json FROM audit_log WHERE action = 'mcp:call' ORDER BY id")

    return [(r["target"], r["outcome"], json.loads(r["detail_json"])) for r in env.c.portal.call(rows)]


@pytest.fixture
def env(web):  # noqa: F811
    """The server switched on, a token of Minh (dev of team Pay), and an epic of the team with an analysis."""
    office, c, team, people, project = web
    office.settings.mcp_server = True
    created = new_token(c, people["Minh"].id)
    epic = c.post("/api/tasks", json={"title": "Nâng hạn mức", "template": "epic", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()

    async def write():
        ticket = await office.tickets.get(epic["key"])
        await put(office, ticket, "analysis", analysis(ticket.key))

    c.portal.call(write)
    return SimpleNamespace(office=office, c=c, team=team, people=people, project=project, token=created["token"], token_id=created["id"], key=epic["key"])


# ---- on and off, the handshake -------------------------------------------------------------------------------------


def test_off_by_default(web) -> None:  # noqa: F811
    office, c, team, people, project = web
    assert office.settings.mcp_server is False
    token = new_token(c, people["Minh"].id)["token"]  # tokens can be prepared before the server is switched on
    assert rpc(c, token, "initialize").status_code == 404 and rpc(c, None, "initialize").status_code == 404
    assert c.get("/mcp").status_code == 404
    info = c.get("/api/mcp/info").json()
    assert info["enabled"] is False and info["url"] == "http://localhost:8765/mcp"
    assert info["config_example"] == {
        "mcpServers": {"office": {"type": "http", "url": "http://localhost:8765/mcp", "headers": {"Authorization": "Bearer <token>"}}}
    }
    office.settings.mcp_server = True
    assert c.get("/api/mcp/info").json()["enabled"] is True
    assert rpc(c, token, "initialize").status_code == 200
    listen = c.get("/mcp")
    assert listen.status_code == 405 and listen.headers["allow"] == "POST", "no stream: the server only answers requests"


def test_handshake_and_tool_list(env) -> None:
    c = env.c
    c.cookies.clear()  # an IDE has no session of the web UI: the bearer token alone must do
    hello = rpc(c, env.token, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "ide", "version": "1"}})
    assert hello.status_code == 200 and hello.headers["content-type"].startswith("application/json")
    body = hello.json()
    assert (body["jsonrpc"], body["id"]) == ("2.0", 1)
    result = body["result"]
    assert result["protocolVersion"] == "2025-06-18" and result["capabilities"] == {"tools": {}}
    assert result["serverInfo"]["name"] == "actor-office" and "ticket" in result["instructions"]
    older = rpc(c, env.token, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert older["protocolVersion"] == mcp_api.PROTOCOLS[0], "a version the server does not know: it offers its newest"

    done = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}, headers={"Authorization": f"Bearer {env.token}"})
    assert done.status_code == 202 and done.content == b"", "a notification gets no answer"
    assert rpc(c, env.token, "ping", msg_id=7).json() == {"jsonrpc": "2.0", "id": 7, "result": {}}

    tools = {t["name"]: t for t in rpc(c, env.token, "tools/list").json()["result"]["tools"]}
    assert set(tools) == READ_ONLY, "exactly the tools that read"
    for tool in tools.values():
        spec = tool["inputSchema"]
        assert spec["type"] == "object" and "ticket" in spec["properties"] and tool["description"] and tool["annotations"] == {"readOnlyHint": True}
        assert set(spec["required"]) <= set(spec["properties"])
    assert tools["code_search"]["inputSchema"]["required"] == ["query"], "`ticket` is optional on the tools the office's AI also has"
    assert tools["ticket_doc"]["inputSchema"]["required"] == ["ticket", "key"] and tools["ticket_docs"]["inputSchema"]["required"] == ["ticket"]

    unknown = rpc(c, env.token, "resources/list").json()
    assert unknown["error"]["code"] == -32601 and "result" not in unknown
    broken = c.post("/mcp", content=b"{not json", headers={"Authorization": f"Bearer {env.token}"})
    assert broken.status_code == 400 and broken.json()["error"]["code"] == -32700
    assert c.post("/mcp", json={"id": 1, "method": "ping"}, headers={"Authorization": f"Bearer {env.token}"}).json()["error"]["code"] == -32600
    batch = c.post(
        "/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}], headers={"Authorization": f"Bearer {env.token}"}
    )
    assert batch.json() == [{"jsonrpc": "2.0", "id": 1, "result": {}}]
    stale = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": f"Bearer {env.token}", "MCP-Protocol-Version": "1999-01-01"})
    assert stale.status_code == 400


# ---- tokens ----------------------------------------------------------------------------------------------------------


def test_only_a_live_token_gets_in(env) -> None:
    c, token = env.c, env.token
    for headers in ({}, {"Authorization": "Bearer aomcp_wrong"}, {"Authorization": "Bearer "}, {"Authorization": f"Basic {token}"}, {"Authorization": token}):
        r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=headers)
        assert r.status_code == 401 and r.headers["www-authenticate"].startswith("Bearer"), headers
    # the web UI's sign-in is not a way in (the client of this test is signed in), and its CSRF header changes nothing
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=HEADERS).status_code == 401
    assert rpc(c, token, "ping").status_code == 200
    foreign = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"Authorization": f"Bearer {token}", "Origin": "https://evil.example"})
    assert foreign.status_code == 403, "a page of another site, not an IDE"

    listed = c.get("/api/mcp/tokens")
    assert token not in listed.text and "token_hash" not in listed.text and hashlib.sha256(token.encode()).hexdigest() not in listed.text
    (row,) = listed.json()
    assert (row["id"], row["name"], row["user_name"], row["team_id"], row["revoked_at"]) == (env.token_id, "laptop", "Minh", None, None)
    assert row["last_used_at"], "a token that was used says when"
    assert c.get(f"/api/mcp/tokens?user_id={env.people['Lan'].id}").json() == []

    async def stored():
        return await env.office.db.fetchone("SELECT * FROM mcp_tokens WHERE id = ?", (env.token_id,))

    kept = c.portal.call(stored)
    assert token not in json.dumps(kept) and kept["token_hash"] == hashlib.sha256(token.encode()).hexdigest(), "only the hash is kept"
    assert env.office.redact(f"sent {token}") == "sent ‹redacted›", "a token never reaches a log"

    assert c.delete(f"/api/mcp/tokens/{env.token_id}").status_code == 403, "revoking is a change of the web UI: its CSRF header applies"
    revoked = c.delete(f"/api/mcp/tokens/{env.token_id}", headers=HEADERS)
    assert revoked.status_code == 200 and revoked.json()["revoked_at"]
    assert rpc(c, token, "ping").status_code == 401, "a revoked token is dead at once"
    assert c.delete("/api/mcp/tokens/mcp_nope", headers=HEADERS).status_code == 404
    c.cookies.clear()
    assert c.get("/api/mcp/tokens").status_code == 401 and c.post("/api/mcp/tokens", json={"user_id": "user"}, headers=HEADERS).status_code == 401


def test_a_token_is_for_a_person_who_may_have_it(env) -> None:
    c, office, team, people = env.c, env.office, env.team, env.people
    assert c.post("/api/mcp/tokens", json={"user_id": "usr_nobody"}, headers=HEADERS).status_code == 404

    async def outsider():
        return await office.teams.create_user("Khách")

    guest = c.portal.call(outsider)
    refused = c.post("/api/mcp/tokens", json={"user_id": guest.id, "team_id": team.id}, headers=HEADERS)
    assert refused.status_code == 422 and "không phải thành viên" in refused.text
    tied = new_token(c, people["Lan"].id, name="  ", team_id="Pay")
    assert (tied["name"], tied["team_id"], tied["team_name"]) == ("IDE", team.id, "Pay")

    async def lock():
        await office.teams.update_user(people["Lan"].id, active=False)

    c.portal.call(lock)
    assert rpc(c, tied["token"], "ping").status_code == 401, "the token of a person who was locked out stops working"
    assert c.post("/api/mcp/tokens", json={"user_id": people["Lan"].id}, headers=HEADERS).status_code == 422


# ---- reading ---------------------------------------------------------------------------------------------------------


def test_code_index_and_service_map(env) -> None:
    c, token = env.c, env.token
    failed, found = call(c, token, "code_search", query="LimitService")
    assert not failed and "### shop:src/shop/limits.py:" in found and "class LimitService" in found, "a hit comes with a source reference to cite"
    failed, repos = call(c, token, "repo_list")
    assert not failed and repos.startswith("- shop: ready")
    failed, read = call(c, token, "code_read", path="src/shop/limits.py", start=3, end=3)
    assert not failed and read.startswith("Nguồn: shop:src/shop/limits.py:3") and "MAX_PER_DAY = 5" in read
    failed, graph = call(c, token, "service_map")
    assert not failed and "shop → audit.internal" in graph and "```mermaid" in graph
    failed, callers = call(c, token, "code_refs", symbol="LimitService.check", ticket=env.key)
    assert not failed and "create_refund" in callers, "the same within a ticket's scope"
    failed, missing = call(c, token, "code_search")
    assert failed and "query" in missing, "a mistake in a call is an error result, not an HTTP error"
    failed, nowhere = call(c, token, "code_read", path="src/shop/nope.py")
    assert failed and "nope.py" in nowhere


def test_documents_of_a_ticket(env) -> None:
    c, token = env.c, env.token
    failed, listed = call(c, token, "ticket_docs", ticket=env.key)
    assert not failed and listed.startswith(f"Tài liệu của {env.key} — Nâng hạn mức (giai đoạn analyze)")
    assert "- analysis: bản 1, chưa duyệt, khớp · cập nhật " in listed and "bởi Copilot" in listed
    failed, body = call(c, token, "ticket_doc", ticket=env.key.lower(), key="analysis")
    assert not failed and body.startswith(f"{env.key} · analysis · bản 1, chưa duyệt") and SECRET_IN_DOC in body and "Nguồn: src/shop/limits.py:3" in body

    async def approve():
        await env.office.db.execute("UPDATE documents SET approved_revision = 1")

    c.portal.call(approve)
    assert "- analysis: bản 1, đã duyệt bản 1" in call(c, token, "ticket_docs", ticket=env.key)[1]
    assert call(c, token, "ticket_doc", ticket=env.key, key="design") == (True, f"{env.key} không có tài liệu 'design'. Gọi ticket_docs để xem các tài liệu của ticket.")
    assert call(c, token, "ticket_docs")[0] and call(c, token, "ticket_doc", key="analysis")[0], "these two need a ticket"
    plain = c.post("/api/tasks", json={"title": "Việc thường", "template": "code", "team_id": env.team.id}, headers=HEADERS).json()
    assert call(c, token, "ticket_docs", ticket=plain["key"]) == (False, f"{plain['key']} chưa có tài liệu nào.")


def test_outside_documents_are_masked_and_reading_them_marks_nothing(env) -> None:
    c, office, token = env.c, env.office, env.token
    leaked = "ghp_" + "a1B2" * 9
    registry.register("fake_wiki", wiki.FakeWiki)
    wiki.PAGES.clear()
    wiki.PAGES[wiki.LIMITS] = {"title": "Quy định hạn mức", "text": f"Hạn mức hoàn tiền là 5 lần mỗi ngày. Token cũ: {leaked}", "version": 3}
    try:

        async def prepare():
            await office.connections.save(ConnectionSpec(id="wiki", use="fake_wiki", scope=f"team:{env.team.id}", config={"spaces": []}))
            ticket = await office.tickets.create("Theo quy định", description=f"Theo quy định ở {wiki.LIMITS}.")
            await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (env.team.id, ticket.id))
            ticket = await office.tickets.get(ticket.id)
            await office.sources.list(ticket)
            return ticket, await office.sources.unread_required(ticket)

        ticket, unread = c.portal.call(prepare)
        assert unread == [wiki.LIMITS]
        failed, page = call(c, token, "source_get", ref=wiki.LIMITS, ticket=ticket.key)
        assert not failed and page.startswith("# Quy định hạn mức\nNguồn: https://wiki.acme/PAY/101 · phiên bản 3 · qua kết nối wiki")
        assert leaked not in page and "Token cũ: ‹redacted›" in page, "what comes from outside still goes through the redactor"
        assert "Đã ghi nhận" not in page

        async def still_unread():
            return await office.sources.unread_required(ticket)

        assert c.portal.call(still_unread) == [wiki.LIMITS], "a person reading in their IDE is not the AI having read the ticket's source"
        failed, hits = call(c, token, "source_search", query="hạn mức")
        assert not failed and hits.startswith(f"- [wiki] {wiki.LIMITS} — Quy định hạn mức") and leaked not in hits, "without a ticket: the team's connections"
        guest = c.portal.call(office.teams.create_user, "Khách")
        failed, nothing = call(c, new_token(c, guest.id)["token"], "source_get", ref=wiki.LIMITS)
        assert failed and "Quy định" not in nothing, "another team's connection is never used"
    finally:
        registry.unregister("fake_wiki")
        wiki.PAGES.clear()


# ---- scope -----------------------------------------------------------------------------------------------------------


def test_nothing_of_a_team_the_person_is_not_in(env) -> None:
    c, office, team, people = env.c, env.office, env.team, env.people

    async def others():
        hr = await office.teams.create_team("HR")
        hoa = await office.teams.create_user("Hoa")
        await office.teams.add_member(hr.id, hoa.id, ["dev"])
        return hr, hoa, await office.teams.create_user("Khách")

    hr, hoa, guest = c.portal.call(others)
    for person in (hoa, guest):  # a member of another team, and a person in no team
        token = new_token(c, person.id)["token"]
        attempts = [
            call(c, token, "code_search", query="LimitService"),
            call(c, token, "code_search", query="LimitService", repo="shop"),
            call(c, token, "code_read", path="src/shop/limits.py", repo="shop"),
            call(c, token, "repo_list", ticket=env.key),
            call(c, token, "code_search", query="LimitService", ticket=env.key),
            call(c, token, "service_map", ticket=env.key),
            call(c, token, "ticket_docs", ticket=env.key),
            call(c, token, "ticket_doc", ticket=env.key, key="analysis"),
            call(c, token, "source_search", query="hạn mức", ticket=env.key),
        ]
        for failed, text in attempts:
            assert failed, text
            assert SECRET_IN_CODE not in text and "limits.py" not in text and SECRET_IN_DOC not in text and "analysis" not in text and "Nâng hạn mức" not in text, text
        assert call(c, token, "repo_list")[1].startswith("Project này chưa có nguồn code"), "their own scope has no repository: nothing of team Pay shows"
        assert "shop" not in call(c, token, "service_map")[1]
        # a ticket of another team is refused in the words used for a ticket that does not exist
        assert call(c, token, "ticket_docs", ticket=env.key)[1].replace(env.key, "X") == call(c, token, "ticket_docs", ticket="AO-999")[1].replace("AO-999", "X")

    # in two teams: the call has to say which, by a ticket or by the token
    c.portal.call(office.teams.add_member, hr.id, people["Lan"].id, ["ba"])
    loose = new_token(c, people["Lan"].id)["token"]
    failed, which = call(c, loose, "code_search", query="LimitService")
    assert failed and "nhiều team (HR, Pay)" in which and SECRET_IN_CODE not in which
    assert not call(c, loose, "code_search", query="LimitService", ticket=env.key)[0]
    tied = new_token(c, people["Lan"].id, team_id=team.id)["token"]
    assert "shop:src/shop/limits.py" in call(c, tied, "code_search", query="LimitService")[1]
    assert call(c, new_token(c, people["Lan"].id, team_id=hr.id)["token"], "code_search", query="LimitService")[0], "tied to HR: no code of Pay"

    # membership is looked at on every call, not when the token was made
    c.portal.call(office.teams.remove_member, team.id, people["Lan"].id)
    failed, gone = call(c, tied, "code_search", query="LimitService")
    assert failed and "không còn là thành viên" in gone and SECRET_IN_CODE not in gone
    assert call(c, loose, "ticket_doc", ticket=env.key, key="analysis")[0]
    assert call(c, env.token, "ticket_doc", ticket=env.key, key="analysis")[0] is False, "Minh is still in the team"

    # the person running the office is an admin: every team's tickets, and a team's code through a token tied to it
    owner = new_token(c, "user", team_id=team.id)["token"]
    assert SECRET_IN_DOC in call(c, owner, "ticket_doc", ticket=env.key, key="analysis")[1] and not call(c, owner, "code_search", query="LimitService")[0]


def test_tools_that_write_do_not_exist_here(env) -> None:
    c, office, token = env.c, env.office, env.token

    async def snapshot():
        ticket = await office.tickets.get(env.key)
        docs = [(d.key, d.revision, d.body) for d in await office.tickets.list_documents(ticket.id)]
        counts = [(await office.db.fetchone(f"SELECT COUNT(*) AS n FROM {table}"))["n"] for table in ("tickets", "documents", "document_revisions", "comments", "interactions")]
        return ticket.model_dump(), docs, counts

    before = c.portal.call(snapshot)
    attempts = {
        "doc_put": {"ticket": env.key, "key": "analysis", "body": "đã bị ghi đè", "base_revision": 1},
        "stage_complete": {"summary": "xong"},
        "ticket_create": {"title": "từ IDE"},
        "ticket_comment": {"ticket": env.key, "body": "x"},
        "source_skip": {"ref": "x", "reason": "y"},
        "Bash": {"command": "echo hi"},
        "nope": {},
        "": {},
    }
    for name, arguments in attempts.items():
        answer = rpc(c, token, "tools/call", {"name": name, "arguments": arguments}).json()
        assert "result" not in answer and answer["error"]["code"] == -32602 and "Unknown tool" in answer["error"]["message"], name
    assert rpc(c, token, "tools/call", {"name": "code_search", "arguments": "LimitService"}).json()["error"]["code"] == -32602
    # an argument a write tool would take does nothing on a tool that reads
    assert not call(c, token, "ticket_doc", ticket=env.key, key="analysis", body="đã bị ghi đè", base_revision=1)[0]
    assert c.portal.call(snapshot) == before, "nothing changed"
    assert [(target, outcome) for target, outcome, _ in audit(env)][:3] == [("doc_put", "unknown"), ("stage_complete", "unknown"), ("ticket_create", "unknown")]


def test_every_call_is_in_the_audit_log_without_its_content(env) -> None:
    c, token = env.c, env.token
    minh = env.people["Minh"].id
    assert audit(env) == [] and rpc(c, token, "tools/list").status_code == 200 and audit(env) == [], "listing the tools is not a call"
    call(c, token, "code_search", query="LimitService")
    call(c, token, "service_map")
    call(c, token, "ticket_doc", ticket=env.key, key="analysis")
    call(c, token, "code_read", path="src/shop/nope.py")
    call(c, token, "ticket_docs", ticket="AO-999")
    rows = audit(env)
    assert [(target, outcome) for target, outcome, _ in rows] == [
        ("code_search", "ok"), ("service_map", "ok"), ("ticket_doc", "ok"), ("code_read", "error"), ("ticket_docs", "denied"),
    ]  # fmt: skip
    assert all(detail["user"] == minh and detail["user_name"] == "Minh" for _, _, detail in rows)
    assert rows[0][2] == {"user": minh, "user_name": "Minh", "ticket": None, "team": env.team.id}
    assert rows[2][2]["ticket"] == env.key and rows[4][2]["ticket"] is None
    text = json.dumps(rows, ensure_ascii=False)
    assert "LimitService" not in text and "nope.py" not in text and SECRET_IN_DOC not in text and token not in text

    async def tokens_log():
        return await env.office.db.fetchall("SELECT outcome, target, detail_json FROM audit_log WHERE action = 'mcp:token' ORDER BY id")

    (made,) = c.portal.call(tokens_log)
    assert (made["outcome"], made["target"]) == ("created", env.token_id) and token not in made["detail_json"]
