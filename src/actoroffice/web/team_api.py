"""JSON API for team copilot: people, teams, the stage board (sidebar), stage controls and run activity."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from actoroffice.app import Office
from actoroffice.domain.errors import NotFound
from actoroffice.domain.stage import default_templates
from actoroffice.domain.team import LOCAL_OWNER_ID, TeamSettings
from actoroffice.domain.work import TicketStatus


class PersonBody(BaseModel):
    name: str
    roles: list[str] = Field(default_factory=list)
    telegram_id: int | None = None
    email: str | None = None
    active: bool | None = None


class TeamBody(BaseModel):
    name: str
    jira_project_key: str | None = None
    confluence_space: str | None = None
    settings: TeamSettings | None = None


class MemberBody(BaseModel):
    user_id: str
    roles: list[str]


class StartBody(BaseModel):
    template: str = "code"
    team_id: str | None = None
    owner_user_id: str | None = None
    mode: str | None = None


class TaskBody(BaseModel):
    title: str
    description: str = ""
    template: str = "code"
    team_id: str | None = None
    owner_user_id: str | None = None
    project_id: str | None = None
    mode: str | None = None


class ControlBody(BaseModel):
    cmd: str  # pause | resume | retry | stop | steer | merged
    text: str = ""


class ParticipantBody(BaseModel):
    user_id: str
    roles: list[str]


def register(app: FastAPI, office: Office) -> None:
    teams = office.teams

    # ---- people & teams ---------------------------------------------------------------------------------

    @app.get("/api/people")
    async def people() -> dict[str, Any]:
        result = []
        for team in await teams.list_teams():
            result.append({**team.model_dump(), "members": [m.model_dump() for m in await teams.members(team.id)]})
        return {"teams": result, "users": [u.model_dump() for u in await teams.list_users()], "templates": list(default_templates())}

    @app.post("/api/people")
    async def add_person(body: PersonBody) -> dict[str, Any]:
        return (await teams.create_user(body.name, roles=body.roles, telegram_id=body.telegram_id, email=body.email)).model_dump()

    @app.put("/api/people/{user_id}")
    async def edit_person(user_id: str, body: PersonBody) -> dict[str, Any]:
        user = await teams.update_user(user_id, name=body.name, roles=body.roles, telegram_id=body.telegram_id, email=body.email, active=body.active)
        return user.model_dump()

    @app.post("/api/teams")
    async def add_team(body: TeamBody) -> dict[str, Any]:
        team = await teams.create_team(body.name, jira_project_key=body.jira_project_key, confluence_space=body.confluence_space, settings=body.settings)
        return team.model_dump()

    @app.put("/api/teams/{team_id}")
    async def edit_team(team_id: str, body: TeamBody) -> dict[str, Any]:
        team = await teams.get_team(team_id)
        updated = team.model_copy(
            update={
                "name": body.name.strip() or team.name,
                "jira_project_key": (body.jira_project_key or "").strip().upper() or None,
                "confluence_space": body.confluence_space,
                "settings": body.settings or team.settings,
            }
        )
        return (await teams.update_team(updated)).model_dump()

    @app.post("/api/teams/{team_id}/members")
    async def set_member(team_id: str, body: MemberBody) -> dict[str, Any]:
        await teams.add_member(team_id, body.user_id, body.roles)
        return {"members": [m.model_dump() for m in await teams.members(team_id)]}

    @app.delete("/api/teams/{team_id}/members/{user_id}")
    async def remove_member(team_id: str, user_id: str) -> dict[str, Any]:
        await teams.remove_member(team_id, user_id)
        return {"ok": True}

    # ---- board (sidebar) ----------------------------------------------------------------------------------

    @app.get("/api/board")
    async def board(viewer: str = LOCAL_OWNER_ID, done: bool = False) -> dict[str, Any]:
        """Teams → tickets running through stages, each with its derived status; "needs you" for the viewer."""
        statuses = None if done else [s for s in TicketStatus if s not in (TicketStatus.DONE, TicketStatus.CANCELLED)]
        rows = await office.db.fetchall(
            "SELECT id FROM tickets WHERE stage IS NOT NULL"
            + ("" if statuses is None else f" AND status IN ({','.join('?' * len(statuses))})")
            + " ORDER BY updated_at DESC LIMIT 300",
            [s.value for s in statuses] if statuses else [],
        )
        items = []
        for row in rows:
            ticket = await office.tickets.get(row["id"])
            st = await office.stages.status(ticket, viewer=viewer)
            last = await office.runs.last_activity(st["run_id"]) if st["run_id"] else None
            items.append({**st, "id": ticket.id, "team_id": ticket.team_id, "updated_at": ticket.updated_at, "activity": last})
        team_rows = await teams.list_teams()
        return {
            "teams": [{"id": t.id, "name": t.name, "needs_you": sum(1 for i in items if i["team_id"] == t.id and i["needs_viewer"])} for t in team_rows],
            "tickets": items,
            "needs_you": sum(1 for i in items if i["needs_viewer"]),
        }

    # ---- stages ----------------------------------------------------------------------------------------------

    @app.post("/api/tasks")
    async def new_task(body: TaskBody) -> dict[str, Any]:
        """Create a ticket and start it on a stage template in one step (what /task does on Telegram)."""
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "Cần tiêu đề")
        ticket = await office.tickets.create(
            title[:200], description=body.description or title, project_id=body.project_id or office.chat.default_project_id, created_by="user"
        )
        ticket = await office.stages.start(
            ticket.id, template=body.template, by=LOCAL_OWNER_ID, team_id=body.team_id, owner_user_id=body.owner_user_id, mode=body.mode
        )
        office.scheduler.kick()
        return ticket.model_dump()

    @app.post("/api/tickets/{key}/stages/start")
    async def start_stages(key: str, body: StartBody) -> dict[str, Any]:
        ticket = await office.stages.start(
            key, template=body.template, by=LOCAL_OWNER_ID, team_id=body.team_id, owner_user_id=body.owner_user_id, mode=body.mode
        )
        office.scheduler.kick()
        return ticket.model_dump()

    @app.get("/api/tickets/{key}/stage")
    async def stage(key: str) -> dict[str, Any]:
        ticket = await office.tickets.get(key)
        if not ticket.stage:
            raise HTTPException(404, f"{ticket.key} không chạy theo quy trình giai đoạn")
        template = await office.stages.template_of(ticket)
        return {
            "status": await office.stages.status(ticket, viewer=LOCAL_OWNER_ID),
            "template": template.model_dump(),
            "stage_runs": [r.model_dump() for r in await office.stages.runs(ticket.id)],
            "participants": [m.model_dump() for m in await teams.participants(ticket.id)],
        }

    @app.post("/api/tickets/{key}/participants")
    async def set_participant(key: str, body: ParticipantBody) -> dict[str, Any]:
        ticket = await office.tickets.get(key)
        await teams.set_participant(ticket.id, body.user_id, body.roles)
        return {"participants": [m.model_dump() for m in await teams.participants(ticket.id)]}

    @app.post("/api/tickets/{key}/control")
    async def control(key: str, body: ControlBody) -> dict[str, Any]:
        stages = office.stages
        by = LOCAL_OWNER_ID
        cmd = body.cmd.strip().lower()
        if cmd == "pause":
            ticket = await stages.pause(key, by=by)
        elif cmd == "resume":
            ticket = await stages.resume(key, by=by)
        elif cmd == "retry":
            ticket = await stages.retry(key, by=by)
        elif cmd == "stop":
            ticket = await stages.stop(key, by=by, reason=body.text)
        elif cmd == "steer":
            ticket = await stages.steer(key, body.text, by=by)
        elif cmd == "merged":
            ticket = await stages.external_passed(key, "merge", by=by, note=body.text or "PR đã merge")
        elif cmd == "approved_script":
            ticket = await stages.external_passed(key, "script", by=by, note=body.text or "đã duyệt")
        else:
            raise HTTPException(422, f"không rõ lệnh {body.cmd}")
        office.scheduler.kick()
        return {"ticket": ticket.model_dump(), "status": await stages.status(ticket, viewer=by)}

    # ---- run activity ---------------------------------------------------------------------------------------

    @app.get("/api/runs/{run_id}/activities")
    async def activities(run_id: str, since_seq: int = 0) -> list[dict[str, Any]]:
        return await office.runs.activities(run_id, since_seq=since_seq)

    @app.get("/api/runs/{run_id}/refs/{name}")
    async def run_ref(run_id: str, name: str) -> PlainTextResponse:
        """A large output referenced from an activity (check log, command output). Names are flat, no paths."""
        if not re.fullmatch(r"[A-Za-z0-9_.\-]{1,120}", name) or not re.fullmatch(r"[A-Za-z0-9_\-]{1,80}", run_id):
            raise HTTPException(422, "bad name")
        path = (office.settings.runs_dir / run_id / name).resolve()
        if not path.is_relative_to(office.settings.runs_dir.resolve()) or not path.is_file():
            raise NotFound(f"{name} không tồn tại")
        return PlainTextResponse(office.redact(path.read_text(encoding="utf-8", errors="replace")))
