from __future__ import annotations

import pytest

from actoroffice.domain.errors import Conflict, InvalidTransition
from actoroffice.domain.work import TicketStatus as S


@pytest.fixture
async def minh(org, dev_position):
    return await org.hire(dev_position.id, "Minh", specialty="Backend Java")


@pytest.fixture
async def lan(org, dev_position):
    return await org.hire(dev_position.id, "Lan", specialty="Frontend")


async def test_create_assigns_key_and_wakes_assignee(tickets, wakes, minh):
    t = await tickets.create("Forgot password", assignee_actor_id=minh.id)

    assert t.key == f"AO-{t.id}"
    pending = await wakes.pending()
    assert [(w.actor_id, w.ticket_id, w.source.value) for w in pending] == [(minh.id, t.id, "assignment")]


async def test_wakes_coalesce_per_actor_and_ticket(tickets, wakes, minh):
    t = await tickets.create("X", assignee_actor_id=minh.id)
    await tickets.add_comment(t.id, "any update?", author_kind="user", author_id="user")

    pending = await wakes.pending()
    assert len(pending) == 1
    assert pending[0].coalesced == 1
    assert [r["source"] for r in pending[0].payload["reasons"]] == ["assignment", "comment"]


async def test_checkout_is_exclusive(tickets, minh):
    t = await tickets.create("X", assignee_actor_id=minh.id)

    owned = await tickets.checkout(t.id, minh.id, "run_a")
    assert owned.status == S.IN_PROGRESS
    assert owned.checkout_run_id == "run_a"
    # Same run may re-checkout; a different run gets a conflict.
    await tickets.checkout(t.id, minh.id, "run_a")
    with pytest.raises(Conflict):
        await tickets.checkout(t.id, minh.id, "run_b")


async def test_checkout_requires_assignee(tickets, minh, lan):
    t = await tickets.create("X", assignee_actor_id=minh.id)
    with pytest.raises(Conflict):
        await tickets.checkout(t.id, lan.id, "run_x")


async def test_release_allows_next_run(tickets, minh):
    t = await tickets.create("X", assignee_actor_id=minh.id)
    await tickets.checkout(t.id, minh.id, "run_a")
    await tickets.release(t.id, "run_a")

    again = await tickets.checkout(t.id, minh.id, "run_b")
    assert again.checkout_run_id == "run_b"


async def test_transition_rules(tickets, minh):
    t = await tickets.create("X", assignee_actor_id=minh.id)
    with pytest.raises(InvalidTransition):
        await tickets.transition(t.id, S.DONE, by="user")  # todo → done skips the work
    await tickets.checkout(t.id, minh.id, "run_a")
    with pytest.raises(Conflict):
        await tickets.transition(t.id, S.DONE, by="Lan", run_id="run_other")
    done = await tickets.transition(t.id, S.DONE, by="Minh", run_id="run_a", comment="Implemented + tests")
    assert done.status == S.DONE
    assert done.checkout_run_id is None


async def test_blockers_gate_checkout_and_wake_on_resolution(tickets, wakes, minh, lan):
    spec = await tickets.create("Spec", assignee_actor_id=lan.id)
    impl = await tickets.create("Implement", assignee_actor_id=minh.id, blocked_by=[spec.id], status=S.BLOCKED)

    with pytest.raises(Conflict, match="blocked by"):
        await tickets.checkout(impl.id, minh.id, "run_m")

    await tickets.checkout(spec.id, lan.id, "run_l")
    await tickets.transition(spec.id, S.DONE, by="Lan", run_id="run_l")

    impl_now = await tickets.get(impl.id)
    assert impl_now.status == S.TODO
    sources = {(w.actor_id, w.ticket_id): w.payload["reasons"][-1]["source"] for w in await wakes.pending()}
    assert sources[(minh.id, impl.id)] == "blockers_resolved"


async def test_documents_use_optimistic_revisions(tickets, minh):
    t = await tickets.create("X", assignee_actor_id=minh.id)
    d1 = await tickets.put_document(t.id, "spec", "v1", author="Mai", base_revision=None)
    assert d1.revision == 1
    d2 = await tickets.put_document(t.id, "spec", "v2", author="Mai", base_revision=1)
    assert d2.revision == 2
    with pytest.raises(Conflict):
        await tickets.put_document(t.id, "spec", "stale write", author="Minh", base_revision=1)
    assert (await tickets.get_document(t.id, "spec")).body == "v2"


async def test_reassign_refused_while_checked_out(tickets, minh, lan):
    t = await tickets.create("X", assignee_actor_id=minh.id)
    await tickets.checkout(t.id, minh.id, "run_a")
    with pytest.raises(Conflict):
        await tickets.assign(t.id, lan.id, by="user")
