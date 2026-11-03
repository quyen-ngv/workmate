from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.ids import new_id
from actoroffice.domain.run import RateLimit, Run, RunKind, RunStatus, TurnResult, TurnStatus
from actoroffice.domain.work import TicketStatus as S
from actoroffice.engine.reaper import reap_orphans
from actoroffice.runtimes.base import GateDecision

from .fakes import FakeRuntime, TurnCtx, deferred
from .seed import seed_db


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, max_concurrent_runs=2), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.install_default_positions(skills=False)
    yield o
    await o.scheduler.shutdown()
    await o.db.close()


async def hire(office: Office, position: str, name: str):
    pos = await office.org.find_position(position)
    return await office.org.hire(pos.id, name)


async def test_happy_path_done(office, runtime):
    minh = await hire(office, "writer", "Minh")

    async def work(ctx: TurnCtx):
        assert await ctx.tool("Write", {"file_path": str(ctx.spec.cwd / "draft.md"), "content": "x"}) == GateDecision.ALLOW
        await ctx.office("ticket_comment", {"body": "Draft written"})
        await ctx.office("ticket_set_status", {"status": "done", "comment": "draft.md written and proofread"})

    runtime.scripts.append(work)
    t = await office.tickets.create("Write intro", assignee_actor_id=minh.id)
    await office.scheduler.wait_idle(timeout=10)

    t = await office.tickets.get(t.id)
    assert t.status == S.DONE and t.checkout_run_id is None
    run = (await office.runs.by_status(RunStatus.SUCCEEDED))[0]
    assert run.usage.input_tokens == 10
    assert await office.runs.get_session(f"{minh.id}:t{t.id}", str(runtime.specs[0].cwd)) == f"sess-{run.id}"
    assert "Writer" in runtime.specs[0].system_prompt and "Vietnamese" in runtime.specs[0].system_prompt
    assert await office.wakes.pending() == []


async def test_missing_disposition_gets_reminder_then_review(office, runtime):
    minh = await hire(office, "writer", "Minh")
    t = await office.tickets.create("Write", assignee_actor_id=minh.id)
    await office.scheduler.wait_idle(timeout=10)

    assert len(runtime.specs) == 2  # original turn + one corrective reminder
    assert "without a final disposition" in runtime.specs[1].prompt
    assert (await office.tickets.get(t.id)).status == S.IN_REVIEW


async def test_disposition_needs_real_waiting_path(office, runtime):
    minh = await hire(office, "writer", "Minh")
    errors: list[str] = []

    async def bad_review(ctx: TurnCtx):
        try:
            await ctx.office("ticket_set_status", {"status": "in_review", "comment": "please check"})
        except Exception as exc:  # ToolError → shown to the model
            errors.append(str(exc))
        await ctx.office("ticket_set_status", {"status": "done", "comment": "ok"})

    runtime.scripts.append(bad_review)
    await office.tickets.create("Write", assignee_actor_id=minh.id)
    await office.scheduler.wait_idle(timeout=10)
    assert errors and "ask_user" in errors[0]


async def test_approval_parks_then_resumes_with_grant(office, runtime):
    dev = await hire(office, "dev", "Minh")
    cmd = {"command": "npm install left-pad"}

    async def first(ctx: TurnCtx):
        assert await ctx.tool("Bash", cmd) == GateDecision.DEFER  # nobody answered within approval_wait_s
        return deferred(ctx.spec, "Bash", cmd)

    async def second(ctx: TurnCtx):
        assert ctx.spec.resume_session_id is not None
        assert await ctx.tool("Bash", cmd) == GateDecision.ALLOW  # the CLI re-issues the deferred call
        await ctx.office("doc_put", {"key": "handoff", "body": "npm install chạy được", "base_revision": None})
        await ctx.office("ticket_set_status", {"status": "done", "comment": "installed"})

    runtime.scripts += [first, second]
    t = await office.tickets.create("Add dependency", assignee_actor_id=dev.id)
    await office.scheduler.wait_idle(timeout=10)

    assert (await office.tickets.get(t.id)).status == S.IN_REVIEW
    [pending] = await office.interactions.pending()
    assert pending.payload["tool"] == "Bash" and pending.payload["parked"] is True

    await office.interactions.resolve(pending.id, {"approved": True}, by="user")
    await office.scheduler.wait_idle(timeout=10)
    assert (await office.tickets.get(t.id)).status == S.DONE
    assert "was approved and has already been run" in runtime.specs[1].prompt


async def test_rejection_is_not_asked_again(office, runtime):
    dev = await hire(office, "dev", "Minh")
    cmd = {"command": "npm install shady"}
    decisions: list[GateDecision] = []

    async def first(ctx: TurnCtx):
        await ctx.tool("Bash", cmd)
        return deferred(ctx.spec, "Bash", cmd)

    async def second(ctx: TurnCtx):
        decisions.append(await ctx.tool("Bash", cmd))
        await ctx.office("ticket_set_status", {"status": "done", "comment": "skipped"})

    runtime.scripts += [first, second]
    await office.tickets.create("Add dependency", assignee_actor_id=dev.id)
    await office.scheduler.wait_idle(timeout=10)
    [pending] = await office.interactions.pending()
    await office.interactions.resolve(pending.id, {"approved": False}, by="user")
    await office.scheduler.wait_idle(timeout=10)

    assert decisions == [GateDecision.DENY]
    assert await office.interactions.pending() == []


async def test_live_approval_within_wait(office, runtime):
    office.settings.approval_wait_s = 5
    dev = await hire(office, "dev", "Minh")
    seen: list[GateDecision] = []

    async def work(ctx: TurnCtx):
        seen.append(await ctx.tool("Bash", {"command": "npm install left-pad"}))  # not allow-listed → asks
        await ctx.office("doc_put", {"key": "handoff", "body": "npm install chạy được", "base_revision": None})
        await ctx.office("ticket_set_status", {"status": "done", "comment": "installed"})

    async def approver():
        for _ in range(500):
            if items := await office.interactions.pending():
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("no approval request was created")
        await office.interactions.resolve(items[0].id, {"approved": True}, by="user")

    runtime.scripts.append(work)
    await office.tickets.create("Run tests", assignee_actor_id=dev.id)
    approve = asyncio.create_task(approver())
    await office.scheduler.wait_idle(timeout=10)
    await approve

    assert seen == [GateDecision.ALLOW]
    assert len(runtime.specs) == 1  # no park, no second run


async def test_question_parks_and_answer_reaches_prompt(office, runtime):
    ba = await hire(office, "ba", "Mai")

    async def ask(ctx: TurnCtx):
        await ctx.office("ask_user", {"question": "Token TTL?", "options": ["15 minutes", "1 hour"]})
        await ctx.office("ticket_set_status", {"status": "in_review", "comment": "waiting for TTL"})

    async def finish(ctx: TurnCtx):
        await ctx.office("ticket_set_status", {"status": "done", "comment": "spec written"})

    runtime.scripts += [ask, finish]
    t = await office.tickets.create("Spec reset password", assignee_actor_id=ba.id)
    await office.scheduler.wait_idle(timeout=10)
    [question] = await office.interactions.pending()
    await office.interactions.resolve(question.id, {"answer": "15 minutes"}, by="user")
    await office.scheduler.wait_idle(timeout=10)

    assert "Token TTL? → A: 15 minutes" in runtime.specs[1].prompt
    assert (await office.tickets.get(t.id)).status == S.DONE


async def test_concurrency_cap_and_one_run_per_actor(office, runtime):
    runtime.hold = asyncio.Event()
    a, b, c = [await hire(office, "writer", n) for n in ("A", "B", "C")]
    for actor in (a, a, b, c):
        await office.tickets.create(f"for {actor.name}", assignee_actor_id=actor.id)

    started = await office.scheduler.tick()
    assert len(started) == 2  # cap 2, and A only once
    assert set(office.scheduler.running) == {a.id, b.id}
    for _ in range(500):  # let both runs reach the runtime before releasing them
        if runtime.active == 2:
            break
        await asyncio.sleep(0.01)
    runtime.hold.set()
    await office.scheduler.wait_idle(timeout=10)
    assert runtime.max_active == 2
    assert len(runtime.specs) == 8  # 4 tickets × (turn + reminder)


async def test_rate_limit_pauses_and_requeues(office, runtime):
    w = await hire(office, "writer", "Lan")
    resets = int(time.time()) + 3600

    async def limited(ctx: TurnCtx):
        office.scheduler.observe_rate_limit({"status": "rejected", "resets_at": resets})
        return TurnResult(status=TurnStatus.RATE_LIMITED, rate_limit=RateLimit(status="rejected", resets_at=resets))

    runtime.scripts.append(limited)
    t = await office.tickets.create("Write", assignee_actor_id=w.id)
    await office.scheduler.wait_idle(timeout=10)

    assert (await office.tickets.get(t.id)).status == S.TODO
    assert office.scheduler.capacity == 0
    [wake] = await office.wakes.pending()
    assert wake.ticket_id == t.id and await office.wakes.pending(due_only=True) == []


async def test_reaper_releases_orphaned_checkout(office):
    w = await hire(office, "writer", "Lan")
    t = await office.tickets.create("Write", assignee_actor_id=w.id)
    run_id = new_id("run")
    await office.runs.start(Run(id=run_id, actor_id=w.id, kind=RunKind.TICKET, ticket_id=t.id))
    await office.tickets.checkout(t.id, w.id, run_id)

    assert await reap_orphans(office) == 1
    t = await office.tickets.get(t.id)
    assert t.status == S.TODO and t.checkout_run_id is None
    assert (await office.runs.get(run_id)).status == RunStatus.FAILED


async def test_quota_survives_a_restart(tmp_path):
    import time as _time

    settings = Settings(data_dir=tmp_path / "home")
    first = Office(settings, runtime=FakeRuntime(scripts=[]))
    await first.open()
    first.scheduler.observe_rate_limit({"status": "rejected", "kind": "five_hour", "resets_at": _time.time() + 600})
    await first.close()
    second = Office(settings, runtime=FakeRuntime(scripts=[]))
    assert second.scheduler.last_rate_limit["kind"] == "five_hour"
    assert second.scheduler.capacity == 0  # still over the limit after the restart


async def test_running_out_of_steps_continues_in_a_new_run(office, runtime):
    minh = await hire(office, "writer", "Minh")

    async def long_job(ctx: TurnCtx):
        await ctx.office("ticket_comment", {"body": "Viết được nửa tài liệu"})
        return TurnResult(status=TurnStatus.MAX_TURNS, session_id=f"sess-{ctx.spec.run_id}")

    runtime.scripts.append(long_job)
    t = await office.tickets.create("Viết bộ tài liệu", assignee_actor_id=minh.id)
    await office.scheduler.wait_idle(timeout=10)

    assert (await office.tickets.get(t.id)).status == S.TODO  # not parked for the user
    [run] = await office.runs.by_actor(minh.id)
    assert run.status == RunStatus.SUCCEEDED and "làm tiếp" in (run.error or "")
    [wake] = await office.wakes.pending()
    assert wake.ticket_id == t.id and wake.payload["reasons"][0]["continuation"] == 1
