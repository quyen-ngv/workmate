"""P0 spike: verify the Claude Agent SDK behaviours the design relies on.

Run:  uv run py spikes/p0_claude_sdk.py
Uses model "haiku" to keep quota usage small.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

import psutil
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    tool,
)

LOG: list[str] = []


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    LOG.append(line)
    print(line, flush=True)


class Probe:
    """Collects what we want to verify across turns."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.pre_tool_calls: list[str] = []
        self.asked: list[str] = []
        self.denied: list[str] = []
        self.rate_limits: list[dict[str, Any]] = []
        self.peak_rss_mb = 0.0
        self.mcp_calls: list[dict[str, Any]] = []
        self.grant_deploy = False

    # PreToolUse sees every tool call, even ones that would be auto-allowed.
    async def pre_tool_use(self, data: dict[str, Any], tool_use_id: str | None, ctx: Any) -> dict[str, Any]:
        name = data.get("tool_name", "")
        args = data.get("tool_input", {}) or {}
        self.pre_tool_calls.append(name)
        decision, reason = self.decide(name, args)
        log(f"PreToolUse {name} {json.dumps(args, ensure_ascii=False)[:120]} -> {decision}")
        if decision == "pass":
            return {}
        out: dict[str, Any] = {"hookEventName": "PreToolUse", "permissionDecision": decision}
        if reason:
            out["permissionDecisionReason"] = reason
        return {"hookSpecificOutput": out}

    def decide(self, name: str, args: dict[str, Any]) -> tuple[str, str]:
        path = args.get("file_path") or args.get("path")
        if path:
            p = Path(path)
            if not p.is_absolute():
                p = self.workspace / p
            try:
                p.resolve().relative_to(self.workspace)
            except ValueError:
                return "deny", f"Path outside workspace is not allowed: {path}"
        if name == "Bash":
            cmd = str(args.get("command", ""))
            if "deploy" in cmd:
                if self.grant_deploy:
                    return "allow", ""
                return "defer", "Needs human approval (long wait)"
            return "ask", "Shell command needs approval"
        if name in ("Read", "Glob", "Grep", "Write", "Edit") or name.startswith("mcp__office__"):
            return "allow", ""
        return "ask", "Unknown tool"

    # Only reached for calls that evaluate to "ask".
    async def can_use_tool(self, name: str, args: dict[str, Any], ctx: Any) -> Any:
        self.asked.append(name)
        log(f"can_use_tool {name} reason={getattr(ctx, 'decision_reason', None)!r} title={getattr(ctx, 'title', None)!r}")
        if name == "Bash" and "rm " in str(args.get("command", "")):
            self.denied.append(name)
            return PermissionResultDeny(message="User rejected destructive command")
        return PermissionResultAllow()

    def sample_rss(self) -> None:
        # Only our own child processes (the spawned CLI), not other Claude apps on the machine.
        total = 0.0
        for proc in psutil.Process().children(recursive=True):
            try:
                total += proc.memory_info().rss / 1024 / 1024
            except psutil.Error:
                pass
        self.peak_rss_mb = max(self.peak_rss_mb, total)


def build_mcp(probe: Probe):
    @tool("ask_user", "Ask the human a question. Returns immediately; end your turn after calling it.", {"question": str})
    async def ask_user(args: dict[str, Any]) -> dict[str, Any]:
        probe.mcp_calls.append(args)
        log(f"MCP ask_user: {args}")
        return {"content": [{"type": "text", "text": "Question posted. End your turn now and wait for the answer."}]}

    return create_sdk_mcp_server(name="office", version="0.1.0", tools=[ask_user])


def options(probe: Probe, *, resume: str | None = None, system_prompt: Any = None) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model="haiku",
        cwd=str(probe.workspace),
        resume=resume,
        system_prompt=system_prompt
        or {"type": "preset", "preset": "claude_code", "append": "You are DEV actor 'Minh'. Be brief."},
        setting_sources=[],  # isolation: do not load the user's ~/.claude settings / CLAUDE.md
        tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        permission_mode="default",
        can_use_tool=probe.can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[probe.pre_tool_use])]},
        mcp_servers={"office": build_mcp(probe)},
        strict_mcp_config=True,
        verbatim_prompts=True,
        max_turns=12,
    )


async def run_turn(probe: Probe, opts: ClaudeAgentOptions, prompt: str) -> ResultMessage | None:
    result: ResultMessage | None = None
    stop = asyncio.Event()

    async def sampler() -> None:
        while not stop.is_set():
            probe.sample_rss()
            await asyncio.sleep(0.5)

    task = asyncio.create_task(sampler())
    started = time.monotonic()
    try:
        async with ClaudeSDKClient(options=opts) as client:
            await client.query(prompt)
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            log(f"assistant: {block.text.strip()[:200]}")
                        elif isinstance(block, ToolUseBlock):
                            log(f"tool_use: {block.name}")
                elif isinstance(msg, RateLimitEvent):
                    info = msg.rate_limit_info
                    probe.rate_limits.append(
                        {"status": info.status, "type": info.rate_limit_type, "utilization": info.utilization, "resets_at": info.resets_at}
                    )
                    log(f"rate_limit: {probe.rate_limits[-1]}")
                elif isinstance(msg, SystemMessage) and msg.subtype in ("compact_boundary",):
                    log(f"system {msg.subtype}: {msg.data}")
                elif isinstance(msg, ResultMessage):
                    result = msg
    finally:
        stop.set()
        await task
    log(f"turn wall time {time.monotonic() - started:.1f}s")
    if result:
        log(
            "result: "
            + json.dumps(
                {
                    "subtype": result.subtype,
                    "session_id": result.session_id,
                    "stop_reason": result.stop_reason,
                    "terminal_reason": result.terminal_reason,
                    "cost_usd": result.total_cost_usd,
                    "usage": result.usage,
                    "deferred": result.deferred_tool_use and result.deferred_tool_use.name,
                    "denials": result.permission_denials,
                },
                default=str,
            )[:900]
        )
    return result


async def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="ao-spike-"))
    ws = root / "ws"
    ws.mkdir()
    (root / "outside").mkdir()
    (root / "outside" / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    probe = Probe(ws)
    checks: dict[str, Any] = {}
    try:
        log("== Turn 1: write file, bash (ask), read outside (deny), MCP tool")
        r1 = await run_turn(
            probe,
            options(probe),
            "Do these steps: 1) create notes.txt containing 'hello'. "
            "2) run the shell command `echo spike-ok`. "
            f"3) try to read {(root / 'outside' / 'secret.txt').as_posix()} and report if it was blocked. "
            "4) call the ask_user tool with question 'Token TTL?'. Then stop.",
        )
        checks["file_created"] = (ws / "notes.txt").exists()
        checks["secret_not_leaked"] = "TOP SECRET" not in "\n".join(LOG)
        checks["asked_bash"] = "Bash" in probe.asked
        checks["mcp_called"] = bool(probe.mcp_calls)

        log("== Turn 2: resume session in a new process")
        r2 = await run_turn(probe, options(probe, resume=r1.session_id if r1 else None), "What file did you create and what did the user get asked? One line.")
        checks["resume_same_session"] = bool(r1 and r2 and r2.session_id == r1.session_id)

        log("== Turn 3: defer a 'deploy' command")
        r3 = await run_turn(probe, options(probe, resume=r2.session_id if r2 else None), "Run the shell command `echo deploy-now`.")
        checks["defer_works"] = bool(r3 and r3.deferred_tool_use)

        log("== Turn 3b: resume after defer with an approval grant")
        probe.grant_deploy = True
        calls_before = len(probe.pre_tool_calls)
        r3b = await run_turn(probe, options(probe, resume=r3.session_id if r3 else None), "The user approved. Continue.")
        checks["defer_resume_runs_tool"] = "deploy-now" in "\n".join(LOG[-20:]) and len(probe.pre_tool_calls) > calls_before
        checks["defer_resume_status"] = r3b.terminal_reason if r3b else None

        log("== Turn 4: custom (non-coding) system prompt, no preset")
        r4 = await run_turn(
            probe,
            options(probe, system_prompt="You are Lan, a concise Vietnamese content writer. Reply in Vietnamese."),
            "Viết một câu chào ngắn rồi lưu vào greeting.md.",
        )
        checks["custom_prompt_ok"] = bool(r4 and not r4.is_error) and (ws / "greeting.md").exists()
    finally:
        checks["pre_tool_calls"] = probe.pre_tool_calls
        checks["rate_limits"] = probe.rate_limits
        checks["peak_claude_rss_mb"] = round(probe.peak_rss_mb)
        print("\n=== CHECKS ===")
        print(json.dumps(checks, indent=2, ensure_ascii=False, default=str))
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
