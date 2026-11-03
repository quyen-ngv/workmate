"""Jira Cloud mirror against a fake Jira (httpx MockTransport)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.work import InteractionKind
from actoroffice.services.jira import JiraClient, JiraMirror, adf_to_text, text_to_adf

from .fakes import FakeRuntime


def adf(*paragraphs: str) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": p}]} for p in paragraphs]}


class FakeJira:
    def __init__(self) -> None:
        self.issues: dict[str, dict[str, Any]] = {}
        self.comments: list[tuple[str, dict[str, Any]]] = []
        self.searches: list[str] = []

    def add(self, key: str, summary: str, description: Any, labels: list[str], reporter: str = "lan@acme.vn") -> None:
        self.issues[key] = {
            "key": key,
            "fields": {"summary": summary, "description": description, "labels": labels, "reporter": {"emailAddress": reporter, "displayName": "Lan"}},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"].startswith("Basic ")
        if request.url.path == "/rest/api/3/search/jql":
            body = json.loads(request.content)
            self.searches.append(body["jql"])
            return httpx.Response(200, json={"issues": list(self.issues.values()), "isLast": True})
        if request.url.path.endswith("/comment"):
            key = request.url.path.split("/")[-2]
            self.comments.append((key, json.loads(request.content)["body"]))
            return httpx.Response(201, json={"id": "1"})
        return httpx.Response(404, json={})


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


@pytest.fixture
def jira(office: Office) -> FakeJira:
    fake = FakeJira()
    office.jira = JiraMirror(office, JiraClient("https://acme.atlassian.net", "bot@acme.vn", "tok", transport=httpx.MockTransport(fake.handler)))
    return fake


async def test_labelled_issues_become_staged_tickets_and_changes_are_mirrored(office: Office, jira: FakeJira) -> None:
    team = await office.teams.create_team("Billing", jira_project_key="BIL")
    lan = await office.teams.create_user("Lan", roles=[], email="lan@acme.vn")
    await office.teams.add_member(team.id, lan.id, ["ba"])
    jira.add("BIL-7", "Hoàn tiền 30 ngày", adf("Khách muốn hoàn tiền.", "Trong 30 ngày."), ["ai"])
    jira.add("BIL-8", "So sánh cổng thanh toán", "plain text desc", ["ai", "ai-research"])

    created = await office.jira.sync()
    assert len(created) == 2 and "project = BIL AND labels = ai" in jira.searches[0]
    t7 = await office.tickets.get(created[0])
    assert t7.jira_key == "BIL-7" and t7.jira_url == "https://acme.atlassian.net/browse/BIL-7"
    assert t7.stage == "understand" and t7.stage_template == "code" and t7.owner_user_id == lan.id and t7.team_id == team.id
    assert t7.description == "Khách muốn hoàn tiền.\n\nTrong 30 ngày."
    assert (await office.tickets.get(created[1])).stage_template == "research"
    assert await office.jira.sync() == []  # idempotent

    # BA approves the requirements → they are posted back to Jira
    await office.tickets.put_document(t7.id, "requirements", "AC-1: Given x When y Then z", author="Copilot", base_revision=None)
    await office.tickets.put_document(t7.id, "dossier", "# Dossier", author="Copilot", base_revision=None)
    stage = await office.stages.stage_def(t7)
    gate = await office.stages._open_gate(t7, stage, "requirement", "xong", [], run_id=None)
    await office.answer(gate.id, {"approved": True}, by=lan.id)
    assert jira.comments and jira.comments[0][0] == "BIL-7"
    assert "AC-1" in adf_to_text(jira.comments[0][1])

    # the reporter edits the description after approval → recorded as a requirement change
    jira.add("BIL-7", "Hoàn tiền 30 ngày", adf("Khách muốn hoàn tiền.", "Trong 45 ngày."), ["ai"])
    await office.jira.sync()
    changes = await office.tickets.changes_after(t7.id, 0)
    assert changes and "45 ngày" in changes[-1].body
    assert [i for i in await office.interactions.pending(ticket_id=t7.id) if i.kind == InteractionKind.APPROVE_STAGE] == []


def test_adf_round_trip_and_rich_nodes() -> None:
    doc = {
        "type": "doc",
        "content": [
            {"type": "heading", "attrs": {"level": 2}, "content": [{"type": "text", "text": "Rules"}]},
            {"type": "bulletList", "content": [
                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "VIP 60 days"}]}]},
                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "others 30"}]}]},
            ]},
            {"type": "codeBlock", "content": [{"type": "text", "text": "SELECT 1"}]},
            {"type": "paragraph", "content": [{"type": "mention", "attrs": {"text": "@Minh"}}, {"type": "text", "text": " xem giúp"}]},
        ],
    }
    text = adf_to_text(doc)
    assert "## Rules" in text and "- VIP 60 days" in text and "```\nSELECT 1\n```" in text and "@Minh xem giúp" in text
    back = text_to_adf("Dòng 1\nDòng 2\n\n```sql\nSELECT 1\n```")
    assert back["content"][0]["content"][1] == {"type": "hardBreak"}
    assert back["content"][1] == {"type": "codeBlock", "content": [{"type": "text", "text": "SELECT 1"}]}
