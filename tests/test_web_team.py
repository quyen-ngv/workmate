"""Team copilot JSON API: people, teams, tasks on a stage template, the board, controls, activity refs."""

from __future__ import annotations

import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.services.review import evaluate
from actoroffice.web.server import create_app

from .fakes import FakeRuntime
from .test_web import HEADERS, TOKEN, client, login  # noqa: F401  (fixture re-export)


@pytest.fixture
def office_client(tmp_path):
    """Like `client`, with the office at hand to prepare stage runs."""
    office = Office(Settings(data_dir=tmp_path / "home"), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        await office.open()
        await office.install_default_positions(skills=False)
        yield
        await office.db.close()

    app.router.lifespan_context = lifespan
    with TestClient(app, base_url="http://localhost:8765") as c:
        yield office, c


@pytest.mark.parametrize("severity", ["medium", "low"])
def test_ac11_stage_api_returns_review_on_pr(office_client, severity: str) -> None:
    office, c = office_client
    login(c)
    task = c.post("/api/tasks", json={"title": "Hoàn tiền", "template": "code"}, headers=HEADERS).json()
    finding = {"id": "F1", "severity": severity, "file": "src/a.py", "line": 3, "claim": "thiếu test", "reply": {"action": "wontfix", "note": "lý do"}}
    check = evaluate({"next": 2, "findings": [finding]}, rounds=2).model_dump()

    async def finish_build() -> None:
        ticket = await office.tickets.get(task["key"])
        await office.db.execute(
            "INSERT INTO stage_runs(ticket_id, stage, attempt, started_at, finished_at, outcome, checks_json) VALUES (?, 'build', 1, ?, ?, 'completed', ?)",
            (ticket.id, "2026-11-03T00:00:00Z", "2026-11-03T01:00:00Z", json.dumps([check])),
        )
        await office.db.execute("UPDATE tickets SET stage = 'pr' WHERE id = ?", (ticket.id,))

    c.portal.call(finish_build)
    review = c.get(f"/api/tickets/{task['key']}/stage").json()["review"]
    if severity == "low":
        assert review == []  # no medium/high finding: no block on the pane
    else:
        assert review == [
            {"id": "F1", "severity": "medium", "where": "src/a.py:3", "claim": "thiếu test", "reply": "không sửa: lý do", "label": "", "reviewer_note": ""}
        ]


def test_ac9_stage_api_returns_review_acs(office_client) -> None:
    office, c = office_client
    login(c)
    task = c.post("/api/tasks", json={"title": "Hoàn tiền", "template": "code"}, headers=HEADERS).json()
    assert c.get(f"/api/tickets/{task['key']}/stage").json()["review_acs"] == []

    async def at_pr() -> None:
        ticket = await office.tickets.get(task["key"])
        requirements = "- AC-1: Given a When b Then c\n- AC-2 [review]: Given README When đọc Then đủ.\n  Cách kiểm: đọc diff\n"
        await office.tickets.put_document(ticket.id, "requirements", requirements, author="Copilot", base_revision=None)
        await office.tickets.put_document(ticket.id, "handoff", "| AC-2 | đã đọc diff |\n", author="Copilot", base_revision=None)
        await office.db.execute("UPDATE tickets SET stage = 'pr' WHERE id = ?", (ticket.id,))

    c.portal.call(at_pr)
    assert c.get(f"/api/tickets/{task['key']}/stage").json()["review_acs"] == [
        {"id": "AC-2", "title": "AC-2 [review]: Given README When đọc Then đủ.", "how": "đọc diff", "done": "| AC-2 | đã đọc diff |"}
    ]


def test_ac9_team_js_renders_review_acs() -> None:
    from pathlib import Path

    import actoroffice

    js = (Path(actoroffice.__file__).parent / "web" / "static" / "team" / "team.js").read_text(encoding="utf-8")
    start = js.index("function reviewAcList(items)")
    body = js[start : js.index("\n}\n", start)]
    assert "AC kiểm bằng review" in body and "Cách kiểm" in body and "Đã kiểm" in body and "a.done" in body
    card = js[js.index("function card(item)") : js.index("function renderPane(key)")]
    assert "reviewAcList(p.review_acs)" in card
    pane = js[js.index("function renderPane(key)") :]
    assert 'reviewList(d.stage?.review) + reviewAcList(d.stage?.review_acs)' in pane


def test_people_teams_tasks_board_and_controls(client):  # noqa: F811
    login(client)
    lan = client.post("/api/people", json={"name": "Lan", "roles": [], "telegram_id": 111}, headers=HEADERS).json()
    team = client.post("/api/teams", json={"name": "Billing", "jira_project_key": "bil"}, headers=HEADERS).json()
    assert team["jira_project_key"] == "BIL"
    r = client.post(f"/api/teams/{team['id']}/members", json={"user_id": lan["id"], "roles": ["ba"]}, headers=HEADERS)
    assert r.json()["members"][0]["roles"] == ["ba"]
    assert client.post("/api/people", json={"name": "X", "roles": ["boss"]}, headers=HEADERS).status_code == 422

    people = client.get("/api/people").json()
    assert people["teams"][0]["members"][0]["user"]["name"] == "Lan" and "code" in people["templates"]

    task = client.post(
        "/api/tasks", json={"title": "Hoàn tiền", "template": "code", "team_id": team["id"], "owner_user_id": lan["id"]}, headers=HEADERS
    ).json()
    assert task["stage"] == "understand" and task["origin_conversation_id"]

    board = client.get("/api/board").json()
    assert board["tickets"][0]["ticket"] == task["key"] and board["tickets"][0]["stage_title"] == "Tìm hiểu"

    stage = client.get(f"/api/tickets/{task['key']}/stage").json()
    assert [s["id"] for s in stage["template"]["stages"]] == ["understand", "plan", "build", "pr", "close"]
    assert stage["stage_runs"][0]["attempt"] == 1

    r = client.post(f"/api/tickets/{task['key']}/control", json={"cmd": "pause"}, headers=HEADERS).json()
    assert r["status"]["status"] == "paused"
    r = client.post(f"/api/tickets/{task['key']}/control", json={"cmd": "steer", "text": "Ưu tiên VIP"}, headers=HEADERS).json()
    assert r["ticket"]["key"] == task["key"]
    assert client.post(f"/api/tickets/{task['key']}/control", json={"cmd": "nope"}, headers=HEADERS).status_code == 422

    # refs are flat file names inside the run's folder only
    assert client.get("/api/runs/run_x/refs/..%2F..%2Foffice.db").status_code in (404, 422)
    assert client.get("/api/runs/run_x/refs/check-repo-test.txt").status_code == 404


def test_team_page_and_knowledge_api(client):  # noqa: F811
    login(client)
    page = client.get("/team")
    assert page.status_code == 200 and "/static/team/team.js?v=" in page.text
    assert client.get("/static/team/team.js").status_code == 200
    assert client.get("/api/kb").json() == []
    task = client.post("/api/tasks", json={"title": "Báo cáo", "template": "research"}, headers=HEADERS).json()
    assert task["stage"] == "understand"
    # a gate note reaches the stage service (answer body accepts `note`)
    from actoroffice.web.server import Answer

    assert Answer(approved=False, note="thiếu AC").note == "thiếu AC"
