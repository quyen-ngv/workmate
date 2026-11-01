"""Team copilot JSON API: people, teams, tasks on a stage template, the board, controls, activity refs."""

from __future__ import annotations

from .test_web import HEADERS, client, login  # noqa: F401  (fixture re-export)


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


def test_model_api_create_switch_and_cancel(client):  # noqa: F811
    login(client)
    bad = client.post("/api/tasks", json={"title": "Sai model", "model": "gpt-5"}, headers=HEADERS)
    assert bad.status_code == 422 and "không hợp lệ" in bad.json()["detail"]
    assert client.get("/api/board").json()["tickets"] == []  # nothing was stored

    task = client.post("/api/tasks", json={"title": "Có model", "model": " opus "}, headers=HEADERS).json()
    key = task["key"]
    assert task["model"] == "opus"
    assert client.get(f"/api/tickets/{key}/model").json()["current"] == "opus"

    r = client.post(f"/api/tickets/{key}/model", json={"model": "haiku", "when": "stage"}, headers=HEADERS).json()
    assert r["current"] == "opus" and r["pending"]["model"] == "haiku" and r["pending"]["scope"] == "stage"
    assert client.get(f"/api/tickets/{key}/stage").json()["status"]["model"]["pending"]["model"] == "haiku"

    r = client.post(f"/api/tickets/{key}/model", json={"cancel": True}, headers=HEADERS).json()
    assert r["pending"] is None and r["current"] == "opus"
    assert client.post(f"/api/tickets/{key}/model", json={"cancel": True}, headers=HEADERS).status_code == 422

    r = client.post(f"/api/tickets/{key}/model", json={"model": "sonnet", "when": "set", "stage": "plan"}, headers=HEADERS).json()
    assert r["stages"] == {"plan": "sonnet"}
    assert client.post(f"/api/tickets/{key}/model", json={"model": "sonnet", "when": "set", "stage": "nope"}, headers=HEADERS).status_code == 422
    r = client.post(f"/api/tickets/{key}/model", json={"model": None, "when": "set", "stage": "plan"}, headers=HEADERS).json()
    assert r["stages"] == {}
    assert client.post(f"/api/tickets/{key}/model", json={"model": "", "when": "turn"}, headers=HEADERS).status_code == 422


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
