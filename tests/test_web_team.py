"""Team copilot JSON API: people, teams, tasks on a stage template, the board, controls, activity refs."""

from __future__ import annotations

import contextlib
import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import actoroffice
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.services.review import evaluate
from actoroffice.web.server import create_app

from .fakes import FakeRuntime
from .seed import seed_db
from .test_web import HEADERS, TOKEN, client, login  # noqa: F401  (fixture re-export)


@pytest.fixture
def office_client(tmp_path):
    """Like `client`, with the office at hand to prepare stage runs."""
    office = Office(Settings(data_dir=tmp_path / "home"), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        seed_db(office.settings.db_path)
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


TEAM_JS = Path(actoroffice.__file__).parent / "web" / "static" / "team" / "team.js"
IDENTITY = ["-c", "user.name=t", "-c", "user.email=t@x", "-c", "commit.gpgsign=false"]


def _js() -> str:
    return TEAM_JS.read_text(encoding="utf-8")


def _fn(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n}\n", start)]


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(path), *IDENTITY, *args], check=True, capture_output=True, text=True).stdout.strip()


def _close(office: Office, c: TestClient, key: str, status: str, *, updated_at: str = "2026-11-01T00:00:00Z", paused: int = 0) -> None:
    async def run() -> None:
        await office.db.execute(
            "UPDATE tickets SET status = ?, stage = 'close', updated_at = ?, paused = ? WHERE key = ?", (status, updated_at, paused, key)
        )

    c.portal.call(run)


def _make_closed(office: Office, c: TestClient, n: int, *, cancelled: set[int]) -> list[str]:
    """n closed tickets on the code workflow, the i-th one updated at minute i (so the last is the newest)."""

    async def run() -> list[str]:
        keys = []
        for i in range(n):
            ticket = await office.tickets.create(f"Đã xong {i}", description="x", created_by="user")
            status = "cancelled" if i in cancelled else "done"
            await office.db.execute(
                "UPDATE tickets SET status = ?, stage = 'close', stage_template = 'code', updated_at = ? WHERE id = ?",
                (status, f"2026-10-01T{i // 60:02d}:{i % 60:02d}:00Z", ticket.id),
            )
            keys.append(ticket.key)
        return keys

    return c.portal.call(run)


def test_ac1_board_default_has_closed_count(office_client) -> None:
    office, c = office_client
    login(c)
    keys = [c.post("/api/tasks", json={"title": f"Việc {i}", "template": "code"}, headers=HEADERS).json()["key"] for i in range(4)]
    _close(office, c, keys[1], "done")
    _close(office, c, keys[2], "done")
    _close(office, c, keys[3], "cancelled")
    board = c.get("/api/board?viewer=user").json()
    assert [t["ticket"] for t in board["tickets"]] == [keys[0]]
    assert board["closed_count"] == 3


def test_ac2_closed_board_pages_and_labels(office_client) -> None:
    office, c = office_client
    login(c)
    active = c.post("/api/tasks", json={"title": "Đang làm", "template": "code"}, headers=HEADERS).json()["key"]
    keys = _make_closed(office, c, 25, cancelled={3})
    newest = list(reversed(keys))

    first = c.get("/api/board?closed=true").json()
    assert [t["ticket"] for t in first["tickets"]] == newest[:20]
    assert active not in [t["ticket"] for t in first["tickets"]]
    assert first["has_more"] is True and first["closed_count"] == 25
    second = c.get("/api/board?closed=true&offset=20").json()
    assert [t["ticket"] for t in second["tickets"]] == newest[20:]
    assert second["has_more"] is False
    statuses = {t["ticket"]: t["ticket_status"] for t in first["tickets"] + second["tickets"]}
    assert set(statuses.values()) == {"done", "cancelled"} and statuses[keys[3]] == "cancelled"
    item = first["tickets"][0]
    assert item["title"] == "Đã xong 24" and item["stage_title"] and item["status"] == "done"

    capped = c.get("/api/board?closed=true&limit=100").json()
    assert capped["limit"] == 50 and len(capped["tickets"]) == 25


def test_ac3_team_js_closed_section() -> None:
    js = _js()
    side = _fn(js, "renderSidebar")
    assert "Đã xong (" in side and "closed_count" in side and "Xem thêm" in side and "đã huỷ" in side and "data-open" in side
    assert "data-closed-toggle" in side and "data-closed-more" in side
    # "no work yet" only when there is neither running nor closed work
    empty = side[side.index("Chưa có việc") - 300 : side.index("Chưa có việc")]
    assert "closed_count" in empty
    load = _fn(js, "loadClosed")
    assert "/api/board?closed=true" in load and "offset" in load
    # the closed list is fetched only when the section is opened
    assert "/api/board?closed=true" not in _fn(js, "loadBoard")
    assert "closed: { open: false" in js


def test_ac4_team_js_keeps_closed_tabs() -> None:
    js = _js()
    boot = js[js.rindex("(async () => {") :]
    assert "state.board.tickets.some" not in boot
    load = _fn(js, "loadTicket")
    assert "404" in load
    info = _fn(js, "ticketInfo")
    assert "state.closed.items" in info and "state.details[key]" in info
    assert "ticketInfo(key)" in _fn(js, "renderTabs")
    assert "ticketInfo" in _fn(js, "keyOfTicketId") and "state.details" in _fn(js, "keyOfTicketId")


def test_ac4_closed_ticket_detail_apis(office_client) -> None:
    office, c = office_client
    login(c)
    key = c.post("/api/tasks", json={"title": "Xong rồi", "template": "code"}, headers=HEADERS).json()["key"]
    _close(office, c, key, "done")
    for path in ("", "/stage", "/changes", "/artifacts"):
        assert c.get(f"/api/tickets/{key}{path}").status_code == 200, path
    assert c.get(f"/api/tickets/{key}/stage").json()["status"]["status"] == "done"


def test_ac5_team_js_closed_pane_read_only() -> None:
    pane = _fn(_js(), "renderPane")
    assert 'const closed = ["done", "cancelled"].includes(t.status)' in pane
    chip = pane[pane.index('${closed ? `<span class="chip') : pane.index(': `<span class="chip ${esc(st.status)}">')]
    assert "Đã xong" in chip and "Đã huỷ" in chip
    start = pane.index('${closed ? `<span class="pane-controls">')
    other = pane.index('` : `<span class="pane-controls">', start)
    read_only = pane[start:other]
    running = pane[other : pane.index("</span>`}", other)]
    assert "data-reopen" in read_only and "data-reopen" not in running
    for control in ('data-cmd="pause"', 'data-cmd="resume"', 'data-cmd="retry"', 'data-cmd="merged"', 'data-cmd="quick"', 'data-cmd="full"', "mergeButton"):
        assert control not in read_only, control
        assert control in running, control  # still there for running tickets
    assert pane.count("data-steer") == 1 and '${closed ? "" : `<div class="composer">' in pane


def test_ac6_control_reopen(office_client) -> None:
    office, c = office_client
    login(c)
    done = c.post("/api/tasks", json={"title": "Xong", "template": "code"}, headers=HEADERS).json()["key"]
    running = c.post("/api/tasks", json={"title": "Đang làm", "template": "code"}, headers=HEADERS).json()["key"]
    stopped = c.post("/api/tasks", json={"title": "Huỷ", "template": "code"}, headers=HEADERS).json()["key"]
    _close(office, c, done, "done")
    _close(office, c, stopped, "cancelled", paused=1)
    ctl = lambda key, **body: c.post(f"/api/tickets/{key}/control", json={"cmd": "reopen", **body}, headers=HEADERS)  # noqa: E731

    assert ctl(done, stage="build", text="  ").status_code == 422
    assert ctl(done, stage="nope", text="thiếu test").status_code == 422
    assert ctl(running, stage="understand", text="thiếu test").status_code == 409
    r = ctl(done, stage="build", text="thiếu test")
    assert r.status_code == 200 and r.json()["ticket"]["status"] == "todo" and r.json()["ticket"]["stage"] == "build"
    comments = c.get(f"/api/tickets/{done}").json()["comments"]
    assert any("↩️ Quay lại giai đoạn" in x["body"] and "thiếu test" in x["body"] for x in comments)

    r = ctl(stopped, stage="build", text="làm tiếp")
    assert r.status_code == 200 and r.json()["ticket"]["status"] == "todo" and r.json()["ticket"]["paused"] in (0, False)
    assert ctl(stopped, stage="build", text="lần nữa").status_code == 409  # open again: no reopen from the web


def test_ac6_team_js_reopen_sheet() -> None:
    js = _js()
    sheet = _fn(js, "pageReopen")
    assert "data-reopen-stage" in sheet and "data-reopen-text" in sheet and '"build"' in sheet
    assert 'cmd: "reopen"' in js and "Cần lý do" in js


def test_ac7_changes_of_office_merged_ticket(office_client) -> None:
    office, c = office_client
    login(c)
    key = c.post("/api/tasks", json={"title": "Đổi file", "template": "code"}, headers=HEADERS).json()["key"]
    ws = office.settings.workspaces_dir / key
    ws.mkdir(parents=True)
    _git(ws, "init", "-q", "-b", "main")
    (ws / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(ws, "add", "a.txt")
    _git(ws, "commit", "-q", "-m", "base")
    _git(ws, "checkout", "-q", "-b", "t/x")
    (ws / "a.txt").write_text("one\nTWO\nthree\n", encoding="utf-8")
    _git(ws, "commit", "-q", "-am", "change")

    def changed() -> dict:
        return {f["path"]: (f["added"], f["removed"]) for f in c.get(f"/api/tickets/{key}/changes").json()["files"]}

    assert changed() == {"a.txt": (2, 1)}  # not merged yet: merge-base with main, as before
    _git(ws, "checkout", "-q", "main")
    _git(ws, "merge", "-q", "--no-ff", "-m", "Merge t/x", "t/x")
    sha = _git(ws, "rev-parse", "HEAD")
    _git(ws, "checkout", "-q", "t/x")
    _close(office, c, key, "done")
    assert changed() == {}  # main holds the branch and no merge record: the old way finds nothing

    async def record_merge() -> None:
        ticket = await office.tickets.get(key)
        await office.tickets.add_work_product(ticket.id, "merge", sha[:8], {"note": "t/x → main", "sha": sha})

    c.portal.call(record_merge)
    body = c.get(f"/api/tickets/{key}/changes").json()
    assert changed() == {"a.txt": (2, 1)} and "+TWO" in body["diff"] and body["base"] == f"{sha}^1"


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
