from __future__ import annotations

import asyncio

from actoroffice.channels import tg_progress
from actoroffice.core.activity import describe_tool, event_line
from actoroffice.domain.run import Run, RunKind, RunStatus, Usage

from .test_telegram import bind, bot, hire, office, until  # noqa: F401  (fixtures)
from .test_telegram_menu import keyboard, last_text, press, say


def test_events_read_as_short_vietnamese_lines():
    assert describe_tool("mcp__office__doc_put", {"key": "requirements"}) == "viết tài liệu: requirements"
    assert describe_tool("Bash", {"command": "uv run pytest -q"}) == "chạy lệnh: uv run pytest -q"
    todos = {"todos": [{"content": "a", "status": "completed"}, {"content": "b", "activeForm": "Viết test", "status": "in_progress"}]}
    assert describe_tool("TodoWrite", todos) == "lập danh sách việc: 1/2 xong · đang: Viết test"
    assert event_line("thinking", {"text": "Cần đọc   yêu cầu\ntrước"}) == ("💭", "Cần đọc yêu cầu trước")
    assert event_line("tool_result", {"error": True, "text": "exit 1"}) == ("⚠️", "lỗi: exit 1")
    assert event_line("tool_result", {"error": False, "text": "ok"}) is None
    assert event_line("text", {"text": "x", "subagent": True}) is None


async def start_run(office, actor, *, kind=RunKind.TICKET, ticket=None, conversation=None) -> Run:
    run = Run(id=f"run_{kind.value}{actor.name}", actor_id=actor.id, kind=kind, ticket_id=ticket.id if ticket else None,
              conversation_id=conversation)
    return await office.runs.start(run)


async def test_telegram_shows_live_progress_for_a_ticket_run(office, bot, monkeypatch):
    monkeypatch.setattr(tg_progress, "EDIT_EVERY_S", 0.0)
    minh = await hire(office, "dev", "Minh")
    await bind(office, bot)
    dm = await office.chat.dm(minh.id)
    ticket = await office.tickets.create("Xuất báo cáo", assignee_actor_id=minh.id, origin_conversation_id=dm.id)
    run = await start_run(office, minh, ticket=ticket)
    await office.runs.event(run.id, "text", {"text": "Kế hoạch: 1. đọc yêu cầu 2. viết code 3. test"})
    await office.runs.event(run.id, "tool_use", {"tool": "Bash", "input": {"command": "uv run pytest -q"}, "id": "t1"})
    await office.runs.event(run.id, "tool_result", {"id": "t1", "error": True, "text": "1 failed"})
    await until(lambda: any("lỗi: 1 failed" in p["text"] for p in bot.sent("editMessageText")))
    progress = [p for p in bot.sent() if p["text"].startswith("⏳")]
    assert len(progress) == 1 and "Minh" in progress[0]["text"] and ticket.key in progress[0]["text"]
    assert progress[0]["message_thread_id"] == await office.telegram.topic_for(dm.id)
    await office.runs.finish(run.id, RunStatus.SUCCEEDED, usage=Usage(input_tokens=1200, output_tokens=300), cost_usd=0.02)
    await until(lambda: any(p["text"].startswith("✅") for p in bot.sent("editMessageText")))
    final = [p for p in bot.sent("editMessageText") if p["text"].startswith("✅")][-1]["text"]
    assert "xong lượt · 3 bước" in final and "chạy lệnh: uv run pytest -q" in final and "<blockquote expandable>" in final


async def test_a_plain_chat_reply_gets_no_progress_message(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    run = await start_run(office, hoa, kind=RunKind.CHAT, conversation=dm.id)
    await office.runs.event(run.id, "text", {"text": "Chào anh"})
    await office.runs.finish(run.id, RunStatus.SUCCEEDED)
    await asyncio.sleep(0.2)
    assert not [p for p in bot.sent() + bot.sent("editMessageText") if "Hoa</b> đang trả lời" in p["text"]]


async def test_activity_command_lists_running_work_and_can_stop_it(office, bot, monkeypatch):
    minh = await hire(office, "dev", "Minh")
    await bind(office, bot)
    ticket = await office.tickets.create("Xuất báo cáo", assignee_actor_id=minh.id)
    run = await start_run(office, minh, ticket=ticket)
    await office.runs.event(run.id, "tool_use", {"tool": "Read", "input": {"file_path": "spec.md"}, "id": "t1"})
    stopped: list[str] = []

    async def cancel(run_id: str) -> None:
        stopped.append(run_id)

    monkeypatch.setattr(office.runtime, "cancel", cancel, raising=False)
    await say(office, "/activity")
    text = last_text(bot)
    assert "Minh" in text and ticket.key in text and "đọc: spec.md" in text
    await press(office, bot, "Xem Minh")
    assert "đọc: spec.md" in last_text(bot)
    await press(office, bot, "Dừng Minh")
    assert stopped == [run.id]
    await say(office, "/progress")
    assert office.telegram.progress.enabled is False and "Đã tắt" in last_text(bot)
    await say(office, "/progress")
    assert office.telegram.progress.enabled is True
    await say(office, "/menu")
    assert "🔄 Ai đang làm gì" in keyboard(bot)


async def test_recent_runs_say_what_they_were_about(office):
    minh = await hire(office, "dev", "Minh")
    ticket = await office.tickets.create("Xuất báo cáo", assignee_actor_id=minh.id)
    run = await start_run(office, minh, ticket=ticket)
    [row] = await office.runs.recent(limit=5)
    assert (row["id"], row["ticket_key"], row["ticket_title"], row["status"]) == (run.id, ticket.key, "Xuất báo cáo", "running")
