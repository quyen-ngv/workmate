"""Kiro parity: a ticket running through stages behaves the same on the Kiro CLI runtime (ACP) as on Claude:
the stage prompt opens the session, tool calls go through the same gate, the same activity lines come out, the
office tools work over MCP and usage is recorded on the stage. Driven by the scripted ACP agent."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.org import PermissionMode
from actoroffice.runtimes.kiro_acp import KiroAcpRuntime

AGENT = [sys.executable, str(Path(__file__).parent / "fake_acp_agent.py")]


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_model="kiro", corrective_turns=0), runtime=KiroAcpRuntime(command=AGENT))
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


async def test_a_stage_run_on_kiro_matches_claude(office: Office) -> None:
    ai = await office.ensure_ai()
    await office.org.update_actor(ai.id, permission_mode=PermissionMode.TRUST_ALL)  # the fake agent runs a command
    team = await office.teams.create_team("Billing")
    ticket = await office.tickets.create("Hoàn tiền", description="x")
    await office.db.execute("UPDATE tickets SET team_id = ?, stage = 'understand', stage_template = 'code' WHERE id = ?", (team.id, ticket.id))
    root = await office.tickets.root_of(ticket.id)
    workspace = office.settings.workspaces_dir / root.key
    workspace.mkdir(parents=True)
    (workspace / "notes.md").write_text("ghi chú", encoding="utf-8")

    await office.tickets.assign(ticket.id, ai.id, by="user")
    await office.scheduler.wait_idle(timeout=60)

    log = json.loads((workspace / "agent_log.json").read_text(encoding="utf-8"))
    assert log["first_prompt"].startswith("<office_instructions>\nYou are Copilot, AI Engineer of team Billing.")
    assert "stage_complete" in log["tools"] and "ask_human" in log["tools"] and "db_query" in log["tools"]
    comments = await office.tickets.comments(ticket.id, limit=50)
    assert any(c.body == "đã xong" and c.author_id == "Copilot" for c in comments)

    run = (await office.runs.for_ticket(ticket.id))[0]
    kinds = [(a["kind"], a["status"]) for a in await office.runs.activities(run.id)]
    assert ("read", "running") in kinds and ("read", "ok") in kinds  # same classifier as Claude
    assert ("command", "running") in kinds and ("step", "ok") in kinds
    assert any(k == "narration" for k, _ in kinds)
    srun = (await office.stages.runs(ticket.id))[0]
    assert srun.cost_usd == pytest.approx(0.0125)
    assert not (workspace.parent / "outside.txt").exists()  # the same workspace rules apply
