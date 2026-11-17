"""The office works as one team: whatever names no team belongs to THE team, and who approves each step is set there."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.stage import GatePolicy
from actoroffice.domain.work import ProjectAccess
from actoroffice.services.teams import TeamError

from .fakes import FakeRuntime
from .seed import seed_db
from .test_docweb import web  # noqa: F401  (fixture)
from .test_web import HEADERS


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    yield o
    await o.close()


async def test_people_tickets_and_repositories_belong_to_the_one_team(office: Office, tmp_path) -> None:
    assert await office.teams.list_teams() == [], "no team until one is needed"
    lan = await office.teams.create_user("Lan", roles=["ba"])
    (team,) = await office.teams.list_teams()
    assert team.name == "Team" and [(m.user.name, m.roles) for m in await office.teams.members(team.id)] == [("Lan", ["ba"])]

    await office.teams.update_user(lan.id, roles=["ba", "lead"])
    assert (await office.teams.members(team.id))[0].roles == ["ba", "lead"], "a person's roles are set in one place"

    ticket = await office.tickets.create("Hạn mức hoàn tiền", description="x")
    assert ticket.team_id == team.id, "a ticket that names no team is the team's"
    started = await office.stages.start(ticket.id, template="code", by="user")
    assert started.team_id == team.id
    policy = await office.teams.gate_policy(started, "requirement")
    assert [u.name for u in await office.teams.approvers(started, policy)] == ["Lan"], "the team's BA approves, nobody had to pick a team"

    project = await office.projects.create("Shop", path=None, access=ProjectAccess.READ_ONLY)
    assert project.team_id == team.id

    await office.db.execute("UPDATE tickets SET team_id = NULL WHERE id = ?", (ticket.id,))  # saved before the team existed
    assert await office.teams.adopt_orphans() >= 1 and (await office.tickets.get(ticket.id)).team_id == team.id


async def test_who_approves_each_step_is_set_for_the_team(office: Office) -> None:
    lan = await office.teams.create_user("Lan", roles=["ba"])
    hung = await office.teams.create_user("Hùng", roles=["lead"])
    gates = await office.teams.gate_policies()
    assert {"requirement", "design", "proposal", "env_access"} <= set(gates) and gates["proposal"][0].roles == ["ba", "lead", "owner"]

    # a BA turns the AI's proposals from the chat into tickets
    assert await office.proposals.can_approve(lan.id, None)
    await office.teams.set_gate("proposal", GatePolicy(roles=["lead"]))
    assert not await office.proposals.can_approve(lan.id, None) and await office.proposals.can_approve(hung.id, None)
    assert (await office.teams.gate_policies())["proposal"][1], "the team set it"
    await office.teams.set_gate("proposal", None)
    assert await office.proposals.can_approve(lan.id, None), "back to the default"

    await office.teams.set_gate("design", GatePolicy(roles=["lead"], fallback=None, submit_by=["dev", "ba"]))
    ticket = await office.tickets.create("T")
    assert (await office.teams.gate_policy(ticket, "design")).submit_by == ["dev", "ba"]
    with pytest.raises(TeamError, match="Vai không hợp lệ"):
        await office.teams.set_gate("design", GatePolicy(roles=["boss"]))
    with pytest.raises(TeamError, match="Không có bước duyệt"):
        await office.teams.set_gate("nope", GatePolicy(roles=["lead"]))
    await office.teams.set_gate("merge", GatePolicy(roles=["lead"]))
    assert (await office.teams.gate_policy(ticket, "merge")).external, "how a gate is resolved (a PR merged) is not a choice"


def test_the_people_page_sets_roles_and_gates(web) -> None:  # noqa: F811
    office, c, team, people, _project = web
    gates = c.get("/api/gates").json()
    design = next(g for g in gates if g["gate"] == "design")
    assert design["title"] == "Thiết kế của task" and design["roles"] == ["lead"] and not design["custom"]
    saved = c.put("/api/gates/design", json={"roles": ["lead", "dev"], "fallback": None, "min": 1, "submit_by": ["dev"]}, headers=HEADERS).json()
    assert next(g for g in saved if g["gate"] == "design")["custom"]
    assert c.put("/api/gates/design", json={"reset": True}, headers=HEADERS).json()[-3]["gate"]
    assert c.put("/api/gates/design", json={"roles": ["chef"]}, headers=HEADERS).status_code == 422
