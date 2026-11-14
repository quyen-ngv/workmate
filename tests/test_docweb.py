"""Doc Core over the JSON API the web page uses: the documents panel of a ticket, the gate card's choices, accepting a
finding, growing an epic."""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.work import ProjectAccess
from actoroffice.web.server import create_app

from .fakes import FakeRuntime
from .seed import build_repo, seed_db
from .test_codeintel import PY_REPO
from .test_docflow import analysis, breakdown, design, put, spec
from .test_web import HEADERS, TOKEN, login


@pytest.fixture
def web(tmp_path):
    office = Office(Settings(data_dir=tmp_path / "home", ai_review=False, doc_sweep_s=0), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        seed_db(office.settings.db_path)
        await office.open()
        await office.install_default_positions(skills=False)
        yield
        await office.close()

    app.router.lifespan_context = lifespan
    repo = build_repo(tmp_path / "shop", PY_REPO)
    with TestClient(app, base_url="http://localhost:8765") as c:
        login(c)

        async def prepare():
            team = await office.teams.create_team("Pay")
            people = {}
            for name, role in (("Lan", "ba"), ("Minh", "dev"), ("Hùng", "lead")):
                people[name] = await office.teams.create_user(name)
                await office.teams.add_member(team.id, people[name].id, [role])
            project = await office.projects.create("Shop", path=str(repo), access=ProjectAccess.READ_ONLY)
            await office.projects.configure(project.id, team_id=team.id)
            return team, people, project

        team, people, project = c.portal.call(prepare)
        yield office, c, team, people, project


def test_epic_is_a_kind_of_work_people_can_start(web) -> None:
    office, c, team, people, project = web
    listed = c.get("/api/people").json()
    assert {"epic", "task", "task-spec"} <= set(listed["templates"]) and listed["template_titles"]["epic"].startswith("Epic")
    task = c.post("/api/tasks", json={"title": "Nâng hạn mức", "template": "epic", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()
    assert task["stage"] == "analyze" and task["stage_template"] == "epic"
    stage = c.get(f"/api/tickets/{task['key']}/stage").json()
    assert [s["id"] for s in stage["template"]["stages"]] == ["analyze", "breakdown", "close"]
    assert stage["docs"] == {"docs": [], "open": [], "warnings": [], "accepted": [], "children": [], "parent": None, "is_epic": True}
    plain = c.post("/api/tasks", json={"title": "Việc thường", "template": "code"}, headers=HEADERS).json()
    assert c.get(f"/api/tickets/{plain['key']}/stage").json()["docs"] is None


def test_breakdown_card_choices_create_the_tasks(web) -> None:
    office, c, team, people, project = web
    epic = c.post("/api/tasks", json={"title": "Nâng hạn mức", "template": "epic", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()

    async def to_business_gate():
        ticket = await office.tickets.get(epic["key"])
        await put(office, ticket, "analysis", analysis(ticket.key))
        await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (ticket.id,))
        await office.db.execute("UPDATE tickets SET stage = 'breakdown' WHERE id = ?", (ticket.id,))
        await office.stages._open_run(ticket.id, "breakdown")
        ticket = await office.tickets.get(ticket.id)
        await put(office, ticket, "breakdown", breakdown(ticket.key))
        for t in ("T-1", "T-2", "T-3"):
            await put(office, ticket, f"spec.{t}", spec(ticket.key, t))
        stage = await office.stages.stage_def(ticket)
        return (await office.stages._open_gate(ticket, stage, "business", "3 task", [], run_id=None)).id

    card_id = c.portal.call(to_business_gate)
    card = c.get(f"/api/interactions/{card_id}").json()["payload"]
    assert card["spawn"] == {"flows": ["task", "task-spec"], "assign": ["dev"], "people": [{"id": people["Minh"].id, "name": "Minh"}]}
    assert [t["id"] for t in card["doc_core"]["tasks"]] == ["T-1", "T-2", "T-3"]
    panel = c.get(f"/api/tickets/{epic['key']}/stage").json()["docs"]
    assert {d["key"]: d["state"] for d in panel["docs"]} == {"analysis": "khop", "breakdown": "khop", "spec.T-1": "khop", "spec.T-2": "khop", "spec.T-3": "khop"}

    body = {"approved": True, "drop": ["T-3"], "flows": {"T-1": "task-spec", "T-2": "task-spec"}, "assign": {"T-1": people["Minh"].id}}
    assert c.post(f"/api/interactions/{card_id}/answer", json=body, headers=HEADERS).status_code == 200
    panel = c.get(f"/api/tickets/{epic['key']}/stage").json()["docs"]
    kids = panel["children"]
    assert [(k["title"], k["template"], k["assigned"]) for k in kids] == [("Nâng hạn mức lên 9", "task-spec", ["Minh"]), ("Lý do từ chối", "task-spec", [])]
    assert kids[0]["docs"] == [{"key": "spec", "state": "khop", "label": "khớp"}]
    child = c.get(f"/api/tickets/{kids[0]['key']}/stage").json()["docs"]
    assert child["parent"]["key"] == epic["key"] and child["is_epic"] is False


def test_accept_a_finding_and_grow_an_epic_from_the_page(web) -> None:
    office, c, team, people, project = web
    epic = c.post("/api/tasks", json={"title": "Nâng hạn mức", "template": "epic", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()

    async def child_with_gap():
        ticket = await office.tickets.get(epic["key"])
        await put(office, ticket, "analysis", analysis(ticket.key))
        await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (ticket.id,))
        await office.db.execute("UPDATE tickets SET stage = 'breakdown' WHERE id = ?", (ticket.id,))
        child = await office.tickets.create("Nâng hạn mức lên 9", project_id=project.id, parent_id=ticket.id)
        await put(office, child, "spec", spec(ticket.key, "T-1"))
        await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (child.id,))
        started = await office.stages.start(child.id, template="task-spec", by="user", team_id=team.id)
        await put(office, started, "design", design(started.key, ticket.key, cover_all=False))
        return started.key

    key = c.portal.call(child_with_gap)
    panel = c.get(f"/api/tickets/{key}/stage").json()["docs"]
    (gap,) = panel["open"]
    assert (gap["rule"], gap["item"]) == ("R-COVER-AC", "AC-1.2") and {d["key"]: d["state"] for d in panel["docs"]}["design"] == "loi"
    assert c.post(f"/api/tickets/{key}/control", json={"cmd": "accept", "stage": gap["id"], "text": ""}, headers=HEADERS).status_code == 400
    assert c.post(f"/api/tickets/{key}/control", json={"cmd": "accept", "stage": gap["id"], "text": "AC-1.2 làm ở task khác"}, headers=HEADERS).status_code == 200
    panel = c.get(f"/api/tickets/{key}/stage").json()["docs"]
    assert panel["open"] == [] and panel["accepted"][0]["reason"] == "AC-1.2 làm ở task khác"
    assert {d["key"]: d["state"] for d in panel["docs"]}["design"] == "khop"

    grown = c.post(f"/api/tickets/{epic['key']}/control", json={"cmd": "grow", "text": "Thêm hạn mức VIP 20 lần."}, headers=HEADERS)
    assert grown.status_code == 200 and grown.json()["ticket"]["stage"] == "analyze" and "Thêm hạn mức VIP" in grown.json()["ticket"]["description"]
    assert c.post(f"/api/tickets/{key}/control", json={"cmd": "grow", "text": "x"}, headers=HEADERS).status_code == 400
