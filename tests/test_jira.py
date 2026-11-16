"""The Jira mirror through a tracker connection (a fake adapter: nothing is called outside)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.work import InteractionKind
from actoroffice.ports.docs import Issue

from .fake_adapters import FakeTracker, connect, install, uninstall
from .fakes import FakeRuntime
from .seed import seed_db


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    install()
    yield o
    uninstall()
    await o.close()


async def approve_requirements(office: Office, ticket_id: int, by: str) -> None:
    ticket = await office.tickets.get(ticket_id)
    await office.tickets.put_document(ticket.id, "requirements", "AC-1: Given x When y Then z", author="Copilot", base_revision=None)
    await office.tickets.put_document(ticket.id, "dossier", "# Dossier", author="Copilot", base_revision=None)
    stage = await office.stages.stage_def(ticket)
    gate = await office.stages._open_gate(ticket, stage, "requirement", "xong", [], run_id=None)
    await office.answer(gate.id, {"approved": True}, by=by)


async def test_labelled_issues_become_staged_tickets_and_changes_are_mirrored(office: Office) -> None:
    await connect(office, "fake_tracker", write=True)
    team = await office.teams.create_team("Billing", jira_project_key="BIL")
    lan = await office.teams.create_user("Lan", roles=[], email="lan@acme.vn")
    await office.teams.add_member(team.id, lan.id, ["ba"])
    FakeTracker.add("BIL-7", "Hoàn tiền 30 ngày", "Khách muốn hoàn tiền.\n\nTrong 30 ngày.", ["ai"])
    FakeTracker.add("BIL-8", "So sánh cổng thanh toán", "plain text desc", ["ai", "ai-research"])

    created = await office.jira.sync()
    assert len(created) == 2 and "project = BIL AND labels = ai" in FakeTracker.searches[0]
    t7 = await office.tickets.get(created[0])
    assert t7.jira_key == "BIL-7" and t7.jira_url == "https://jira.test/browse/BIL-7"
    assert t7.stage == "understand" and t7.stage_template == "code" and t7.owner_user_id == lan.id and t7.team_id == team.id
    assert t7.description == "Khách muốn hoàn tiền.\n\nTrong 30 ngày."
    assert (await office.tickets.get(created[1])).stage_template == "research"
    assert await office.jira.sync() == []  # idempotent

    # BA approves the requirements → they are posted back to Jira
    await approve_requirements(office, t7.id, lan.id)
    assert FakeTracker.comments and FakeTracker.comments[0][0] == "BIL-7" and "AC-1" in FakeTracker.comments[0][1]

    # the reporter edits the description after approval → recorded as a requirement change
    FakeTracker.add("BIL-7", "Hoàn tiền 30 ngày", "Khách muốn hoàn tiền.\n\nTrong 45 ngày.", ["ai"])
    await office.jira.sync()
    changes = await office.tickets.changes_after(t7.id, 0)
    assert changes and "45 ngày" in changes[-1].body
    assert [i for i in await office.interactions.pending(ticket_id=t7.id) if i.kind == InteractionKind.APPROVE_STAGE] == []


async def test_the_owner_is_found_by_name_when_the_tracker_hides_the_email(office: Office) -> None:
    await connect(office, "fake_tracker")
    await office.teams.create_team("Billing", jira_project_key="BIL")
    lan = await office.teams.create_user("Lan", roles=[], email="lan@acme.vn")
    FakeTracker.add("BIL-1", "Có tên", "x", ["ai"], reporter_email="", reporter_name="lan")
    FakeTracker.add("BIL-2", "Người lạ", "x", ["ai"], reporter_email="ai@khac.vn", reporter_name="Ai đó")
    one, two = [await office.tickets.get(key) for key in await office.jira.sync()]
    assert one.owner_user_id == lan.id and two.owner_user_id is None


async def test_nothing_is_mirrored_without_a_tracker_and_a_connection_added_later_starts_it(office: Office) -> None:
    office.settings.jira_poll_s = 0.02
    await office.teams.create_team("Billing", jira_project_key="BIL")
    FakeTracker.add("BIL-7", "Hoàn tiền", "x", ["ai"])
    assert not await office.jira.ready() and await office.jira.sync() == []
    task = asyncio.create_task(office.jira.run_forever())  # what `Office.start` runs, whether or not a tracker is connected
    try:
        await asyncio.sleep(0.1)
        assert FakeTracker.searches == [] and await office.db.fetchone("SELECT 1 FROM tickets WHERE jira_key = 'BIL-7'") is None
        await connect(office, "fake_tracker")  # no restart
        for _ in range(100):
            if await office.db.fetchone("SELECT 1 FROM tickets WHERE jira_key = 'BIL-7'"):
                break
            await asyncio.sleep(0.02)
        assert await office.db.fetchone("SELECT 1 FROM tickets WHERE jira_key = 'BIL-7'") is not None
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_each_team_is_mirrored_through_its_nearest_tracker(office: Office) -> None:
    billing = await office.teams.create_team("Billing", jira_project_key="BIL")
    await office.teams.create_team("Core", jira_project_key="CORE")
    await connect(office, "fake_tracker", scope=f"team:{billing.id}")  # only Billing has a tracker
    FakeTracker.add("BIL-7", "Hoàn tiền", "x", ["ai"])
    assert await office.jira.ready()
    assert len(await office.jira.sync()) == 1
    assert len(FakeTracker.searches) == 1 and "project = BIL" in FakeTracker.searches[0], "Core has no tracker in its scope: not asked"


async def test_a_read_only_tracker_mirrors_but_posts_nothing_back(office: Office) -> None:
    await connect(office, "fake_tracker")  # read-only
    team = await office.teams.create_team("Billing", jira_project_key="BIL")
    lan = await office.teams.create_user("Lan", roles=[], email="lan@acme.vn")
    await office.teams.add_member(team.id, lan.id, ["ba"])
    FakeTracker.add("BIL-7", "Hoàn tiền", "x", ["ai"])
    (key,) = await office.jira.sync()
    ticket = await office.tickets.get(key)
    await approve_requirements(office, ticket.id, lan.id)
    assert FakeTracker.comments == []
    assert not any("Không ghi được lên Jira" in c.body for c in await office.tickets.comments(ticket.id)), "read-only on purpose is not an error"


async def test_a_description_that_only_changed_its_markup_is_not_a_requirement_change(office: Office) -> None:
    """An office that mirrored with the old client stored plain text; the adapter gives markdown."""
    await connect(office, "fake_tracker", write=True)
    team = await office.teams.create_team("Billing", jira_project_key="BIL")
    lan = await office.teams.create_user("Lan", roles=[], email="lan@acme.vn")
    await office.teams.add_member(team.id, lan.id, ["ba"])
    FakeTracker.add("BIL-7", "Hoàn tiền", "Luật:\n- VIP 60 ngày\n- khác 30", ["ai"])
    (key,) = await office.jira.sync()
    ticket = await office.tickets.get(key)
    await approve_requirements(office, ticket.id, lan.id)
    FakeTracker.issues["BIL-7"] = FakeTracker.issues["BIL-7"].model_copy(update={"description": "Luật:\n\n- **VIP** 60 ngày\n- khác 30"})
    await office.jira.sync()
    assert (await office.tickets.get(key)).description == "Luật:\n\n- **VIP** 60 ngày\n- khác 30", "the text is brought up to date"
    assert await office.tickets.changes_after(ticket.id, 0) == []


async def test_jira_cloud_asks_for_the_reporter_and_fills_it() -> None:
    """What the mirror reads of an issue comes from the adapter: key, title, description, labels, link, reporter."""
    import json

    import httpx

    from actoroffice.adapters.atlassian.jira_cloud import JiraCloud, JiraCloudConfig

    asked: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        asked.append(json.loads(request.content)["fields"])
        fields = {"summary": "Hoàn tiền", "labels": ["ai", "AI-Research"], "reporter": {"emailAddress": "Lan@acme.vn", "displayName": "Lan"}, "description": "mô tả"}
        return httpx.Response(200, json={"issues": [{"key": "BIL-7", "fields": fields}, {"key": "BIL-8", "fields": {"summary": "Không ai"}}], "isLast": True})

    adapter = JiraCloud(JiraCloudConfig(site="https://acme.atlassian.net"), {"email": "bot@acme.vn", "token": "t"}, transport=httpx.MockTransport(handler))
    one, two = await adapter.search("project = BIL", 50)
    await adapter.close()
    assert "reporter" in asked[0] and {"summary", "description", "labels"} <= set(asked[0])
    assert (one.key, one.title, one.description, one.labels, one.url) == ("BIL-7", "Hoàn tiền", "mô tả", ["ai", "AI-Research"], "https://acme.atlassian.net/browse/BIL-7")
    assert (one.reporter_email, one.reporter_name) == ("Lan@acme.vn", "Lan") and (two.reporter_email, two.reporter_name) == ("", "")
    assert (Issue(key="A-1", title="t").reporter_email, Issue(key="A-1", title="t").reporter_name) == ("", "")
