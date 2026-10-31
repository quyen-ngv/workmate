from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.web.server import create_app

from .fakes import FakeRuntime

TOKEN = "t" * 40
HEADERS = {"X-Requested-With": "actoroffice"}


@pytest.fixture
def client(tmp_path):
    office = Office(Settings(data_dir=tmp_path / "home"), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await office.open()  # aiosqlite must be opened on the client's event loop
        await office.install_default_positions(skills=False)
        yield
        await office.db.close()

    app.router.lifespan_context = lifespan

    with TestClient(app, base_url="http://localhost:8765") as c:
        yield c


def login(client: TestClient) -> None:
    r = client.get(f"/login?t={TOKEN}", follow_redirects=False)
    assert r.status_code == 303


def test_api_requires_login(client):
    assert client.get("/api/state").status_code == 401
    assert client.get("/login?t=wrong").status_code == 401
    assert client.get("/").status_code == 401


def test_foreign_host_is_rejected(client):
    login(client)
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 403


def test_mutations_need_custom_header(client):
    login(client)
    assert client.post("/api/tickets", json={"title": "x"}).status_code == 403


def test_hire_chat_and_ticket_flow(client):
    login(client)
    state = client.get("/api/state").json()
    writer = next(p for p in state["positions"] if p["name"] == "writer")

    r = client.post("/api/actors", json={"position_id": writer["id"], "name": "Lan", "specialty": "blog"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    lan = r.json()
    assert client.post("/api/actors", json={"position_id": writer["id"], "name": "Two Words"}, headers=HEADERS).status_code == 422

    conv = client.post("/api/conversations", json={"kind": "dm", "actor_id": lan["id"]}, headers=HEADERS).json()
    msg = client.post(f"/api/conversations/{conv['id']}/messages", json={"body": "Chào Lan"}, headers=HEADERS).json()
    assert msg["author_kind"] == "user"
    detail = client.get(f"/api/conversations/{conv['id']}").json()
    assert detail["messages"][0]["body"] == "Chào Lan"

    t = client.post("/api/tickets", json={"title": "Viết bài", "assignee_id": lan["id"]}, headers=HEADERS).json()
    got = client.get(f"/api/tickets/{t['key']}").json()
    assert got["ticket"]["title"] == "Viết bài" and got["comments"] == []
    assert client.get("/api/tickets/AO-999").status_code == 404


def test_team_knowledge_and_project_endpoints(client, tmp_path):
    login(client)
    st = client.get("/api/state").json()
    assert st["default_project_id"] and st["projects"][0]["name"] == "Chung"

    # new position, then hire into it
    r = client.post("/api/positions", json={"name": "seo", "title": "SEO Specialist", "tools": ["Read", "Write", "Rm"], "web": "allow"}, headers=HEADERS)
    assert r.status_code == 200, r.text
    seo = r.json()
    assert seo["tools"] == ["Read", "Write"] and seo["policy"]["web"] == "allow"
    a = client.post("/api/actors", json={"position_id": seo["id"], "name": "Vy"}, headers=HEADERS).json()

    # permissions
    r = client.patch(f"/api/actors/{a['id']}", json={"permission_mode": "trust_all", "allow_rules": ["Bash(npm *)", " ", "Bash(npm *)"]}, headers=HEADERS)
    assert r.json()["permission_mode"] == "trust_all" and r.json()["allow_rules"] == ["Bash(npm *)"]

    # memory
    m = client.post(f"/api/actors/{a['id']}/memories", json={"content": "Khách hàng ở Hà Nội", "pinned": True}, headers=HEADERS).json()
    assert client.patch(f"/api/memories/{m['id']}", json={"content": "Khách hàng ở Đà Nẵng"}, headers=HEADERS).json()["content"] == "Khách hàng ở Đà Nẵng"
    profile = client.get(f"/api/actors/{a['id']}").json()
    assert [x["content"] for x in profile["memories"]] == ["Khách hàng ở Đà Nẵng"]
    assert client.delete(f"/api/memories/{m['id']}", headers=HEADERS).status_code == 200

    # skills (shared and per actor) with a sub-skill file
    assert client.put("/api/skills/shared/-/keyword-research", json={"description": "Nghiên cứu từ khoá", "body": "B1"}, headers=HEADERS).status_code == 200
    assert client.put("/api/skills/actor/Vy/on-page", json={"description": "Tối ưu on-page", "body": "B2"}, headers=HEADERS).status_code == 200
    assert client.put("/api/skills/actor/Vy/on-page/files/checklists/meta.md", json={"content": "title ≤ 60"}, headers=HEADERS).status_code == 200
    assert client.get("/api/skills/actor/Vy/on-page/files/checklists/meta.md").json()["content"] == "title ≤ 60"
    assert client.put("/api/skills/actor/Vy/on-page/files/..%2Fx.md", json={"content": "x"}, headers=HEADERS).status_code in (404, 422)
    names = {s["ref"]["name"] for s in client.get(f"/api/actors/{a['id']}").json()["skills"]}
    assert names == {"keyword-research", "on-page"}

    # project with a folder, its own DM, and tickets filtered by project
    folder = tmp_path / "site"
    folder.mkdir()
    p = client.post("/api/projects", json={"name": "Website", "path": str(folder), "access": "direct"}, headers=HEADERS).json()
    assert p["path"] == str(folder.resolve())
    bad = client.post("/api/projects", json={"name": "Bad", "path": str(tmp_path / "missing"), "access": "read_only"}, headers=HEADERS)
    assert bad.status_code == 422
    dm = client.post("/api/conversations", json={"kind": "dm", "actor_id": a["id"], "project_id": p["id"]}, headers=HEADERS).json()
    assert dm["project_id"] == p["id"]
    t = client.post("/api/tickets", json={"title": "Audit SEO", "project_id": p["id"]}, headers=HEADERS).json()
    assert [x["key"] for x in client.get(f"/api/tickets?project={p['id']}").json()] == [t["key"]]
    detail = client.get(f"/api/conversations/{dm['id']}").json()
    assert detail["usage"]["runs"] == 0 and detail["run_usage"] == {}
    assert client.post(f"/api/conversations/{dm['id']}/new-session", headers=HEADERS).status_code == 200


def test_security_headers_and_index(client):
    login(client)
    r = client.get("/")
    assert r.status_code == 200 and "Văn phòng AI" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-frame-options"] == "DENY"
    assert client.get("/static/app.js").headers["content-type"].startswith("text/javascript")


def test_enterprise_endpoints(client):
    login(client)
    state = client.get("/api/state").json()
    assert {"ba", "lead", "researcher", "analyst", "assistant", "consultant"} <= {p["name"] for p in state["positions"]}
    sec = next(p for p in state["positions"] if p["name"] == "assistant")
    actor = client.post("/api/actors", json={"position_id": sec["id"], "name": "Thu"}, headers=HEADERS).json()

    # tickets carry a type and a due date; the user can record a requirement change on the tree
    root = client.post("/api/tickets", json={"title": "Báo cáo", "type": "feature", "due_at": "2026-10-30"}, headers=HEADERS).json()
    assert root["type"] == "feature" and root["due_at"] == "2026-10-30"
    assert client.post(f"/api/tickets/{root['key']}/due", json={"due_at": None}, headers=HEADERS).json()["due_at"] is None
    result = client.post(f"/api/tickets/{root['key']}/change", json={"summary": "Thêm cột lợi nhuận"}, headers=HEADERS).json()
    assert result["notified"] == [root["key"]]
    detail = client.get(f"/api/tickets/{root['key']}").json()
    assert any(c["author_kind"] == "change" for c in detail["comments"]) and detail["root"] is None
    assert client.post(f"/api/tickets/{root['key']}/change", json={"summary": "  "}, headers=HEADERS).status_code == 422

    # automations (reminders included) are listed through the API
    assert client.get(f"/api/actors/{actor['id']}").json()["automations"] == []
    assert client.get(f"/api/tickets/{root['key']}/work").json()["runs"] == 0
    assert client.get("/api/automations").json() == []
    assert client.get("/api/automations/nope").status_code == 404

    # refresh templates (idempotent)
    assert client.post("/api/positions/sync", headers=HEADERS).json() == {"created": []}
