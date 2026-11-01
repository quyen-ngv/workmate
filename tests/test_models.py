"""Model per task / per stage and switching while a task runs (AO-4). Tests are named after the AC they prove."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.services.models import ModelError, provider, resolve_model, validate_model

from .fakes import FakeRuntime, TurnCtx


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, corrective_turns=0), runtime=runtime)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


async def start(office: Office, **kw):
    ticket = await office.tickets.create("Việc cần làm", description="mô tả", **kw)
    await office.stages.start(ticket.key, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    return ticket


async def again(office: Office, key: str) -> None:
    """Another run on the same stage attempt (a steer wakes the AI)."""
    await office.stages.steer(key, "làm tiếp nhé", by="user")
    await office.scheduler.wait_idle(timeout=20)


def models_used(runtime: FakeRuntime) -> list[str | None]:
    return [s.model for s in runtime.specs]


# ---- pure rules -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["haiku", "sonnet", "opus", "kiro", "kiro:claude-4", "  opus  "])
def test_ac3_valid_models_are_accepted_and_trimmed(value: str) -> None:
    assert validate_model(value) == value.strip()


@pytest.mark.parametrize("value", ["", "   ", None, "gpt-5", "kiro:", "Opus4"])
def test_ac3_invalid_models_are_refused_in_vietnamese(value) -> None:
    with pytest.raises(ModelError) as exc:
        validate_model(value)
    assert "Model" in str(exc.value) and ("không" in str(exc.value))


def test_ac5_ac16_ac17_priority_order() -> None:
    chain = dict(stage_model="s", task_model="t", team_model="team", ai_model="ai", actor_model="a", position_model="p", default_model="d")
    order = ["stage_model", "task_model", "team_model", "ai_model", "actor_model", "position_model"]
    expected = ["s", "t", "team", "ai", "a", "p"]
    for i, want in enumerate(expected):
        assert resolve_model(**chain) == want
        chain[order[i]] = None
    assert resolve_model(**chain) == "d"


def test_provider_follows_is_kiro() -> None:
    assert provider("kiro") == provider("kiro:x") == "kiro" and provider("opus") == "claude"


# ---- US-1 choose the model of a task ----------------------------------------------------------------------


async def test_ac1_task_model_is_stored_and_used(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office, model="opus")
    assert (await office.tickets.get(ticket.id)).model == "opus"
    assert models_used(runtime) == ["opus"]
    run = (await office.runs.for_ticket(ticket.id))[0]
    row = await office.db.fetchone("SELECT model FROM runs WHERE id = ?", (run.id,))
    assert row and row["model"] == "opus"


async def test_ac2_without_model_the_old_chain_applies(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office)
    assert (await office.tickets.get(ticket.id)).model is None
    assert models_used(runtime) == [office.settings.default_model]


async def test_ac3_invalid_model_stores_nothing(office: Office) -> None:
    with pytest.raises(ModelError):
        await office.tickets.create("Sai", model="gpt-5")
    assert await office.tickets.list() == []
    ticket = await office.tickets.create("Đúng")
    with pytest.raises(ModelError):
        await office.models.set_task_model(ticket.key, "  ", by="user")
    with pytest.raises(ModelError):
        await office.models.request_switch(ticket.key, "gpt-5", scope="turn", by="user")
    after = await office.tickets.get(ticket.id)
    assert after.model is None and after.pending_model is None


# ---- US-2 per stage ---------------------------------------------------------------------------------------


async def test_ac4_ac5_ac6_stage_model_beats_task_model(office: Office, runtime: FakeRuntime) -> None:
    ticket = await office.tickets.create("Việc", description="x", model="sonnet")
    await office.models.set_stage_model(ticket.key, "plan", "opus", by="user")
    await office.stages.start(ticket.key, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime) == ["sonnet"]  # understand has no model of its own → task model (AC-5)

    await office.stages.advance(ticket.id, by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime) == ["sonnet", "opus"]  # plan → its own model (AC-4)

    await office.models.set_stage_model(ticket.key, "plan", None, by="user")  # AC-6
    await again(office, ticket.key)
    assert models_used(runtime)[-1] == "sonnet"


async def test_ac7_model_of_a_finished_stage_only_matters_on_rerun(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office, model="sonnet")
    await office.stages.advance(ticket.id, by="user")
    await office.scheduler.wait_idle(timeout=20)
    seen = len(runtime.specs)
    await office.models.set_stage_model(ticket.key, "understand", "haiku", by="user")
    await again(office, ticket.key)
    assert models_used(runtime)[seen:] == ["sonnet"]  # the plan stage is running: nothing changed
    assert (await office.tickets.get(ticket.id)).stage_models == {"understand": "haiku"}
    await office.stages.reopen(ticket.key, "understand", reason="làm lại", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime)[-1] == "haiku"


async def test_stage_name_must_exist(office: Office) -> None:
    ticket = await start(office)
    with pytest.raises(ModelError):
        await office.models.set_stage_model(ticket.key, "nope", "opus", by="user")


# ---- US-3 switching while running -------------------------------------------------------------------------


async def test_ac8_ac12_turn_switch_waits_for_the_next_run_and_is_audited(office: Office, runtime: FakeRuntime) -> None:
    ticket = await office.tickets.create("Việc", description="x", model="sonnet")

    async def ask_switch(_ctx: TurnCtx):
        await office.models.request_switch(ticket.key, "opus", scope="turn", by="user")

    runtime.scripts.append(ask_switch)
    await office.stages.start(ticket.key, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime) == ["sonnet"]  # the running run is not touched
    assert (await office.tickets.get(ticket.id)).pending_model["model"] == "opus"

    await again(office, ticket.key)
    assert models_used(runtime) == ["sonnet", "opus"]
    after = await office.tickets.get(ticket.id)
    assert after.model == "opus" and after.pending_model is None
    audit = [c.body for c in await office.tickets.comments(ticket.id, limit=100) if "sonnet → opus" in c.body]
    assert audit and "Owner" in audit[-1] and "từ lượt chạy kế tiếp" in audit[-1] and "lúc" in audit[-1]


async def test_ac9_stage_switch_holds_for_the_whole_stage(office: Office, runtime: FakeRuntime) -> None:
    ticket = await office.tickets.create("Việc", description="x", model="sonnet")

    async def ask_switch(_ctx: TurnCtx):
        await office.models.request_switch(ticket.key, "opus", scope="stage", by="user")

    runtime.scripts.append(ask_switch)
    await office.stages.start(ticket.key, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    await again(office, ticket.key)
    await again(office, ticket.key)
    assert models_used(runtime) == ["sonnet"] * 3  # wake-ups inside the stage keep the old model
    assert (await office.tickets.get(ticket.id)).pending_model is not None

    await office.stages.advance(ticket.id, by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime)[-1] == "opus"


async def test_ac10_status_shows_pending_and_cancel_keeps_current(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office, model="sonnet")
    await office.models.request_switch(ticket.key, "opus", scope="stage", by="user")
    info = (await office.stages.status(ticket.key, viewer="user"))["model"]
    assert info["current"] == "sonnet" and info["pending"]["model"] == "opus" and info["pending"]["scope"] == "stage"
    assert info["pending"]["scope_label"] == "từ giai đoạn kế tiếp"

    await office.models.cancel_switch(ticket.key, by="user")
    with pytest.raises(ModelError):
        await office.models.cancel_switch(ticket.key, by="user")
    await office.stages.advance(ticket.id, by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime)[-1] == "sonnet"


async def test_ac11_switch_with_nothing_running_lands_on_the_next_run(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office, model="sonnet")
    await office.models.request_switch(ticket.key, "haiku", scope="turn", by="user")
    await again(office, ticket.key)
    assert models_used(runtime) == ["sonnet", "haiku"]


async def test_switch_for_one_stage_only(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office, model="sonnet")
    await office.models.request_switch(ticket.key, "opus", scope="turn", by="user", stage="understand")
    await again(office, ticket.key)
    assert (await office.tickets.get(ticket.id)).stage_models == {"understand": "opus"}
    assert models_used(runtime)[-1] == "opus"


# ---- US-4 context is kept ---------------------------------------------------------------------------------


async def test_ac13_claude_to_claude_resumes_the_session(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office, model="haiku")
    first = runtime.specs[0]
    await office.models.request_switch(ticket.key, "opus", scope="turn", by="user")
    await again(office, ticket.key)
    second = runtime.specs[1]
    assert second.model == "opus" and second.resume_session_id == f"sess-{first.run_id}"


async def test_ac14_ac15_claude_to_kiro_starts_a_session_with_full_context(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office, model="opus")
    first = runtime.specs[0]
    await office.models.request_switch(ticket.key, "kiro", scope="turn", by="user")
    await again(office, ticket.key)
    kiro = runtime.specs[1]
    assert kiro.model == "kiro" and kiro.resume_session_id is None
    assert "Current stage: understand" in kiro.system_prompt and "làm tiếp nhé" in kiro.prompt  # context rebuilt, steer kept
    assert first.resume_session_id is None

    await again(office, ticket.key)  # AC-15: the new session is the one resumed
    assert runtime.specs[2].model == "kiro" and runtime.specs[2].resume_session_id == f"sess-{kiro.run_id}"


async def test_ac14_kiro_to_claude_starts_a_session(office: Office, runtime: FakeRuntime) -> None:
    ticket = await start(office, model="kiro:fast")
    await office.models.request_switch(ticket.key, "sonnet", scope="turn", by="user")
    await again(office, ticket.key)
    assert runtime.specs[1].model == "sonnet" and runtime.specs[1].resume_session_id is None


# ---- US-5 compatibility -----------------------------------------------------------------------------------


async def test_ac16_workspace_model_still_applies_without_task_model(office: Office, runtime: FakeRuntime) -> None:
    office.settings.ai_model = "haiku"
    await start(office)
    assert models_used(runtime) == ["haiku"]


async def test_ac16_team_model_now_beats_workspace_model(office: Office, runtime: FakeRuntime) -> None:
    office.settings.ai_model = "haiku"
    team = await office.teams.create_team("Billing")
    team.settings.model = "opus"
    await office.teams.update_team(team)
    ticket = await office.tickets.create("Việc", description="x")
    await office.stages.start(ticket.key, template="code", by="user", team_id=team.id)
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime) == ["opus"]
    await office.models.set_task_model(ticket.key, "sonnet", by="user")  # the task model beats the team model
    await again(office, ticket.key)
    assert models_used(runtime)[-1] == "sonnet"


async def test_ac17_task_model_wins_without_team_or_stage(office: Office, runtime: FakeRuntime) -> None:
    await office.install_default_positions(skills=False)
    writer = await office.org.find_position("writer")
    minh = await office.org.hire(writer.id, "Minh")
    office.settings.ai_model = "haiku"
    plain = await office.tickets.create("Không stage", assignee_actor_id=minh.id, model="opus")
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime) == ["opus"] and (await office.tickets.get(plain.id)).stage is None


async def test_ac17_plain_ticket_ignores_workspace_model_like_before(office: Office, runtime: FakeRuntime) -> None:
    await office.install_default_positions(skills=False)
    writer = await office.org.find_position("writer")
    minh = await office.org.hire(writer.id, "Minh")
    office.settings.ai_model = "haiku"
    await office.tickets.create("Không stage", assignee_actor_id=minh.id)
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime) == [office.settings.default_model if not writer.model else writer.model]


async def test_cli_task_model_switch_and_errors(office: Office, runtime: FakeRuntime, capsys) -> None:
    from argparse import Namespace

    from actoroffice.cli_team import cmd_model, cmd_task
    from actoroffice.domain.errors import OfficeError

    base = dict(team=None, owner=None, project=None, mode=None, template="code", description=None)
    with pytest.raises(OfficeError):
        await cmd_task(office, Namespace(title="Sai", model="gpt-5", **base))
    assert await office.tickets.list() == []
    await cmd_task(office, Namespace(title="Đúng", model="opus", **base))
    await office.scheduler.wait_idle(timeout=20)
    assert models_used(runtime) == ["opus"]

    args = dict(key="AO-1", model=None, stage=None, when="turn", clear=False, cancel=False)
    await cmd_model(office, Namespace(**{**args, "model": "haiku", "when": "stage"}))
    assert "đang chờ: haiku (từ giai đoạn kế tiếp)" in capsys.readouterr().out
    await cmd_model(office, Namespace(**{**args, "cancel": True}))
    out = capsys.readouterr().out
    assert "model hiện tại: opus" in out and "đang chờ" not in out
    await cmd_model(office, Namespace(**{**args, "model": "sonnet", "when": "set", "stage": "plan"}))
    assert "giai đoạn plan: sonnet" in capsys.readouterr().out
    await cmd_model(office, Namespace(**{**args, "clear": True, "stage": "plan"}))
    assert "giai đoạn plan" not in capsys.readouterr().out


async def test_old_database_rows_without_the_new_columns_values_load(office: Office) -> None:
    ticket = await office.tickets.create("Cũ")
    await office.db.execute("UPDATE tickets SET model = NULL, stage_models = NULL, pending_model = NULL WHERE id = ?", (ticket.id,))
    loaded = await office.tickets.get(ticket.id)
    assert loaded.model is None and loaded.stage_models == {} and loaded.pending_model is None
