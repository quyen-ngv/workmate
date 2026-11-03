"""Tools for shared data infrastructure, logs, messaging topics and flow scenarios. Shared resources need an
active grant for the ticket (env_access); answers are shaped and capped so raw dumps never flood the context."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from actoroffice.domain.errors import OfficeError

from .registry import ToolCtx, ToolDef, ToolError, schema

RES = {"resource": {"type": "string", "description": "resource name (access_list)"}}
WINDOW = {
    "since": {"type": "string", "description": "e.g. 30m, 2h, 1d or an ISO time (default: last 60 minutes)"},
    "until": {"type": "string"},
}


async def _resource(args: dict[str, Any], ctx: ToolCtx, kinds: tuple[str, ...]) -> Any:
    try:
        return await ctx.office.infra.resource_for(ctx.ticket_id, str(args.get("resource") or ""), kinds)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc


async def _guard(coro: Any) -> Any:
    try:
        return await coro
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    except Exception as exc:  # driver / network errors become a readable tool error
        raise ToolError(f"{type(exc).__name__}: {ctx_safe(str(exc))}") from exc


def ctx_safe(text: str) -> str:
    return re.sub(r"://[^@/\s]+@", "://***@", text)[:500]


# ---- databases -------------------------------------------------------------------------------------------------


async def db_schema(args: dict[str, Any], ctx: ToolCtx) -> str:
    if args.get("database") and not args.get("resource"):  # a SQLite file in the workspace / project (no grant needed)
        from .work_tools import db_schema as local_schema

        return await local_schema(args, ctx)
    resource = await _resource(args, ctx, ("db",))
    return await _guard(ctx.office.infra.schema(resource, str(args.get("table") or "") or None))


async def db_query(args: dict[str, Any], ctx: ToolCtx) -> str:
    from actoroffice.services.infra import format_table

    if args.get("database") and not args.get("resource"):
        from .work_tools import db_query as local_query

        return await local_query(args, ctx)
    resource = await _resource(args, ctx, ("db",))
    columns, rows, more = await _guard(ctx.office.infra.query(resource, str(args.get("sql") or ""), limit=int(args.get("limit") or 50)))
    return format_table(columns, rows, more)


# ---- redis / kafka / rabbitmq ----------------------------------------------------------------------------------


async def redis_scan(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource = await _resource(args, ctx, ("redis",))
    keys = await _guard(ctx.office.infra.redis_scan(resource, str(args.get("pattern") or "*"), int(args.get("limit") or 100)))
    return "\n".join(keys) or "(no keys)"


async def redis_get(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource = await _resource(args, ctx, ("redis",))
    return await _guard(ctx.office.infra.redis_get(resource, str(args.get("key") or "")))


async def kafka_peek(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource = await _resource(args, ctx, ("kafka",))
    messages = await _guard(ctx.office.infra.kafka_peek(resource, str(args.get("topic") or ""), min(int(args.get("n") or 10), 50), str(args.get("from") or "latest")))
    return "\n".join(f"p{m['partition']}@{m['offset']} key={m['key']} {m['value']}" for m in messages) or "(no messages)"


async def mq_queues(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource = await _resource(args, ctx, ("mq",))
    queues = await _guard(ctx.office.infra.mq_queues(resource))
    return "\n".join(f"{q['vhost']} {q['name']}: {q['messages']} messages, {q['consumers']} consumers" for q in queues) or "(no queues)"


async def mq_peek(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource = await _resource(args, ctx, ("mq",))
    messages = await _guard(ctx.office.infra.mq_peek(resource, str(args.get("queue") or ""), int(args.get("n") or 5), str(args.get("vhost") or "/")))
    return "\n".join(f"[{m['routing_key']}] {m['payload']}" for m in messages) or "(queue empty)"


# ---- logs ------------------------------------------------------------------------------------------------------


async def _logs(args: dict[str, Any], ctx: ToolCtx) -> tuple[Any, Any, Any]:
    from actoroffice.services.logs import window

    resource = await _resource(args, ctx, ("logs",))
    try:
        since, until = window(args.get("since"), args.get("until"))
    except (OfficeError, ValueError) as exc:
        raise ToolError(str(exc)) from exc
    return resource, since, until


async def logs_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource, since, until = await _logs(args, ctx)
    return await _guard(ctx.office.logs.search(resource, str(args.get("service") or "*"), str(args.get("query") or ""), since, until, args.get("level"), min(int(args.get("limit") or 100), 200)))


async def logs_cluster(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource, since, until = await _logs(args, ctx)
    return await _guard(ctx.office.logs.clusters(resource, str(args.get("service") or "*"), str(args.get("query") or ""), since, until))


async def logs_errors(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource, since, until = await _logs(args, ctx)
    return await _guard(ctx.office.logs.errors(resource, str(args.get("service") or "*"), since, until))


async def logs_trace(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource, since, until = await _logs(args, ctx)
    return await _guard(ctx.office.logs.trace(resource, str(args.get("trace_id") or ""), since, until))


async def logs_diff(args: dict[str, Any], ctx: ToolCtx) -> str:
    resource, since, until = await _logs(args, ctx)
    return await _guard(ctx.office.logs.diff(resource, str(args.get("service") or "*"), since, until))


async def app_logs(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Logs of the app running in this ticket's workspace (profile.logs globs, default logs/**/*.log)."""
    from actoroffice.domain.access import Resource, ResourceKind
    from actoroffice.services.logs import window

    if ctx.ticket_id is None or ctx.workspace is None:
        raise ToolError("app_logs works inside a ticket.")
    ticket = await ctx.office.tickets.get(ctx.ticket_id)
    project = await ctx.office.projects.get(ticket.project_id) if ticket.project_id else None
    globs = (project.profile.get("logs") if project else None) or ["logs/**/*.log", "*.log", "target/*.log", "build/*.log"]
    paths = [str(ctx.workspace / g) for g in globs]
    local = Resource(id="local", kind=ResourceKind.LOGS, name="app", env="local", config={"store": "files", "paths": {"*": paths}})
    since, until = window(args.get("since"), args.get("until"), default_minutes=24 * 60)
    mode = str(args.get("mode") or "search")
    logs = ctx.office.logs
    if mode == "cluster":
        return await _guard(logs.clusters(local, "app", str(args.get("query") or ""), since, until))
    if mode == "errors":
        return await _guard(logs.errors(local, "app", since, until))
    return await _guard(logs.search(local, "app", str(args.get("query") or ""), since, until, args.get("level"), min(int(args.get("limit") or 100), 200)))


# ---- topics and scenarios --------------------------------------------------------------------------------------

PRODUCE = re.compile(r"(send|produce|publish|convertAndSend|basicPublish|emit)\s*\(", re.IGNORECASE)
CONSUME = re.compile(r"(@KafkaListener|@RabbitListener|subscribe|consume|@StreamListener|on_message|Consumer)", re.IGNORECASE)
CODE_EXT = {".java", ".kt", ".py", ".go", ".ts", ".js", ".cs", ".scala", ".rb", ".yml", ".yaml", ".properties", ".json"}


async def topic_info(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Who produces and who consumes a topic/queue: scan the workspace and reference repos, plus KB contracts."""
    topic = str(args.get("topic") or "").strip()
    if not topic:
        raise ToolError("topic is required")
    roots = [ctx.workspace] if ctx.workspace else []
    gate = ctx.office.gates.get(ctx.run_id)
    if gate is not None:
        roots += list(gate.read_dirs)
    producers, consumers, mentions = [], [], []
    for root in roots:
        for path in Path(root).rglob("*"):
            if path.suffix not in CODE_EXT or not path.is_file() or any(p in path.parts for p in (".git", "node_modules", "target", "build", ".venv")):
                continue
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for i, line in enumerate(lines):
                if topic not in line:
                    continue
                around = " ".join(lines[max(0, i - 3) : i + 4])
                where = f"{path}:{i + 1}: {line.strip()[:160]}"
                if CONSUME.search(around):
                    consumers.append(where)
                elif PRODUCE.search(around):
                    producers.append(where)
                else:
                    mentions.append(where)
            if len(producers) + len(consumers) + len(mentions) > 200:
                break
    ticket = await ctx.office.tickets.get(ctx.ticket_id) if ctx.ticket_id else None
    contracts = await ctx.office.knowledge.search(topic, team_id=ticket.team_id if ticket else None, kinds=["contract", "flow"], limit=5)
    parts = [f"Topic {topic}"]
    parts += ["Producers:"] + [f"  {p}" for p in producers[:20]] if producers else ["Producers: none found in the code you can read"]
    parts += ["Consumers:"] + [f"  {c}" for c in consumers[:20]] if consumers else ["Consumers: none found"]
    if mentions:
        parts += ["Other mentions (config, constants):"] + [f"  {m}" for m in mentions[:20]]
    if contracts:
        parts += ["Knowledge base:"] + [f"  {c.line()}" for c in contracts]
    else:
        parts.append("No contract in the knowledge base yet: once you confirm schema/key/ordering, save it with kb_note kind contract.")
    return "\n".join(parts)


async def scenario_record(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Capture the state before a step of a flow (masked rows + messages) as a fixture file in the repo, so tests
    can start from it (reviewed in the pull request like any other file)."""
    if ctx.workspace is None or ctx.ticket_id is None:
        raise ToolError("scenario_record works inside a ticket.")
    name = re.sub(r"[^a-z0-9_\-]+", "-", str(args.get("name") or "").lower()).strip("-")
    if not name:
        raise ToolError("name is required (e.g. order-acked)")
    data: dict[str, Any] = {"name": name, "flow": args.get("flow"), "step": args.get("step"), "tables": {}, "messages": args.get("messages") or []}
    for q in args.get("queries") or []:
        resource = await _resource({"resource": q.get("resource")}, ctx, ("db",))
        columns, rows, _more = await _guard(ctx.office.infra.query(resource, str(q.get("sql") or ""), limit=int(q.get("limit") or 50)))
        data["tables"][str(q.get("table") or q.get("sql"))[:80]] = [dict(zip(columns, r, strict=False)) for r in rows]
    folder = ctx.workspace / str(args.get("folder") or "test-fixtures/scenarios")
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    rows = sum(len(v) for v in data["tables"].values())
    return f"Scenario '{name}' saved to {path.relative_to(ctx.workspace)} ({rows} rows, {len(data['messages'])} messages; masked columns stay ***). Load it in tests."


async def scenario_load(args: dict[str, Any], ctx: ToolCtx) -> str:
    if ctx.workspace is None:
        raise ToolError("scenario_load works inside a ticket.")
    name = re.sub(r"[^a-z0-9_\-]+", "-", str(args.get("name") or "").lower()).strip("-")
    for path in ctx.workspace.rglob(f"{name}.json"):
        if "scenarios" in path.parts:
            return path.read_text(encoding="utf-8")[:12000]
    raise ToolError(f"No scenario '{name}' (scenario_record creates one)")


def _when(extra: dict[str, Any]) -> dict[str, Any]:
    return {**WINDOW, **extra}


INFRA_TOOLS: list[ToolDef] = [
    ToolDef(
        "db_schema",
        "Tables of a database resource (or of a SQLite file: database=path), or the columns of one table.",
        schema({**RES, "database": {"type": "string"}, "table": {"type": "string"}}, required=[]),
        db_schema,
        read_only=True,
    ),
    ToolDef(
        "db_query",
        "Read-only SQL (one SELECT/WITH/EXPLAIN) on a database resource (or a SQLite file: database=path); at most "
        "200 rows, sensitive columns masked.",
        schema({**RES, "database": {"type": "string"}, "sql": {"type": "string"}, "limit": {"type": "integer"}}, required=["sql"]),
        db_query,
        read_only=True,
    ),
    ToolDef(
        "redis_scan",
        "List Redis keys matching a pattern (SCAN, capped).",
        schema({**RES, "pattern": {"type": "string"}, "limit": {"type": "integer"}}, required=["resource"]),
        redis_scan,
        read_only=True,
    ),
    ToolDef("redis_get", "Read one Redis key (type, TTL, value capped).", schema({**RES, "key": {"type": "string"}}, required=["resource", "key"]), redis_get, read_only=True),
    ToolDef(
        "kafka_peek",
        "Read the last n messages of a Kafka topic without committing offsets (no consumer group is affected).",
        schema({**RES, "topic": {"type": "string"}, "n": {"type": "integer"}, "from": {"type": "string", "enum": ["latest", "earliest"]}}, required=["resource", "topic"]),
        kafka_peek,
        read_only=True,
    ),
    ToolDef("mq_queues", "RabbitMQ queues with message and consumer counts.", schema({**RES}, required=["resource"]), mq_queues, read_only=True),
    ToolDef(
        "mq_peek",
        "Look at up to 50 messages of a RabbitMQ queue; they are requeued (nothing is lost).",
        schema({**RES, "queue": {"type": "string"}, "n": {"type": "integer"}, "vhost": {"type": "string"}}, required=["resource", "queue"]),
        mq_peek,
        read_only=True,
    ),
    ToolDef(
        "logs_search",
        "Log lines of a service in a time window (filter by text and level). Capped: start with logs_errors / logs_cluster.",
        schema(_when({**RES, "service": {"type": "string"}, "query": {"type": "string"}, "level": {"type": "string"}, "limit": {"type": "integer"}}), required=["resource", "service"]),
        logs_search,
        read_only=True,
    ),
    ToolDef(
        "logs_cluster",
        "Group a service's log lines into templates (ids, numbers, timestamps masked) with counts: 100k lines → ~50 rows.",
        schema(_when({**RES, "service": {"type": "string"}, "query": {"type": "string"}}), required=["resource", "service"]),
        logs_cluster,
        read_only=True,
    ),
    ToolDef(
        "logs_errors",
        "Errors and stack traces of a service grouped by exception and top frame.",
        schema(_when({**RES, "service": {"type": "string"}}), required=["resource", "service"]),
        logs_errors,
        read_only=True,
    ),
    ToolDef(
        "logs_trace",
        "Every log line of one request across services (needs the request/trace id you sent, e.g. X-Request-ID).",
        schema(_when({**RES, "trace_id": {"type": "string"}}), required=["resource", "trace_id"]),
        logs_trace,
        read_only=True,
    ),
    ToolDef(
        "logs_diff",
        "What is new or much more frequent in the window than in the window before it (e.g. after your change).",
        schema(_when({**RES, "service": {"type": "string"}}), required=["resource", "service"]),
        logs_diff,
        read_only=True,
    ),
    ToolDef(
        "app_logs",
        "Logs of the app you run in this workspace (mode search | cluster | errors).",
        schema(_when({"query": {"type": "string"}, "level": {"type": "string"}, "mode": {"type": "string", "enum": ["search", "cluster", "errors"]}, "limit": {"type": "integer"}}), required=[]),
        app_logs,
        read_only=True,
    ),
    ToolDef("topic_info", "Who produces and consumes a Kafka topic / queue (code you can read + knowledge base contracts).", schema({"topic": {"type": "string"}}), topic_info, read_only=True),
    ToolDef(
        "scenario_record",
        "Save the state before a flow step (masked DB rows from read-only queries + example messages) as a JSON fixture "
        "in the repo (test-fixtures/scenarios/<name>.json) so tests can start from it.",
        schema(
            {
                "name": {"type": "string"},
                "flow": {"type": "string"},
                "step": {"type": "string"},
                "queries": {"type": "array", "items": {"type": "object"}, "description": "[{resource, table, sql, limit}]"},
                "messages": {"type": "array", "items": {"type": "object"}},
                "folder": {"type": "string"},
            },
            required=["name"],
        ),
        scenario_record,
    ),
    ToolDef("scenario_load", "Read a saved flow scenario fixture.", schema({"name": {"type": "string"}}), scenario_load, read_only=True),
]

