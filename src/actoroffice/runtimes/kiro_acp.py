"""Kiro CLI as an actor runtime, over the Agent Client Protocol (JSON-RPC 2.0, one message per line on stdio).

`kiro-cli acp --agent-engine=v3 --auth-method=cli` runs the agent; the office is the ACP *client*:

- it advertises file-system and terminal capabilities, so the agent reads/writes files and runs commands
  through the office — every such call passes the same gate as with Claude (workspace containment, approvals,
  "always allow" rules, change interrupts) and lands in the per-run stats;
- `session/request_permission` (tools the agent runs itself) is decided by the gate too;
- the office tools reach the agent as an MCP server (see office_mcp.py);
- streamed updates become the usual run events: narration, thinking, tool calls, failures.

A tool call that needs a human decision cannot be parked mid-turn in ACP: the permission is refused, the turn
is cancelled and the ticket waits (DEFERRED); when the user approves, the actor is woken and retries the call,
which the stored approval then lets through.

ACP has no system prompt: the office instructions open the first message of each new session.
Model: an actor whose model is "kiro" uses Kiro's default; "kiro:<model id>" picks one (`_meta.kiro.modelId`).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from actoroffice.domain.run import DeferredTool, TurnResult, TurnStatus, Usage
from actoroffice.proctree import kill_tree

from .base import EventSink, GateDecision, RuntimeCaps, ToolGate, TurnSpec
from .office_mcp import SERVER_NAME, ToolServer

log = logging.getLogger(__name__)

MAX_LINE = 32 * 1024 * 1024
TERMINAL_OUTPUT = 256 * 1024
STOP = {
    "end_turn": TurnStatus.COMPLETED,
    "max_tokens": TurnStatus.COMPLETED,
    "refusal": TurnStatus.COMPLETED,
    "max_turn_requests": TurnStatus.MAX_TURNS,
    "cancelled": TurnStatus.CANCELLED,
}


class AcpError(Exception):
    def __init__(self, message: str, code: int = -32000) -> None:
        super().__init__(message)
        self.code = code


def acp_mcp_entry(name: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """A team connector in ACP's session/new format (env and headers are lists of name/value pairs)."""
    if cfg.get("type") in ("http", "sse"):
        headers = [{"name": k, "value": str(v)} for k, v in (cfg.get("headers") or {}).items()]
        return {"type": cfg["type"], "name": name, "url": cfg["url"], "headers": headers}
    env = [{"name": k, "value": str(v)} for k, v in (cfg.get("env") or {}).items()]
    return {"name": name, "command": cfg["command"], "args": list(cfg.get("args") or []), "env": env}


def find_kiro_cli() -> str | None:
    found = shutil.which("kiro-cli")
    if found:
        return found
    candidates = [Path.home() / ".local" / "bin" / "kiro-cli"]
    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        local = Path(os.environ["LOCALAPPDATA"])
        candidates += [local / "Programs" / "kiro-cli" / "kiro-cli.exe", local / "Kiro-Cli" / "kiro-cli.exe"]
    return next((str(p) for p in candidates if p.is_file()), None)


class KiroAcpRuntime:
    name = "kiro"
    caps = RuntimeCaps(resume=True, defer=False, custom_system_prompt=False)

    def __init__(self, *, cli_path: str | None = None, engine: str = "v3", command: list[str] | None = None) -> None:
        self.cli_path = cli_path
        self.engine = engine
        self.command = command  # tests: a fake ACP agent instead of kiro-cli
        self._turns: dict[str, _Turn] = {}

    def _argv(self) -> list[str] | None:
        if self.command:
            return list(self.command)
        exe = self.cli_path or find_kiro_cli()
        if not exe:
            return None
        return [exe, "acp", *(["--agent-engine=v3", "--auth-method=cli"] if self.engine == "v3" else [])]

    async def cancel(self, run_id: str) -> None:
        turn = self._turns.get(run_id)
        if turn is not None:
            await turn.cancel()

    def pid(self, run_id: str) -> int | None:
        turn = self._turns.get(run_id)
        return turn.proc.pid if turn is not None and turn.proc is not None else None

    async def abort(self, run_id: str) -> None:
        pid = self.pid(run_id)
        if pid is not None:
            kill_tree(pid)

    async def run_turn(self, spec: TurnSpec, gate: ToolGate, sink: EventSink) -> TurnResult:
        argv = self._argv()
        if argv is None:
            return TurnResult(
                status=TurnStatus.ERROR,
                error="Không tìm thấy kiro-cli. Cài Kiro CLI (irm 'https://cli.kiro.dev/install.ps1' | iex), đăng nhập "
                "bằng `kiro-cli login`, hoặc đặt kiro_cli_path trong config.yaml.",
            )
        turn = _Turn(argv, spec, gate, sink)
        self._turns[spec.run_id] = turn
        try:
            return await turn.run()
        finally:
            self._turns.pop(spec.run_id, None)


# ---- JSON-RPC over the agent's stdio ---------------------------------------------------------------


class _Connection:
    def __init__(self, proc: asyncio.subprocess.Process, handlers: dict[str, Any], on_notification: Any) -> None:
        self.proc = proc
        self.handlers = handlers
        self.on_notification = on_notification
        self._next = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        self._reader = asyncio.create_task(self._read())

    async def request(self, method: str, params: dict[str, Any]) -> Any:
        self._next += 1
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[self._next] = future
        await self._send({"jsonrpc": "2.0", "id": self._next, "method": method, "params": params})
        return await future

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def _send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")
        await self.proc.stdin.drain()

    async def _read(self) -> None:
        assert self.proc.stdout is not None
        try:
            while line := await self.proc.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue  # not protocol (stray output)
                if "method" not in message:
                    future = self._pending.pop(message.get("id"), None)
                    if future is not None and not future.done():
                        if "error" in message:
                            err = message["error"] or {}
                            future.set_exception(AcpError(str(err.get("message", "error")), int(err.get("code", -32000))))
                        else:
                            future.set_result(message.get("result"))
                elif "id" in message:
                    task = asyncio.create_task(self._answer(message))
                    self._tasks.add(task)
                    task.add_done_callback(self._tasks.discard)
                else:
                    await self.on_notification(message["method"], message.get("params") or {})
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(AcpError("kiro-cli exited"))

    async def _answer(self, message: dict[str, Any]) -> None:
        handler = self.handlers.get(message["method"])
        try:
            if handler is None:
                raise AcpError(f"{message['method']} is not supported by this client", -32601)
            reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"], "result": await handler(message.get("params") or {})}
        except AcpError as exc:
            reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": exc.code, "message": str(exc)}}
        except Exception as exc:  # never leave the agent waiting on an answer
            log.exception("ACP handler %s failed", message["method"])
            reply = {"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32603, "message": f"{type(exc).__name__}: {exc}"}}
        with contextlib.suppress(Exception):
            await self._send(reply)

    async def close(self) -> None:
        self._reader.cancel()
        for task in list(self._tasks):
            task.cancel()


# ---- one prompt turn ------------------------------------------------------------------------------


@dataclass
class _Terminal:
    proc: asyncio.subprocess.Process
    limit: int
    output: bytearray = field(default_factory=bytearray)
    truncated: bool = False
    pump: asyncio.Task[None] | None = None


class _Turn:
    def __init__(self, argv: list[str], spec: TurnSpec, gate: ToolGate, sink: EventSink) -> None:
        self.argv = argv
        self.spec = spec
        self.gate = gate
        self.sink = sink
        self.session_id: str | None = None
        self.conn: _Connection | None = None
        self.proc: asyncio.subprocess.Process | None = None
        self.tools = ToolServer(spec.internal_tools, gate) if spec.internal_tools else None
        self.loading = False  # session/load replays history: not this run's events
        self.texts: list[str] = []
        self._text = ""
        self._thought = ""
        self.cost = 0.0
        self.deferred: DeferredTool | None = None
        self.approved_commands: set[str] = set()
        self.terminals: dict[str, _Terminal] = {}
        self._terminal_seq = 0
        self.stderr = bytearray()  # the agent's own log, kept short: shown when it fails
        self._stderr_task: asyncio.Task[None] | None = None

    async def _read_stderr(self) -> None:
        assert self.proc is not None and self.proc.stderr is not None
        while chunk := await self.proc.stderr.read(8192):
            self.stderr += chunk
            del self.stderr[: max(0, len(self.stderr) - 16384)]

    async def run(self) -> TurnResult:
        spec = self.spec
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *self.argv,
                cwd=str(spec.cwd),
                env={**os.environ, **spec.env},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE,
            )
        except OSError as exc:
            return TurnResult(status=TurnStatus.ERROR, error=f"Không chạy được kiro-cli: {exc}")
        handlers = {
            "session/request_permission": self.on_permission,
            "fs/read_text_file": self.on_read,
            "fs/write_text_file": self.on_write,
            "terminal/create": self.on_terminal_create,
            "terminal/output": self.on_terminal_output,
            "terminal/wait_for_exit": self.on_terminal_wait,
            "terminal/kill": self.on_terminal_kill,
            "terminal/release": self.on_terminal_release,
            "_kiro/openExternalUrl": self.on_ignore,  # e.g. an MCP OAuth page: never opened unattended
        }
        self.conn = _Connection(self.proc, handlers, self.on_notification)
        self._stderr_task = asyncio.create_task(self._read_stderr())
        try:
            return await self._prompt()
        except AcpError as exc:
            tail = self.stderr.decode("utf-8", "replace").strip()[-800:]
            error = f"Kiro: {exc}" + (f"\n{tail}" if tail else "")
            return TurnResult(status=TurnStatus.ERROR, session_id=self.session_id, error=error, text="\n\n".join(self.texts))
        finally:
            await self._shutdown()

    async def _prompt(self) -> TurnResult:
        assert self.conn is not None
        spec = self.spec
        init = await self.conn.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}, "terminal": True},
                "clientInfo": {"name": "actor-office", "version": "0.1"},
            },
        )
        can_load = bool(((init or {}).get("agentCapabilities") or {}).get("loadSession"))
        mcp = []
        if self.tools is not None:
            port = await self.tools.start()
            mcp.append(self.tools.mcp_server_entry(port))
        mcp += [acp_mcp_entry(name, cfg) for name, cfg in spec.mcp_servers.items()]
        common = {"cwd": str(spec.cwd), "mcpServers": mcp, "additionalDirectories": [str(d) for d in spec.add_dirs]}
        fresh = True
        if spec.resume_session_id and can_load:
            self.loading = True
            try:
                await self.conn.request("session/load", {**common, "sessionId": spec.resume_session_id})
                self.session_id, fresh = spec.resume_session_id, False
            except AcpError:
                log.warning("kiro: could not load session %s; starting a new one", spec.resume_session_id)
            finally:
                self.loading = False
        if fresh:
            model = spec.model.split(":", 1)[1] if spec.model.startswith("kiro:") else ""
            meta = {"_meta": {"kiro": {"modelId": model}}} if model else {}
            created = await self.conn.request("session/new", {**common, **meta})
            self.session_id = str(created["sessionId"])
        text = f"<office_instructions>\n{spec.system_prompt}\n</office_instructions>\n\n{spec.prompt}" if fresh else spec.prompt
        reply = await self.conn.request("session/prompt", {"sessionId": self.session_id, "prompt": [{"type": "text", "text": text}]})
        await self._flush()
        reason = str((reply or {}).get("stopReason", "end_turn"))
        status = STOP.get(reason, TurnStatus.COMPLETED)
        if self.deferred is not None or (self.tools is not None and self.tools.deferred is not None):
            status = TurnStatus.DEFERRED
            if self.deferred is None and self.tools is not None and self.tools.deferred is not None:
                name, args = self.tools.deferred
                self.deferred = DeferredTool(tool_use_id="", name=f"mcp__{SERVER_NAME}__{name}", input=args)
        return TurnResult(
            status=status,
            session_id=self.session_id,
            text="\n\n".join(self.texts).strip(),
            usage=Usage(),  # Kiro reports credits, not tokens
            cost_usd=self.cost,
            deferred=self.deferred,
            error="Kiro từ chối trả lời yêu cầu này" if reason == "refusal" else None,
        )

    async def cancel(self) -> None:
        if self.conn is not None and self.session_id:
            with contextlib.suppress(Exception):
                await self.conn.notify("session/cancel", {"sessionId": self.session_id})

    async def _shutdown(self) -> None:
        """Agent first (with everything it started, e.g. the MCP shim), then our side: the shim holds a socket to
        the tool server and the agent's stderr, so closing in the other order waits forever."""
        for terminal in self.terminals.values():
            _kill(terminal.proc)
        if self.proc is not None:
            children = _children(self.proc.pid)
            if self.proc.returncode is None:
                with contextlib.suppress(Exception):
                    assert self.proc.stdin is not None
                    self.proc.stdin.close()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), 3)
            _kill(self.proc)
            for child in children:
                with contextlib.suppress(Exception):
                    child.kill()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.proc.wait(), 5)
        if self.conn is not None:
            await self.conn.close()
        if self._stderr_task is not None:
            self._stderr_task.cancel()
        if self.tools is not None:
            await self.tools.close()

    # ---- streamed updates ------------------------------------------------------------------------

    async def on_notification(self, method: str, params: dict[str, Any]) -> None:
        if method != "session/update" or self.loading:
            return
        update = params.get("update") or {}
        kind = update.get("sessionUpdate")
        if ((update.get("_meta") or {}).get("kiro") or {}).get("replay"):
            return
        if kind == "agent_message_chunk":
            self._text += _block_text(update.get("content"))
        elif kind == "agent_thought_chunk":
            self._thought += _block_text(update.get("content"))
        elif kind == "tool_call":
            await self._flush()
            name, args = _gate_view(update)
            await self.sink("tool_use", {"tool": name, "input": args, "id": update.get("toolCallId")})
        elif kind == "tool_call_update" and update.get("status") in ("completed", "failed"):
            failed = update.get("status") == "failed"
            await self.sink("tool_result", {"id": update.get("toolCallId"), "error": failed, "text": _content_text(update)[:400]})
        elif kind == "plan":
            await self._flush()
            todos = [{"content": e.get("content", ""), "status": e.get("status", "pending")} for e in update.get("entries") or []]
            await self.sink("tool_use", {"tool": "TodoWrite", "input": {"todos": todos}, "id": None})
        elif kind == "usage_update":
            cost = update.get("cost") or {}
            if str(cost.get("currency", "USD")).upper() == "USD" and cost.get("amount") is not None:
                self.cost = float(cost["amount"])

    async def _flush(self) -> None:
        if self._thought.strip():
            await self.sink("thinking", {"text": self._thought.strip()[:6000]})
        if self._text.strip():
            self.texts.append(self._text.strip())
            await self.sink("text", {"text": self._text.strip()})
        self._text = self._thought = ""

    # ---- agent → client requests --------------------------------------------------------------

    async def on_permission(self, params: dict[str, Any]) -> dict[str, Any]:
        call = params.get("toolCall") or {}
        options = params.get("options") or []
        mcp = ((call.get("_meta") or {}).get("kiro") or {}).get("mcpTool") or {}
        if mcp.get("serverName") == SERVER_NAME or str(call.get("title", "")).startswith(f"{SERVER_NAME}_"):
            verdict_ok = True  # office tools are gated inside the tool server, with their real arguments
        else:
            name, args = _gate_view(call)
            verdict = await self.gate.before_tool(name, args, call.get("toolCallId"))
            verdict_ok = verdict.decision == GateDecision.ALLOW
            if verdict.decision == GateDecision.DEFER:
                self.deferred = DeferredTool(tool_use_id=str(call.get("toolCallId") or ""), name=name, input=args)
                asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.cancel()))
            elif verdict_ok and name in ("Bash", "PowerShell"):
                self.approved_commands.add(str(args.get("command", "")))
        wanted = ("allow_once", "allow_always") if verdict_ok else ("reject_once", "reject_always")
        for kind in wanted:
            option = next((o for o in options if o.get("kind") == kind), None)
            if option is not None:
                return {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}
        return {"outcome": {"outcome": "cancelled"}}

    async def _check(self, tool: str, args: dict[str, Any]) -> None:
        verdict = await self.gate.before_tool(tool, args, None)
        if verdict.decision == GateDecision.DEFER:
            self.deferred = DeferredTool(tool_use_id="", name=tool, input=args)
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(self.cancel()))
            raise AcpError("Waiting for the user's approval; end your turn.")
        if verdict.decision != GateDecision.ALLOW:
            raise AcpError(verdict.reason or "Denied by the office policy.")

    async def on_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = str(params.get("path", ""))
        await self._check("Read", {"file_path": path})
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise AcpError(f"Cannot read {path}: {exc}") from None
        line, limit = params.get("line"), params.get("limit")
        if line or limit:
            lines = text.splitlines(keepends=True)
            start = max(int(line or 1) - 1, 0)
            text = "".join(lines[start : start + int(limit)] if limit else lines[start:])
        return {"content": text}

    async def on_write(self, params: dict[str, Any]) -> dict[str, Any]:
        path, content = str(params.get("path", "")), str(params.get("content", ""))
        await self._check("Write", {"file_path": path, "content": content})
        try:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_text(content, encoding="utf-8")
        except OSError as exc:
            raise AcpError(f"Cannot write {path}: {exc}") from None
        await self.gate.after_tool("Write", {"file_path": path}, "ok")
        return {}

    async def on_terminal_create(self, params: dict[str, Any]) -> dict[str, Any]:
        command = str(params.get("command", ""))
        args = [str(a) for a in params.get("args") or []]
        line = " ".join([command, *args]).strip()
        if line not in self.approved_commands:  # already decided when the agent asked for permission
            await self._check("Bash", {"command": line})
        env = {**os.environ, **self.spec.env, **{e["name"]: e["value"] for e in params.get("env") or [] if "name" in e}}
        cwd = params.get("cwd") or str(self.spec.cwd)
        try:
            if args:
                proc = await asyncio.create_subprocess_exec(command, *args, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
            else:
                proc = await asyncio.create_subprocess_shell(command, cwd=cwd, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL)
        except OSError as exc:
            raise AcpError(f"Cannot start {command}: {exc}") from None
        self._terminal_seq += 1
        terminal_id = f"term-{self._terminal_seq}"
        terminal = _Terminal(proc, int(params.get("outputByteLimit") or TERMINAL_OUTPUT))
        terminal.pump = asyncio.create_task(_pump(terminal))
        self.terminals[terminal_id] = terminal
        if self.spec.command_guard is not None:
            def told(note: str) -> None:
                terminal.output += f"\n{note}\n".encode()

            self.spec.command_guard.watch(f"{self.spec.run_id}:{terminal_id}", line, proc.pid, on_stop=told)
        return {"terminalId": terminal_id}

    def _terminal(self, params: dict[str, Any]) -> _Terminal:
        terminal = self.terminals.get(str(params.get("terminalId")))
        if terminal is None:
            raise AcpError("Unknown terminal")
        return terminal

    async def on_terminal_output(self, params: dict[str, Any]) -> dict[str, Any]:
        terminal = self._terminal(params)
        code = terminal.proc.returncode
        return {
            "output": terminal.output.decode("utf-8", "replace"),
            "truncated": terminal.truncated,
            "exitStatus": {"exitCode": code, "signal": None} if code is not None else None,
        }

    async def on_terminal_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        terminal = self._terminal(params)
        code = await terminal.proc.wait()
        if terminal.pump is not None:
            await terminal.pump
        return {"exitCode": code, "signal": None}

    async def on_terminal_kill(self, params: dict[str, Any]) -> dict[str, Any]:
        _kill(self._terminal(params).proc)
        return {}

    async def on_terminal_release(self, params: dict[str, Any]) -> dict[str, Any]:
        terminal = self.terminals.pop(str(params.get("terminalId")), None)
        if terminal is not None:
            _kill(terminal.proc)
        return {}

    async def on_ignore(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {}


# ---- helpers ----------------------------------------------------------------------------------------

KIRO_TOOL_NAMES = {
    "execute_bash": "Bash", "shell": "Bash", "execute_cmd": "Bash", "fs_write": "Write", "write": "Write",
    "fs_read": "Read", "read": "Read", "glob": "Glob", "grep": "Grep", "web_fetch": "WebFetch", "web_search": "WebSearch",
}


def _gate_view(call: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """What a tool call is, in the office's terms (the names the policy and the activity view understand)."""
    raw = call.get("rawInput") if isinstance(call.get("rawInput"), dict) else {}
    kind = str(call.get("kind") or "")
    named = KIRO_TOOL_NAMES.get(str(call.get("name") or "").lower())
    path = raw.get("path") or raw.get("file_path") or next((loc.get("path") for loc in call.get("locations") or [] if loc.get("path")), None)
    if kind == "execute" or named == "Bash":
        command = raw.get("command") or raw.get("cmd") or call.get("title") or ""
        args = raw.get("args")
        if isinstance(args, list):
            command = " ".join([str(command), *map(str, args)])
        return "Bash", {"command": str(command)}
    if kind == "edit" or named == "Write":
        if raw.get("oldText") is not None or raw.get("old_str") is not None:
            return "Edit", {"file_path": path or "", "old_string": raw.get("oldText") or raw.get("old_str") or "", "new_string": raw.get("newText") or raw.get("new_str") or ""}
        return "Write", {"file_path": path or "", "content": str(raw.get("content") or raw.get("newText") or raw.get("file_text") or "")}
    if kind in ("delete", "move"):
        return "Bash", {"command": f"{kind} {path or call.get('title', '')}"}
    if kind == "read" or named == "Read":
        return "Read", {"file_path": path or ""}
    if kind == "search" or named in ("Glob", "Grep"):
        return (named or "Grep"), {"pattern": raw.get("pattern") or raw.get("query") or call.get("title", ""), "path": path}
    if kind == "fetch" or named in ("WebFetch", "WebSearch"):
        return (named or "WebFetch"), {"url": raw.get("url") or raw.get("query") or call.get("title", "")}
    return str(call.get("name") or call.get("title") or "kiro_tool"), raw


def _block_text(block: Any) -> str:
    return str(block.get("text", "")) if isinstance(block, dict) and block.get("type") == "text" else ""


def _content_text(update: dict[str, Any]) -> str:
    parts = []
    for item in update.get("content") or []:
        if isinstance(item, dict):
            inner = item.get("content") if item.get("type") == "content" else item
            parts.append(_block_text(inner) if isinstance(inner, dict) else "")
    raw = update.get("rawOutput")
    if not any(parts) and raw is not None:
        parts.append(raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))
    return " ".join(p for p in parts if p).strip()


async def _pump(terminal: _Terminal) -> None:
    assert terminal.proc.stdout is not None
    while chunk := await terminal.proc.stdout.read(65536):
        terminal.output += chunk
        if len(terminal.output) > terminal.limit:
            del terminal.output[: len(terminal.output) - terminal.limit]  # keep the end: errors are there
            terminal.truncated = True


def _children(pid: int) -> list[Any]:
    import psutil

    try:
        return psutil.Process(pid).children(recursive=True)
    except psutil.Error:
        return []


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    kill_tree(proc.pid)
