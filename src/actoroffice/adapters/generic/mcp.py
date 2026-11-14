"""`use: mcp`: a tool of any MCP server (a local command over stdio, or an HTTP URL) as a document source.

The server entry has the shape of a team's `mcp_servers` entry. The config names the tool that fetches a document and
how its arguments are filled from `{ref}`; the text blocks of the tool's result are the document, or its JSON fields
when paths are given. The server is started on first use, kept for later calls and stopped by `close()`.

Uses the `mcp` client library (it comes with claude-agent-sdk); it is imported on first use, so an office without it
still loads every other adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from actoroffice.adapters._http import kind_for_status, now_iso
from actoroffice.adapters.generic.common import (
    SECRET_FIELDS,
    DocMapping,
    HitMapping,
    Recognizer,
    SourceConfig,
    Strict,
    allowed,
    as_text,
    check_header_name,
    check_plain_headers,
    check_secret_env,
    check_url,
    document,
    fill,
    hits,
    missing_secrets,
    parse_json,
    require_placeholder,
    short_line,
)
from actoroffice.ports.connection import Capability, ConnectorError, Health, Scope
from actoroffice.ports.docs import DocContent, DocHit, DocRef

NAME = "mcp"
LABEL = "MCP"
MAX_HITS = 50
CONNECTION_CLOSED = -32000  # JSON-RPC error code the client library raises when the server is gone
CLOSE_WAIT_S = 15.0  # the library closes stdin, waits, then kills the server's process tree: all bounded

Streams = tuple[Any, Any]
Opener = Callable[[], contextlib.AbstractAsyncContextManager[Streams]]


class McpServerConfig(Strict):
    """One MCP server, as in a team's `mcp_servers`: `{type: stdio, command, args, env}` or `{type: http, url, headers}`."""

    type: Literal["stdio", "http", "sse"] = Field(default="stdio", description="stdio: chạy một lệnh trên máy office; http / sse: gọi một địa chỉ")
    command: str = Field(default="", validate_default=True, description="Lệnh chạy server (type: stdio), ví dụ npx")
    args: list[str] = Field(default_factory=list, description='Đối số của lệnh, dạng danh sách: ["-y", "@acme/docs-mcp"]')
    env: dict[str, str] = Field(default_factory=dict, description="Biến môi trường không bí mật cho server")
    cwd: str = Field(default="", description="Thư mục chạy lệnh (đường dẫn tuyệt đối)")
    url: str = Field(default="", validate_default=True, description="Địa chỉ của server (type: http / sse)")
    headers: dict[str, str] = Field(default_factory=dict, description="Header không bí mật gửi kèm (type: http / sse)")

    @field_validator("command")
    @classmethod
    def _command(cls, value: str, info: ValidationInfo) -> str:
        value = value.strip()
        if info.data.get("type") == "stdio" and not value:
            raise ValueError("cần lệnh chạy MCP server khi type: stdio, ví dụ npx")
        return value

    @field_validator("args", mode="before")
    @classmethod
    def _args(cls, value: Any) -> Any:
        if isinstance(value, str):
            raise ValueError('phải là danh sách đối số, ví dụ ["-y", "@acme/docs-mcp"], không phải một chuỗi')
        return [str(arg) for arg in value] if isinstance(value, list | tuple) else value

    @field_validator("cwd")
    @classmethod
    def _cwd(cls, value: str) -> str:
        value = value.strip()
        if value and not Path(value).is_absolute():
            raise ValueError("phải là đường dẫn tuyệt đối")
        return value

    @field_validator("url")
    @classmethod
    def _url(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("type") in ("http", "sse"):
            return check_url(value, sends_secret=False, what="url")
        return value.strip()

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str]) -> dict[str, str]:
        return check_plain_headers(value)


class McpFetch(DocMapping):
    tool: str = Field(min_length=1, description="Tên tool đọc một tài liệu, ví dụ fetch_page")
    args: dict[str, Any] = Field(default_factory=dict, description='Tham số của tool, ví dụ {url: "{ref}"}')

    @model_validator(mode="after")
    def _has_ref(self) -> McpFetch:
        require_placeholder(self.args, "ref", "args")
        return self


class McpSearch(HitMapping):
    tool: str = Field(min_length=1, description="Tên tool tìm tài liệu, ví dụ search")
    args: dict[str, Any] = Field(default_factory=dict, description='Tham số của tool, ví dụ {query: "{query}", limit: "{limit}"}')

    @model_validator(mode="after")
    def _has_query(self) -> McpSearch:
        require_placeholder(self.args, "query", "args")
        return self


class GenericMcpConfig(SourceConfig):
    server: McpServerConfig = Field(description="MCP server: {type: stdio, command, args, env} hoặc {type: http, url, headers}")
    auth: Literal["none", "bearer", "header"] = Field(default="none", description="Với server http / sse: bearer gửi token trong Authorization; header gửi token trong header tên ở auth_header")
    auth_header: str = Field(default="", validate_default=True, description="Tên header mang token khi auth: header")
    secret_env: dict[str, str] = Field(
        default_factory=dict, description="Với server stdio: biến môi trường lấy từ bí mật của kết nối: {NOTION_TOKEN: token} hoặc [NOTION_TOKEN] (ô bí mật trùng tên biến)"
    )
    fetch: McpFetch = Field(description="Tool đọc một tài liệu theo tham chiếu")
    search: McpSearch | None = Field(default=None, description="Tool tìm tài liệu theo từ khoá (không khai thì kết nối không tìm kiếm)")
    timeout_s: float = Field(default=60.0, gt=0, le=600, description="Thời gian chờ server khởi động và mỗi lần gọi tool (giây)")

    @field_validator("auth_header")
    @classmethod
    def _auth_header(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("auth") != "header":
            return value.strip()
        if not value.strip():
            raise ValueError("cần tên header khi auth: header, ví dụ X-Api-Key")
        return check_header_name(value)

    @field_validator("secret_env", mode="before")
    @classmethod
    def _secret_env(cls, value: Any) -> Any:
        return check_secret_env(value)

    @model_validator(mode="after")
    def _secret_matches_transport(self) -> GenericMcpConfig:
        if self.server.type == "stdio":
            if self.auth != "none":
                raise ValueError("auth chỉ dùng với server type: http / sse. Server stdio nhận bí mật qua secret_env")
        else:
            if self.secret_env:
                raise ValueError("secret_env chỉ dùng với server type: stdio. Server http / sse nhận bí mật qua auth")
            check_url(self.server.url, sends_secret=self.auth != "none", what="server.url")
        return self


# ---- transports (the library's, opened in the task that will also close them) ---------------------------------


@contextlib.asynccontextmanager
async def _stdio(server: McpServerConfig, env: dict[str, str]) -> AsyncIterator[Streams]:
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    # The library starts the command with a small default environment plus `env`, never the office's whole environment.
    params = StdioServerParameters(command=server.command, args=list(server.args), env=env or None, cwd=server.cwd or None)
    with open(os.devnull, "w", encoding="utf-8") as quiet:  # what the server writes to stderr is never shown or kept
        async with stdio_client(params, errlog=quiet) as streams:
            yield streams[0], streams[1]


@contextlib.asynccontextmanager
async def _http(server: McpServerConfig, headers: dict[str, str]) -> AsyncIterator[Streams]:
    if server.type == "sse":
        from mcp.client.sse import sse_client

        async with sse_client(server.url, headers=headers) as streams:
            yield streams[0], streams[1]
        return
    try:
        from mcp.client.streamable_http import streamable_http_client  # mcp 2.x
    except ImportError:
        from mcp.client.streamable_http import streamablehttp_client  # mcp 1.x: takes the headers itself

        async with streamablehttp_client(server.url, headers=headers) as streams:
            yield streams[0], streams[1]
        return
    from mcp.shared._httpx_utils import create_mcp_http_client

    async with create_mcp_http_client(headers=headers) as client, streamable_http_client(server.url, http_client=client) as streams:
        yield streams[0], streams[1]


def _leaves(exc: BaseException) -> Iterator[BaseException]:
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            yield from _leaves(inner)
    else:
        yield exc


def _start_error(exc: BaseException, what: str) -> ConnectorError:
    """Why the server could not be reached, without its own words (they may hold anything)."""
    leaves = list(_leaves(exc))
    if any(isinstance(e, ImportError) for e in leaves):
        return ConnectorError("invalid", f"{LABEL}: office này không có thư viện `mcp` (đi kèm claude-agent-sdk) nên không dùng được adapter mcp")
    if any(isinstance(e, FileNotFoundError | PermissionError | NotADirectoryError) for e in leaves):
        return ConnectorError("invalid", f"{LABEL}: không chạy được {what}: không tìm thấy lệnh hoặc thư mục trên máy chạy office")
    status = next((s for s in (getattr(getattr(e, "response", None), "status_code", None) for e in leaves) if isinstance(s, int)), None)
    if status is not None and status >= 400:
        return ConnectorError(kind_for_status(status), f"{LABEL}: {what} trả lời {status} khi kết nối")
    return ConnectorError("network", f"{LABEL}: không kết nối được {what} ({', '.join(sorted({type(e).__name__ for e in leaves}))})")


class _Link:
    """One live session with the server: started on first use, shared by every call, ended by `close`.

    The library's transports must be entered and left by the same task, so one background task owns them and waits
    there until it is told to stop."""

    def __init__(self, opener: Opener, timeout_s: float, what: str) -> None:
        self._opener = opener
        self._timeout_s = timeout_s
        self._what = what
        self._lock = asyncio.Lock()
        self._task: asyncio.Future[None] | None = None
        self._stop: asyncio.Event | None = None
        self.session: Any = None
        self.info: Any = None  # the server's answer to `initialize`

    async def _own(self, ready: asyncio.Future[None], stop: asyncio.Event) -> None:
        try:
            from mcp import ClientSession

            async with self._opener() as streams, ClientSession(streams[0], streams[1]) as session:
                self.info = await session.initialize()
                self.session = session
                if not ready.done():
                    ready.set_result(None)
                await stop.wait()
        except (Exception, BaseExceptionGroup) as exc:  # after `ready`: the transport broke; the next call starts again
            if not ready.done():
                ready.set_exception(_start_error(exc, self._what))
        finally:
            self.session = None

    async def get(self) -> Any:
        async with self._lock:
            if self.session is not None and self._task is not None and not self._task.done():
                return self.session
            await self._end()
            ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
            self._stop = asyncio.Event()
            self._task = asyncio.ensure_future(self._own(ready, self._stop))
            try:
                async with asyncio.timeout(self._timeout_s):
                    await ready
            except TimeoutError:
                await self._end()
                raise ConnectorError("network", f"{LABEL}: {self._what} không sẵn sàng sau {self._timeout_s:g} giây") from None
            except ConnectorError:
                await self._end()
                raise
            return self.session

    async def _end(self) -> None:
        task, stop, started = self._task, self._stop, self.session is not None
        self._task = self._stop = self.session = None
        if task is None:
            return
        if stop is not None:
            stop.set()
        if not started:
            task.cancel()  # still starting: it is not waiting for `stop` yet
        with contextlib.suppress(TimeoutError, Exception):
            await asyncio.wait_for(task, CLOSE_WAIT_S)

    async def close(self) -> None:
        async with self._lock:
            await self._end()


class GenericMcp:
    name: ClassVar[str] = NAME
    title: ClassVar[str] = "MCP server (nguồn tài liệu)"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS})
    config_schema: ClassVar[type[BaseModel]] = GenericMcpConfig
    secret_fields: ClassVar[tuple[str, ...]] = SECRET_FIELDS
    optional_secret_fields: ClassVar[tuple[str, ...]] = SECRET_FIELDS  # only what `auth` / `secret_env` name is needed

    def __init__(self, config: GenericMcpConfig, secret: dict[str, str], *, transport: Opener | None = None) -> None:
        """`transport` (tests): a factory of `(read, write)` stream context managers used instead of the configured server."""
        self.config = config
        self.recognizer = Recognizer(config.recognize)
        self._secrets = [value for value in secret.values() if value]
        server = config.server
        needed = [*dict.fromkeys(config.secret_env.values()), *(("token",) if config.auth != "none" else ())]
        missing = missing_secrets(needed, secret)
        self.blocked = ConnectorError("unconfigured", f"{LABEL}: chưa có bí mật ({', '.join(missing)}) cho MCP server. Vào Cài đặt › Kết nối để nhập.") if missing else None
        what = f"MCP server «{Path(server.command).name}»" if server.type == "stdio" else "MCP server"
        if transport is not None:
            opener = transport
        elif server.type == "stdio":
            env = {**server.env, **{name: secret.get(field, "") for name, field in config.secret_env.items()}}
            opener = functools.partial(_stdio, server, env)
        else:
            headers = dict(server.headers)
            if config.auth == "bearer":
                headers["Authorization"] = f"Bearer {secret.get('token', '')}"
            elif config.auth == "header":
                headers[config.auth_header] = secret.get("token", "")
            opener = functools.partial(_http, server, headers)
        self._link = _Link(opener, config.timeout_s, what)

    async def close(self) -> None:
        await self._link.close()

    # ---- talking to the server

    async def _request(self, what: str, call: Callable[[Any], Awaitable[Any]]) -> Any:
        if self.blocked is not None:
            raise self.blocked
        session = await self._link.get()
        try:
            async with asyncio.timeout(self.config.timeout_s):
                return await call(session)
        except TimeoutError:
            await self._link.close()
            raise ConnectorError("network", f"{LABEL}: server không trả lời {what} trong {self.config.timeout_s:g} giây nên đã bị dừng") from None
        except Exception as exc:
            error = getattr(exc, "error", None)
            code = getattr(error, "code", None)
            if isinstance(code, int) and code != CONNECTION_CLOSED:
                said = short_line(str(getattr(error, "message", "")), self._secrets)
                raise ConnectorError("invalid", f"{LABEL}: server từ chối {what}" + (f": {said}" if said else "")) from None
            await self._link.close()
            raise ConnectorError("network", f"{LABEL}: mất kết nối với server khi gọi {what} (tiến trình server đã thoát hoặc đường truyền đứt). Lần gọi sau sẽ khởi động lại server.") from None

    async def _call(self, tool: str, args: dict[str, Any]) -> tuple[str, Any, int]:
        """A tool's result as `(text, JSON or None, number of parts that are not text)`."""
        result = await self._request(f"tool «{tool}»", lambda session: session.call_tool(tool, args))
        blocks = list(getattr(result, "content", None) or [])
        texts = [text for text in (getattr(b, "text", None) or getattr(getattr(b, "resource", None), "text", None) for b in blocks) if isinstance(text, str)]
        raw = "\n\n".join(texts)
        if getattr(result, "is_error", None) or getattr(result, "isError", None):  # mcp 2.x / 1.x
            said = short_line(raw, self._secrets)
            raise ConnectorError("invalid", f"{LABEL}: tool «{tool}» báo lỗi" + (f": {said}" if said else ""))
        structured = getattr(result, "structured_content", None) or getattr(result, "structuredContent", None)
        data = structured if isinstance(structured, dict) else parse_json(raw)
        if not raw and data is None:
            raise ConnectorError("invalid", f"{LABEL}: tool «{tool}» không trả về chữ nào")
        return raw or as_text(data), data, len(blocks) - len(texts)

    async def test(self) -> Health:
        if self.blocked is not None:
            return Health(state="unconfigured", checked_at=now_iso(), detail=str(self.blocked))
        try:
            listed = await self._request("tools/list", lambda session: session.list_tools())
        except ConnectorError as exc:
            return Health(state="error", checked_at=now_iso(), detail=str(exc))
        names = {str(getattr(tool, "name", "")) for tool in getattr(listed, "tools", None) or []}
        wanted = [self.config.fetch.tool, *([self.config.search.tool] if self.config.search else [])]
        missing = [tool for tool in dict.fromkeys(wanted) if tool not in names]
        info = getattr(self._link.info, "server_info", None) or getattr(self._link.info, "serverInfo", None)
        identity = " ".join(str(part) for part in (getattr(info, "name", ""), getattr(info, "version", "")) if part) or None
        if missing:
            more = getattr(listed, "next_cursor", None) or getattr(listed, "nextCursor", None)
            state, tail = ("degraded", " trong trang đầu của danh sách tool") if more else ("error", "")
            detail = f"Server không có tool {', '.join(f'«{t}»' for t in missing)}{tail}. Server có: {', '.join(sorted(names)[:30]) or '(không tool nào)'}."
            return Health(state=state, checked_at=now_iso(), detail=detail, identity=identity)
        return Health(state="ok", checked_at=now_iso(), detail=f"Đã kết nối MCP server, có đủ tool: {', '.join(dict.fromkeys(wanted))}.", identity=identity)

    # ---- references

    def recognize(self, text: str) -> list[DocRef]:
        return [DocRef(ref=ref, kind=NAME) for ref in self.recognizer.find(text)]

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        value = self.recognizer.canonical(ref.ref)
        return value is not None and allowed(value, self.config.allow)

    # ---- reading

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        value = self.recognizer.canonical(ref.ref)
        if value is None:
            raise ConnectorError("invalid", f"{LABEL}: «{ref.ref[:80]}» không phải tham chiếu mà kết nối này nhận ra")
        if not allowed(value, self.config.allow):
            raise ConnectorError("forbidden", f"{LABEL}: {value} nằm ngoài các tham chiếu được phép của kết nối (allow)")
        raw, data, skipped = await self._call(self.config.fetch.tool, fill(self.config.fetch.args, {"ref": value}))
        content = document(NAME, self.recognizer, value, raw, data, self.config.fetch, LABEL)
        if skipped:
            content.notes.append(f"{skipped} phần không phải chữ (ảnh, tệp) trong kết quả của tool: AI không đọc được")
        return content

    async def search(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]:
        query = query.strip()
        if self.config.search is None or not query:
            return []
        limit = max(1, min(limit, MAX_HITS))
        raw, data, _ = await self._call(self.config.search.tool, fill(self.config.search.args, {"query": query, "limit": limit}))
        # A result that is not JSON is read line by line: each line holding a reference is a hit.
        return hits(NAME, self.recognizer, self.config.allow, data if data is not None else raw.splitlines(), self.config.search, limit, LABEL)
