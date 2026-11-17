"""The start chat of the team UI: the office AI turns what people write into ticket proposals and points them to the
page where something must be set up."""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import InteractionKind
from actoroffice.engine import context
from actoroffice.web.server import create_app

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db
from .test_web import HEADERS, TOKEN, login


@pytest.fixture
def client(tmp_path):
    runtime = FakeRuntime(scripts=[])
    office = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False, doc_sweep_s=0), runtime=runtime)
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        seed_db(office.settings.db_path)
        await office.open()  # on the client's event loop
        await office.start_notifier()
        yield
        await office.close()

    app.router.lifespan_context = lifespan
    with TestClient(app, base_url="http://localhost:8765") as c:
        login(c)
        yield office, runtime, c


def test_a_request_in_the_start_chat_becomes_a_proposal_and_a_button(client) -> None:
    office, runtime, c = client

    async def ai(ctx: TurnCtx) -> TurnResult:
        assert "# Start chat (team UI)" in ctx.spec.system_prompt, "the office AI's direct chat follows the start-chat rules"
        await ctx.office("suggest_actions", {"text": "Chưa có kết nối Jira: thêm ở trang Kết nối.", "actions": [{"page": "connections"}]})
        await ctx.office("propose_tickets", {"items": [
            {"title": "Hạn mức hoàn tiền theo loại khách", "why": "BA yêu cầu trong chat", "description": "AC-1: khách VIP 20 lần/ngày", "workflow": "epic"},
        ]})
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s1", text="Tôi đã đề xuất 1 epic, bạn xem và duyệt nhé.")

    runtime.scripts.append(ai)
    start = c.get("/api/copilot", headers=HEADERS).json()
    assert start["messages"] == [] and start["ai"]["name"] == office.settings.ai_name
    conv = start["conversation"]["id"]
    assert c.get("/api/copilot", headers=HEADERS).json()["conversation"]["id"] == conv, "one start chat, created once"
    c.post(f"/api/conversations/{conv}/messages", json={"body": "Cần thêm hạn mức hoàn tiền theo loại khách"}, headers=HEADERS)
    c.portal.call(lambda: office.scheduler.wait_idle(timeout=15))
    after = c.get("/api/copilot", headers=HEADERS).json()

    bodies = [(m["author_kind"], m["body"], m["meta"].get("actions")) for m in after["messages"]]
    assert bodies[0][:2] == ("user", "Cần thêm hạn mức hoàn tiền theo loại khách")
    assert ("actor", "Chưa có kết nối Jira: thêm ở trang Kết nối.", [{"page": "connections", "label": "kết nối Jira, Confluence, Bitbucket, Oracle… (Kết nối)"}]) in bodies
    assert bodies[-1][:2] == ("actor", "Tôi đã đề xuất 1 epic, bạn xem và duyệt nhé.")
    (card,) = after["pending"]
    assert card["kind"] == InteractionKind.PROPOSE_TICKETS.value and card["payload"]["items"][0]["workflow"] == "epic" and not after["thinking"]


def test_the_start_chat_rules_are_only_for_the_office_ais_direct_chat() -> None:
    assert context.COPILOT_CHAT_RULES_BLOCK.startswith("\n\n# Start chat (team UI)")
    assert "propose_tickets" in context.COPILOT_CHAT_RULES and "suggest_actions" in context.COPILOT_CHAT_RULES
