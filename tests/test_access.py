"""Access envelope: shared environments reached through a local proxy with time-limited, approved grants."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.core.http import ssl_context
from actoroffice.domain.errors import PolicyDenied
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import InteractionKind
from actoroffice.runtimes.base import GateDecision
from actoroffice.services.access import save_secret

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db

TOKEN = "Bearer s3cr3t-payments-token"


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


@pytest.fixture
async def team(office: Office):
    team = await office.teams.create_team("Billing")
    minh = await office.teams.create_user("Minh")
    lan = await office.teams.create_user("Lan")
    await office.teams.add_member(team.id, minh.id, ["dev"])
    await office.teams.add_member(team.id, lan.id, ["ba"])
    return team, minh, lan


def upstream(seen: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"order": 42, "path": request.url.path})

    return httpx.MockTransport(handler)


async def test_production_is_never_a_resource(office: Office, team) -> None:
    t, *_ = team
    with pytest.raises(PolicyDenied):
        await office.access.add_resource("orders", "http", team_id=t.id, env="prod", config={"base_url": "https://orders.dev"})
    with pytest.raises(PolicyDenied):
        await office.access.add_resource("orders", "http", team_id=t.id, env="dev", config={"base_url": "https://orders.prod.acme.vn"})


async def test_grant_opens_a_credentialed_proxy_that_enforces_the_level(office: Office, runtime: FakeRuntime, team) -> None:
    t, minh, lan = team
    seen: list[httpx.Request] = []
    office.access.transport = upstream(seen)
    save_secret(office.settings.data_dir, "payments", {"headers": {"Authorization": TOKEN}})
    await office.access.add_resource(
        "payments-api", "http", team_id=t.id, env="dev", config={"base_url": "https://payments.dev.acme.vn", "secret": "payments"}, max_level="write-sandboxed"
    )
    ticket = await office.tickets.create("Đối soát", description="x")
    envs: list[dict[str, str]] = []

    async def ask(ctx: TurnCtx) -> TurnResult:
        listing = await ctx.office("access_list", {})
        assert "payments-api" in listing and "not granted" in listing
        out = await ctx.office("env_access", {"resource": "payments-api", "level": "read", "reason": "đọc trạng thái thanh toán cho test AC-2"})
        assert "Minh" in out
        dump = await ctx.gate.before_tool("Bash", {"command": "printenv"}, None)
        assert dump.decision == GateDecision.DENY
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    async def use(ctx: TurnCtx) -> TurnResult:
        envs.append(dict(ctx.spec.env))
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts += [ask, use]
    await office.stages.start(ticket.id, template="code", by="user", team_id=t.id, owner_user_id=lan.id)
    await office.scheduler.wait_idle(timeout=20)
    item = next(i for i in await office.interactions.pending(ticket_id=ticket.id) if i.kind == InteractionKind.ENV_ACCESS)
    assert item.payload["approver_names"] == ["Minh"] and (await office.stages.status(ticket.id))["status"] == "needs_you"
    with pytest.raises(PolicyDenied):
        await office.answer(item.id, {"approved": True}, by=lan.id)  # a BA cannot grant environment access
    await office.answer(item.id, {"approved": True}, by=minh.id)
    await office.scheduler.wait_idle(timeout=20)

    url = envs[0]["AO_PAYMENTS_API_URL"]
    assert url.startswith("http://127.0.0.1:") and TOKEN not in str(envs[0])  # the run never sees the token
    async with httpx.AsyncClient(verify=ssl_context()) as client:  # the CA bundle is loaded once per process
        got = await client.get(f"{url}/v1/payments/42?expand=1")
        assert got.status_code == 200 and got.json()["order"] == 42
        denied = await client.post(f"{url}/v1/refunds", json={"x": 1})
        assert denied.status_code == 403 and "chỉ đọc" in denied.json()["error"]
    assert len(seen) == 1 and seen[0].headers["authorization"] == TOKEN and seen[0].url.query == b"expand=1"
    audit = await office.db.fetchall("SELECT action, outcome FROM audit_log WHERE action = 'access:payments-api' ORDER BY id")
    assert [a["outcome"] for a in audit] == ["proxied", "denied"]

    grant, _ = (await office.access.active(ticket.id))[0]
    await office.access.revoke(grant.id, by=minh.id)
    assert await office.access.active(ticket.id) == []
    with pytest.raises(httpx.HTTPError):
        async with httpx.AsyncClient(verify=ssl_context()) as client:
            await client.get(f"{url}/v1/payments/42")


async def test_access_asked_in_the_plan_is_approved_with_the_plan(office: Office, runtime: FakeRuntime, team) -> None:
    t, minh, lan = team
    office.access.transport = upstream([])
    await office.access.add_resource("orders-api", "http", team_id=t.id, config={"base_url": "https://orders.dev.acme.vn"})
    ticket = await office.tickets.create("Kế hoạch", description="x")
    await office.db.execute("UPDATE tickets SET team_id = ?, stage = 'plan', stage_template = 'code', owner_user_id = ? WHERE id = ?", (t.id, lan.id, ticket.id))
    await office.tickets.put_document(ticket.id, "requirements", "AC-1: Given a When b Then c", author="Copilot", base_revision=None)
    plan = "## Phương án A\nx\n## Phương án B\ny\nAC-1 → test_ac1\n## Truy cập\norders-api read"

    async def planning(ctx: TurnCtx) -> TurnResult:
        out = await ctx.office("env_access", {"resource": "orders-api", "level": "read", "reason": "contract test với orders-api dev"})
        assert "together with the plan" in out
        await ctx.office("doc_put", {"key": "plan", "body": plan, "base_revision": None})
        await ctx.office("stage_complete", {"summary": "2 phương án"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts.append(planning)
    await office.tickets.assign(ticket.id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=20)
    gate = next(i for i in await office.interactions.pending(ticket_id=ticket.id) if i.kind == InteractionKind.APPROVE_STAGE)
    assert gate.payload["access"] and "orders-api" in gate.payload["access"][0]
    await office.answer(gate.id, {"approved": True}, by=minh.id)
    active = await office.access.active(ticket.id)
    assert [r.name for _g, r in active] == ["orders-api"]
    await office.scheduler.wait_idle(timeout=20)
