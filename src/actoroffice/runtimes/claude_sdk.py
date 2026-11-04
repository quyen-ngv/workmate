"""Claude Code via the Claude Agent SDK (uses the user's local Claude login / subscription)."""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    tool,
)

from actoroffice.domain.run import DeferredTool, RateLimit, TurnResult, TurnStatus, Usage
from actoroffice.proctree import kill_tree
from actoroffice.tools.registry import BoundTool, ToolError

from .base import EventSink, GateDecision, RuntimeCaps, ToolGate, TurnSpec

log = logging.getLogger(__name__)
THINKING_LIMIT = 6000
MAX_MESSAGE_BYTES = 32 * 1024 * 1024  # the SDK default (1 MB) is too small for large file reads
RESULT_PREVIEW = 400
# Windows caps a whole command line at 32,767 characters; the prompt shares it with every other argument.
PROMPT_ARG_LIMIT = 8000

INTERNAL_SERVER = "office"
SHELLS = {"Bash": "sh", "PowerShell": "ps"}  # shell tools whose commands the office limits (engine/command_guard.py)


class ClaudeSdkRuntime:
    name = "claude"
    caps = RuntimeCaps(resume=True, defer=True, custom_system_prompt=True)

    def __init__(self, *, cli_path: str | None = None) -> None:
        self.cli_path = cli_path
        self._clients: dict[str, ClaudeSDKClient] = {}

    async def cancel(self, run_id: str) -> None:
        client = self._clients.get(run_id)
        if client is not None:
            await client.interrupt()

    def pid(self, run_id: str) -> int | None:
        """The CLI process of a running turn. The SDK keeps it on a private attribute: None when that changes."""
        client = self._clients.get(run_id)
        process = getattr(getattr(client, "_transport", None), "_process", None)
        pid = getattr(process, "pid", None)
        return pid if isinstance(pid, int) else None

    async def abort(self, run_id: str) -> None:
        pid = self.pid(run_id)
        if pid is not None:
            kill_tree(pid)

    async def run_turn(self, spec: TurnSpec, gate: ToolGate, sink: EventSink) -> TurnResult:
        prompt_file = _prompt_file(spec.system_prompt)
        options = self._options(spec, gate, prompt_file)
        texts: list[str] = []
        rate_limit: RateLimit | None = None
        result: ResultMessage | None = None
        assistant_error: str | None = None
        model = ""
        session_id: str | None = None  # known from the start: a turn cut off without a result can still be resumed
        try:
            async with ClaudeSDKClient(options=options) as client:
                self._clients[spec.run_id] = client
                await client.query(spec.prompt)
                async for msg in client.receive_response():
                    if isinstance(msg, AssistantMessage):
                        assistant_error = msg.error or assistant_error
                        for block in msg.content:
                            if isinstance(block, TextBlock) and block.text.strip():
                                texts.append(block.text)
                                await sink("text", {"text": block.text, "subagent": msg.parent_tool_use_id is not None})
                            elif isinstance(block, ThinkingBlock) and block.thinking.strip():
                                await sink("thinking", {"text": block.thinking[:THINKING_LIMIT], "subagent": msg.parent_tool_use_id is not None})
                            elif isinstance(block, ToolUseBlock):
                                await sink("tool_use", {"tool": block.name, "input": block.input, "id": block.id})
                    elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                        for block in msg.content:
                            if isinstance(block, ToolResultBlock):
                                await sink("tool_result", {"id": block.tool_use_id, "error": bool(block.is_error), "text": _result_text(block.content)})
                    elif isinstance(msg, RateLimitEvent):
                        info = msg.rate_limit_info
                        rate_limit = RateLimit(
                            status=info.status, kind=info.rate_limit_type, utilization=info.utilization, resets_at=info.resets_at
                        )
                        await sink("rate_limit", rate_limit.model_dump())
                    elif isinstance(msg, SystemMessage) and msg.subtype == "init":
                        model = str((msg.data or {}).get("model") or "")
                        session_id = str((msg.data or {}).get("session_id") or "") or None
                    elif isinstance(msg, SystemMessage) and msg.subtype == "compact_boundary":
                        await sink("compacted", {"data": msg.data})
                    elif isinstance(msg, ResultMessage):
                        result = msg
        except Exception as exc:  # the CLI process failed to start or crashed
            log.exception("claude run %s failed", spec.run_id)
            return TurnResult(status=TurnStatus.ERROR, error=_error_text(exc), rate_limit=rate_limit, session_id=session_id)
        finally:
            self._clients.pop(spec.run_id, None)
            if prompt_file is not None:
                prompt_file.unlink(missing_ok=True)
        return _to_turn_result(result, texts, rate_limit, assistant_error, model, session_id)

    def _options(self, spec: TurnSpec, gate: ToolGate, prompt_file: Path | None = None) -> ClaudeAgentOptions:
        guard = spec.command_guard

        async def pre_tool_use(data: Any, tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
            args = data.get("tool_input") or {}
            res = await gate.before_tool(data["tool_name"], args, tool_use_id)
            out: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": res.decision.value}
            if res.reason:
                out["permissionDecisionReason"] = res.reason
            tool = data["tool_name"]
            if guard is not None and res.decision == GateDecision.ALLOW and tool in SHELLS and args.get("command"):
                # Only after the gate allowed the command as written: the prefix lets the office stop it in time.
                out["updatedInput"] = {**args, "command": guard.wrap(tool_use_id, str(args["command"]), SHELLS[tool])}
            return {"hookSpecificOutput": out}

        def told(event: str, tool_use_id: str | None) -> dict[str, Any]:
            notes = guard.tool_ended(tool_use_id) if guard is not None else None
            return {"hookSpecificOutput": {"hookEventName": event, "additionalContext": notes}} if notes else {}

        async def post_tool_use(data: Any, tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
            args = data.get("tool_input") or {}
            if guard is not None and isinstance(args.get("command"), str):
                args = {**args, "command": guard.unwrap(args["command"])}
            await gate.after_tool(data["tool_name"], args, data.get("tool_response"))
            return told("PostToolUse", tool_use_id)

        async def post_tool_use_failure(_data: Any, tool_use_id: str | None, _ctx: Any) -> dict[str, Any]:
            return told("PostToolUseFailure", tool_use_id)

        async def can_use_tool(name: str, _args: dict[str, Any], _ctx: Any) -> Any:
            # Every call is decided by the PreToolUse hook; reaching here means the CLI asked on its own.
            return PermissionResultDeny(message=f"{name} was not approved by the office policy")

        extra_args: dict[str, str | None] = {}
        system_prompt: Any
        if prompt_file is not None:
            # A long prompt goes in a file: on the command line it breaks the Windows limit (WinError 206).
            if spec.coding_preset:
                system_prompt = {"type": "preset", "preset": "claude_code"}
                extra_args["append-system-prompt-file"] = str(prompt_file)
            else:
                system_prompt = {"type": "file", "path": str(prompt_file)}
        elif spec.coding_preset:
            system_prompt = {"type": "preset", "preset": "claude_code", "append": spec.system_prompt}
        else:
            system_prompt = spec.system_prompt
        mcp_servers: dict[str, Any] = {name: dict(cfg) for name, cfg in spec.mcp_servers.items() if name != INTERNAL_SERVER}
        if spec.internal_tools:
            mcp_servers[INTERNAL_SERVER] = create_sdk_mcp_server(
                name=INTERNAL_SERVER, version="0.1.0", tools=[_sdk_tool(t) for t in spec.internal_tools]
            )
        return ClaudeAgentOptions(
            model=spec.model,
            cwd=str(spec.cwd),
            add_dirs=[str(d) for d in spec.add_dirs],
            resume=spec.resume_session_id,
            system_prompt=system_prompt,
            tools=spec.tools,
            setting_sources=[],  # actors never inherit the user's own ~/.claude settings or memories
            permission_mode="default",
            can_use_tool=can_use_tool,
            hooks={
                "PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool_use], timeout=spec.hook_timeout_s)],
                "PostToolUse": [HookMatcher(matcher=None, hooks=[post_tool_use])],
                "PostToolUseFailure": [HookMatcher(matcher=None, hooks=[post_tool_use_failure])],
            },
            mcp_servers=mcp_servers,
            strict_mcp_config=True,
            verbatim_prompts=True,  # chat content must not trigger @file expansion or slash commands
            max_turns=spec.max_turns,
            max_buffer_size=MAX_MESSAGE_BYTES,  # a big file read or tool result must not kill the run
            # The model decides when to think; "summarized" returns readable thinking so the user can follow it.
            thinking={"type": "adaptive", "display": "summarized"} if spec.show_thinking else None,
            # The CLI then gives the model a StructuredOutput tool and returns its input as ResultMessage.structured_output.
            output_format={"type": "json_schema", "schema": spec.output_schema} if spec.output_schema else None,
            cli_path=self.cli_path,
            env={"CLAUDE_AGENT_SDK_CLIENT_APP": "actoroffice/0.1", **_bash_timeouts(guard), **spec.env},
            extra_args=extra_args,
        )


def _bash_timeouts(guard: Any) -> dict[str, str]:
    """The CLI's own Bash timeout, a minute past the office limit: the office stops a long foreground command before
    the CLI moves it to the background (the CLI never stops it)."""
    if guard is None:
        return {}
    ms = str(int((guard.limit_s + 60) * 1000))
    return {"BASH_DEFAULT_TIMEOUT_MS": ms, "BASH_MAX_TIMEOUT_MS": ms}


def _error_text(exc: BaseException) -> str:
    """The error of a turn that crashed, with its cause: the SDK reports a refused command line (WinError 206) as
    "Claude Code not found", and only the cause tells the two apart."""
    text = f"{type(exc).__name__}: {exc}"
    cause = exc.__cause__
    if cause is not None and str(cause) and str(cause) not in text:
        text += f" (cause: {type(cause).__name__}: {cause})"
    return text


def _prompt_file(system_prompt: str) -> Path | None:
    """A file holding the system prompt when it is too long for the command line, else None."""
    if len(system_prompt) <= PROMPT_ARG_LIMIT:
        return None
    fd, name = tempfile.mkstemp(prefix="ao-prompt-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(system_prompt)
    return Path(name)


def _result_text(content: Any) -> str:
    """A short preview of what a tool returned (the full result stays in the agent's own context)."""
    if isinstance(content, list):
        content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    text = str(content or "").strip()
    return text[:RESULT_PREVIEW] + ("…" if len(text) > RESULT_PREVIEW else "")


def _sdk_tool(bound: BoundTool) -> Any:
    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        try:
            text = await bound.call(args)
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "is_error": True}
        return {"content": [{"type": "text", "text": text}]}

    return tool(bound.name, bound.definition.description, bound.definition.input_schema)(handler)


def _main_model(model_usage: dict[str, Any] | None) -> str:
    """The model that wrote most of the turn (the CLI may also use a small model for side tasks)."""
    def output(name: str) -> int:
        entry = (model_usage or {}).get(name)
        return int(entry.get("outputTokens") or 0) if isinstance(entry, dict) else 0

    return max(model_usage, key=output) if model_usage else ""


def _to_turn_result(
    result: ResultMessage | None,
    texts: list[str],
    rate_limit: RateLimit | None,
    assistant_error: str | None,
    model: str = "",
    session_id: str | None = None,
) -> TurnResult:
    if result is None:
        return TurnResult(
            status=TurnStatus.ERROR, error="no result from Claude", rate_limit=rate_limit, usage=Usage(model=model), session_id=session_id
        )
    usage = Usage.from_sdk(result.usage)
    usage.model = model or _main_model(result.model_usage)
    structured = getattr(result, "structured_output", None)
    common = {
        "session_id": result.session_id,
        "text": "\n\n".join(texts).strip(),
        "usage": usage,
        "cost_usd": float(result.total_cost_usd or 0.0),
        "rate_limit": rate_limit,
        "structured": structured if isinstance(structured, dict) else None,
    }
    reason = result.terminal_reason or ""
    if result.deferred_tool_use is not None:
        d = result.deferred_tool_use
        return TurnResult(status=TurnStatus.DEFERRED, deferred=DeferredTool(tool_use_id=d.id, name=d.name, input=d.input), **common)
    rate_limited = (
        assistant_error == "rate_limit"
        or result.api_error_status == 429
        or (rate_limit is not None and rate_limit.status == "rejected")
    )
    if result.is_error and rate_limited:
        return TurnResult(status=TurnStatus.RATE_LIMITED, error="rate limited", **common)
    if reason.startswith("aborted"):
        return TurnResult(status=TurnStatus.CANCELLED, **common)
    if reason == "max_turns" or result.subtype == "error_max_turns":
        return TurnResult(status=TurnStatus.MAX_TURNS, **common)
    if result.is_error:
        detail = "; ".join(result.errors or []) or result.result or result.subtype
        return TurnResult(status=TurnStatus.ERROR, error=detail, **common)
    return TurnResult(status=TurnStatus.COMPLETED, **common)
