"""Normalized activity: what the AI is doing, as short typed lines every channel shows the same way.

Raw run events come from the runtime (Claude SDK or Kiro ACP, already mapped to Claude-style tool names) and from
the engine (checks, steps, stage changes). `ActivityTracker.classify()` turns them into `Activity` records —
the classifier sits above the runtimes, so both runtimes produce the same activity.

A tool call yields two activities with the same `call_id`: one when it starts (status "running") and one when it
ends ("ok" / "error", with duration). UIs replace the first with the second.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

READ_TOOLS = {"Read", "NotebookRead", "list_dir", "doc_get", "ticket_get", "skill_read", "stage_info"}
SEARCH_TOOLS = {"Grep", "Glob", "history_search", "memory_search", "kb_search", "rule_search", "WebSearch", "tickets_list"}
SOURCE_TOOLS = {"WebFetch", "db_query", "db_schema", "logs_search", "logs_errors", "logs_cluster", "logs_trace", "kafka_peek", "mq_peek", "redis_scan"}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
READ_COMMANDS = re.compile(r"^\s*(cat|head|tail|less|more|type|sed -n|wc|ls|dir|tree|find|git (status|log|show|diff|branch))\b")
SEARCH_COMMANDS = re.compile(r"^\s*(rg|grep|ag|findstr|git grep)\b")
QUIET_OFFICE = {"plan_steps", "step_done"}  # shown through their own "step" events
NARRATION_LIMIT = 300


class Activity(BaseModel):
    kind: str  # narration | thinking | read | search | source | edit | command | office | question | step | check |
    #            deviation | stage | usage | health | error | compacted
    title: str
    status: str = "ok"  # running | ok | error
    call_id: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    refs: dict[str, str] = Field(default_factory=dict)


@dataclass
class _Call:
    tool: str
    args: dict[str, Any]
    started: float
    activity: Activity


@dataclass
class ActivityTracker:
    calls: dict[str, dict[str, _Call]] = field(default_factory=dict)  # run_id → tool_use_id → call

    def forget(self, run_id: str) -> None:
        self.calls.pop(run_id, None)

    def classify(self, run_id: str, kind: str, payload: dict[str, Any]) -> Activity | None:
        if payload.get("subagent"):
            return None
        if kind == "text":
            text = _one_line(str(payload.get("text", "")), NARRATION_LIMIT)
            return Activity(kind="narration", title=text) if text else None
        if kind == "thinking":
            text = _one_line(str(payload.get("text", "")), NARRATION_LIMIT)
            return Activity(kind="thinking", title=text) if text else None
        if kind == "tool_use":
            return self._start(run_id, str(payload.get("tool") or ""), payload.get("input") or {}, payload.get("id"))
        if kind == "tool_result":
            return self._end(run_id, payload)
        if kind == "check":
            status = str(payload.get("status"))
            icon = {"passed": "pass", "failed": "FAIL", "skipped": "bỏ qua"}.get(status, status)
            return Activity(
                kind="check",
                title=f"Check {payload.get('id')}: {icon} — {_one_line(str(payload.get('summary', '')), 160)}",
                status="error" if status == "failed" else "ok",
                fields={"id": payload.get("id"), "result": status},
                refs={"output_ref": payload["output_ref"]} if payload.get("output_ref") else {},
            )
        if kind == "steps":
            steps = payload.get("steps") or []
            done = sum(1 for s in steps if s.get("done"))
            current = next((s.get("text") for s in steps if not s.get("done")), None)
            title = f"Các bước {done}/{len(steps)}" + (f" · tiếp: {current}" if current else " · xong hết")
            return Activity(kind="step", title=title, fields={"done": done, "total": len(steps)})
        if kind == "deviation":
            return Activity(kind="deviation", title=f"Lệch plan: {_one_line(str(payload.get('what', '')), 160)}", fields=payload)
        if kind == "stage":
            return Activity(kind="stage", title=f"Giai đoạn: {payload.get('from') or '-'} → {payload.get('to')}", fields=payload)
        if kind == "turn":
            usage = payload.get("usage") or {}
            tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
            status = str(payload.get("status"))
            return Activity(
                kind="usage",
                title=f"Hết lượt ({status}) · {tokens / 1000:.1f}k tok" + (f" · ${payload['cost_usd']:.3f}" if payload.get("cost_usd") else ""),
                status="error" if status in ("error", "max_turns") else "ok",
                fields={"tokens": tokens, "status": status},
            )
        if kind == "health":
            return Activity(kind="health", title=str(payload.get("title") or payload.get("state")), status="error", fields=payload)
        if kind == "compacted":
            return Activity(kind="compacted", title="Gọn lại ngữ cảnh để làm tiếp")
        if kind == "change":
            return Activity(kind="narration", title="Yêu cầu vừa thay đổi")
        return None

    def _start(self, run_id: str, tool: str, args: dict[str, Any], call_id: Any) -> Activity | None:
        name = re.sub(r"^mcp__\w+?__", "", tool)
        activity = describe_call(name, args)
        if activity is None:
            return None
        activity.status = "running"
        activity.call_id = str(call_id) if call_id else None
        if call_id:
            self.calls.setdefault(run_id, {})[str(call_id)] = _Call(name, args, time.monotonic(), activity)
        return activity

    def _end(self, run_id: str, payload: dict[str, Any]) -> Activity | None:
        call = self.calls.get(run_id, {}).pop(str(payload.get("id")), None)
        if call is None:
            if payload.get("error"):
                return Activity(kind="error", title="Lỗi: " + _one_line(str(payload.get("text", "")), 200), status="error")
            return None
        done = call.activity.model_copy(deep=True)
        done.status = "error" if payload.get("error") else "ok"
        seconds = time.monotonic() - call.started
        done.fields["duration_s"] = round(seconds, 1)
        text = str(payload.get("text") or "")
        if done.kind == "command":
            suffix = "lỗi" if payload.get("error") else "xong"
            done.title = f"{done.title} → {suffix} ({_duration(seconds)})"
            done.fields["output_tail"] = text[-600:]
        elif done.kind == "search" and not payload.get("error"):
            hits = _hits(text)
            if hits is not None:
                done.title += f" → {hits} kết quả"
                done.fields["hits"] = hits
        elif payload.get("error"):
            done.title += " → lỗi: " + _one_line(text, 160)
        return done


def describe_call(tool: str, args: dict[str, Any]) -> Activity | None:
    if tool in QUIET_OFFICE:
        return None
    path = str(args.get("file_path") or args.get("path") or args.get("notebook_path") or "")
    if tool in EDIT_TOOLS:
        added, removed = _line_delta(tool, args)
        return Activity(kind="edit", title=f"Sửa `{_short_path(path)}` +{added} −{removed}", fields={"path": path, "added": added, "removed": removed})
    if tool in READ_TOOLS:
        target = path or str(args.get("key") or args.get("name") or args.get("ticket") or "")
        verb = {"doc_get": "Đọc tài liệu", "ticket_get": "Đọc ticket", "skill_read": "Đọc skill", "list_dir": "Xem thư mục", "stage_info": "Xem giai đoạn"}.get(tool, "Đọc")
        return Activity(kind="read", title=f"{verb} `{_short_path(target)}`" if target else verb, fields={"tool": tool, "path": target})
    if tool in SEARCH_TOOLS:
        query = str(args.get("pattern") or args.get("query") or args.get("glob") or "")
        return Activity(kind="search", title=f"Tìm `{_one_line(query, 80)}`" + (f" trong `{_short_path(path)}`" if path else ""), fields={"tool": tool, "query": query})
    if tool in SOURCE_TOOLS:
        target = str(args.get("url") or args.get("sql") or args.get("query") or args.get("topic") or args.get("service") or "")
        return Activity(kind="source", title=f"{tool}: {_one_line(target, 100)}", fields={"tool": tool, "target": target})
    if tool in SHELL_TOOLS:
        command = str(args.get("command") or "")
        if READ_COMMANDS.match(command):
            return Activity(kind="read", title=f"`{_one_line(command, 90)}`", fields={"command": command})
        if SEARCH_COMMANDS.match(command):
            return Activity(kind="search", title=f"`{_one_line(command, 90)}`", fields={"command": command})
        return Activity(kind="command", title=f"`{_one_line(command, 90)}`", fields={"command": command})
    if tool == "ask_human":
        roles = "/".join(args.get("roles") or [])
        return Activity(kind="question", title=f"Hỏi {roles}: {_one_line(str(args.get('question', '')), 120)}", fields={"roles": args.get("roles")})
    if tool in ("ask_user", "ask_colleague"):
        return Activity(kind="question", title=f"Hỏi: {_one_line(str(args.get('question', '')), 120)}")
    if tool in ("TodoWrite", "todo"):
        todos = args.get("todos") or []
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        return Activity(kind="step", title=f"Danh sách việc {done}/{len(todos)}")
    if tool == "dossier_update":
        return Activity(kind="office", title=f"Cập nhật dossier: {args.get('section')}", fields={"section": args.get("section")})
    if tool == "doc_put":
        return Activity(kind="office", title=f"Viết tài liệu `{args.get('key')}`", fields={"key": args.get("key")})
    if tool == "stage_complete":
        return Activity(kind="stage", title="Nộp giai đoạn để kiểm tra", fields={})
    if tool == "stage_reopen":
        return Activity(kind="stage", title=f"Quay lại giai đoạn {args.get('stage')}", fields={"to": args.get("stage")})
    from actoroffice.core.activity import describe_tool

    return Activity(kind="office", title=describe_tool(tool, args), fields={"tool": tool})


def _line_delta(tool: str, args: dict[str, Any]) -> tuple[int, int]:
    if tool == "Write":
        return _lines(args.get("content")), 0
    if tool == "Edit":
        return _lines(args.get("new_string")), _lines(args.get("old_string"))
    if tool == "MultiEdit":
        edits = args.get("edits") or []
        return sum(_lines(e.get("new_string")) for e in edits), sum(_lines(e.get("old_string")) for e in edits)
    return 0, 0


def _lines(text: Any) -> int:
    return len(str(text).splitlines()) if text else 0


def _hits(text: str) -> int | None:
    m = re.match(r"^\s*Found (\d+)", text)
    if m:
        return int(m.group(1))
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines or text.startswith("No "):
        return 0
    return len(lines)


def _short_path(path: str, keep: int = 3) -> str:
    parts = re.split(r"[\\/]+", path.strip())
    return "/".join(parts[-keep:]) if len(parts) > keep else path


def _duration(seconds: float) -> str:
    return f"{seconds:.0f}s" if seconds < 90 else f"{seconds / 60:.1f} phút"


def _one_line(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")
