"""Reading logs efficiently: the AI never gets raw log dumps. Every answer is shaped and capped:

- search:  lines for one service in a time window (JSON logs: filter by level / fields)
- cluster: lines grouped into templates (numbers, ids, uuids, hex, quoted values masked) → count, first/last seen
- errors:  error lines and stack traces grouped by exception and top frame
- trace:   every line carrying one request/trace id, across services, in time order
- diff:    templates that are new or much more frequent in a window than in the baseline before it

Log stores are adapters (`kind` in the resource config): "files" (glob of log files, also used for local app
logs), "elasticsearch" (also OpenSearch) and "loki". Traces work only when the services log a request/trace id;
without it, trace says so and suggests searching by time and content.
"""

from __future__ import annotations

import glob
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from actoroffice.domain.access import Resource
from actoroffice.domain.errors import OfficeError

if TYPE_CHECKING:
    from actoroffice.app import Office

MAX_LINES = 200
MAX_CHARS = 8000
TRACE_KEYS = ("traceId", "trace_id", "requestId", "request_id", "correlationId", "X-Request-ID", "rid")
LEVEL_RE = re.compile(r"\b(TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|CRITICAL)\b")
TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?)")
MASKS = [
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"), "<ip>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\"[^\"]{0,200}\"|'[^']{0,200}'"), "<str>"),
    (re.compile(r"\b\d+(?:\.\d+)?\b"), "<n>"),
]
EXCEPTION_RE = re.compile(r"\b([A-Za-z_][\w.$]*(?:Exception|Error|Throwable|Panic|Fault))\b")
FRAME_RE = re.compile(r"^\s+at\s+([\w.$<>]+)\(|File \"([^\"]+)\", line (\d+)")


class LogError(OfficeError):
    code = 422


@dataclass
class LogLine:
    ts: str
    service: str
    level: str
    message: str
    fields: dict[str, Any]

    def short(self) -> str:
        rid = next((f" {k}={self.fields[k]}" for k in TRACE_KEYS if self.fields.get(k)), "")
        return f"{self.ts[:23]} {self.service} {self.level:5}{rid} {self.message[:400]}"


def parse_line(raw: str, service: str) -> LogLine:
    raw = raw.rstrip("\n")
    if raw.startswith("{"):
        try:
            data = json.loads(raw)
            msg = str(data.get("message") or data.get("msg") or data.get("log") or raw)
            ts = str(data.get("@timestamp") or data.get("timestamp") or data.get("time") or data.get("ts") or "")
            level = str(data.get("level") or data.get("severity") or data.get("lvl") or "").upper()
            if data.get("stack_trace") or data.get("exception"):
                msg += "\n" + str(data.get("stack_trace") or data.get("exception"))
            return LogLine(ts, str(data.get("service") or data.get("app") or service), level, msg, data)
        except ValueError:
            pass
    ts_match, level_match = TS_RE.search(raw), LEVEL_RE.search(raw)
    ts = ts_match.group(1) if ts_match else ""
    level = (level_match.group(1) if level_match else "").replace("WARNING", "WARN")
    fields = {k: m.group(1) for k in TRACE_KEYS if (m := re.search(rf"\b{re.escape(k)}[=:]\s*\"?([\w\-.]+)", raw))}
    return LogLine(ts, service, level, raw, fields)


def template(message: str) -> str:
    text = message.splitlines()[0] if message else ""
    for pattern, repl in MASKS:
        text = pattern.sub(repl, text)
    return re.sub(r"\s+", " ", text).strip()[:300]


def cluster(lines: list[LogLine], top: int = 40) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for line in lines:
        key = f"{line.level}|{template(line.message)}"
        g = groups.setdefault(key, {"level": line.level, "template": key.split("|", 1)[1], "count": 0, "first": line.ts, "last": line.ts, "sample": line.message[:400]})
        g["count"] += 1
        g["first"] = min(g["first"], line.ts) if line.ts else g["first"]
        g["last"] = max(g["last"], line.ts) if line.ts else g["last"]
    return sorted(groups.values(), key=lambda g: -g["count"])[:top]


def error_groups(lines: list[LogLine], top: int = 20) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for line in lines:
        if line.level not in ("ERROR", "FATAL", "CRITICAL") and not EXCEPTION_RE.search(line.message):
            continue
        exc = EXCEPTION_RE.search(line.message)
        frame = next(((m.group(1) or f"{m.group(2)}:{m.group(3)}") for m in (FRAME_RE.search(ln) for ln in line.message.splitlines()[1:]) if m), "")
        key = f"{exc.group(1) if exc else template(line.message)} @ {frame}"
        g = groups.setdefault(key, {"error": key, "count": 0, "first": line.ts, "last": line.ts, "sample": line.message[:1500]})
        g["count"] += 1
        g["last"] = max(g["last"], line.ts)
    return sorted(groups.values(), key=lambda g: -g["count"])[:top]


def shape(text: str) -> str:
    return text if len(text) <= MAX_CHARS else text[:MAX_CHARS] + f"\n… ({len(text) - MAX_CHARS} more characters: narrow the query or the time window)"


class LogService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self.transport: httpx.AsyncBaseTransport | None = None

    async def fetch(self, resource: Resource, *, service: str, query: str, since: datetime, until: datetime, level: str | None = None, limit: int = 2000) -> list[LogLine]:
        store = str(resource.config.get("store") or "files")
        if store == "files":
            return self._files(resource, service, query, since, until, level, limit)
        if store in ("elasticsearch", "opensearch"):
            return await self._elastic(resource, service, query, since, until, level, limit)
        if store == "loki":
            return await self._loki(resource, service, query, since, until, level, limit)
        raise LogError(f"Chưa có adapter log '{store}' (có: files, elasticsearch, loki)")

    def _files(self, resource: Resource, service: str, query: str, since: datetime, until: datetime, level: str | None, limit: int) -> list[LogLine]:
        patterns = resource.config.get("paths") or {}
        globs = (
            [g for value in patterns.values() for g in (value if isinstance(value, list) else [value])]
            if service in ("*", "")  # a trace crosses services: read every configured service's files
            else patterns.get(service) or patterns.get("*") or []
        )
        out: list[LogLine] = []
        for pattern in globs if isinstance(globs, list) else [globs]:
            for path in sorted(glob.glob(str(pattern), recursive=True)):
                out += _read_file(Path(path), service, query, since, until, level, limit - len(out))
                if len(out) >= limit:
                    return out
        return out

    async def _elastic(self, resource: Resource, service: str, query: str, since: datetime, until: datetime, level: str | None, limit: int) -> list[LogLine]:
        base = str(resource.config.get("url") or "").rstrip("/")
        index = resource.config.get("index") or "logs-*"
        service_field = resource.config.get("service_field") or "service"
        must: list[dict[str, Any]] = [{"range": {"@timestamp": {"gte": since.isoformat(), "lte": until.isoformat()}}}]
        if service and service != "*":
            must.append({"term": {service_field: service}})
        if query:
            must.append({"query_string": {"query": query, "default_operator": "AND"}})
        if level:
            must.append({"term": {resource.config.get("level_field") or "level": level.lower()}})
        secret = self.office.infra.secret(resource) or {}
        headers = {k: str(v) for k, v in (secret.get("headers") or {}).items()} if isinstance(secret, dict) else {}
        async with httpx.AsyncClient(timeout=30, transport=self.transport, headers=headers) as http:
            response = await http.post(f"{base}/{index}/_search", json={"size": min(limit, 2000), "sort": [{"@timestamp": "asc"}], "query": {"bool": {"must": must}}})
        if response.status_code >= 400:
            raise LogError(f"Elasticsearch {response.status_code}: {response.text[:200]}")
        hits = response.json().get("hits", {}).get("hits", [])
        return [parse_line(json.dumps(h.get("_source") or {}, ensure_ascii=False), service) for h in hits]

    async def _loki(self, resource: Resource, service: str, query: str, since: datetime, until: datetime, level: str | None, limit: int) -> list[LogLine]:
        base = str(resource.config.get("url") or "").rstrip("/")
        label = resource.config.get("service_label") or "app"
        logql = f'{{{label}="{service}"}}'
        if query:
            logql += f' |= "{query.replace(chr(34), "")}"'
        if level:
            logql += f' |~ "(?i){level}"'
        secret = self.office.infra.secret(resource) or {}
        headers = {k: str(v) for k, v in (secret.get("headers") or {}).items()} if isinstance(secret, dict) else {}
        params = {"query": logql, "start": str(int(since.timestamp() * 1e9)), "end": str(int(until.timestamp() * 1e9)), "limit": str(min(limit, 5000)), "direction": "forward"}
        async with httpx.AsyncClient(timeout=30, transport=self.transport, headers=headers) as http:
            response = await http.get(f"{base}/loki/api/v1/query_range", params=params)
        if response.status_code >= 400:
            raise LogError(f"Loki {response.status_code}: {response.text[:200]}")
        out = []
        for stream in response.json().get("data", {}).get("result", []):
            svc = (stream.get("stream") or {}).get(label, service)
            for ts_ns, line in stream.get("values", []):
                parsed = parse_line(line, svc)
                parsed.ts = parsed.ts or datetime.fromtimestamp(int(ts_ns) / 1e9, UTC).isoformat()
                out.append(parsed)
        return sorted(out, key=lambda x: x.ts)

    # ---- shaped answers ----------------------------------------------------------------------------------------

    async def search(self, resource: Resource, service: str, query: str, since: datetime, until: datetime, level: str | None, limit: int = MAX_LINES) -> str:
        lines = await self.fetch(resource, service=service, query=query, since=since, until=until, level=level, limit=limit + 1)
        more = len(lines) > limit
        body = "\n".join(line.short() for line in lines[:limit]) or "(no lines)"
        return shape(body + (f"\n… more than {limit} lines: use logs_cluster or narrow the query" if more else ""))

    async def clusters(self, resource: Resource, service: str, query: str, since: datetime, until: datetime) -> str:
        lines = await self.fetch(resource, service=service, query=query, since=since, until=until)
        groups = cluster(lines)
        rows = [f"{g['count']:>6} × [{g['level'] or '-'}] {g['template']}  ({g['first'][:19]} → {g['last'][:19]})" for g in groups]
        return shape(f"{len(lines)} lines → {len(groups)} templates (top):\n" + "\n".join(rows))

    async def errors(self, resource: Resource, service: str, since: datetime, until: datetime) -> str:
        lines = await self.fetch(resource, service=service, query="", since=since, until=until)
        groups = error_groups(lines)
        if not groups:
            return "No errors in this window."
        return shape("\n\n".join(f"{g['count']} × {g['error']} (last {g['last'][:19]})\n{g['sample']}" for g in groups))

    async def trace(self, resource: Resource, trace_id: str, since: datetime, until: datetime) -> str:
        lines = await self.fetch(resource, service="*", query=trace_id, since=since, until=until)
        lines = [x for x in lines if trace_id in x.message or trace_id in json.dumps(x.fields, default=str)]
        if not lines:
            return (
                f"No line carries {trace_id}. If these services do not log a request/trace id, search by time and content "
                "(logs_search), and propose a team rule to log one (traceId in MDC / structured logs)."
            )
        return shape("\n".join(x.short() for x in sorted(lines, key=lambda x: x.ts)))

    async def diff(self, resource: Resource, service: str, since: datetime, until: datetime) -> str:
        window = until - since
        current = cluster(await self.fetch(resource, service=service, query="", since=since, until=until), top=400)
        baseline = {f"{g['level']}|{g['template']}": g["count"] for g in cluster(await self.fetch(resource, service=service, query="", since=since - window, until=since), top=400)}
        rows = []
        for g in current:
            before = baseline.get(f"{g['level']}|{g['template']}", 0)
            if before == 0 or g["count"] >= 3 * before + 5:
                rows.append(f"{'NEW' if before == 0 else 'UP'} {before}→{g['count']} [{g['level'] or '-'}] {g['template']}")
        return shape("\n".join(rows[:60]) or "Nothing new or unusually frequent compared with the window before.")


def _read_file(path: Path, service: str, query: str, since: datetime, until: datetime, level: str | None, limit: int) -> list[LogLine]:
    out: list[LogLine] = []
    if limit <= 0:
        return out
    current: LogLine | None = None
    with path.open(encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            if current is not None and (raw.startswith((" ", "\t")) or raw.startswith("Caused by")):
                current.message += "\n" + raw.rstrip("\n")  # stack trace continuation
                continue
            line = parse_line(raw, service)
            current = None
            when = parse_ts(line.ts)
            if when is not None and (when < since or when > until):
                continue
            if level and line.level != level.upper():
                continue
            if query and query.lower() not in raw.lower():
                continue
            out.append(line)
            current = line
            if len(out) >= limit:
                break
    return out


def parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace(",", ".").replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def window(since: str | None, until: str | None, default_minutes: int = 60) -> tuple[datetime, datetime]:
    """'30m', '2h', '1d' (relative) or ISO timestamps; until defaults to now."""
    now = datetime.now(UTC)

    def parse(value: str | None, fallback: datetime) -> datetime:
        if not value:
            return fallback
        m = re.fullmatch(r"(\d+)\s*([mhd])", value.strip())
        if m:
            delta = {"m": timedelta(minutes=1), "h": timedelta(hours=1), "d": timedelta(days=1)}[m.group(2)] * int(m.group(1))
            return now - delta
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return when if when.tzinfo else when.replace(tzinfo=UTC)

    end = parse(until, now)
    start = parse(since, end - timedelta(minutes=default_minutes))
    if (end - start) > timedelta(days=7):
        raise LogError("Khoảng thời gian tối đa 7 ngày")
    return start, end
