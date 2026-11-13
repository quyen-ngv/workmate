"""The office's own tools (tickets, chat, memory, automations…) for agents that run outside this process.

Claude runs the tools in-process (SDK MCP server). An ACP agent such as Kiro CLI can only start MCP servers
as subprocesses, so the run exposes its tools on a local socket (ToolServer) and the agent starts this module
as a stdio MCP server (`python -m actoroffice.runtimes.office_mcp --port N`) that forwards to it.

Every call still goes through the run's gate (`mcp__office__<tool>`), exactly like with Claude: a change of
requirements interrupts the agent, and permission rules apply. The socket listens on 127.0.0.1 only and needs
the run's random token (passed to the subprocess in its environment).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import secrets
import socket
import sys
from typing import Any

from actoroffice.tools.registry import BoundTool, ToolError

from .base import GateDecision, ToolGate

log = logging.getLogger(__name__)

TOKEN_ENV = "ACTOROFFICE_MCP_TOKEN"
SERVER_NAME = "office"
MAX_LINE = 32 * 1024 * 1024


class ToolServer:
    """Serves one run's office tools on 127.0.0.1 to the stdio shim below."""

    def __init__(self, tools: list[BoundTool], gate: ToolGate) -> None:
        self.tools = {t.name: t for t in tools}
        self.gate = gate
        self.token = secrets.token_hex(16)
        self.deferred: tuple[str, dict[str, Any]] | None = None  # a call the user has to approve later
        self._server: asyncio.base_events.Server | None = None
        self.listed = asyncio.Event()  # the agent has asked for the tools at least once

    async def wait_listed(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self.listed.wait(), timeout)
        except TimeoutError:
            return False
        return True

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", 0, limit=MAX_LINE)
        return int(self._server.sockets[0].getsockname()[1])

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2)  # never wait on a stuck client

    def listing(self) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.definition.description, "inputSchema": t.definition.input_schema}
            for t in self.tools.values()
        ]

    async def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        bound = self.tools.get(name)
        if bound is None:
            return {"text": f"Unknown office tool {name}", "isError": True}
        verdict = await self.gate.before_tool(f"mcp__{SERVER_NAME}__{name}", args, None)
        if verdict.decision == GateDecision.DENY:
            return {"text": verdict.reason or "Denied by the office policy.", "isError": True}
        if verdict.decision == GateDecision.DEFER:
            self.deferred = (name, args)
            return {"text": "Waiting for the user's approval. End your turn now; you will be woken with the answer.", "isError": True}
        try:
            text = await bound.call(args)
            error = False
        except ToolError as exc:
            text, error = str(exc), True
        except Exception as exc:  # a tool that breaks answers with an error; it must not drop the agent's connection
            log.exception("office tool %s failed", name)
            text, error = f"{name} failed: {type(exc).__name__}: {exc}", True
        await self.gate.after_tool(f"mcp__{SERVER_NAME}__{name}", args, text)
        return {"text": text, "isError": error}

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while line := await reader.readline():
                request = json.loads(line)
                if not secrets.compare_digest(str(request.get("token", "")), self.token):
                    break
                if request.get("op") == "list":
                    reply: Any = self.listing()
                    self.listed.set()
                else:
                    reply = await self.call(str(request.get("name", "")), request.get("args") or {})
                writer.write(json.dumps({"id": request.get("id"), "result": reply}, ensure_ascii=False).encode() + b"\n")
                await writer.drain()
        except (ConnectionError, json.JSONDecodeError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    def mcp_server_entry(self, port: int) -> dict[str, Any]:
        """The ACP `mcpServers` entry that makes the agent start the stdio shim."""
        return {
            "name": SERVER_NAME,
            "command": sys.executable,
            "args": ["-m", "actoroffice.runtimes.office_mcp", "--port", str(port)],
            "env": [{"name": TOKEN_ENV, "value": self.token}, {"name": "PYTHONUTF8", "value": "1"}],
        }


# ---- the stdio MCP server the agent starts ------------------------------------------------------------


def _shim(port: int) -> None:
    """Minimal MCP over stdio (newline-delimited JSON-RPC): initialize, tools/list, tools/call, ping."""
    token = os.environ.get(TOKEN_ENV, "")
    sock = socket.create_connection(("127.0.0.1", port))
    stream = sock.makefile("rwb")
    counter = 0

    def office(op: str, **fields: Any) -> Any:
        nonlocal counter
        counter += 1
        stream.write(json.dumps({"id": counter, "token": token, "op": op, **fields}, ensure_ascii=False).encode() + b"\n")
        stream.flush()
        return json.loads(stream.readline())["result"]

    out = sys.stdout.buffer
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        message = json.loads(raw)
        method, msg_id = message.get("method"), message.get("id")
        if msg_id is None:
            continue  # notifications (initialized, cancelled, …)
        params = message.get("params") or {}
        if method == "initialize":
            result: Any = {
                "protocolVersion": params.get("protocolVersion") or "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0"},
            }
        elif method == "tools/list":
            result = {"tools": office("list")}
        elif method == "tools/call":
            reply = office("call", name=params.get("name"), args=params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": reply["text"]}], "isError": bool(reply["isError"])}
        elif method == "ping":
            result = {}
        else:
            out.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"{method} not supported"}}).encode() + b"\n")
            out.flush()
            continue
        out.write(json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result}, ensure_ascii=False).encode() + b"\n")
        out.flush()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    _shim(parser.parse_args().port)
