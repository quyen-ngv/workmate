"""JSON API for team copilot: people, teams, the stage board (sidebar), stage controls and run activity."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field

from actoroffice.app import Office
from actoroffice.domain.errors import NotFound
from actoroffice.domain.stage import TEMPLATE_TITLES, default_templates
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
        return {"teams": result, "users": [u.model_dump() for u in await teams.list_users()], "templates": list(default_templates()), "template_titles": TEMPLATE_TITLES}

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
            "review": await office.stages.last_review(ticket.id),
            "review_acs": await office.stages.review_acs(ticket.id),
            "merge": await office.merges.panel(ticket.key) if ticket.stage == "pr" else {"available": False},
        }

    @app.post("/api/tickets/{key}/merge/start")
    async def merge_start(key: str) -> dict[str, Any]:
        """The "Merge into <main>" button (repos without a code host): merges, tests and passes the gate in the background."""
        return await office.merges.start(key, by=LOCAL_OWNER_ID)

    @app.get("/api/tickets/{key}/changes")
    async def ticket_changes(key: str) -> dict[str, Any]:
        """Files / Changes panel: what the ticket's branch changed so far (live, from git)."""
        from actoroffice.engine.workspace import changes

        ticket = await office.tickets.get(key)
        root = await office.tickets.root_of(ticket.id)
        project = await office.projects.get(ticket.project_id) if ticket.project_id else None
        return await changes(office.settings.workspaces_dir / root.key, project.main_branch if project else "main")

    @app.get("/api/tickets/{key}/artifacts")
    async def ticket_artifacts(key: str) -> list[dict[str, Any]]:
        ticket = await office.tickets.get(key)
        return [p for p in await office.tickets.work_products(ticket.id) if p["kind"] in ("screenshot", "video", "trace", "report", "file")]

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
        elif cmd in ("quick", "full"):
            target = "quick" if cmd == "quick" else "code"
            label = "Sửa nhỏ" if cmd == "quick" else "đầy đủ"
            ticket = await stages.switch_template(key, target, reason=body.text or f"người chọn quy trình {label}", by=by)
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


class RuleBody(BaseModel):
    title: str
    body: str = ""
    kind: str = "guideline"
    scope: str = "global"
    stage: str = "any"
    spec: dict[str, Any] = Field(default_factory=dict)
    activate: bool = True


def register_rules(app: FastAPI, office: Office) -> None:
    rules = office.rules

    @app.get("/api/rules")
    async def list_rules(scope: str | None = None, status: str | None = None, kind: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        found = await rules.search(q, limit=50, active_only=False) if q else await rules.list(scope=scope, status=status, kind=kind)
        return [r.model_dump() for r in found]

    @app.get("/api/rules/{rule_id}")
    async def get_rule(rule_id: str) -> dict[str, Any]:
        return {"rule": (await rules.get(rule_id)).model_dump(), "versions": await rules.versions(rule_id)}

    @app.post("/api/rules")
    async def add_rule(body: RuleBody) -> dict[str, Any]:
        rule = await rules.propose(
            title=body.title, body=body.body, kind=body.kind, scope=body.scope, stage=body.stage, spec=body.spec, by=LOCAL_OWNER_ID, activate=body.activate
        )
        return rule.model_dump()

    @app.put("/api/rules/{rule_id}")
    async def edit_rule(rule_id: str, body: RuleBody) -> dict[str, Any]:
        return (await rules.update(rule_id, by=LOCAL_OWNER_ID, title=body.title, body=body.body, spec=body.spec, stage=body.stage)).model_dump()

    @app.post("/api/rules/{rule_id}/approve")
    async def approve_rule(rule_id: str) -> dict[str, Any]:
        return (await rules.approve(rule_id, by=LOCAL_OWNER_ID)).model_dump()

    @app.post("/api/rules/{rule_id}/disable")
    async def disable_rule(rule_id: str) -> dict[str, Any]:
        return (await rules.disable(rule_id, by=LOCAL_OWNER_ID)).model_dump()

    @app.post("/api/projects/{project_id}/import-conventions")
    async def import_conventions(project_id: str) -> list[dict[str, Any]]:
        project = await office.projects.get(project_id)
        return [r.model_dump() for r in await rules.import_conventions(project, by=LOCAL_OWNER_ID)]

    @app.get("/api/kb")
    async def kb(team_id: str | None = None, status: str | None = None, kind: str | None = None, q: str | None = None) -> list[dict[str, Any]]:
        if q:
            items = await office.knowledge.search(q, team_id=team_id, limit=50)
        else:
            items = await office.knowledge.list(team_id=team_id, status=status, kind=kind)
        return [i.model_dump() for i in items if i.status != "rejected" or status == "rejected"]

    @app.post("/api/kb/{item_id}/status")
    async def kb_status(item_id: int, body: dict[str, Any]) -> dict[str, Any]:
        return (await office.knowledge.set_status(item_id, str(body.get("status") or ""), by=LOCAL_OWNER_ID)).model_dump()

    @app.get("/api/resources")
    async def resources(team_id: str | None = None) -> list[dict[str, Any]]:
        return [r.model_dump() for r in await office.access.resources_for(team_id)]

    @app.get("/api/tickets/{key}/access")
    async def ticket_access(key: str) -> list[dict[str, Any]]:
        ticket = await office.tickets.get(key)
        return [{"grant": g.model_dump(), "resource": r.model_dump()} for g, r in await office.access.active(ticket.id)]

    @app.post("/api/grants/{grant_id}/revoke")
    async def revoke_grant(grant_id: str) -> dict[str, Any]:
        await office.access.revoke(grant_id, by=LOCAL_OWNER_ID)
        return {"ok": True}

    @app.get("/api/tickets/{key}/rules")
    async def ticket_rules(key: str) -> dict[str, Any]:
        """Rules in effect for the ticket's current stage."""
        ticket = await office.tickets.get(key)
        project = await office.projects.get(ticket.project_id) if ticket.project_id else None
        compiled = await rules.compile(ticket=ticket, project=project, stage=ticket.stage or "any")
        return compiled.model_dump()


# ---- setup from the web (projects, connections, AI model) ----------------------------------------------


class SetupProject(BaseModel):
    id: str | None = None
    name: str
    path: str
    team_id: str | None = None
    repo_url: str | None = None
    test: str = ""
    build: str = ""
    lint: str = ""


class JiraBody(BaseModel):
    base_url: str
    email: str
    token: str


class BitbucketBody(BaseModel):
    email: str
    token: str
    workspace: str


class TelegramBody(BaseModel):
    token: str


class PermissionsBody(BaseModel):
    mode: str | None = None
    allow_rules: list[str] | None = None


class AiBody(BaseModel):
    model: str


def _argv(text: str) -> list[str] | None:
    import shlex

    text = text.strip()
    return shlex.split(text, posix=True) if text else None


def _cmd(profile: dict[str, Any], key: str) -> str:
    import shlex

    value = profile.get(key)
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return shlex.join(value)
    return ""


def _telegram_env_key() -> str | None:
    import os

    from actoroffice.channels.telegram import ENV_KEYS

    return next((k for k in ENV_KEYS if os.environ.get(k, "").strip()), None)


async def _bot_name(office: Office) -> str | None:
    if office.telegram is None:
        return None
    return office.telegram.bot_name or getattr(office.telegram.api, "bot_id", None)


SETUP_TRANSPORT: Any = None  # tests plug an httpx.MockTransport here to fake Jira/Bitbucket/Telegram


def register_setup(app: FastAPI, office: Office) -> None:
    from actoroffice.domain.work import ProjectAccess

    @app.get("/api/setup")
    async def setup() -> dict[str, Any]:
        from actoroffice.channels.telegram import load_token
        from actoroffice.services.atlassian import load_bitbucket
        from actoroffice.services.jira import load_credentials

        jira = load_credentials(office.settings.data_dir)
        bitbucket = load_bitbucket(office.settings.data_dir)
        projects = [p for p in await office.projects.list() if not p.is_default]
        return {
            "ai": {"name": office.settings.ai_name, "model": office.settings.ai_model or office.settings.default_model},
            "projects": [
                {**p.model_dump(), "test": _cmd(p.profile, "test"), "build": _cmd(p.profile, "build"), "lint": _cmd(p.profile, "lint")}
                for p in projects
            ],
            "teams": [t.model_dump() for t in await office.teams.list_teams()],
            "people": len([u for u in await office.teams.list_users() if u.id != LOCAL_OWNER_ID]),
            "connections": {
                "telegram": {
                    "configured": bool(load_token(office.settings)),
                    "running": office.telegram is not None,
                    "bound": bool(office.telegram and office.telegram.chat_id),
                    "bot": await _bot_name(office),
                    "env_override": _telegram_env_key(),
                },
                "jira": {"configured": bool(jira), "base_url": (jira or {}).get("base_url"), "email": (jira or {}).get("email")},
                "bitbucket": {"configured": bool(bitbucket), "workspace": (bitbucket or {}).get("workspace")},
            },
        }

    @app.post("/api/setup/project")
    async def setup_project(body: SetupProject) -> dict[str, Any]:
        if body.id:
            current = await office.projects.get(body.id)
            project = await office.projects.update(
                body.id, name=body.name, path=body.path, access=current.access if current.path == body.path else ProjectAccess.WORKTREE, description=current.description
            )
        else:
            project = await office.projects.create(body.name, path=body.path, access=ProjectAccess.WORKTREE)
        profile = dict(project.profile)
        for key in ("test", "build", "lint"):
            value = _argv(getattr(body, key))
            if value:
                profile[key] = value
            else:
                profile.pop(key, None)
        project = await office.projects.configure(project.id, team_id=body.team_id or None, repo_url=body.repo_url or None, profile=profile)
        imported = await office.rules.import_conventions(project, by=LOCAL_OWNER_ID)
        return {"project": project.model_dump(), "imported_rules": [r.id for r in imported]}

    @app.post("/api/setup/jira")
    async def setup_jira(body: JiraBody) -> dict[str, Any]:
        from actoroffice.services.jira import JiraClient, JiraError, save_credentials

        if not (body.base_url.strip() and body.email.strip() and body.token.strip()):
            raise HTTPException(422, "Cần đủ URL, email và API token")
        if not body.base_url.strip().startswith("https://"):
            raise HTTPException(422, "URL phải là https://<site>.atlassian.net")
        client = JiraClient(body.base_url.strip(), body.email.strip(), body.token.strip(), transport=SETUP_TRANSPORT)
        try:
            me = await client._call("GET", "/rest/api/3/myself")
        except JiraError as exc:
            raise HTTPException(422, f"Jira từ chối: {exc}") from exc
        finally:
            await client.close()
        save_credentials(office.settings.data_dir, body.base_url.strip(), body.email.strip(), body.token.strip())
        await office.jira.close()
        office.jira.client = None
        office.atlassian._wiki = None
        if office.jira.ready() and not any(t.get_name() == "jira" for t in office._tasks):
            import asyncio

            office._tasks.append(asyncio.create_task(office.jira.run_forever(), name="jira"))
        return {"ok": True, "user": me.get("displayName")}

    @app.post("/api/setup/bitbucket")
    async def setup_bitbucket(body: BitbucketBody) -> dict[str, Any]:
        from actoroffice.services.atlassian import AtlassianError, BitbucketCloud, _call, save_bitbucket

        if not (body.email.strip() and body.token.strip() and body.workspace.strip()):
            raise HTTPException(422, "Cần đủ email, API token và workspace")
        client = BitbucketCloud(body.email.strip(), body.token.strip(), body.workspace.strip(), transport=SETUP_TRANSPORT)
        try:
            await _call(client.http, "GET", f"/workspaces/{body.workspace}")
        except AtlassianError as exc:
            raise HTTPException(422, f"Bitbucket từ chối: {exc}") from exc
        finally:
            await client.close()
        save_bitbucket(office.settings.data_dir, body.email.strip(), body.token.strip(), body.workspace.strip())
        office.atlassian._code = None
        return {"ok": True}

    @app.post("/api/setup/telegram")
    async def setup_telegram(body: TelegramBody) -> dict[str, Any]:
        from actoroffice.channels.telegram import TOKEN_RE, BotApi, TelegramError, token_path

        token = body.token.strip()
        if not TOKEN_RE.match(token):
            raise HTTPException(422, "Token không đúng dạng 123456789:AA… (lấy từ @BotFather)")
        import httpx

        api = BotApi(token, client=httpx.AsyncClient(transport=SETUP_TRANSPORT) if SETUP_TRANSPORT else None)
        try:
            me = await api.call("getMe")
        except TelegramError as exc:
            raise HTTPException(422, f"Telegram từ chối token: {exc}") from exc
        finally:
            await api.close()
        path = token_path(office.settings)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        running = office.telegram is not None and getattr(office.telegram.api, "bot_id", None) == token.split(":", 1)[0]
        if not running and SETUP_TRANSPORT is None:
            await office.stop_telegram()  # another bot was running: switch now
            await office.start_telegram(token)
        bound = bool(office.telegram and office.telegram.chat_id)
        env = _telegram_env_key()
        return {
            "ok": True,
            "bot": me.get("username"),
            "bound": bound,
            "next": "Bot đã vào group cũ và tiếp tục dùng các topic." if bound
            else "Tạo group Telegram (hoặc dùng group cũ), bật Topics, thêm bot làm admin rồi nhắn /start trong group.",
            "warning": f"Biến môi trường {env} đang đặt token khác: lần khởi động sau token đó sẽ được dùng. Hãy xoá biến này." if env else None,
        }

    @app.post("/api/setup/telegram/unbind")
    async def setup_telegram_unbind() -> dict[str, Any]:
        """Move the bot to another group: it leaves a notice in the current one, then waits for /start."""
        if office.telegram is None:
            raise HTTPException(409, "Telegram chưa chạy")
        if office.telegram.chat_id is None:
            raise HTTPException(409, "Bot chưa gắn group nào")
        await office.telegram.unbind()
        return {"ok": True, "next": "Nhắn /start trong group mới (đã bật Topics, bot là admin)."}

    @app.post("/api/setup/telegram/off")
    async def setup_telegram_off() -> dict[str, Any]:
        from actoroffice.channels.telegram import token_path

        await office.stop_telegram()
        token_path(office.settings).unlink(missing_ok=True)
        return {"ok": True, "warning": f"Biến môi trường {_telegram_env_key()} vẫn còn token: lần khởi động sau bot sẽ chạy lại." if _telegram_env_key() else None}

    @app.post("/api/projects/{project_id}/permissions")
    async def project_permissions(project_id: str, body: PermissionsBody) -> dict[str, Any]:
        """The project's permission mode and its "always allow" rules (removing a rule asks again next time)."""
        before = await office.projects.get(project_id)
        project = await office.projects.set_permissions(project_id, mode=body.mode, allow_rules=body.allow_rules)
        await office.audit.record(
            "permission:project", "changed", target=project.name,
            detail={"from": {"mode": before.permission_mode, "rules": before.allow_rules}, "to": body.model_dump(exclude_none=True)},
        )
        # pending cards the new permission allows are approved by themselves (a narrower one approves nothing)
        return {**project.model_dump(), "auto_approved": await office.permissions_recheck(project_id=project.id)}

    @app.post("/api/setup/ai")
    async def setup_ai(body: AiBody) -> dict[str, Any]:
        import yaml

        model = body.model.strip()
        if not model:
            raise HTTPException(422, "Chọn model")
        office.settings.ai_model = model
        path = office.settings.config_path
        data = (yaml.safe_load(path.read_text(encoding="utf-8")) or {}) if path.exists() else {}
        data["ai_model"] = model
        path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return {"ok": True, "model": model}
