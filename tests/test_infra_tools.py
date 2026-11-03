"""Shared data infrastructure and logs for the AI: grants required, read-only, masked, shaped answers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.errors import PolicyDenied
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.services.infra import check_read_only_sql
from actoroffice.services.logs import cluster, error_groups, parse_line, template

from .fakes import FakeRuntime, TurnCtx


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


def test_sql_guard() -> None:
    assert check_read_only_sql("SELECT * FROM orders WHERE note = 'delete me';") == "SELECT * FROM orders WHERE note = 'delete me'"
    for bad in ("DELETE FROM orders", "SELECT 1; DROP TABLE x", "WITH x AS (DELETE FROM o RETURNING *) SELECT * FROM x", "UPDATE o SET a = 1"):
        with pytest.raises(PolicyDenied):
            check_read_only_sql(bad)


def test_log_parsing_templates_and_errors() -> None:
    json_line = parse_line('{"@timestamp":"2026-11-01T10:00:00Z","level":"error","message":"order 123 failed","traceId":"abc"}', "orders")
    assert json_line.level == "ERROR" and json_line.fields["traceId"] == "abc"
    text_line = parse_line("2026-11-01 10:00:01.123 WARN [main] requestId=r-9 slow query 1530ms", "orders")
    assert text_line.level == "WARN" and text_line.fields["requestId"] == "r-9"
    assert template("user 42 paid 19.99 at 2026-11-01T10:00:00Z id=0f3a2b4c5d6e7f80") == "user <n> paid <n> at <ts> id=<hex>"
    lines = [parse_line(f"2026-11-01T10:00:0{i}Z INFO order {i} created", "o") for i in range(5)] + [parse_line("2026-11-01T10:00:09Z ERROR boom", "o")]
    groups = cluster(lines)
    assert groups[0]["count"] == 5 and groups[0]["template"].endswith("order <n> created")
    trace = parse_line("2026-11-01T10:00:00Z ERROR NullPointerException: x\n\tat com.acme.Refund.apply(Refund.java:42)", "o")
    assert error_groups([trace])[0]["error"].startswith("NullPointerException @ com.acme.Refund.apply")


async def test_tools_need_a_grant_and_answer_read_only(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    team = await office.teams.create_team("Billing")
    minh = await office.teams.create_user("Minh")
    await office.teams.add_member(team.id, minh.id, ["dev"])
    db = tmp_path / "orders.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE orders (id INTEGER, status TEXT, email TEXT)")
        conn.executemany("INSERT INTO orders VALUES (?, ?, ?)", [(i, "ACKED" if i % 2 else "NEW", f"u{i}@x.vn") for i in range(300)])
    from actoroffice.services.access import save_secret

    save_secret(office.settings.data_dir, "orders-db", {"dsn": {"read": f"sqlite:///{db.as_posix()}"}})
    await office.access.add_resource("orders-db", "db", team_id=team.id, config={"secret": "orders-db", "mask_columns": ["email"]})
    mq_calls: list[httpx.Request] = []

    def mq(request: httpx.Request) -> httpx.Response:
        mq_calls.append(request)
        if request.url.path == "/api/queues":
            return httpx.Response(200, json=[{"vhost": "/", "name": "orders.matched", "messages": 3, "consumers": 1}])
        return httpx.Response(200, json=[{"routing_key": "orders.matched", "payload": '{"order": 7}', "redelivered": False}])

    office.infra.transport = httpx.MockTransport(mq)
    await office.access.add_resource("orders-mq", "mq", team_id=team.id, env="local", config={"management_url": "http://mq.local:15672"})
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    now = datetime.now(UTC)
    old = (now - timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (logs_dir / "orders.log").write_text(
        "\n".join(
            [f"{old} INFO order {i} created requestId=r-{i}" for i in range(20)]
            + [f"{new} INFO order {i} created requestId=r-{i}" for i in range(20, 40)]
            + [f"{new} ERROR requestId=r-77 RefundException: amount < 0", "\tat com.acme.Refund.apply(Refund.java:42)"]
        ),
        encoding="utf-8",
    )
    await office.access.add_resource("billing-logs", "logs", team_id=team.id, env="local", config={"store": "files", "paths": {"orders": [str(logs_dir / "*.log")]}})

    ticket = await office.tickets.create("Khớp lệnh", description="x")
    out: dict[str, str] = {}

    async def explore(ctx: TurnCtx) -> TurnResult:
        from actoroffice.tools.registry import ToolError

        with pytest.raises(ToolError, match="env_access"):
            await ctx.office("db_query", {"resource": "orders-db", "sql": "SELECT count(*) FROM orders"})
        await ctx.office("env_access", {"resource": "orders-db", "level": "read", "reason": "xem trạng thái đơn trước bước khớp"})
        out["mq"] = await ctx.office("mq_peek", {"resource": "orders-mq", "queue": "orders.matched"})  # local: no grant needed
        out["errors"] = await ctx.office("logs_errors", {"resource": "billing-logs", "service": "orders", "since": "2h"})
        out["cluster"] = await ctx.office("logs_cluster", {"resource": "billing-logs", "service": "orders", "since": "2h"})
        out["trace"] = await ctx.office("logs_trace", {"resource": "billing-logs", "trace_id": "r-77", "since": "2h"})
        out["diff"] = await ctx.office("logs_diff", {"resource": "billing-logs", "service": "orders", "since": "20m"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    async def query(ctx: TurnCtx) -> TurnResult:
        out["schema"] = await ctx.office("db_schema", {"resource": "orders-db"})
        out["rows"] = await ctx.office("db_query", {"resource": "orders-db", "sql": "SELECT id, status, email FROM orders"})
        out["count"] = await ctx.office("db_query", {"resource": "orders-db", "sql": "SELECT status, count(*) AS n FROM orders GROUP BY status ORDER BY status"})
        from actoroffice.tools.registry import ToolError

        with pytest.raises(ToolError):
            await ctx.office("db_query", {"resource": "orders-db", "sql": "DELETE FROM orders"})
        out["scenario"] = await ctx.office(
            "scenario_record",
            {
                "name": "order-acked",
                "flow": "đặt lệnh",
                "step": "khớp",
                "queries": [{"resource": "orders-db", "table": "orders", "sql": "SELECT * FROM orders WHERE id = 1"}],
                "messages": [{"topic": "orders.routed", "value": {"order": 1}}],
            },
        )
        out["loaded"] = await ctx.office("scenario_load", {"name": "order-acked"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts += [explore, query]
    await office.stages.start(ticket.id, template="code", by="user", team_id=team.id)
    await office.scheduler.wait_idle(timeout=20)
    request = next(i for i in await office.interactions.pending(ticket_id=ticket.id) if i.kind.value == "env_access")
    await office.answer(request.id, {"approved": True}, by=minh.id)
    await office.scheduler.wait_idle(timeout=20)

    assert "orders.matched" in out["mq"] and mq_calls[-1].url.path.endswith("/get") and json.loads(mq_calls[-1].content)["ackmode"] == "ack_requeue_true"
    assert "RefundException @ com.acme.Refund.apply" in out["errors"]
    assert "order <n> created requestId=r-<n>" in out["cluster"] and "41 lines" in out["cluster"]
    assert "r-77" in out["trace"] and "RefundException" in out["trace"]
    assert "NEW" in out["diff"] and "RefundException" in out["diff"]
    assert "orders" in out["schema"]
    assert "u1@x.vn" not in out["rows"] and "***" in out["rows"] and "first 50 rows" in out["rows"]
    assert "ACKED | 150" in out["count"] and "NEW | 150" in out["count"]
    assert "1 rows" in out["scenario"] and '"status": "ACKED"' in out["loaded"] and '"email": "***"' in out["loaded"]
    assert "u1@x.vn" not in json.dumps(await office.runs.events((await office.runs.for_ticket(ticket.id))[0].id), ensure_ascii=False)


async def test_topic_info_finds_producers_and_consumers(office: Office, runtime: FakeRuntime) -> None:
    ticket = await office.tickets.create("Topic", description="x")
    out: list[str] = []

    async def scan(ctx: TurnCtx) -> TurnResult:
        (ctx.spec.cwd / "Producer.java").write_text('class P {\n  void go() {\n    kafka.send("orders.matched", event);\n  }\n}\n', encoding="utf-8")
        (ctx.spec.cwd / "Consumer.java").write_text('class C {\n  @KafkaListener(topics = "orders.matched")\n  void on(Event e) {}\n}\n', encoding="utf-8")
        out.append(await ctx.office("topic_info", {"topic": "orders.matched"}))
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts.append(scan)
    await office.stages.start(ticket.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    text = out[0]
    producers, consumers = text.split("Consumers:")
    assert "Producer.java:3" in producers and "Consumer.java:2" in consumers


