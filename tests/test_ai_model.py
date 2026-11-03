"""SI-18: the office AI runs on ai_model on every path (ticket, chat, stage), and each run records the model used."""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus, Usage
from actoroffice.engine.activity import ActivityTracker
from actoroffice.runtimes.claude_sdk import _to_turn_result

from .fakes import FakeRuntime, TurnCtx

AI_MODEL = "claude-opus-5-5"


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    settings = Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, corrective_turns=0, ai_model=AI_MODEL)
    o = Office(settings, runtime=runtime)
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    yield o
    await o.close()


async def hire(office: Office, name: str, model: str | None = None):
    # the AI actor of the main office was hired before ai_model was set: no model of its own, position = sonnet
    pos = await office.org.find_position("writer")
    assert pos.model == "sonnet"
    return await office.org.hire(pos.id, name, model=model)


async def run_plain_ticket(office: Office, actor_id: str):
    ticket = await office.tickets.create("Write intro", assignee_actor_id=actor_id)
    await office.scheduler.wait_idle(timeout=10)
    return ticket


async def chat_once(office: Office, actor_id: str) -> None:
    conv = await office.chat.dm(actor_id)
    await office.chat.post_user(conv.id, "Chào")
    await office.scheduler.wait_idle(timeout=10)


async def run_stage_ticket(office: Office, team_model: str | None) -> int:
    team = await office.teams.create_team("Billing", jira_project_key="bil")
    team.settings.model = team_model
    await office.teams.update_team(team)
    lan = await office.teams.create_user("Lan", telegram_id=111)
    await office.teams.add_member(team.id, lan.id, ["ba"])
    ticket = await office.tickets.create("Export CSV", description="x")
    await office.stages.start(ticket.key, template="quick", by="user", team_id=team.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    return ticket.id


async def test_ac1_ai_actor_uses_ai_model_for_plain_ticket_and_chat(office, runtime):
    ai = await hire(office, office.settings.ai_name)
    await run_plain_ticket(office, ai.id)
    await chat_once(office, ai.id)
    assert [s.model for s in runtime.specs] == [AI_MODEL, AI_MODEL]


async def test_ac1_other_actors_keep_their_model(office, runtime):
    minh = await hire(office, "Minh")
    lan = await hire(office, "Lan", model="haiku")
    await run_plain_ticket(office, minh.id)
    await chat_once(office, lan.id)
    assert [s.model for s in runtime.specs] == ["sonnet", "haiku"]


async def test_ac1_ai_actor_without_ai_model_keeps_old_choice(tmp_path, runtime):
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, corrective_turns=0), runtime=runtime)
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    try:
        ai = await hire(o, o.settings.ai_name)
        await chat_once(o, ai.id)
        assert runtime.specs[0].model == "sonnet"
    finally:
        await o.close()


async def test_ac2_team_model_overrides_ai_model_on_stage_turns(office, runtime):
    await run_stage_ticket(office, "haiku")
    assert runtime.specs and runtime.specs[0].model == "haiku"


async def test_ac2_stage_turns_use_ai_model_without_team_model(office, runtime):
    await run_stage_ticket(office, None)
    assert runtime.specs and runtime.specs[0].model == AI_MODEL


async def turn_title(office: Office, run_id: str) -> str:
    turn = [e for e in await office.runs.events(run_id) if e["kind"] == "turn"][-1]
    activity = ActivityTracker().classify(run_id, "turn", turn["payload"])
    assert activity is not None
    return activity.title


async def test_ac3_run_records_reported_model(office, runtime):
    ai = await hire(office, office.settings.ai_name)

    async def reported(ctx: TurnCtx) -> TurnResult:
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s", usage=Usage(input_tokens=10, output_tokens=5, model="claude-opus-5-5-20261001"))

    runtime.scripts.append(reported)
    ticket = await run_plain_ticket(office, ai.id)
    run = (await office.runs.for_ticket(ticket.id))[0]
    assert run.usage.model == "claude-opus-5-5-20261001"
    assert (await turn_title(office, run.id)).endswith("· claude-opus-5-5-20261001")


async def test_ac3_run_records_requested_model_when_runtime_is_silent(office, runtime):
    ai = await hire(office, office.settings.ai_name)
    await chat_once(office, ai.id)
    run_id = runtime.specs[0].run_id
    run = await office.runs.get(run_id)
    assert run.usage.model == AI_MODEL
    assert (await turn_title(office, run_id)).endswith(f"· {AI_MODEL}")


async def test_ac3_stage_usage_sums_tokens_and_keeps_model(office, runtime):
    ticket_id = await run_stage_ticket(office, None)
    usage = (await office.stages.runs(ticket_id))[0].usage
    assert usage["model"] == AI_MODEL
    assert usage["input_tokens"] == 10 * len(runtime.specs) and usage["output_tokens"] == 5 * len(runtime.specs)


def result_message(model_usage: dict | None) -> SimpleNamespace:
    return SimpleNamespace(
        usage={"input_tokens": 3, "output_tokens": 2},
        model_usage=model_usage,
        session_id="s",
        total_cost_usd=0.0,
        terminal_reason="",
        deferred_tool_use=None,
        api_error_status=None,
        is_error=False,
        subtype="success",
        errors=None,
        result="",
    )


def test_ac3_sdk_model_from_init_then_result_message():
    usage = {"claude-haiku-5-5": {"outputTokens": 4}, "claude-opus-5-5": {"outputTokens": 900}}
    assert _to_turn_result(result_message(usage), [], None, None, "claude-opus-5-5").usage.model == "claude-opus-5-5"
    assert _to_turn_result(result_message(usage), [], None, None).usage.model == "claude-opus-5-5"
    assert _to_turn_result(result_message(None), [], None, None).usage.model == ""
