# P0 spike — Claude Agent SDK (2026-10-10)

Script: `spikes/p0_claude_sdk.py` (`uv run py spikes/p0_claude_sdk.py`). SDK `claude-agent-sdk` 0.2.165, bundled CLI 2.1.294, model `haiku`.

| Check | Result | Notes |
|---|---|---|
| Write file in workspace | ✅ | `cwd` = workspace |
| `PreToolUse` hook sees every tool call | ✅ | Including MCP tools (`mcp__office__ask_user`) |
| Hook `deny` blocks a read outside the workspace | ✅ | The secret did not leak into the log |
| Hook `ask` → `can_use_tool` callback | ✅ | `ctx.decision_reason` carries the hook reason |
| In-process MCP tool (`create_sdk_mcp_server`) | ✅ | |
| Resume a session in a new process | ✅ | Same `session_id`, context retained |
| Hook `defer` stops the run | ✅ | `terminal_reason = "tool_deferred"`, `deferred_tool_use` set |
| Resume after `defer` re-issues the deferred tool | ✅ | `PreToolUse` fires again right away → approval "grant" can allow it |
| Custom system prompt (no `claude_code` preset) | ✅ | Base cache ≈ 5.4k tokens vs ≈ 10.3k with the preset |
| `RateLimitEvent` | ✅ | `status`, `rate_limit_type`, `resets_at`; `utilization` is `None` while `allowed` |
| RAM per run | ~350–410 MB | Child processes of our process only |
| Latency per resumed turn | 5–14 s | Includes CLI startup |

## Design consequences

- **Policy lives in the `PreToolUse` hook** (allow / deny / ask / defer). `can_use_tool` is only a fallback for CLI-originated asks.
- **Long human waits use `defer`** and park the run. Resume re-issues the tool and the hook allows it via a one-time grant `(actor, ticket, tool, input hash)`.
- `setting_sources=[]` isolates actors from the user's own `~/.claude` (settings, CLAUDE.md, memories). Persona comes from the system prompt.
- `verbatim_prompts=True` stops `@path` expansion from chat content.
- `strict_mcp_config=True` stops actors from loading MCP servers we did not grant.
- Non-coding positions use a custom system prompt, which halves the base context.
- Quota: use `RateLimitEvent` for status and reset time. The utilization % only appears near the limit. Dashboard percentages can optionally use the OAuth usage endpoint, the way Paperclip does.
- Default concurrency **2 runs** (~0.8 GB) on a weak machine; configurable.
