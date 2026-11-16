"""JSON API for team copilot: people, teams, the stage board (sidebar), stage controls and run activity."""

from __future__ import annotations

import difflib
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from actoroffice.app import Office
from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.stage import TEMPLATE_TITLES, default_templates
from actoroffice.domain.team import LOCAL_OWNER_ID, TeamSettings
from actoroffice.domain.work import TicketStatus
from actoroffice.services.docflow import pack_templates
from actoroffice.services.stages import stage_title


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
    cmd: str  # pause | resume | retry | stop | steer | merged | reopen
    text: str = ""
    stage: str = ""  # reopen: the stage to go back to


CLOSED = (TicketStatus.DONE, TicketStatus.CANCELLED)
CLOSED_PAGE = 20  # closed tickets per sidebar page
CLOSED_MAX = 50
ARTIFACT_KINDS = ("screenshot", "video", "trace", "report", "file")


class ParticipantBody(BaseModel):
    user_id: str
    roles: list[str]


USER_HEADER = "x-office-user"
DOCS_TTL_S = 5.0  # how long the documents panel of a ticket is reused when nothing it is made from changed


async def acting_user(request: Request, office: Office) -> str:
    """Who a web request acts as: the person picked in the sidebar ("Tôi là …", header X-Office-User) when the id names
    an active user, else the local owner. A convenience on a single shared login, NOT authentication: whoever holds
    the office's login cookie may pick anyone. It makes the audit trail and the role checks honest between
    colleagues who share one office; it does not protect against a colleague who lies."""
    raw = unquote(request.headers.get(USER_HEADER) or "").strip()
    if not raw or raw == LOCAL_OWNER_ID:
        return LOCAL_OWNER_ID
    try:
        user = await office.teams.get_user(raw)
    except OfficeError:
        return LOCAL_OWNER_ID
    return user.id if getattr(user, "active", True) else LOCAL_OWNER_ID


def _rest_lines(body: str, items: list[Any]) -> list[str]:
    """The lines of a document that belong to no item (headings of sections, free text, tables)."""
    ids = {i.id for i in items}
    inside = Counter(line.strip() for i in items for line in i.body.splitlines()[1:] if line.strip())
    out = []
    for line in body.splitlines():
        head = re.match(r"\s*(?:#{1,6}\s+|[-*]\s+)?(?:\*\*|__)?([A-Za-z]+-[A-Za-z0-9.]+)", line)
        if head and head.group(1).rstrip(".") in ids:
            continue
        key = line.strip()
        if key and inside[key] > 0:
            inside[key] -= 1
            continue
        out.append(line)
    return out


def doc_diff(old_body: str, new_body: str, doctype: str | None, pack: Any) -> dict[str, Any]:
    """What changed between two revisions of a document: items added / changed / removed by id with both texts
    (Doc Core's parser tells which), and a line diff of everything that is not an item."""
    from actoroffice.doccore import impact, parse

    items: list[dict[str, Any]] = []
    old_rest, new_rest = old_body.splitlines(), new_body.splitlines()
    if doctype is not None:
        old, new = parse(old_body, doctype, pack), parse(new_body, doctype, pack)
        before, after = {i.id: i for i in old.items}, {i.id: i for i in new.items}
        changed = impact(old, new)
        for item_id in [i for i in after if i in changed] + [i for i in before if i in changed and i not in after]:
            a, b = before.get(item_id), after.get(item_id)
            ref = b or a
            items.append({
                "id": item_id, "kind": ref.kind, "title": ref.title, "change": "added" if a is None else "removed" if b is None else "changed",
                "old": a.body if a else "", "new": b.body if b else "",
            })  # fmt: skip
        old_rest, new_rest = _rest_lines(old_body, old.items), _rest_lines(new_body, new.items)
    rest = [line for line in difflib.unified_diff(old_rest, new_rest, lineterm="", n=2) if not line.startswith(("---", "+++"))]
    return {"items": items, "rest": rest, "same": not items and not rest}


def register(app: FastAPI, office: Office) -> None:
    teams = office.teams
    docs_cache: dict[int, tuple[float, tuple[Any, ...], Any]] = {}

    @app.middleware("http")
    async def drop_docs_cache(request: Request, call_next: Any) -> Any:
        # anything a person does on the web (an answer, an accepted difference, a control) shows at once
        if request.method not in ("GET", "HEAD"):
            docs_cache.clear()
        return await call_next(request)

    async def docs_panel(ticket: Any) -> Any:
        """`office.docs.panel` re-reads and re-checks every document of the ticket and of its tasks; the page asks for
        it on every live event. Reused for a few seconds while the ticket, its documents and its tasks are unchanged."""
        row = await office.db.fetchone(
            "SELECT (SELECT COALESCE(SUM(revision), 0) || ':' || COALESCE(SUM(approved_revision), 0) || ':' || COUNT(*) || ':' || COALESCE(MAX(updated_at), '')"
            " FROM documents WHERE ticket_id = ? OR ticket_id = ? OR ticket_id IN (SELECT id FROM tickets WHERE parent_id = ?)) AS docs,"
            " (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), '') FROM tickets WHERE parent_id = ?) AS kids",
            (ticket.id, ticket.parent_id or 0, ticket.id, ticket.id),
        )
        sig = (ticket.updated_at, ticket.stage, ticket.status.value, row["docs"] if row else "", row["kids"] if row else "")
        hit = docs_cache.get(ticket.id)
        now = time.monotonic()
        if hit and hit[1] == sig and now - hit[0] < DOCS_TTL_S:
            return hit[2]
        panel = await office.docs.panel(ticket)
        docs_cache[ticket.id] = (now, sig, panel)
        if len(docs_cache) > 200:
            for old in sorted(docs_cache, key=lambda k: docs_cache[k][0])[:100]:
                docs_cache.pop(old, None)
        return panel

    # ---- people & teams ---------------------------------------------------------------------------------

    @app.get("/api/people")
    async def people() -> dict[str, Any]:
        result = []
        for team in await teams.list_teams():
            result.append({**team.model_dump(), "members": [m.model_dump() for m in await teams.members(team.id)]})
        return {"teams": result, "users": [u.model_dump() for u in await teams.list_users()], "templates": [*default_templates(), *pack_templates()], "template_titles": TEMPLATE_TITLES}

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
    async def board(
        viewer: str = LOCAL_OWNER_ID, closed: bool = False, limit: int = CLOSED_PAGE, offset: int = 0, team: str | None = None
    ) -> dict[str, Any]:
        """Teams → tickets running through stages, each with its derived status; "needs you" for the viewer.
        `closed=true`: one page of done/cancelled tickets instead, read from the tickets table only (no derived status).
        `team`: the closed page and `closed_count` only count that team's tickets (the sidebar's team filter)."""
        closed_where = f"stage IS NOT NULL AND status IN ({','.join('?' * len(CLOSED))})"
        closed_args: list[Any] = [s.value for s in CLOSED]
        if team:
            closed_where += " AND team_id = ?"
            closed_args.append(team)
        count = await office.db.fetchone(f"SELECT COUNT(*) AS n FROM tickets WHERE {closed_where}", closed_args)
        closed_count = count["n"] if count else 0
        if closed:
            limit = max(1, min(limit, CLOSED_MAX))
            offset = max(0, offset)
            page = await office.db.fetchall(
                "SELECT id, key, title, team_id, stage, stage_template, status, updated_at FROM tickets"
                f" WHERE {closed_where} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
                [*closed_args, limit + 1, offset],
            )
            return {
                "tickets": [
                    {
                        "id": r["id"], "ticket": r["key"], "title": r["title"], "team_id": r["team_id"], "stage": r["stage"],
                        "stage_title": stage_title(r["stage"]), "template": r["stage_template"], "updated_at": r["updated_at"],
                        "ticket_status": r["status"], "status": r["status"],
                    }
                    for r in page[:limit]
                ],
                "has_more": len(page) > limit,
                "limit": limit,
                "offset": offset,
                "closed_count": closed_count,
            }
        statuses = [s for s in TicketStatus if s not in CLOSED]
        rows = await office.db.fetchall(
            f"SELECT id FROM tickets WHERE stage IS NOT NULL AND status IN ({','.join('?' * len(statuses))}) ORDER BY updated_at DESC LIMIT 300",
            [s.value for s in statuses],
        )
        items = []
        for row in rows:
            ticket = await office.tickets.get(row["id"])
            st = await office.stages.status(ticket, viewer=viewer)
            last = await office.runs.last_activity(st["run_id"]) if st["run_id"] else None
            items.append({**st, "id": ticket.id, "team_id": ticket.team_id, "updated_at": ticket.updated_at, "activity": last})
        by_team = await office.db.fetchall(
            f"SELECT team_id, COUNT(*) AS n FROM tickets WHERE {closed_where} AND team_id IS NOT NULL GROUP BY team_id", closed_args
        )
        team_rows = await teams.list_teams()
        backlog_count, backlog = await office.proposals.backlog()
        return {
            "teams": [{"id": t.id, "name": t.name, "needs_you": sum(1 for i in items if i["team_id"] == t.id and i["needs_viewer"])} for t in team_rows],
            "tickets": items,
            "needs_you": sum(1 for i in items if i["needs_viewer"]),
            "closed_count": closed_count,
            "closed_by_team": {r["team_id"]: r["n"] for r in by_team},
            "backlog_count": backlog_count,
            "backlog": backlog,
            "projects": [{"id": p.id, "name": p.name} for p in await office.projects.list()],  # choices on proposal cards
        }

    # ---- stages ----------------------------------------------------------------------------------------------

    @app.post("/api/tasks")
    async def new_task(body: TaskBody, request: Request) -> dict[str, Any]:
        """Create a ticket and start it on a stage template in one step (what /task does on Telegram)."""
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "Cần tiêu đề")
        ticket = await office.tickets.create(
            title[:200], description=body.description or title, project_id=body.project_id or office.chat.default_project_id, created_by="user"
        )
        ticket = await office.stages.start(
            ticket.id, template=body.template, by=await acting_user(request, office), team_id=body.team_id, owner_user_id=body.owner_user_id, mode=body.mode
        )
        office.scheduler.kick()
        return ticket.model_dump()

    @app.post("/api/tickets/{key}/stages/start")
    async def start_stages(key: str, body: StartBody, request: Request) -> dict[str, Any]:
        ticket = await office.stages.start(
            key, template=body.template, by=await acting_user(request, office), team_id=body.team_id, owner_user_id=body.owner_user_id, mode=body.mode
        )
        office.scheduler.kick()
        return ticket.model_dump()

    @app.post("/api/tickets/{key}/backlog/start")
    async def start_backlog(key: str, request: Request) -> dict[str, Any]:
        """The "Bắt đầu" button of the sidebar's Backlog list (SI-24): run the ticket's suggested workflow."""
        ticket = await office.proposals.start_backlog(key, by=await acting_user(request, office))
        office.scheduler.kick()
        return ticket.model_dump()

    async def card_rights(ticket: Any, viewer: str) -> dict[str, Any]:
        """Per pending card of the ticket: may the person looking answer it (the same role check the answer runs)."""
        out: dict[str, Any] = {}
        for item in await office.interactions.pending(ticket_id=ticket.id):
            roles = [str(r) for r in item.payload.get("roles") or []]
            may = viewer == LOCAL_OWNER_ID or await teams.can_act(viewer, ticket, roles)
            out[item.id] = {"may_answer": may, "approved": any(a.get("by") == viewer for a in item.payload.get("approvals") or [])}
        return out

    @app.get("/api/tickets/{key}/stage")
    async def stage(key: str, request: Request) -> dict[str, Any]:
        ticket = await office.tickets.get(key)
        if not ticket.stage:
            raise HTTPException(404, f"{ticket.key} không chạy theo quy trình giai đoạn")
        template = await office.stages.template_of(ticket)
        shape = template.model_dump()
        for s in shape["stages"]:
            s["title"] = s["title"] or stage_title(s["id"])  # the reopen sheet shows names, sends ids
        viewer = await acting_user(request, office)
        return {
            "status": await office.stages.status(ticket, viewer=viewer),
            "viewer": viewer,
            "cards": await card_rights(ticket, viewer),
            "template": shape,
            "stage_runs": [r.model_dump() for r in await office.stages.runs(ticket.id)],
            "participants": [m.model_dump() for m in await teams.participants(ticket.id)],
            "review": await office.stages.last_review(ticket.id),
            "review_acs": await office.stages.review_acs(ticket.id),
            "ui_evidence": await office.stages.ui_evidence(ticket.id),
            "merge": await office.merges.panel(ticket.key) if ticket.stage == "pr" else {"available": False},
            "docs": await docs_panel(ticket),
        }

    @app.get("/api/tickets/{key}/documents/{doc}/diff")
    async def document_diff(key: str, doc: str, to: int | None = None, since: str = "", start: int | None = Query(None, alias="from")) -> dict[str, Any]:
        """Two revisions of a document side by side for the approver: `from` → `to` (default: the current one).
        Without `from`: the revision that existed at `since` (the moment it was sent back), else the approved
        revision, else the one before."""
        ticket = await office.tickets.get(key)
        current = await office.tickets.get_document(ticket.id, doc)
        if current is None:
            raise NotFound(f"{ticket.key} không có tài liệu {doc}")
        to = to or current.revision
        basis = "bản bạn chọn"
        if start is None and since:
            row = await office.db.fetchone(
                "SELECT MAX(revision) AS r FROM document_revisions WHERE ticket_id = ? AND key = ? AND created_at <= ? AND revision < ?", (ticket.id, doc, since, to)
            )
            start, basis = (row or {}).get("r"), "bản lúc bị trả lại"
        if start is None and current.approved_revision and current.approved_revision < to:
            start, basis = current.approved_revision, "bản đã duyệt"
        if start is None:
            start, basis = to - 1, "bản trước"
        if start < 1 or start >= to:
            raise NotFound(f"Chưa có bản nào trước bản {to} của {doc} để so")
        bodies = {}
        for revision in (start, to):
            row = await office.db.fetchone("SELECT body FROM document_revisions WHERE ticket_id = ? AND key = ? AND revision = ?", (ticket.id, doc, revision))
            if row is None:
                raise NotFound(f"{ticket.key} không còn bản {revision} của {doc}")
            bodies[revision] = str(row["body"] or "")
        pack = office.docs.pack_of(ticket)
        doctype = office.docs.doctype_of(doc, pack) if office.docs.applies(ticket) else None
        return {"doc": doc, "from": start, "to": to, "basis": basis, **doc_diff(bodies[start], bodies[to], doctype, pack)}

    @app.get("/api/tickets/{key}/documents/source")
    async def document_source(key: str, path: str) -> dict[str, Any]:
        """A file a document of the ticket (or of its epic) cites on a `Nguồn:` line, for the reader of that document.
        Only a path written in one of those documents, only inside the ticket's workspace or its project folder."""
        ticket = await office.tickets.get(key)
        wanted = path.strip().replace("\\", "/").removeprefix("./")
        owners = [ticket.id] + ([ticket.parent_id] if ticket.parent_id else [])
        cited = False
        for owner in owners:
            cited = cited or any(wanted in d.body for d in await office.tickets.list_documents(owner))
        from actoroffice.codeintel.index import SECRET_NAMES

        if not wanted or not cited or SECRET_NAMES.search(wanted.rsplit("/", 1)[-1]):  # never a key or an .env file, cited or not
            raise NotFound(f"Không tài liệu nào của {ticket.key} dẫn nguồn {path}")
        root = await office.tickets.root_of(ticket.id)
        bases = [(office.settings.workspaces_dir / root.key).resolve()]
        if ticket.project_id:
            project = await office.projects.get(ticket.project_id)
            if project.path:
                bases.append(Path(project.path).resolve())
        secrets_dir = (office.settings.data_dir / "secrets").resolve()
        for base in bases:
            file = (base / wanted).resolve()
            if file.is_relative_to(base) and not file.is_relative_to(secrets_dir) and file.is_file():
                raw = file.read_bytes()
                limit = 512 * 1024
                return {"path": wanted, "text": office.redact(raw[:limit].decode("utf-8", errors="replace")), "truncated": len(raw) > limit}
        raise NotFound(f"Không thấy file {path} trong thư mục code của {ticket.key}")

    @app.post("/api/tickets/{key}/merge/start")
    async def merge_start(key: str, request: Request) -> dict[str, Any]:
        """The "Merge into <main>" button (repos without a code host): merges, tests and passes the gate in the background."""
        return await office.merges.start(key, by=await acting_user(request, office))

    @app.get("/api/tickets/{key}/changes")
    async def ticket_changes(key: str) -> dict[str, Any]:
        """Files / Changes panel: what the ticket's branch changed so far (live, from git)."""
        from actoroffice.engine.workspace import changes

        ticket = await office.tickets.get(key)
        root = await office.tickets.root_of(ticket.id)
        project = await office.projects.get(ticket.project_id) if ticket.project_id else None
        base = project.main_branch if project else "main"
        if ticket.status in CLOSED:
            base = await office.stages.merged_base(root) or base
        return await changes(office.settings.workspaces_dir / root.key, base)

    @app.get("/api/tickets/{key}/artifacts")
    async def ticket_artifacts(key: str) -> list[dict[str, Any]]:
        ticket = await office.tickets.get(key)
        return [p for p in await office.tickets.work_products(ticket.id) if p["kind"] in ARTIFACT_KINDS]

    @app.get("/api/tickets/{key}/artifacts/{product_id}/file")
    async def ticket_artifact_file(key: str, product_id: int) -> FileResponse:
        """The file of one artifact (screenshots in the pr pane and gate card, SI-23): only an artifact of this
        ticket, only from its own folder data_dir/artifacts/<KEY>/."""
        ticket = await office.tickets.get(key)
        row = await office.db.fetchone("SELECT ticket_id, kind, ref FROM work_products WHERE id = ?", (product_id,))
        if row is None or row["ticket_id"] != ticket.id or row["kind"] not in ARTIFACT_KINDS:
            raise NotFound(f"{ticket.key} không có artifact {product_id}")
        folder = (office.settings.data_dir / "artifacts" / ticket.key).resolve()
        path = Path(row["ref"]).resolve()
        if not path.is_relative_to(folder):
            raise HTTPException(403, "artifact nằm ngoài thư mục của ticket")
        if not path.is_file():
            raise NotFound(f"file của artifact {product_id} không còn")
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})

    @app.get("/api/tickets/{key}/sources")
    async def ticket_sources(key: str) -> list[dict[str, Any]]:
        """The "Nguồn" tab (SI-39): what the ticket names and whether the AI read it. Another team's ticket: key only."""
        ticket = await office.tickets.get(key)
        return await office.sources.list(ticket)

    @app.get("/api/tickets/{key}/sources/file")
    async def ticket_source_file(key: str, path: str) -> dict[str, Any]:
        """A file that is a source of the ticket, from its workspace or project folder only."""
        ticket = await office.tickets.get(key)
        return await office.sources.file_text(ticket, path)

    @app.post("/api/tickets/{key}/participants")
    async def set_participant(key: str, body: ParticipantBody) -> dict[str, Any]:
        ticket = await office.tickets.get(key)
        await teams.set_participant(ticket.id, body.user_id, body.roles)
        return {"participants": [m.model_dump() for m in await teams.participants(ticket.id)]}

    @app.post("/api/tickets/{key}/control")
    async def control(key: str, body: ControlBody, request: Request) -> dict[str, Any]:
        stages = office.stages
        by = await acting_user(request, office)
        cmd = body.cmd.strip().lower()
        held = False  # the AI reads a comment or a direction only after the open gate card is answered
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
            held = bool(ticket.stage) and await stages.waits_for_card(ticket)
        elif cmd == "ask":  # a question or a remark for the AI / the author that does not answer the card
            text = body.text.strip()
            if not text:
                raise HTTPException(422, "Câu hỏi trống")
            ticket = await office.tickets.get(key)
            name = "Owner" if by == LOCAL_OWNER_ID else (await teams.get_user(by)).name
            await office.tickets.add_comment(ticket.id, f"❓ {text}", author_kind="user", author_id=name)
            held = bool(ticket.stage) and await stages.waits_for_card(ticket)
        elif cmd == "withdraw":  # the person who sent a stage for approval takes it back
            withdraw = getattr(stages, "withdraw_submission", None)
            if withdraw is None:
                raise HTTPException(501, "Phiên bản này chưa hỗ trợ rút lại bản đã gửi duyệt")
            await withdraw(key, by=by)
            ticket = await office.tickets.get(key)
        elif cmd == "merged":
            ticket = await stages.external_passed(key, "merge", by=by, note=body.text or "PR đã merge")
        elif cmd in ("quick", "full"):
            target = "quick" if cmd == "quick" else "code"
            label = "Sửa nhỏ" if cmd == "quick" else "đầy đủ"
            ticket = await stages.switch_template(key, target, reason=body.text or f"người chọn quy trình {label}", by=by)
        elif cmd == "flow":  # stage = the flow to move to (two flows of the pack that share their beginning)
            ticket = await stages.switch_template(key, body.stage.strip(), reason=body.text or "người đổi quy trình", by=by)
        elif cmd == "approved_script":
            ticket = await stages.external_passed(key, "script", by=by, note=body.text or "đã duyệt")
        elif cmd == "grow":
            ticket = await office.docs.epic_grew(await office.tickets.get(key), body.text, by=by)
        elif cmd == "accept":  # text = the reason, stage = the finding id
            ticket = await office.tickets.get(key)
            await office.docs.accept(ticket, body.stage.strip(), by=by, reason=body.text)
        elif cmd == "reopen":
            current = await office.tickets.get(key)
            if current.status not in CLOSED:
                raise HTTPException(409, f"{current.key} chưa đóng: dùng ↻ hoặc chỉ dẫn cho AI")
            if not body.text.strip():
                raise HTTPException(422, "Cần lý do mở lại")
            ticket = await stages.reopen(key, body.stage.strip(), reason=body.text.strip(), by=by)
        else:
            raise HTTPException(422, f"không rõ lệnh {body.cmd}")
        office.scheduler.kick()
        return {"ticket": ticket.model_dump(), "status": await stages.status(ticket, viewer=by), "held": held}

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
    # UI evidence (SI-23): how the office runs the app for screenshots; all empty = no UI profile
    ui_seed: str = ""
    ui_start: str = ""
    ui_url: str = ""
    ui_paths: str = ""  # globs, separated by commas, spaces or new lines


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


class OneAtATimeBody(BaseModel):
    enabled: bool


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


def _plain_cmd(profile: dict[str, Any], key: str) -> str:
    """Like _cmd, but quotes only arguments that need it: "{port}" stays readable in the form."""
    import shlex

    value = profile.get(key)
    if not (isinstance(value, list) and value and all(isinstance(v, str) for v in value)):
        return ""
    return " ".join(v if v and not re.search(r"[\s'\"\\]", v) else shlex.quote(v) for v in value)


def _ui_fields(profile: dict[str, Any]) -> dict[str, str]:
    ui = profile.get("ui") if isinstance(profile.get("ui"), dict) else {}
    return {
        "ui_seed": _plain_cmd(ui, "seed"),
        "ui_start": _plain_cmd(ui, "start"),
        "ui_url": str(ui.get("url") or ""),
        "ui_paths": ", ".join(str(p) for p in ui.get("paths") or []),
    }


def _ui_profile(body: SetupProject) -> dict[str, Any] | None:
    """profile["ui"] from the form: None when every UI field is empty; 422 when start or url is missing."""
    seed, start = _argv(body.ui_seed), _argv(body.ui_start)
    url = body.ui_url.strip()
    paths = [p for p in re.split(r"[\s,]+", body.ui_paths) if p]
    if not (seed or start or url or paths):
        return None
    if not start or not url:
        raise HTTPException(422, "Cấu hình giao diện cần cả lệnh start và URL")
    ui: dict[str, Any] = {"start": start, "url": url, "paths": paths}
    if seed:
        ui["seed"] = seed
    return ui


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
                {**p.model_dump(), "test": _cmd(p.profile, "test"), "build": _cmd(p.profile, "build"), "lint": _cmd(p.profile, "lint"), **_ui_fields(p.profile)}
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
        ui = _ui_profile(body)  # checked before anything is saved
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
        if ui:
            profile["ui"] = ui
        else:
            profile.pop("ui", None)
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

    @app.post("/api/projects/{project_id}/one-at-a-time")
    async def project_one_at_a_time(project_id: str, body: OneAtATimeBody) -> dict[str, Any]:
        """The project's "Chạy từng ticket một" switch (SI-31). Off releases every ticket waiting only for its turn;
        on leaves the running tickets alone: only tickets started afterwards wait."""
        before = await office.projects.get(project_id)
        project = await office.projects.set_one_at_a_time(project_id, body.enabled)
        released = 0 if project.runs_one_at_a_time else await office.tickets.release_queue(project.id)
        await office.audit.record(
            "project:one_at_a_time", "changed", target=project.name,
            detail={"from": before.runs_one_at_a_time, "to": project.runs_one_at_a_time, "released": released},
        )
        if released:
            office.scheduler.kick()
        return {**project.model_dump(), "released": released}

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
