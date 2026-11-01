"""Local web UI + JSON API + WebSocket event stream.

Security model (local, single user):
- binds to 127.0.0.1 by default;
- a random token (kept in <data_dir>/secrets/web_token) is exchanged once at /login for an HttpOnly,
  SameSite=Strict cookie, so other sites cannot ride on it (no CSRF) and the token is not in every URL;
- mutating requests need an `X-Requested-With: actoroffice` header (a second CSRF guard);
- the Host header must be a loopback name (blocks DNS-rebinding), WebSocket Origin must match.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import secrets
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.automation import AutomationStatus
from actoroffice.domain.chat import ConversationKind
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.ids import new_id
from actoroffice.domain.knowledge import MemoryKind, MemoryScope, SkillRef, SkillScope
from actoroffice.domain.org import ActorStatus, PermissionMode, Position, PositionPolicy, Rule, WorkspaceKind
from actoroffice.domain.run import RunStatus
from actoroffice.domain.work import Priority, ProjectAccess, TicketStatus

from . import team_api

STATIC = Path(__file__).parent / "static"
COOKIE = "ao_session"
LOOPBACK = {"127.0.0.1", "localhost", "[::1]"}

# Windows often maps .js to text/plain in the registry; with nosniff the browser would refuse to run it.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")


def load_or_create_token(settings: Settings) -> str:
    path = settings.data_dir / "secrets" / "web_token"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            return token
    token = secrets.token_urlsafe(32)
    path.write_text(token, encoding="utf-8")
    return token


# ---- request bodies ---------------------------------------------------------------------------------


class NewConversation(BaseModel):
    kind: ConversationKind
    project_id: str | None = None
    actor_id: str | None = None
    title: str = ""
    actor_ids: list[str] = []


class NewMessage(BaseModel):
    body: str


class Members(BaseModel):
    actor_ids: list[str]


class Answer(BaseModel):
    approved: bool | None = None
    remember: str | None = None  # an "always allow" rule chosen from the card's suggestions
    confirmed: bool | None = None
    answer: str | None = None
    title: str | None = None
    description: str | None = None
    run_now: bool | None = None  # approving an automation also runs it once
    access: str | None = None  # approving project access: "worktree" or "direct"
    note: str | None = None  # a stage gate sent back (or knowledge items dropped: "bỏ 2, 5")


class NewTicket(BaseModel):
    title: str
    description: str = ""
    assignee_id: str | None = None
    priority: Priority = Priority.MEDIUM
    project_id: str | None = None
    type: str = "task"
    due_at: date | None = None


class ChangeBody(BaseModel):
    summary: str
    reopen_done: bool = False


class DueBody(BaseModel):
    due_at: date | None = None


class Toggle(BaseModel):
    enabled: bool


class StatusChange(BaseModel):
    status: TicketStatus
    comment: str = ""


class Assign(BaseModel):
    actor_id: str | None = None


class NewActor(BaseModel):
    position_id: str
    name: str = ""  # empty: the next suggested English name
    specialty: str = ""
    emoji: str = ""
    model: str | None = None


class ActorPatch(BaseModel):
    name: str | None = None
    status: ActorStatus | None = None
    emoji: str | None = None
    specialty: str | None = None
    model: str | None = None
    position_id: str | None = None
    permission_mode: PermissionMode | None = None
    allow_rules: list[str] | None = None


class ProjectBody(BaseModel):
    name: str
    description: str = ""
    path: str | None = None
    access: ProjectAccess = ProjectAccess.READ_ONLY


class PositionBody(BaseModel):
    name: str
    title: str
    instructions: str = ""
    workspace_kind: WorkspaceKind = WorkspaceKind.FOLDER
    model: str = "sonnet"
    prompt_preset: str = "custom"
    tools: list[str] = Field(default_factory=lambda: ["Read", "Write", "Edit", "Glob", "Grep"])
    exec_default: Rule = "ask"
    bash_allow: list[str] = []
    web: Rule = "deny"
    external: Rule = "ask"
    max_turns: int = 40
    done_docs: list[str] = []


class MemoryBody(BaseModel):
    content: str
    kind: MemoryKind = MemoryKind.FACT
    scope: MemoryScope = MemoryScope.ACTOR
    pinned: bool = False


class MemoryPatch(BaseModel):
    content: str | None = None
    pinned: bool | None = None


class SkillBody(BaseModel):
    description: str
    body: str


class FileBody(BaseModel):
    content: str


ALLOWED_TOOLS = {"Read", "Write", "Edit", "Glob", "Grep", "Bash", "WebFetch", "WebSearch", "TodoWrite", "NotebookEdit"}


def run_usage(r: Any) -> dict[str, Any]:
    duration = None
    if r.started_at and r.finished_at:
        duration = int((datetime.fromisoformat(r.finished_at) - datetime.fromisoformat(r.started_at)).total_seconds())
    return {**r.usage.model_dump(), "cost_usd": r.cost_usd, "stats": r.stats, "duration_s": duration}


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def create_app(office: Office, token: str) -> FastAPI:
    app = FastAPI(title="actor-office", docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    team_api.register(app, office)
    team_api.register_rules(app, office)

    def authed(request: Request) -> bool:
        return secrets.compare_digest(request.cookies.get(COOKIE, ""), token)

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host not in LOOPBACK:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        path = request.url.path
        if path.startswith("/api/") and not authed(request):
            return JSONResponse({"error": "not signed in"}, status_code=401)
        if request.method not in ("GET", "HEAD") and request.headers.get("x-requested-with") != "actoroffice":
            return JSONResponse({"error": "missing request header"}, status_code=403)
        response = await call_next(request)
        if path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"  # revalidate; index links carry a version anyway
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; connect-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(OfficeError)
    async def office_error(_request: Request, exc: OfficeError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=exc.code)

    @app.exception_handler(ValueError)
    async def value_error(_request: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=422)

    # ---- pages -------------------------------------------------------------------------------

    @app.get("/login")
    async def login(t: str = "", next: str = "/") -> Any:
        if not secrets.compare_digest(t, token):
            return HTMLResponse(_SIGNED_OUT, status_code=401)
        target = next if next in ("/", "/team") else "/"  # never an open redirect
        response = RedirectResponse(target, status_code=303)
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=60 * 60 * 24 * 30)
        return response

    @app.get("/team")
    async def team_page(request: Request) -> Any:
        """The team copilot UI (herdr-like: teams and tickets, tabs, split panes, Files/Changes)."""
        if not authed(request):
            return HTMLResponse(_SIGNED_OUT, status_code=401)
        folder = STATIC / "team"
        version = str(int(max((folder / f).stat().st_mtime for f in ("team.js", "team.css"))))
        html = (folder / "index.html").read_text(encoding="utf-8").replace("{{v}}", version)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    @app.get("/")
    async def index(request: Request) -> Any:
        if not authed(request):
            return HTMLResponse(_SIGNED_OUT, status_code=401)
        version = str(int(max((STATIC / f).stat().st_mtime for f in ("app.js", "app.css"))))
        html = (STATIC / "index.html").read_text(encoding="utf-8").replace("{{v}}", version)
        return HTMLResponse(html, headers={"Cache-Control": "no-store"})

    # ---- state -------------------------------------------------------------------------------

    @app.get("/api/state")
    async def state() -> dict[str, Any]:
        return {
            "language": office.settings.language,
            "max_runs": office.settings.max_concurrent_runs,
            "projects": [p.model_dump() for p in await office.projects.list()],
            "default_project_id": office.chat.default_project_id,
            "positions": [p.model_dump() for p in await office.org.list_positions()],
            "actors": [a.model_dump() for a in await office.org.list_actors()],
            "conversations": await office.chat.list(),
            "pending": [i.model_dump() for i in await office.interactions.pending()],
            "runs": [r.model_dump() for r in await office.runs.by_status(RunStatus.RUNNING)],
            "queued": [{"actor_id": w.actor_id, "ticket_id": w.ticket_id} for w in await office.wakes.pending(due_only=True)],
            "capacity": office.scheduler.capacity,
            "rate_limit": office.scheduler.last_rate_limit,
            "suggested_name": await office.org.suggest_name(),
        }

    @app.get("/api/usage")
    async def usage() -> dict[str, Any]:
        return {
            "today": await office.runs.usage_by_actor(since=_since(1)),
            "week": await office.runs.usage_by_actor(since=_since(7)),
            "all": await office.runs.usage_by_actor(),
            "rate_limit": office.scheduler.last_rate_limit,
            "capacity": office.scheduler.capacity,
        }

    # ---- projects ----------------------------------------------------------------------------

    @app.post("/api/projects")
    async def new_project(body: ProjectBody) -> dict[str, Any]:
        return (await office.projects.create(body.name, path=body.path, access=body.access, description=body.description)).model_dump()

    @app.put("/api/projects/{project_id}")
    async def edit_project(project_id: str, body: ProjectBody) -> dict[str, Any]:
        project = await office.projects.update(project_id, name=body.name, path=body.path, access=body.access, description=body.description)
        return project.model_dump()

    # ---- chat --------------------------------------------------------------------------------

    @app.post("/api/conversations")
    async def new_conversation(body: NewConversation) -> dict[str, Any]:
        if body.kind == ConversationKind.DM:
            if not body.actor_id:
                raise HTTPException(422, "actor_id is required")
            return (await office.chat.dm(body.actor_id, body.project_id)).model_dump()
        if not body.actor_ids:
            raise HTTPException(422, "pick at least one actor")
        conv = await office.chat.create_group(body.title, body.actor_ids, created_by="Bạn", project_id=body.project_id)
        return conv.model_dump()

    @app.get("/api/conversations/{conv_id}")
    async def conversation(conv_id: str) -> dict[str, Any]:
        conv = await office.chat.get(conv_id)
        tickets = await office.tickets.list(origin_conversation_id=conv_id, limit=50)
        chat_runs = await office.runs.chat_runs(conv_id)
        messages = await office.chat.messages(conv_id, limit=200)
        work = {}
        for key in {m.meta.get("ticket_key") for m in messages if m.meta.get("report")} - {None}:
            with contextlib.suppress(OfficeError):
                work[key] = await office.runs.work_summary((await office.tickets.get(key)).id)
        return {
            "conversation": conv.model_dump(),
            "messages": [m.model_dump() for m in messages],
            "tickets": [t.model_dump() for t in tickets],
            "usage": await office.runs.usage_summary(conversation_id=conv_id, ticket_ids=[t.id for t in tickets]),
            "run_usage": {r.id: run_usage(r) for r in chat_runs},
            "ticket_work": work,
        }

    @app.get("/api/conversations/{conv_id}/messages")
    async def messages(conv_id: str, after: int = 0) -> list[dict[str, Any]]:
        return [m.model_dump() for m in await office.chat.messages(conv_id, after_id=after)]

    @app.post("/api/conversations/{conv_id}/messages")
    async def post_message(conv_id: str, body: NewMessage) -> dict[str, Any]:
        text = body.body.strip()
        if not text:
            raise HTTPException(422, "empty message")
        msg = await office.post_user_message(conv_id, text[:20000])
        office.scheduler.kick()
        return msg.model_dump()

    @app.post("/api/conversations/{conv_id}/members")
    async def add_members(conv_id: str, body: Members) -> dict[str, Any]:
        await office.chat.add_members(conv_id, body.actor_ids, by="Bạn")
        return (await office.chat.get(conv_id)).model_dump()

    @app.post("/api/conversations/{conv_id}/new-session")
    async def new_session(conv_id: str) -> dict[str, Any]:
        """Like /clear: actors start a fresh session in this chat (long-term memory is kept)."""
        conv = await office.chat.get(conv_id)
        cleared = 0
        for actor_id in conv.actor_ids:
            cleared += await office.runs.clear_sessions(actor_id=actor_id, key_prefix=f"{actor_id}:c{conv_id}")
        await office.chat.post_system(conv_id, "Bắt đầu phiên mới: các actor sẽ không nhớ lịch sử chat cũ (memory dài hạn vẫn giữ).")
        return {"cleared": cleared}

    # ---- interactions ------------------------------------------------------------------------

    @app.get("/api/interactions/{interaction_id}")
    async def interaction(interaction_id: str) -> dict[str, Any]:
        return (await office.interactions.get(interaction_id)).model_dump()

    @app.post("/api/interactions/{interaction_id}/answer")
    async def answer(interaction_id: str, body: Answer) -> dict[str, Any]:
        response = body.model_dump(exclude_none=True)
        if not response:
            raise HTTPException(422, "empty answer")
        resolved = await office.answer(interaction_id, response, by="user")
        office.scheduler.kick()
        return resolved.model_dump()

    # ---- tickets -----------------------------------------------------------------------------

    @app.get("/api/tickets")
    async def tickets(all: bool = False, project: str | None = None) -> list[dict[str, Any]]:
        statuses = None if all else [s for s in TicketStatus if s not in (TicketStatus.DONE, TicketStatus.CANCELLED)]
        return [t.model_dump() for t in await office.tickets.list(status=statuses, project_id=project, limit=300)]

    @app.post("/api/tickets")
    async def new_ticket(body: NewTicket) -> dict[str, Any]:
        ticket = await office.tickets.create(
            body.title.strip(),
            description=body.description,
            priority=body.priority,
            assignee_actor_id=body.assignee_id,
            project_id=body.project_id or office.chat.default_project_id,
            type=body.type.strip() or "task",
            due_at=body.due_at.isoformat() if body.due_at else None,
        )
        office.scheduler.kick()
        return ticket.model_dump()

    @app.get("/api/tickets/{key}")
    async def ticket(key: str) -> dict[str, Any]:
        t = await office.tickets.get(key)
        children = await office.tickets.list(parent_id=t.id)
        root = await office.tickets.root_of(t.id)
        room = await office.db.fetchone(
            "SELECT id FROM conversations WHERE ticket_id = ? AND kind = 'group' AND archived = 0", (root.id,)
        )
        parent = await office.tickets.get(t.parent_id) if t.parent_id else None
        return {
            "ticket": t.model_dump(),
            "parent": parent.model_dump() if parent else None,
            "root": root.model_dump() if root.id != t.id else None,
            "meeting_room": room["id"] if room else None,
            "comments": [c.model_dump() for c in await office.tickets.comments(t.id, limit=500)],
            "documents": [d.model_dump() for d in await office.tickets.list_documents(t.id)],
            "children": [c.model_dump() for c in children],
            "blockers": await office.tickets.open_blockers(t.id),
            "work_products": await office.tickets.work_products(t.id),
            "runs": [r.model_dump() for r in await office.runs.for_ticket(t.id)],
            "pending": [i.model_dump() for i in await office.interactions.pending(ticket_id=t.id)],
            "usage": await office.runs.usage_summary(ticket_ids=[t.id, *[c.id for c in children]]),
        }

    @app.post("/api/tickets/{key}/comments")
    async def comment(key: str, body: NewMessage) -> dict[str, Any]:
        c = await office.tickets.add_comment(key, body.body.strip(), author_kind="user", author_id="user")
        office.scheduler.kick()
        return c.model_dump()

    @app.post("/api/tickets/{key}/status")
    async def set_status(key: str, body: StatusChange) -> dict[str, Any]:
        t = await office.tickets.transition(key, body.status, by="user", comment=body.comment or None)
        return t.model_dump()

    @app.post("/api/tickets/{key}/change")
    async def change(key: str, body: ChangeBody) -> dict[str, Any]:
        """The user changes a requirement: every affected ticket in the tree is told (and done ones reopened)."""
        summary = body.summary.strip()
        if not summary:
            raise HTTPException(422, "Mô tả thay đổi")
        result = await office.tickets.change_notice(key, summary, by="Bạn", reopen_done=body.reopen_done)
        office.scheduler.kick()
        return result

    @app.get("/api/tickets/{key}/work")
    async def ticket_work(key: str) -> dict[str, Any]:
        return await office.runs.work_summary((await office.tickets.get(key)).id)

    @app.get("/api/tickets/{key}/merge")
    async def merge_status(key: str) -> dict[str, Any]:
        return await office.merges.status(key)

    @app.post("/api/tickets/{key}/merge")
    async def merge(key: str) -> dict[str, Any]:
        """The user's approved merge of a finished ticket tree into the project's main branch."""
        return await office.merges.merge(key, by="Bạn")

    @app.post("/api/tickets/{key}/due")
    async def set_due(key: str, body: DueBody) -> dict[str, Any]:
        return (await office.tickets.set_due(key, body.due_at.isoformat() if body.due_at else None)).model_dump()

    @app.post("/api/tickets/{key}/assign")
    async def assign(key: str, body: Assign) -> dict[str, Any]:
        t = await office.tickets.assign(key, body.actor_id, by="user")
        office.scheduler.kick()
        return t.model_dump()

    # ---- runs --------------------------------------------------------------------------------

    @app.get("/api/runs")
    async def recent_runs(limit: int = 40, actor_id: str | None = None) -> list[dict[str, Any]]:
        return await office.runs.recent(limit=min(limit, 200), actor_id=actor_id)

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> list[dict[str, Any]]:
        return await office.runs.events(run_id)

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict[str, Any]:
        await office.scheduler.stop_run(run_id)
        return {"ok": True}

    # ---- automations -------------------------------------------------------------------------

    @app.get("/api/automations")
    async def automations(actor_id: str | None = None) -> list[dict[str, Any]]:
        rows = []
        for a in await office.automations.list(owner_actor_id=actor_id):
            last = await office.automations.runs(a.id, limit=1)
            rows.append({**a.model_dump(), "last_run": last[0].model_dump() if last else None, "running": a.id in office.automations.running_ids})
        return rows

    @app.get("/api/automations/{ref}")
    async def automation(ref: str) -> dict[str, Any]:
        a = await office.automations.get(ref)
        return {
            "automation": a.model_dump(),
            "code": {s.file: office.automations.script_code(a, s.file) for s in a.steps if s.file},
            "runs": [r.model_dump() for r in await office.automations.runs(a.id, limit=30)],
            "outputs": office.automations.outputs(a),
            "running": a.id in office.automations.running_ids,
        }

    @app.get("/api/automations/{ref}/files/{path:path}")
    async def automation_file(ref: str, path: str) -> dict[str, Any]:
        a = await office.automations.get(ref)
        return {"path": path, "content": office.automations.read_output(a, path)}

    @app.post("/api/automations/{ref}/run")
    async def run_automation(ref: str) -> dict[str, Any]:
        a = await office.automations.get(ref)
        if a.status == AutomationStatus.PENDING_REVIEW:
            raise HTTPException(409, "Automation chưa được duyệt")
        office.automations.start(a, "manual")
        return {"started": True}

    @app.post("/api/automations/{ref}/enabled")
    async def toggle_automation(ref: str, body: Toggle) -> dict[str, Any]:
        a = await office.automations.get(ref)
        if a.status == AutomationStatus.PENDING_REVIEW:
            raise HTTPException(409, "Duyệt automation trên thẻ yêu cầu trước")
        return (await office.automations.set_enabled(a.id, body.enabled, by="user")).model_dump()

    @app.delete("/api/automations/{ref}")
    async def delete_automation(ref: str) -> dict[str, Any]:
        await office.automations.delete(ref)
        return {"ok": True}

    # ---- team: positions ---------------------------------------------------------------------

    @app.post("/api/positions/sync")
    async def sync_positions() -> dict[str, Any]:
        """Refresh the built-in positions and standard skills from the templates shipped with the app."""
        created = await office.install_default_positions(update=True)
        office.bus.publish("team.changed")
        return {"created": [p.name for p in created]}

    def _position(body: PositionBody, position_id: str) -> Position:
        name = body.name.strip().lower()
        if not name or not name.replace("-", "").replace("_", "").isalnum():
            raise HTTPException(422, "Mã vị trí: chữ thường, số, '-' hoặc '_'")
        tools = [t for t in body.tools if t in ALLOWED_TOOLS]
        return Position(
            id=position_id,
            name=name,
            title=body.title.strip() or name,
            instructions=body.instructions,
            workspace_kind=body.workspace_kind,
            model=body.model,
            prompt_preset="claude_code" if body.prompt_preset == "claude_code" else "custom",
            tools=tools,
            policy=PositionPolicy(
                exec_default=body.exec_default,
                bash_allow=[p.strip() for p in body.bash_allow if p.strip()],
                web=body.web,
                external=body.external,
                max_turns=max(5, min(body.max_turns, 200)),
            ),
            done_docs=[d.strip() for d in body.done_docs if d.strip()],
        )

    @app.post("/api/positions")
    async def new_position(body: PositionBody) -> dict[str, Any]:
        position = await office.org.create_position(_position(body, new_id("pos")))
        office.bus.publish("team.changed")
        return position.model_dump()

    @app.put("/api/positions/{position_id}")
    async def edit_position(position_id: str, body: PositionBody) -> dict[str, Any]:
        await office.org.get_position(position_id)
        position = await office.org.update_position(_position(body, position_id))
        office.bus.publish("team.changed")
        return position.model_dump()

    # ---- team: actors ------------------------------------------------------------------------

    @app.post("/api/actors")
    async def hire(body: NewActor) -> dict[str, Any]:
        name = body.name.strip() or await office.org.suggest_name()
        if " " in name:
            raise HTTPException(422, "Tên một từ, không dấu cách (dùng để @mention)")
        actor = await office.org.hire(body.position_id, name, emoji=body.emoji.strip(), specialty=body.specialty.strip(), model=body.model or None)
        office.bus.publish("team.changed")
        return actor.model_dump()

    @app.get("/api/actors/{actor_id}")
    async def actor_profile(actor_id: str) -> dict[str, Any]:
        actor = await office.org.get_actor(actor_id)
        position = await office.org.get_position(actor.position_id)
        runs = await office.runs.by_actor(actor_id, limit=20)
        return {
            "actor": actor.model_dump(),
            "position": position.model_dump(),
            "skills": [s.model_dump() for s in office.skills.for_actor(actor, position)],
            "memories": [m.model_dump() for m in await office.memory.visible(actor)],
            "usage": {
                "today": await office.runs.usage_summary(actor_id=actor_id, since=_since(1)),
                "week": await office.runs.usage_summary(actor_id=actor_id, since=_since(7)),
                "all": await office.runs.usage_summary(actor_id=actor_id),
            },
            "runs": [r.model_dump() for r in runs],
            "automations": [a.model_dump() for a in await office.automations.list(owner_actor_id=actor_id, active_only=True)],
        }

    @app.patch("/api/actors/{actor_id}")
    async def edit_actor(actor_id: str, body: ActorPatch) -> dict[str, Any]:
        changes = body.model_dump(exclude_none=True)
        name = changes.pop("name", None)
        if name is not None:
            await office.rename_actor(actor_id, name, by="user")
        status = changes.pop("status", None)
        if status is not None:
            await office.org.set_actor_status(actor_id, ActorStatus(status))
            office.scheduler.kick()
        if "model" in changes and not changes["model"]:
            changes["model"] = None
        if changes:
            before = await office.org.get_actor(actor_id)
            await office.org.update_actor(actor_id, **changes)
            if "permission_mode" in changes or "allow_rules" in changes:
                await office.audit.record(
                    "permission:update",
                    "changed",
                    actor_id=actor_id,
                    detail={"from": {"mode": before.permission_mode, "rules": before.allow_rules}, "to": changes},
                )
        office.bus.publish("team.changed")
        return (await office.org.get_actor(actor_id)).model_dump()

    # ---- memory ------------------------------------------------------------------------------

    @app.post("/api/actors/{actor_id}/memories")
    async def add_memory(actor_id: str, body: MemoryBody) -> dict[str, Any]:
        actor = await office.org.get_actor(actor_id)
        memory = await office.memory.add(actor, body.content, kind=body.kind, scope=body.scope, created_by="user", pinned=body.pinned)
        return memory.model_dump()

    @app.post("/api/actors/{actor_id}/memories/clear")
    async def clear_memories(actor_id: str) -> dict[str, Any]:
        return {"cleared": await office.memory.clear(await office.org.get_actor(actor_id))}

    @app.post("/api/actors/{actor_id}/memories/compact")
    async def compact(actor_id: str) -> dict[str, Any]:
        from actoroffice.engine.compaction import compact_memories

        result = await compact_memories(office, await office.org.get_actor(actor_id))
        office.bus.publish("memory.saved", memory={"created_by": "compaction"}, actor_id=actor_id)
        return result

    @app.patch("/api/memories/{memory_id}")
    async def edit_memory(memory_id: int, body: MemoryPatch) -> dict[str, Any]:
        return (await office.memory.update(memory_id, content=body.content, pinned=body.pinned)).model_dump()

    @app.delete("/api/memories/{memory_id}")
    async def delete_memory(memory_id: int) -> dict[str, Any]:
        await office.memory.delete(memory_id)
        return {"ok": True}

    # ---- skills ------------------------------------------------------------------------------

    def _ref(scope: SkillScope, owner: str, name: str) -> SkillRef:
        return SkillRef(scope=scope, owner="" if scope == SkillScope.SHARED else owner, name=name)

    @app.get("/api/skills")
    async def skills(scope: SkillScope | None = None, owner: str | None = None) -> list[dict[str, Any]]:
        return [s.model_dump() for s in office.skills.list(scope, owner)]

    @app.get("/api/skills/{scope}/{owner}/{name}")
    async def skill(scope: SkillScope, owner: str, name: str) -> dict[str, Any]:
        return office.skills.get(_ref(scope, owner, name)).model_dump()

    @app.put("/api/skills/{scope}/{owner}/{name}")
    async def save_skill(scope: SkillScope, owner: str, name: str, body: SkillBody) -> dict[str, Any]:
        return (await office.skills.save(_ref(scope, owner, name), body.description, body.body, author="user")).model_dump()

    @app.delete("/api/skills/{scope}/{owner}/{name}")
    async def delete_skill(scope: SkillScope, owner: str, name: str) -> dict[str, Any]:
        await office.skills.delete(_ref(scope, owner, name), author="user")
        return {"ok": True}

    @app.get("/api/skills/{scope}/{owner}/{name}/files/{path:path}")
    async def skill_file(scope: SkillScope, owner: str, name: str, path: str) -> dict[str, Any]:
        return {"path": path, "content": office.skills.read_file(_ref(scope, owner, name), path)}

    @app.put("/api/skills/{scope}/{owner}/{name}/files/{path:path}")
    async def save_skill_file(scope: SkillScope, owner: str, name: str, path: str, body: FileBody) -> dict[str, Any]:
        return (await office.skills.save_file(_ref(scope, owner, name), path, body.content, author="user")).model_dump()

    @app.delete("/api/skills/{scope}/{owner}/{name}/files/{path:path}")
    async def delete_skill_file(scope: SkillScope, owner: str, name: str, path: str) -> dict[str, Any]:
        await office.skills.delete(_ref(scope, owner, name), author="user", path=path)
        return {"ok": True}

    # ---- live events -------------------------------------------------------------------------

    @app.websocket("/api/ws")
    async def ws(socket: WebSocket) -> None:
        host = socket.headers.get("host", "")
        origin = socket.headers.get("origin", "")
        if not secrets.compare_digest(socket.cookies.get(COOKIE, ""), token) or origin not in (f"http://{host}", f"https://{host}"):
            await socket.close(code=4401)
            return
        await socket.accept()
        async with office.bus.subscribe() as queue:

            async def pump() -> None:
                while True:
                    event = await queue.get()
                    await socket.send_text(json.dumps({"kind": event.kind, "data": event.data, "ts": event.ts}, default=str, ensure_ascii=False))

            async def drain() -> None:
                while True:
                    await socket.receive_text()

            tasks = [asyncio.create_task(pump()), asyncio.create_task(drain())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            except WebSocketDisconnect:
                pass
            finally:
                for task in tasks:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
                        await task

    return app


_SIGNED_OUT = """<!doctype html><html lang="vi"><meta charset="utf-8"><title>actor-office</title>
<body style="font-family:system-ui;max-width:32rem;margin:15vh auto;padding:0 1rem;color:#333">
<h2>Chưa đăng nhập</h2>
<p>Mở đường link đăng nhập được in trong terminal khi chạy <code>actoroffice serve</code>.</p>
</body></html>"""
