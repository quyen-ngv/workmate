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
    assert "state.open = state.open.filter" not in boot  # tabs are not dropped against the board
    # every tab off the board is loaded at start (name/dot for closed tickets, 404 drops deleted ones), not only the visible panes
    assert "state.open.filter((k) => !paneKeys().includes(k) && !state.board.tickets.some" in boot
    assert boot.index("!paneKeys().includes(k)") < boot.index("renderTabs()", boot.index("!paneKeys().includes(k)"))
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


def test_ac1_closed_count_and_page_by_team(office_client) -> None:
    office, c = office_client
    login(c)
    billing = c.post("/api/teams", json={"name": "Billing"}, headers=HEADERS).json()["id"]
    ops = c.post("/api/teams", json={"name": "Ops"}, headers=HEADERS).json()["id"]
    keys = _make_closed(office, c, 5, cancelled=set())

    async def assign() -> None:
        for i, key in enumerate(keys):
            await office.db.execute("UPDATE tickets SET team_id = ? WHERE key = ?", (billing if i < 3 else ops, key))

    c.portal.call(assign)
    board = c.get("/api/board?viewer=user").json()
    assert board["closed_count"] == 5 and board["closed_by_team"] == {billing: 3, ops: 2}
    page = c.get(f"/api/board?closed=true&team={ops}").json()
    assert [t["ticket"] for t in page["tickets"]] == [keys[4], keys[3]] and page["closed_count"] == 2
    assert c.get("/api/board?closed=true").json()["closed_count"] == 5


def test_ac1_team_js_closed_count_by_team() -> None:
    js = _js()
    side = _fn(js, "renderSidebar")
    assert "state.team ? (state.board.closed_by_team || {})[state.team] || 0 : state.board.closed_count" in side
    load = _fn(js, "loadClosed")
    assert "&team=${encodeURIComponent(team)}" in load and "closed_by_team" in load
    # a stale page (other team, or "Xem thêm" overtaken by a reload) is dropped
    assert "const seq = ++closedSeq" in load and load.index("if (seq !== closedSeq) return") < load.index("state.closed.items =")
    # switching team with the section open reloads it from the server
    switch = js[js.index("if (ds.team !== undefined)") : js.index("if (ds.closedToggle !== undefined)")]
    assert "state.closed.open" in switch and "loadClosed(0)" in switch


def test_ac2_team_js_soon_reloads_open_closed_section() -> None:
    soon = _fn(_js(), "soon")
    assert "if (state.closed.open) await act(() => loadClosed(0))" in soon
    assert soon.index("loadBoard") < soon.index("loadClosed(0)")


def test_ac3_reopen_wakes_the_ai(office_client) -> None:
    office, c = office_client
    login(c)
    key = c.post("/api/tasks", json={"title": "Xong", "template": "code"}, headers=HEADERS).json()["key"]
    _close(office, c, key, "done")

    async def clear() -> None:
        await office.db.execute("UPDATE wake_requests SET status = 'done' WHERE ticket_id = (SELECT id FROM tickets WHERE key = ?)", (key,))

    c.portal.call(clear)
    r = c.post(f"/api/tickets/{key}/control", json={"cmd": "reopen", "stage": "build", "text": "thiếu test"}, headers=HEADERS)
    assert r.status_code == 200

    async def pending():
        ticket = await office.tickets.get(key)
        assert ticket.assignee_actor_id
        return await office.wakes.pending_for(ticket.assignee_actor_id, ticket.id)

    wake = c.portal.call(pending)
    assert wake is not None and wake.source == "manual"
    assert wake.payload["reasons"][0]["stage"] == "build" and wake.payload["reasons"][0]["reopened"] == "thiếu test"


def test_ac4_stage_api_fills_stage_titles(office_client) -> None:
    office, c = office_client
    login(c)
    key = c.post("/api/tasks", json={"title": "Xong", "template": "code"}, headers=HEADERS).json()["key"]
    stages = c.get(f"/api/tickets/{key}/stage").json()["template"]["stages"]
    assert [(s["id"], s["title"]) for s in stages][:3] == [("understand", "Tìm hiểu"), ("plan", "Lên kế hoạch"), ("build", "Làm")]
    assert all(s["title"] for s in stages)
    # the sheet shows the title and sends the id
    assert '<option value="${esc(s.id)}"' in _fn(_js(), "pageReopen") and "esc(s.title || s.id)" in _fn(_js(), "pageReopen")


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


# ---- SI-24: proposed tickets and the Backlog list ------------------------------------------------------------


def _propose(office: Office, c: TestClient, *, ticket_key: str | None = None) -> tuple[str, str | None]:
    """The AI proposes ITEMS from a ticket session (ticket_key) or from its DM; returns (card id, DM id)."""
    from actoroffice.tools.proposal_tools import propose_tickets

    from .test_propose_tickets import ITEMS, ctx_for

    async def propose() -> tuple[str, str | None]:
        conv = None
        if ticket_key:
            ctx = await ctx_for(office, ticket_id=(await office.tickets.get(ticket_key)).id)
        else:
            conv = (await office.chat.dm((await office.ensure_ai()).id)).id
            ctx = await ctx_for(office, conversation=conv)
        await propose_tickets({"items": ITEMS}, ctx)
        [card] = [i for i in await office.interactions.pending() if i.kind.value == "propose_tickets"]
        return card.id, conv

    return c.portal.call(propose)


def test_ac10_ticket_detail_has_pending_proposal(office_client) -> None:
    office, c = office_client
    login(c)
    task = c.post("/api/tasks", json={"title": "Nghiên cứu backlog", "template": "research"}, headers=HEADERS).json()
    card_id, _ = _propose(office, c, ticket_key=task["key"])
    [card] = [i for i in c.get(f"/api/tickets/{task['key']}").json()["pending"] if i["kind"] == "propose_tickets"]
    assert card["id"] == card_id
    second = card["payload"]["items"][1]
    assert {"n", "title", "why", "description", "workflow", "project_id", "project", "run_now", "depends_on"} <= set(second)
    assert second["depends_on"] == [1] and second["run_now"] is False
    board = c.get("/api/board", headers=HEADERS).json()
    assert any(p["id"] == second["project_id"] for p in board["projects"])  # the card's project choices
    edits = [{"n": 1, "selected": True, "title": "Hàng đợi (sửa)"}, {"n": 3, "selected": False}]
    r = c.post(f"/api/interactions/{card_id}/answer", json={"approved": True, "items": edits}, headers=HEADERS)
    assert r.status_code == 200 and len(r.json()["response"]["created"]) == 2 and r.json()["response"]["dropped"] == [3]
    again = c.post(f"/api/interactions/{card_id}/answer", json={"approved": True}, headers=HEADERS)
    assert again.status_code == 409  # a double click creates nothing more
    assert any("Đã tạo 2 ticket" in cm["body"] for cm in c.get(f"/api/tickets/{task['key']}").json()["comments"])


def test_ac10_bad_edit_is_rejected_and_card_stays(office_client) -> None:
    office, c = office_client
    login(c)
    card_id, _ = _propose(office, c)
    r = c.post(f"/api/interactions/{card_id}/answer", json={"approved": True, "items": [{"n": 1, "workflow": "research"}]}, headers=HEADERS)
    assert r.status_code == 422 and "mục 1" in r.json()["error"]
    assert c.get(f"/api/interactions/{card_id}").json()["status"] == "pending"


def test_ac11_chat_proposal_approved_posts_created_list(office_client) -> None:
    office, c = office_client
    login(c)
    card_id, conv = _propose(office, c)
    keys = c.post(f"/api/interactions/{card_id}/answer", json={"approved": True}, headers=HEADERS).json()["response"]["created"]
    assert len(keys) == 3

    async def messages() -> list[str]:
        return [m.body for m in await office.chat.messages(conv)]

    bodies = c.portal.call(messages)
    assert any(all(k in b for k in keys) and "Đã tạo 3 ticket" in b for b in bodies)


def test_ac13_board_backlog_and_start(office_client) -> None:
    office, c = office_client
    login(c)
    card_id, _ = _propose(office, c)
    k1, k2, k3 = c.post(f"/api/interactions/{card_id}/answer", json={"approved": True}, headers=HEADERS).json()["response"]["created"]
    board = c.get("/api/board", headers=HEADERS).json()
    assert board["backlog_count"] == 2
    assert [(b["ticket"], b["template"], b["blockers"]) for b in board["backlog"]] == [(k2, "quick", [k1]), (k3, "code", [])]
    assert k1 in [t["ticket"] for t in board["tickets"]]

    started = c.post(f"/api/tickets/{k3}/backlog/start", headers=HEADERS)
    assert started.status_code == 200 and started.json()["stage"] == "understand" and started.json()["status"] == "todo"
    board = c.get("/api/board", headers=HEADERS).json()
    assert board["backlog_count"] == 1 and k3 in [t["ticket"] for t in board["tickets"]]
    # A-3: still blocked by k1 → enters its workflow, waits in blocked
    blocked = c.post(f"/api/tickets/{k2}/backlog/start", headers=HEADERS).json()
    assert blocked["status"] == "blocked" and blocked["stage_template"] == "quick" and blocked["stage"]
    assert c.post(f"/api/tickets/{k3}/backlog/start", headers=HEADERS).status_code == 409


# ---- SI-31: one ticket at a time per project ------------------------------------------------------------------


def _code_project(office: Office, c: TestClient, tmp_path: Path, name: str = "Leave") -> str:
    from actoroffice.domain.work import ProjectAccess

    async def make() -> str:
        return (await office.projects.create(name, path=str(tmp_path / name.lower()), access=ProjectAccess.WORKTREE)).id

    return c.portal.call(make)


def _task(c: TestClient, project_id: str, title: str) -> dict:
    return c.post("/api/tasks", json={"title": title, "template": "code", "project_id": project_id}, headers=HEADERS).json()


@pytest.mark.parametrize("path", ["tasks", "stages/start", "backlog/start"])
def test_ac1_tasks_and_stage_start_queue(office_client, tmp_path: Path, path: str) -> None:
    from actoroffice.domain.work import TicketStatus

    office, c = office_client
    login(c)
    project_id = _code_project(office, c, tmp_path)
    a = _task(c, project_id, "SI-a")

    async def drop_a() -> None:
        await office.db.execute("DELETE FROM wake_requests WHERE ticket_id = ?", (a["id"],))

    c.portal.call(drop_a)
    if path == "tasks":
        b = _task(c, project_id, "SI-b")
    else:
        status = TicketStatus.BACKLOG if path == "backlog/start" else TicketStatus.TODO

        async def make() -> str:
            return (await office.tickets.create("SI-b", description="x", project_id=project_id, status=status)).key

        key = c.portal.call(make)
        body = {"template": "code"} if path == "stages/start" else None
        response = c.post(f"/api/tickets/{key}/{path}", json=body, headers=HEADERS)
        assert response.status_code == 200, response.text
        b = response.json()
    assert b["status"] == "blocked" and b["stage"] == "understand"

    async def check() -> tuple[list[str], list[str]]:
        return await office.tickets.open_blockers(b["id"]), await office.scheduler.tick()

    blockers, started = c.portal.call(check)
    assert blockers == [a["key"]] and started == []


def test_ac2_board_shows_waiting(office_client, tmp_path: Path) -> None:
    office, c = office_client
    login(c)
    project_id = _code_project(office, c, tmp_path)
    a = _task(c, project_id, "SI-a")
    b = _task(c, project_id, "SI-b")
    row = next(t for t in c.get("/api/board", headers=HEADERS).json()["tickets"] if t["ticket"] == b["key"])
    assert (row["status"], row["status_label"], row["waiting_for"]) == ("blocked", f"đang chờ {a['key']} xong", a["key"])
    stage = c.get(f"/api/tickets/{b['key']}/stage", headers=HEADERS).json()["status"]
    assert stage["status_label"] == f"đang chờ {a['key']} xong"


def test_ac11_save_option_audited(office_client, tmp_path: Path) -> None:
    office, c = office_client
    login(c)
    project_id = _code_project(office, c, tmp_path)
    _task(c, project_id, "SI-a")
    b = _task(c, project_id, "SI-b")

    off = c.post(f"/api/projects/{project_id}/one-at-a-time", json={"enabled": False}, headers=HEADERS)
    assert off.status_code == 200
    assert off.json()["runs_one_at_a_time"] is False and off.json()["one_at_a_time"] is False and off.json()["released"] == 1
    on = c.post(f"/api/projects/{project_id}/one-at-a-time", json={"enabled": True}, headers=HEADERS).json()
    assert on["runs_one_at_a_time"] is True and on["released"] == 0
    assert c.post("/api/projects/nope/one-at-a-time", json={"enabled": True}, headers=HEADERS).status_code == 404

    async def read() -> tuple[bool, list[dict], str]:
        rows = await office.db.fetchall("SELECT detail_json FROM audit_log WHERE action = 'project:one_at_a_time' ORDER BY id")
        project = await office.projects.get(project_id)
        return project.runs_one_at_a_time, [json.loads(r["detail_json"]) for r in rows], (await office.tickets.get(b["key"])).status.value

    stored, audit, b_status = c.portal.call(read)
    assert stored is True and b_status == "todo"
    assert audit == [{"from": True, "to": False, "released": 1}, {"from": False, "to": True, "released": 0}]
    listed = next(p for p in c.get("/api/setup", headers=HEADERS).json()["projects"] if p["id"] == project_id)
    assert listed["runs_one_at_a_time"] is True


def test_ac12_team_js_switch_and_waiting_label() -> None:
    js = _js()
    form = _fn(js, "projectForm")
    assert 'data-one-at-a-time="${k}"' in form and 'p.runs_one_at_a_time ? "checked"' in form and "Chạy từng ticket một" in form
    assert "/api/projects/${queue}/one-at-a-time" in js and "enabled: e.target.checked" in js
    assert 't.waiting_for ? " · " + esc(t.status_label)' in js  # sidebar
    assert "st.waiting_for ? st.status_label" in _fn(js, "renderPane")  # pane


def test_ac15_pane_shows_the_failure_kind_of_the_last_run(office_client) -> None:
    """SI-38 AC-15: the ticket API returns the kind of a failed run, and the pane shows its label and guidance."""
    from actoroffice.domain.run import Run, RunKind, RunStatus
    from actoroffice.engine.failures import FailureKind, run_error

    office, c = office_client
    login(c)
    task = c.post("/api/tasks", json={"title": "Gửi email hoàn tiền", "template": "code"}, headers=HEADERS).json()
    error = run_error(FailureKind.NETWORK, "Claude", 3, "API Error: Unable to connect to API")

    async def failed() -> None:
        ticket = await office.tickets.get(task["key"])
        ai = await office.ensure_ai()
        await office.runs.start(Run(id="run_net", actor_id=ai.id, kind=RunKind.TICKET, ticket_id=ticket.id))
        await office.runs.finish("run_net", RunStatus.FAILED, error=error, error_kind=FailureKind.NETWORK)

    c.portal.call(failed)
    [run] = [r for r in c.get(f"/api/tickets/{task['key']}").json()["runs"] if r["id"] == "run_net"]
    assert run["error_kind"] == "network" and "mạng/API" in run["error"] and "bấm Thử lại" in run["error"]

    js = _js()
    label = _fn(js, "runError")
    assert 'run.status !== "failed" || !run.error_kind' in label and "FAILURE_LABEL[run.error_kind]" in label and "esc(run.error" in label
    assert 'network: "mạng/API"' in js and 'command_timeout: "lệnh quá giờ"' in js
    assert '${closed ? "" : runError(d.detail.runs)}' in _fn(js, "renderPane")
    css = (TEAM_JS.parent / "team.css").read_text(encoding="utf-8")
    assert ".run-error {" in css and ".run-error.passing" in css
