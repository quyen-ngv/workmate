from __future__ import annotations

import asyncio
from typing import Any

from actoroffice.domain.knowledge import SkillRef, SkillScope
from actoroffice.domain.work import ProjectAccess

from .test_telegram import GROUP, FakeBot, bind, bot, hire, office, tg_message, until  # noqa: F401  (fixtures)


def keyboard(bot: FakeBot) -> dict[str, str]:
    """Buttons of the latest message or edit that has a keyboard: text → callback data."""
    for method, params in reversed(bot.calls):
        markup = params.get("reply_markup")
        if method in ("sendMessage", "editMessageText") and markup and markup.get("inline_keyboard"):
            return {b["text"]: b.get("callback_data", b.get("url", "")) for row in markup["inline_keyboard"] for b in row}
    return {}


def press_button(bot: FakeBot, label_part: str) -> str:
    """The newest button whose label contains `label_part` (the user can scroll up to an older menu)."""
    for method, params in reversed(bot.calls):
        markup = params.get("reply_markup") or {}
        if method not in ("sendMessage", "editMessageText"):
            continue
        for row in markup.get("inline_keyboard") or []:
            for b in row:
                if label_part in b["text"] and "callback_data" in b:
                    return b["callback_data"]
    raise AssertionError(f"no button with {label_part!r}; latest: {list(keyboard(bot))}")


async def press(office: Any, bot: FakeBot, label_part: str, thread: int | None = None) -> str:
    data = press_button(bot, label_part)
    update = {"update_id": 9, "callback_query": {"id": "cb", "data": data, "message": {"chat": {"id": GROUP}, "message_id": 555, "message_thread_id": thread}}}
    await office.telegram.handle_update(update)
    return bot.sent("answerCallbackQuery")[-1]["text"]


async def say(office: Any, text: str, thread: int | None = None) -> None:
    await office.telegram.handle_update(tg_message(text, thread))


def last_text(bot: FakeBot) -> str:
    return [p for m, p in bot.calls if m in ("sendMessage", "editMessageText")][-1]["text"]


# ---- create things --------------------------------------------------------------------------------


async def test_new_ticket_form_asks_project_assignee_title_and_description(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    project = await office.projects.create("GoRoute", path=None, access=ProjectAccess.READ_ONLY)
    await say(office, "/new_ticket")
    await press(office, bot, "GoRoute")
    await press(office, bot, "Hoa")
    await say(office, "Báo cáo doanh thu tuần")
    assert office.telegram.menus.wizards[None].expects == "description"
    await press(office, bot, "Bỏ qua")
    [ticket] = await office.tickets.list()
    assert (ticket.title, ticket.assignee_actor_id, ticket.project_id, ticket.description) == ("Báo cáo doanh thu tuần", hoa.id, project.id, "")
    assert f"Đã tạo <b>{ticket.key}</b>" in last_text(bot) and None not in office.telegram.menus.wizards


async def test_new_group_form_toggles_members_then_asks_the_name(office, bot):
    hoa, minh = await hire(office, "ba", "Hoa"), await hire(office, "dev", "Minh")
    await hire(office, "qa", "Thu")
    await bind(office, bot)
    await say(office, "/new_group")
    await press(office, bot, "Hoa")
    await press(office, bot, "Minh")
    await press(office, bot, "Xong (2")
    await say(office, "Nhóm báo cáo")
    groups = [c for c in await office.chat.list() if c["kind"] == "group"]
    assert [g["title"] for g in groups] == ["Nhóm báo cáo"]
    assert {m["id"] for m in groups[0]["members"] if m["kind"] == "actor"} == {hoa.id, minh.id}
    assert "👥 Nhóm báo cáo" in [p["name"] for p in bot.sent("createForumTopic")]


async def test_new_project_and_hire_forms(office, bot):
    await bind(office, bot)
    await say(office, "/new_project")
    await say(office, "Kho dữ liệu")
    await press(office, bot, "Chỉ đọc")
    await press(office, bot, "Bỏ qua")
    assert "Kho dữ liệu" in [p.name for p in await office.projects.list()]
    await say(office, "/hire")
    await press(office, bot, "Business Analyst")
    await say(office, "Hai Hoa")  # must be one word
    assert office.telegram.menus.wizards[None].expects == "name"
    await say(office, "Hoa")
    assert (await office.org.find_actor("Hoa")) is not None
    assert any(p["name"].startswith("💬 Hoa · ") for p in bot.sent("createForumTopic"))


async def test_cancel_stops_a_form_and_text_goes_back_to_the_chat(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    thread = await office.telegram.topic_for(dm.id)
    await say(office, "/new_ticket", thread)
    await press(office, bot, "Hoa", thread)
    await say(office, "/cancel", thread)
    await say(office, "chào em", thread)
    assert not await office.tickets.list()
    assert (await office.chat.messages(dm.id))[-1].body == "chào em"


# ---- tickets ----------------------------------------------------------------------------------------


async def test_ticket_detail_documents_comment_reassign_and_cancel(office, bot):
    hoa, minh = await hire(office, "ba", "Hoa"), await hire(office, "dev", "Minh")
    await bind(office, bot)
    ticket = await office.tickets.create("Nghỉ phép", description="**Mục tiêu**: xin nghỉ", assignee_actor_id=hoa.id)
    await office.tickets.put_document(ticket.id, "requirements", "# Yêu cầu\n- US-1", author="Hoa", base_revision=None)
    await say(office, "/tickets")
    await press(office, bot, ticket.key)
    detail = last_text(bot)
    assert "<b>Mục tiêu</b>" in detail and "requirements" in " ".join(keyboard(bot))
    await press(office, bot, "requirements")
    assert "US-1" in last_text(bot)
    await press(office, bot, "Bình luận")  # the ticket's buttons are still the latest keyboard
    await say(office, "Thêm loại nghỉ cưới")
    assert any(c.body == "Thêm loại nghỉ cưới" and c.author_kind == "user" for c in await office.tickets.comments(ticket.id))
    await press(office, bot, "Giao lại")
    await press(office, bot, "Minh")
    assert (await office.tickets.get(ticket.key)).assignee_actor_id == minh.id
    await press(office, bot, "Huỷ ticket")
    assert (await office.tickets.get(ticket.key)).status.value != "cancelled"  # asks first
    await press(office, bot, "Huỷ ticket này")
    assert (await office.tickets.get(ticket.key)).status.value == "cancelled"


async def test_requirement_change_from_telegram_reaches_the_tree(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    ticket = await office.tickets.create("Nghỉ phép", assignee_actor_id=hoa.id)
    await say(office, "/tickets")
    await press(office, bot, ticket.key)
    await press(office, bot, "Thay đổi yêu cầu")
    await say(office, "Bỏ nghỉ không lương")
    assert any(c.author_kind == "change" for c in await office.tickets.comments(ticket.id))
    assert "Đã ghi nhận thay đổi" in last_text(bot)


# ---- automations, team, skills, inbox, usage ---------------------------------------------------------


async def test_automation_menu_runs_and_toggles(office, bot):
    an = await hire(office, "assistant", "An")
    await bind(office, bot)
    dm = await office.chat.dm(an.id)
    auto = await office.automations.save(
        "nhac-hop", steps=[{"type": "notify", "text": "Họp lúc 9h"}], description="Nhắc họp", owner_actor_id=an.id,
        conversation_id=dm.id, schedule="daily@08:00",
    )
    await say(office, "/automations")
    await press(office, bot, "nhac-hop")
    assert "Họp lúc 9h" in last_text(bot)
    assert "Đã bắt đầu chạy" in await press(office, bot, "Chạy ngay")
    await asyncio.sleep(0.3)
    assert any(m.body == "Họp lúc 9h" for m in await office.chat.messages(dm.id))
    await press(office, bot, "Tắt")
    assert (await office.automations.get(auto.id)).status.value == "disabled"
    await press(office, bot, "Bật")
    assert (await office.automations.get(auto.id)).status.value == "enabled"


async def test_team_skills_inbox_and_usage(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    await office.memory.add(hoa, "Anh thích báo cáo ngắn", created_by="user")
    await say(office, "/team")
    await press(office, bot, "Hoa")
    assert "1 memory" in last_text(bot) and any(t == "💬 Mở chat" for t in keyboard(bot))
    await press(office, bot, "Memory")
    assert "Anh thích báo cáo ngắn" in last_text(bot)
    await press(office, bot, "Tạm dừng")
    assert (await office.org.get_actor(hoa.id)).status.value == "paused"
    await office.skills.save(SkillRef(scope=SkillScope.SHARED, name="viet-bao-cao"), "Cách viết báo cáo", "1. Mở đầu bằng kết luận", author="user")
    await say(office, "/skills")
    await press(office, bot, "viet-bao-cao")
    assert "Mở đầu bằng kết luận" in last_text(bot)
    await say(office, "/inbox")
    assert "Không có việc nào" in last_text(bot)
    await say(office, "/usage")
    assert "Hôm nay" in last_text(bot) and "7 ngày" in last_text(bot)
    await say(office, "/menu")
    assert "🎫 Tạo ticket" in keyboard(bot)


async def test_rename_and_suggested_name_from_telegram(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    await say(office, "/team")
    await press(office, bot, "Hoa")
    await press(office, bot, "Đổi tên")
    await say(office, "Olivia")
    assert (await office.org.get_actor(hoa.id)).name == "Olivia"
    await until(lambda: any("💬 Olivia" in p["name"] for p in bot.sent("editForumTopic")))  # the DM topic follows
    await say(office, "/hire")
    await press(office, bot, "Developer")
    await press(office, bot, "Dùng tên Emma")
    assert await office.org.find_actor("Emma") is not None
