"""Fixes from watching a real office: read-only projects, deciding about tickets in chat, superseded proposals."""

from __future__ import annotations

import subprocess

import pytest

from actoroffice.channels import tg_format as fmt
from actoroffice.domain.errors import PolicyDenied
from actoroffice.domain.work import InteractionKind, ProjectAccess, TicketStatus
from actoroffice.engine.workspace import WorkspaceManager
from actoroffice.tools.chat_tools import propose_ticket
from actoroffice.tools.office_tools import project_access_request, ticket_update
from actoroffice.tools.registry import ToolCtx, ToolError

from .test_telegram import hire, office  # noqa: F401  (fixture)


async def ctx_for(office, actor, *, ticket=None, conversation=None) -> ToolCtx:
    """A tool call always happens inside a real run."""
    from actoroffice.domain.ids import new_id
    from actoroffice.domain.run import Run, RunKind

    position = await office.org.get_position(actor.position_id)
    run = await office.runs.start(Run(id=new_id("run"), actor_id=actor.id, kind=RunKind.TICKET if ticket else RunKind.CHAT,
                                      ticket_id=ticket.id if ticket else None, conversation_id=conversation))
    return ToolCtx(office, actor, position, run_id=run.id, ticket_id=ticket.id if ticket else None, conversation_id=conversation)


# ---- read-only project → one-click access ---------------------------------------------------------


async def test_access_request_makes_a_read_only_project_writable_after_one_click(office, tmp_path):
    folder = tmp_path / "goroute"
    folder.mkdir()
    project = await office.projects.create("GoRoute", path=str(folder), access=ProjectAccess.READ_ONLY)
    minh = await hire(office, "dev", "Minh")
    ticket = await office.tickets.create("Viết lại docs", assignee_actor_id=minh.id, project_id=project.id)

    ctx = await ctx_for(office, minh, ticket=ticket)
    reply = await project_access_request({"reason": "Ghi docs mới vào .agents/docs"}, ctx)
    assert "in_review" in reply
    [card] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.APPROVE_ACCESS]
    assert card.payload["options"] == ["direct"]  # not a git repo: worktree is not offered
    again = await project_access_request({"reason": "lần nữa"}, await ctx_for(office, minh, ticket=ticket))
    assert "already waiting" in again

    from actoroffice.domain.run import RunStatus

    await office.runs.finish(ctx.run_id, RunStatus.PARKED)  # the actor ended its turn waiting for the decision
    for wake in await office.wakes.pending():
        await office.wakes.drop(wake.id, "test: only the decision may wake Minh")
    await office.answer(card.id, {"approved": True, "access": "direct"})
    assert (await office.projects.get(project.id)).access == ProjectAccess.DIRECT
    assert any(w.ticket_id == ticket.id for w in await office.wakes.pending())  # Minh goes on right away


async def test_access_can_only_be_one_of_the_offered_modes(office, tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    project = await office.projects.create("P", path=str(folder), access=ProjectAccess.READ_ONLY)
    minh = await hire(office, "dev", "Minh")
    await project_access_request({"reason": "x"}, await ctx_for(office, minh, conversation=(await office.chat.dm(minh.id, project.id)).id))
    [card] = [i for i in await office.interactions.pending() if i.kind == InteractionKind.APPROVE_ACCESS]
    with pytest.raises(PolicyDenied):
        await office.answer(card.id, {"approved": True, "access": "worktree"})
    await office.answer(card.id, {"approved": False})
    assert (await office.projects.get(project.id)).access == ProjectAccess.READ_ONLY


async def test_drafts_from_the_read_only_phase_stay_readable(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "--allow-empty", "-m", "init"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    from actoroffice.domain.org import Position
    from actoroffice.domain.work import Project, Ticket

    manager = WorkspaceManager(tmp_path / "ws")
    scratch = tmp_path / "ws" / "AO-5"
    scratch.mkdir(parents=True)
    (scratch / "draft.md").write_text("nháp", encoding="utf-8")
    ticket = Ticket(id=5, key="AO-5", title="Docs", status=TicketStatus.TODO, created_by="user", created_at="", updated_at="")
    position = Position(id="pos", name="dev", title="Developer")

    direct = await manager.realize(ticket, Project(id="p", name="P", path=str(repo), access=ProjectAccess.DIRECT), position)
    assert direct.path == repo and scratch.resolve() in direct.read_dirs

    tree = await manager.realize(ticket, Project(id="p", name="P", path=str(repo), access=ProjectAccess.WORKTREE), position)
    assert (tree.path / ".git").exists() and (tmp_path / "ws" / "AO-5-draft" / "draft.md").exists()
    assert (tmp_path / "ws" / "AO-5-draft") in tree.read_dirs


async def test_writing_into_a_read_only_folder_points_to_the_access_request(office, tmp_path):
    from actoroffice.security.policy import ToolCallContext

    minh = await hire(office, "dev", "Minh")
    position = await office.org.get_position(minh.position_id)
    project_dir = tmp_path / "goroute"
    project_dir.mkdir()
    ctx = ToolCallContext(position=position, workspace=tmp_path / "ws", extra_read_dirs=[project_dir])
    verdict = office.policy.decide("Write", {"file_path": str(project_dir / "README.md"), "content": "x"}, ctx)
    assert verdict.decision.value == "deny" and "project_access_request" in verdict.reason


# ---- decisions about existing tickets, from chat ----------------------------------------------------


async def test_pm_applies_the_users_decisions_about_tickets(office):
    phuong, minh, lan = await hire(office, "pm", "Phuong"), await hire(office, "dev", "Minh"), await hire(office, "writer", "Lan")
    dup = await office.tickets.create("Rà soát docs (trùng)", assignee_actor_id=minh.id)
    misassigned = await office.tickets.create("ISSUES và kiểm link", assignee_actor_id=lan.id)
    pm = await ctx_for(office, phuong)

    await ticket_update({"ticket": dup.key, "status": "cancelled", "comment": "Anh Đức: AO-13 trùng AO-6, huỷ"}, pm)
    await ticket_update({"ticket": misassigned.key, "assignee": "Minh", "comment": "Anh Đức chốt: việc dev giao Minh"}, pm)
    assert (await office.tickets.get(dup.key)).status == TicketStatus.CANCELLED
    assert (await office.tickets.get(misassigned.key)).assignee_actor_id == minh.id
    with pytest.raises(ToolError, match="comment is required"):
        await ticket_update({"ticket": dup.key, "status": "todo"}, pm)


async def test_only_owner_creator_pm_or_lead_may_change_a_ticket(office):
    minh, lan = await hire(office, "dev", "Minh"), await hire(office, "writer", "Lan")
    ticket = await office.tickets.create("Viết code", assignee_actor_id=minh.id)
    with pytest.raises(ToolError, match="Only the assignee"):
        await ticket_update({"ticket": ticket.key, "status": "cancelled", "comment": "x"}, await ctx_for(office, lan))


async def test_resuming_a_ticket_wakes_its_owner(office):
    minh = await hire(office, "dev", "Minh")
    ticket = await office.tickets.create("Khung docs", assignee_actor_id=minh.id)
    await office.tickets.transition(ticket.key, TicketStatus.BLOCKED, by="office")
    for wake in await office.wakes.pending():
        await office.wakes.drop(wake.id, "test")
    await ticket_update({"ticket": ticket.key, "status": "todo", "comment": "Anh Đức: duyệt, làm tiếp"}, await ctx_for(office, minh))
    assert (await office.tickets.get(ticket.key)).status == TicketStatus.TODO
    assert [w.ticket_id for w in await office.wakes.pending()] == [ticket.id]


async def test_a_replacing_proposal_withdraws_the_old_card(office):
    phuong, minh = await hire(office, "pm", "Phuong"), await hire(office, "dev", "Minh")
    group = await office.chat.create_group("dev team", [phuong.id, minh.id], created_by="Bạn")
    pm = await ctx_for(office, phuong, conversation=group.id)
    await propose_ticket({"title": "Gom docs thành 1 bộ", "description": "x", "assignee": "Minh"}, pm)
    reply = await propose_ticket({"title": "Gom docs theo feature", "description": "y", "assignee": "Minh", "replace_previous": True}, pm)
    pending = [i for i in await office.interactions.pending() if i.kind == InteractionKind.CONFIRM_TICKET]
    assert [i.payload["title"] for i in pending] == ["Gom docs theo feature"] and "Gom docs thành 1 bộ" in reply


def test_telegram_card_for_project_access():
    item = {"id": "ix_1", "kind": "approve_access", "status": "pending",
            "payload": {"actor": "Minh", "project": "GoRoute", "path": "F:\\00_goroute", "reason": "Ghi docs", "options": ["worktree", "direct"]}}
    text, rows = fmt.card(item)
    labels = [b["text"] for row in rows for b in row]
    assert "GoRoute" in text and any("worktree" in t for t in labels) and any("trực tiếp" in t for t in labels)
    assert fmt.response_for(item, "gd") == {"approved": True, "access": "direct"}
    assert fmt.response_for(item, "n") == {"approved": False}
    assert "sửa trực tiếp" in fmt.verdict({**item, "status": "answered", "response": {"approved": True, "access": "direct"}})
