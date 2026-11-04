"""What an actor is doing, in words: run events (tool calls, narration, thinking, errors) → short lines.

Shared by the web activity page and the Telegram progress messages so both read the same way.
"""

from __future__ import annotations

import re
from typing import Any

TOOL_VI = {
    "ticket_get": "đọc ticket", "ticket_comment": "ghi comment", "ticket_set_status": "cập nhật trạng thái",
    "ticket_create": "tạo ticket", "doc_get": "đọc tài liệu", "doc_put": "viết tài liệu", "ask_user": "hỏi bạn",
    "list_actors": "xem đồng nghiệp", "tickets_list": "xem danh sách ticket", "propose_ticket": "đề xuất ticket",
    "chat_create_group": "tạo nhóm", "work_product_add": "ghi kết quả bàn giao", "memory_save": "ghi nhớ",
    "memory_search": "lục trí nhớ", "history_search": "tìm lịch sử", "skill_list": "xem skill", "skill_read": "đọc skill",
    "skill_save": "lưu skill", "ask_colleague": "hỏi đồng nghiệp", "change_notify": "báo thay đổi yêu cầu",
    "automation_save": "lưu automation", "automation_stop": "dừng automation", "automation_list": "xem automation",
    "automation_run": "chạy automation", "db_schema": "xem cấu trúc DB", "db_query": "truy vấn DB", "list_dir": "xem thư mục",
    "todo": "cập nhật việc cần làm", "Read": "đọc", "Write": "ghi file", "Edit": "sửa file", "MultiEdit": "sửa file",
    "Bash": "chạy lệnh", "PowerShell": "chạy lệnh", "Glob": "tìm file", "Grep": "tìm nội dung", "WebFetch": "đọc web",
    "WebSearch": "tìm web", "TodoWrite": "lập danh sách việc", "NotebookEdit": "sửa notebook",
    "source_skip": "bỏ qua nguồn",
}
TARGET_KEYS = ("command", "file_path", "path", "url", "pattern", "title", "key", "ticket", "name", "query", "question", "ref")


def describe_tool(tool: str, args: dict[str, Any] | None) -> str:
    name = re.sub(r"^mcp__\w+?__", "", str(tool or ""))
    verb = TOOL_VI.get(name, name)
    target = next((args[k] for k in TARGET_KEYS if isinstance(args, dict) and args.get(k)), None)
    if name == "TodoWrite" and isinstance(args, dict) and isinstance(args.get("todos"), list):
        todos = args["todos"]
        done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
        current = next((t.get("activeForm") or t.get("content") for t in todos if isinstance(t, dict) and t.get("status") == "in_progress"), None)
        return f"{verb}: {done}/{len(todos)} xong" + (f" · đang: {current}" if current else "")
    if target is None:
        return verb
    return f"{verb}: {_one_line(str(target), 100)}"


def event_line(kind: str, payload: dict[str, Any]) -> tuple[str, str] | None:
    """(icon, text) for one run event, or None when it is not worth showing."""
    if payload.get("subagent"):
        return None
    if kind == "thinking":
        return "💭", _one_line(str(payload.get("text", "")), 400)
    if kind == "text":
        text = _one_line(str(payload.get("text", "")), 300)
        return ("💬", text) if text else None
    if kind == "tool_use":
        return "🔧", describe_tool(str(payload.get("tool", "")), payload.get("input") or {})
    if kind == "tool_result" and payload.get("error"):
        return "⚠️", "lỗi: " + _one_line(str(payload.get("text", "")), 200)
    if kind == "compacted":
        return "🗜", "gọn lại ngữ cảnh để làm tiếp"
    if kind == "change":
        return "🔁", "yêu cầu vừa thay đổi"
    return None


def _one_line(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")
