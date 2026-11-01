"""Read access to shared data infrastructure for the AI's tools: databases of any kind (adapter per DSN scheme),
Redis, Kafka, RabbitMQ. Every call needs an active grant for the resource (services/access.py) unless the resource
is local; credentials come from <data>/secrets/resources.json and never reach the AI.

Drivers are optional: sqlite3 is built in; postgresql (psycopg or asyncpg), mysql (pymysql), redis (redis),
kafka (aiokafka) are used when installed, otherwise the tool says which package to add.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import re
import sqlite3
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from actoroffice.domain.access import LEVEL_ORDER, AccessLevel, Resource
from actoroffice.domain.errors import OfficeError, PolicyDenied

from .access import load_secret

if TYPE_CHECKING:
    from actoroffice.app import Office

MAX_ROWS = 200
MAX_CELL = 500
READ_SQL = re.compile(r"^\s*(select|with|explain|show|describe|desc|pragma\s+table_info)\b", re.IGNORECASE)
WRITE_SQL = re.compile(r"\b(insert|update|delete|merge|upsert|drop|alter|create|truncate|grant|revoke|replace|call|exec|execute|copy|attach|vacuum)\b", re.IGNORECASE)


class InfraError(OfficeError):
    code = 422


def _missing(package: str) -> InfraError:
    return InfraError(f"Thiếu driver: thêm gói '{package}' vào môi trường của office (uv add {package}).")


def check_read_only_sql(sql: str) -> str:
    text = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.DOTALL).strip().rstrip(";").strip()
    if ";" in text:
        raise PolicyDenied("Chỉ một câu lệnh")
    if not READ_SQL.match(text):
        raise PolicyDenied("Chỉ được đọc (SELECT / WITH / EXPLAIN / SHOW)")
    stripped = re.sub(r"'(?:''|[^'])*'", "''", text)  # ignore keywords inside string literals
    if WRITE_SQL.search(stripped) and not stripped.lower().startswith("explain"):
        raise PolicyDenied("Câu lệnh có thao tác ghi")
    return text


def limited(sql: str, limit: int) -> str:
    if re.match(r"^\s*(select|with)\b", sql, re.IGNORECASE):
        return f"SELECT * FROM ({sql}) ao_q LIMIT {int(limit)}"
    return sql


def mask(columns: list[str], rows: list[list[Any]], masked: list[str]) -> list[list[Any]]:
    hide = {i for i, c in enumerate(columns) if c.lower() in {m.lower() for m in masked}}
    out = []
    for row in rows:
        cells = []
        for i, value in enumerate(row):
            if i in hide and value is not None:
                cells.append("***")
            else:
                text = value if isinstance(value, (int, float)) or value is None else str(value)
                cells.append(text[:MAX_CELL] + "…" if isinstance(text, str) and len(text) > MAX_CELL else text)
        out.append(cells)
    return out


def format_table(columns: list[str], rows: list[list[Any]], more: bool) -> str:
    lines = [" | ".join(columns)] + [" | ".join("NULL" if v is None else str(v) for v in r) for r in rows]
    if more:
        lines.append(f"… (only the first {len(rows)} rows: add WHERE / aggregate)")
    return "\n".join(lines) if columns else "(no rows)"


class InfraService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self.transport: httpx.AsyncBaseTransport | None = None  # tests: fake RabbitMQ / log stores

    async def resource_for(self, ticket_id: int | None, name: str, kinds: tuple[str, ...], need: AccessLevel = AccessLevel.READ) -> Resource:
        office = self.office
        ticket = await office.tickets.get(ticket_id) if ticket_id else None
        resource = await office.access.resource(name, ticket.team_id if ticket else None)
        if resource.kind.value not in kinds:
            raise InfraError(f"{resource.name} là {resource.kind.value}, không phải {'/'.join(kinds)}")
        if resource.env == "local":
            return resource
        if ticket is None:
            raise PolicyDenied("Cần một ticket có quyền truy cập")
        for grant, granted in await office.access.active(ticket.id):
            if granted.id == resource.id and LEVEL_ORDER[grant.level] >= LEVEL_ORDER[need]:
                return resource
        raise PolicyDenied(f"Chưa có quyền {need.value} vào {resource.name}: xin bằng env_access")

    def secret(self, resource: Resource) -> Any:
        return load_secret(self.office.settings.data_dir, resource.config.get("secret")) or {}

    # ---- databases ------------------------------------------------------------------------------------------

    def dsn(self, resource: Resource, level: str = "read") -> str:
        secret = self.secret(resource)
        dsn = secret.get("dsn") if isinstance(secret, dict) else None
        if isinstance(dsn, dict):
            dsn = dsn.get(level) or dsn.get("read")
        dsn = dsn or resource.config.get("dsn")  # local resources may keep a credential-free DSN in config
        if not dsn:
            raise InfraError(f"{resource.name}: chưa có connection string (secret.dsn)")
        self.office.redact.register(str(dsn))
        return str(dsn)

    async def query(self, resource: Resource, sql: str, *, limit: int = MAX_ROWS) -> tuple[list[str], list[list[Any]], bool]:
        sql = check_read_only_sql(sql)
        dsn = self.dsn(resource)
        scheme = urlsplit(dsn).scheme.split("+")[0].lower()
        limit = max(1, min(int(limit), MAX_ROWS))
        wrapped = limited(sql, limit + 1)
        timeout = float(resource.config.get("statement_timeout_s") or 15)
        if scheme == "sqlite":
            columns, rows = await asyncio.to_thread(_sqlite_query, dsn, wrapped, timeout)
        elif scheme in ("postgresql", "postgres"):
            columns, rows = await _pg_query(dsn, wrapped, timeout)
        elif scheme in ("mysql", "mariadb"):
            columns, rows = await asyncio.to_thread(_mysql_query, dsn, wrapped, timeout)
        else:
            raise InfraError(f"Chưa có adapter cho '{scheme}' (có: sqlite, postgresql, mysql). Thêm adapter trong services/infra.py.")
        more = len(rows) > limit
        rows = mask(columns, rows[:limit], list(resource.config.get("mask_columns") or []))
        return columns, rows, more

    async def schema(self, resource: Resource, table: str | None = None) -> str:
        dsn = self.dsn(resource)
        scheme = urlsplit(dsn).scheme.split("+")[0].lower()
        if scheme == "sqlite":
            if table:
                cols, rows, _ = await self.query(resource, f"SELECT name, type, \"notnull\", pk FROM pragma_table_info('{_ident(table)}')")
            else:
                cols, rows, _ = await self.query(resource, "SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name")
            return format_table(cols, rows, False)
        if table:
            schema_name, _, name = table.rpartition(".")
            where = f"table_name = '{_ident(name)}'" + (f" AND table_schema = '{_ident(schema_name)}'" if schema_name else "")
            cols, rows, _ = await self.query(
                resource, f"SELECT column_name, data_type, is_nullable FROM information_schema.columns WHERE {where} ORDER BY ordinal_position"
            )
        else:
            cols, rows, _ = await self.query(
                resource,
                "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog', 'information_schema', 'mysql', 'sys', 'performance_schema') ORDER BY 1, 2",
            )
        return format_table(cols, rows, False)

    # ---- redis ------------------------------------------------------------------------------------------------

    async def redis(self, resource: Resource) -> Any:
        try:
            redis_asyncio = importlib.import_module("redis.asyncio")
        except ImportError as exc:
            raise _missing("redis") from exc
        url = self.secret(resource).get("url") if isinstance(self.secret(resource), dict) else None
        url = url or resource.config.get("url")
        if not url:
            raise InfraError(f"{resource.name}: chưa có url")
        self.office.redact.register(str(url))
        return redis_asyncio.from_url(url, decode_responses=True, socket_timeout=10)

    async def redis_scan(self, resource: Resource, pattern: str, limit: int = 100) -> list[str]:
        client = await self.redis(resource)
        try:
            keys: list[str] = []
            async for key in client.scan_iter(match=pattern or "*", count=200):
                keys.append(key)
                if len(keys) >= min(limit, 1000):
                    break
            return keys
        finally:
            await client.aclose()

    async def redis_get(self, resource: Resource, key: str) -> str:
        client = await self.redis(resource)
        try:
            kind = await client.type(key)
            ttl = await client.ttl(key)
            if kind == "string":
                value: Any = await client.get(key)
            elif kind == "hash":
                value = await client.hgetall(key)
            elif kind == "list":
                value = await client.lrange(key, 0, 49)
            elif kind == "set":
                value = sorted(await client.srandmember(key, 50))
            elif kind == "zset":
                value = await client.zrange(key, 0, 49, withscores=True)
            else:
                value = None
            text = json.dumps(value, ensure_ascii=False, default=str)
            return f"{key} [{kind}, ttl {ttl}s]: {text[:4000]}"
        finally:
            await client.aclose()

    # ---- kafka ----------------------------------------------------------------------------------------------

    async def kafka_peek(self, resource: Resource, topic: str, n: int = 20, from_: str = "latest") -> list[dict[str, Any]]:
        try:
            aiokafka = importlib.import_module("aiokafka")
        except ImportError as exc:
            raise _missing("aiokafka") from exc
        secret = self.secret(resource) if isinstance(self.secret(resource), dict) else {}
        config = {**(resource.config.get("client") or {}), **(secret.get("client") or {})}
        consumer = aiokafka.AIOKafkaConsumer(
            bootstrap_servers=resource.config.get("bootstrap") or secret.get("bootstrap"), group_id=None, enable_auto_commit=False, **config
        )
        await consumer.start()
        try:
            partitions = [aiokafka.TopicPartition(topic, p) for p in (consumer.partitions_for_topic(topic) or [])]
            if not partitions:
                await consumer.topics()
                partitions = [aiokafka.TopicPartition(topic, p) for p in (consumer.partitions_for_topic(topic) or [])]
            consumer.assign(partitions)
            ends = await consumer.end_offsets(partitions)
            for tp in partitions:
                start = max(0, ends[tp] - n) if from_ == "latest" else 0
                consumer.seek(tp, start)
            out: list[dict[str, Any]] = []
            deadline = time.monotonic() + 10
            while len(out) < n and time.monotonic() < deadline:
                batch = await consumer.getmany(timeout_ms=1000, max_records=n)
                for tp, records in batch.items():
                    for r in records:
                        out.append({"partition": tp.partition, "offset": r.offset, "key": _decode(r.key), "value": _decode(r.value)[:3000], "ts": r.timestamp})
                if all((await consumer.position(tp)) >= ends[tp] for tp in partitions):
                    break
            return out[-n:]
        finally:
            await consumer.stop()

    # ---- rabbitmq -------------------------------------------------------------------------------------------

    async def _mq(self, resource: Resource) -> httpx.AsyncClient:
        secret = self.secret(resource) if isinstance(self.secret(resource), dict) else {}
        base = str(resource.config.get("management_url") or "").rstrip("/")
        if not base:
            raise InfraError(f"{resource.name}: chưa có config.management_url")
        auth = (secret.get("user"), secret.get("password")) if secret.get("user") else None
        return httpx.AsyncClient(base_url=base, auth=auth, timeout=20, transport=self.transport)

    async def mq_queues(self, resource: Resource) -> list[dict[str, Any]]:
        async with await self._mq(resource) as http:
            response = await http.get("/api/queues")
            response.raise_for_status()
            return [{"vhost": q.get("vhost"), "name": q.get("name"), "messages": q.get("messages"), "consumers": q.get("consumers")} for q in response.json()]

    async def mq_peek(self, resource: Resource, queue: str, n: int = 10, vhost: str = "/") -> list[dict[str, Any]]:
        async with await self._mq(resource) as http:
            response = await http.post(
                f"/api/queues/{quote(vhost, safe='')}/{quote(queue, safe='')}/get",
                json={"count": max(1, min(n, 50)), "ackmode": "ack_requeue_true", "encoding": "auto", "truncate": 4000},
            )
            response.raise_for_status()
            return [{"routing_key": m.get("routing_key"), "payload": str(m.get("payload"))[:3000], "redelivered": m.get("redelivered")} for m in response.json()]


def _ident(text: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][\w$]*", text):
        raise PolicyDenied(f"Tên không hợp lệ: {text}")
    return text


def _decode(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _sqlite_query(dsn: str, sql: str, timeout: float) -> tuple[list[str], list[list[Any]]]:
    # sqlite:///relative.db · sqlite:////abs/path.db · sqlite:///C:/path.db
    path = unquote(dsn.split(":///", 1)[1]) if ":///" in dsn else unquote(dsn.split("://", 1)[1])
    if re.match(r"^/[A-Za-z]:", path):
        path = path[1:]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=timeout)
    try:
        conn.set_authorizer(lambda action, *_: sqlite3.SQLITE_OK if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_PRAGMA) else sqlite3.SQLITE_DENY)
        deadline = time.monotonic() + timeout
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10_000)
        cur = conn.execute(sql)
        columns = [d[0] for d in cur.description or []]
        return columns, [list(r) for r in cur.fetchmany(MAX_ROWS + 1)]
    finally:
        conn.close()


async def _pg_query(dsn: str, sql: str, timeout: float) -> tuple[list[str], list[list[Any]]]:
    dsn = re.sub(r"^postgresql\+\w+://", "postgresql://", dsn)
    try:
        psycopg = importlib.import_module("psycopg")
    except ImportError:
        psycopg = None
    if psycopg is not None:
        async with await psycopg.AsyncConnection.connect(dsn, connect_timeout=10) as conn:
            await conn.execute("SET TRANSACTION READ ONLY")
            await conn.execute(f"SET LOCAL statement_timeout = {int(timeout * 1000)}")
            cur = await conn.execute(sql)
            columns = [d.name for d in cur.description or []]
            return columns, [list(r) for r in await cur.fetchmany(MAX_ROWS + 1)]
    try:
        asyncpg = importlib.import_module("asyncpg")
    except ImportError as exc:
        raise _missing("psycopg[binary]") from exc
    conn = await asyncpg.connect(dsn, timeout=10)
    try:
        async with conn.transaction(readonly=True):
            await conn.execute(f"SET LOCAL statement_timeout = {int(timeout * 1000)}")
            records = await conn.fetch(sql)
        columns = list(records[0].keys()) if records else []
        return columns, [list(r.values()) for r in records[: MAX_ROWS + 1]]
    finally:
        await conn.close()


def _mysql_query(dsn: str, sql: str, timeout: float) -> tuple[list[str], list[list[Any]]]:
    try:
        pymysql = importlib.import_module("pymysql")
    except ImportError as exc:
        raise _missing("pymysql") from exc
    u = urlsplit(dsn)
    conn = pymysql.connect(
        host=u.hostname, port=u.port or 3306, user=unquote(u.username or ""), password=unquote(u.password or ""),
        database=u.path.lstrip("/"), read_timeout=int(timeout), connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute(f"SET SESSION MAX_EXECUTION_TIME = {int(timeout * 1000)}")
            cur.execute(sql)
            columns = [d[0] for d in cur.description or []]
            return columns, [list(r) for r in cur.fetchmany(MAX_ROWS + 1)]
    finally:
        conn.close()
