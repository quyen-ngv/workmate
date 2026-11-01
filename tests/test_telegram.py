from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from actoroffice.app import Office
from actoroffice.channels import tg_format as fmt
from actoroffice.channels.telegram import TelegramBridge, TelegramError
from actoroffice.channels.tg_progress import Progress
from actoroffice.config import Settings
from actoroffice.domain.work import InteractionKind, ProjectAccess

from .fakes import FakeRuntime

GROUP = -100123


class FakeBot:
    """Records Bot API calls; topics and messages get increasing ids."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.documents: list[tuple[int | None, str]] = []
        self.failing: set[str] = set()  # methods that answer with a Telegram error (e.g. the bot was kicked)
        self._next = 100

    async def call(self, method: str, **params: Any) -> Any:
        if method == "getUpdates":
            await asyncio.sleep(0.05)
            return []
        self.calls.append((method, params))
        if method in self.failing:
            raise TelegramError("Forbidden: bot was kicked from the supergroup chat", 403)
        self._next += 1
        if method == "createForumTopic":
            return {"message_thread_id": self._next, "name": params["name"]}
        if method == "sendMessage":
            return {"message_id": self._next}
        return True

    async def send_document(self, chat_id: int, thread_id: int | None, path: Any, caption: str = "") -> Any:
        self.documents.append((thread_id, path.name))
        return {"message_id": 1}

    async def close(self) -> None:
        pass

    def sent(self, method: str = "sendMessage") -> list[dict[str, Any]]:
        return [p for m, p in self.calls if m == method]


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=FakeRuntime(scripts=[]))
    await o.open()
    await o.install_default_positions(skills=False)
    await o.start_notifier()
    yield o
    await o.close()


@pytest.fixture
async def bot(office) -> AsyncIterator[FakeBot]:
    fake = FakeBot()
    office.telegram = TelegramBridge(office, fake)
    ready = asyncio.Event()
    task = asyncio.create_task(office.telegram.run(ready))
    await ready.wait()
    yield fake
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def until(check: Any, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def tg_message(text: str, thread: int | None = None, *, chat: int = GROUP, reply_to: int | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {
        "message_id": 9000,
        "from": {"id": 1, "is_bot": False},
        "chat": {"id": chat, "type": "supergroup", "is_forum": True},
        "text": text,
    }
    if thread:
        msg.update(message_thread_id=thread, is_topic_message=True)
    if reply_to:
        msg["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": 1, "message": msg}


async def hire(office: Office, position: str, name: str):
    return await office.org.hire((await office.org.find_position(position)).id, name)


async def bind(office: Office, bot: FakeBot) -> None:
    await office.telegram.handle_update(tg_message("/start"))
    assert office.telegram.chat_id == GROUP


# ---- binding and topics ---------------------------------------------------------------------------


async def test_first_forum_group_binds_and_gets_a_topic_per_actor(office, bot):
    await hire(office, "ba", "Hoa")
    await hire(office, "dev", "Minh")
    private = tg_message("hello")
    private["message"]["chat"] = {"id": 7, "type": "private"}
    await office.telegram.handle_update(private)
    assert office.telegram.chat_id is None  # a private chat never binds the office
    await bind(office, bot)
    names = [p["name"] for p in bot.sent("createForumTopic")]
    assert any(n.startswith("💬 Hoa · ") for n in names) and any(n.startswith("💬 Minh · ") for n in names)
    assert any("Đã kết nối" in p["text"] for p in bot.sent())
    await office.telegram.handle_update(tg_message("/sync"))
    assert len(bot.sent("createForumTopic")) == 2  # idempotent


async def test_other_groups_are_ignored_once_bound(office, bot):
    await hire(office, "ba", "Hoa")
    await bind(office, bot)
    before = len(bot.calls)
    await office.telegram.handle_update(tg_message("hi", 5, chat=-999))
    assert len(bot.calls) == before


async def test_only_start_binds_a_group(office, bot):
    await office.telegram.handle_update(tg_message("tin nhắn cũ", 5))
    assert office.telegram.chat_id is None and not bot.calls
    await office.telegram.handle_update(tg_message("/start@OfficeBot"))
    assert office.telegram.chat_id == GROUP


async def test_start_in_another_group_explains_how_to_switch(office, bot):
    await bind(office, bot)
    before = len(bot.calls)
    await office.telegram.handle_update(tg_message("/start", 5, chat=-999))
    reply = bot.calls[before:]
    assert len(reply) == 1 and reply[0][0] == "sendMessage"
    assert reply[0][1]["chat_id"] == -999 and reply[0][1]["message_thread_id"] == 5 and "Cài đặt" in reply[0][1]["text"]
    assert office.telegram.chat_id == GROUP


# ---- switching to a new group ---------------------------------------------------------------------

NEW_GROUP = -100456


async def bound_state(office: Office) -> str | None:
    row = await office.db.fetchone("SELECT value FROM telegram_state WHERE key = 'chat_id'")
    return row["value"] if row else None


async def test_ac3_unbind_notifies_old_group_once_and_forgets_it(office, bot):
    await bind(office, bot)
    before = len(bot.calls)
    await office.telegram.unbind()
    notice = bot.calls[before:]
    assert len(notice) == 1 and notice[0][0] == "sendMessage"
    assert notice[0][1]["chat_id"] == GROUP and notice[0][1].get("message_thread_id") is None and "chuyển sang" in notice[0][1]["text"]
    assert office.telegram.chat_id is None and await bound_state(office) is None


async def test_ac4_unbind_succeeds_when_old_group_is_gone(office, bot):
    await bind(office, bot)
    bot.failing.add("sendMessage")
    await office.telegram.unbind()
    assert office.telegram.chat_id is None and await bound_state(office) is None


async def test_ac5_unbound_bot_stays_silent(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    thread = await office.telegram.topic_for(dm.id)
    await office.telegram.unbind()
    before = len(bot.calls)
    for chat in (GROUP, NEW_GROUP):
        await office.telegram.handle_update(tg_message("chào", thread, chat=chat))
        await office.telegram.handle_update(tg_message("/help", chat=chat))
    await office.chat.post_actor(dm.id, hoa, "tin khi đang chờ", wake=False)
    await asyncio.sleep(0.2)
    assert bot.calls[before:] == []
    assert all(m.body != "chào" for m in await office.chat.messages(dm.id))


async def test_ac6_start_in_new_group_binds_it_and_recreates_topics(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    group = await office.chat.create_group("Dự án A", [hoa.id])
    await office.telegram.unbind()
    before = len(bot.calls)
    await office.telegram.handle_update(tg_message("/start", chat=NEW_GROUP))
    assert office.telegram.chat_id == NEW_GROUP and await bound_state(office) == str(NEW_GROUP)
    calls = bot.calls[before:]
    assert any(m == "sendMessage" and p["chat_id"] == NEW_GROUP and "Đã kết nối" in p["text"] for m, p in calls)
    topics = [p for m, p in calls if m == "createForumTopic"]
    assert all(p["chat_id"] == NEW_GROUP for p in topics)
    names = [p["name"] for p in topics]
    assert any(n.startswith("💬 Hoa · ") for n in names) and any(n == f"👥 {group.title}" for n in names)
    dm = await office.chat.dm(hoa.id)
    thread = await office.telegram.topic_for(dm.id)
    await office.chat.post_actor(dm.id, hoa, "tin ở group mới", wake=False)
    await until(lambda: any("tin ở group mới" in p["text"] for p in bot.sent()))
    msg = next(p for p in bot.sent() if "tin ở group mới" in p["text"])
    assert msg["chat_id"] == NEW_GROUP and msg["message_thread_id"] == thread


async def test_ac7_start_in_old_group_rebinds_and_reuses_topics(office, bot):
    await hire(office, "ba", "Hoa")
    await bind(office, bot)
    topics = len(bot.sent("createForumTopic"))
    await office.telegram.unbind()
    await bind(office, bot)
    assert len(bot.sent("createForumTopic")) == topics
    assert await bound_state(office) == str(GROUP)


async def test_ac9_old_cards_stay_after_switch(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    item = await office.interactions.create(
        InteractionKind.QUESTION, {"asked_by": "Hoa", "question": "Gửi lúc mấy giờ?", "options": ["8h", "9h"], "conversation_id": dm.id}, actor_id=hoa.id
    )
    await until(lambda: any("Gửi lúc mấy giờ" in p["text"] for p in bot.sent()))
    await office.telegram.unbind()
    press = {"update_id": 5, "callback_query": {"id": "cb-wait", "data": f"i:{item.id}:o0", "message": {"chat": {"id": GROUP}}}}
    await office.telegram.handle_update(press)
    assert bot.sent("answerCallbackQuery")[-1] == {"callback_query_id": "cb-wait"}  # silent while waiting
    await office.telegram.handle_update(tg_message("/start", chat=NEW_GROUP))
    await asyncio.sleep(0.1)
    assert not any("Gửi lúc mấy giờ" in p["text"] for p in bot.sent() if p["chat_id"] == NEW_GROUP)
    press["callback_query"]["id"] = "cb-old"
    await office.telegram.handle_update(press)
    assert (await office.interactions.get(item.id)).status.value == "pending"  # a press in the old group does nothing
    await office.answer(item.id, {"answer": "8h"})
    assert (await office.interactions.get(item.id)).status.value == "answered"


async def test_ac9_switch_forgets_live_progress(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    await office.chat.post_actor(dm.id, hoa, "trả lời ở group cũ", meta={"run_id": "run_old"}, wake=False)
    await until(lambda: "run_old" in office.telegram._runs)
    flush = asyncio.create_task(asyncio.sleep(30))
    office.telegram.progress.runs["run_live"] = Progress("run_live", 5, "Hoa", chat=False, message_id=777, flush=flush)
    await office.telegram.unbind()
    await asyncio.sleep(0)
    assert flush.cancelled() and not office.telegram.progress.runs and not office.telegram._runs
    await office.telegram.handle_update(tg_message("/start", chat=NEW_GROUP))
    office.bus.publish("run.finished", run_id="run_old", status="succeeded", error=None, run={"usage": {}, "stats": {}})
    office.bus.publish("run.finished", run_id="run_live", status="succeeded", error=None, run={"usage": {}, "stats": {}})
    await asyncio.sleep(0.2)
    assert not bot.sent("editMessageText")  # message ids of the old group are never edited in the new one


# ---- messages both ways ---------------------------------------------------------------------------


async def test_user_text_in_a_topic_goes_to_that_conversation_without_echo(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    thread = await office.telegram.topic_for(dm.id)
    sent_before = len(bot.sent())
    await office.telegram.handle_update(tg_message("Em làm báo cáo tuần nhé", thread))
    messages = await office.chat.messages(dm.id)
    assert messages[-1].author_kind == "user" and messages[-1].body == "Em làm báo cáo tuần nhé"
    assert await office.wakes.pending()  # Hoa is woken, as on the web
    await asyncio.sleep(0.2)
    assert len(bot.sent()) == sent_before  # not mirrored back to Telegram


async def test_actor_reply_has_a_header_markdown_and_later_the_usage_line(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    thread = await office.telegram.topic_for(dm.id)
    await office.chat.post_actor(dm.id, hoa, "Em cần **3 thông tin**:\n- hạn chót\n- `format`", meta={"run_id": "run_x"}, wake=False)
    await until(lambda: any(p.get("message_thread_id") == thread and "Hoa" in p["text"] for p in bot.sent()))
    msg = [p for p in bot.sent() if p.get("message_thread_id") == thread][-1]
    assert msg["parse_mode"] == "HTML" and "<b>3 thông tin</b>" in msg["text"] and "• hạn chót" in msg["text"] and "<code>format</code>" in msg["text"]
    run = {"usage": {"input_tokens": 1000, "output_tokens": 500}, "stats": {"files_changed": 2, "lines_added": 30, "lines_removed": 4, "commands": 3},
           "cost_usd": 0.0123, "started_at": "2026-10-11T01:00:00+00:00", "finished_at": "2026-10-11T01:01:05+00:00"}
    office.bus.publish("run.finished", run_id="run_x", status="succeeded", error=None, run=run)
    await until(lambda: bot.sent("editMessageText"))
    edited = bot.sent("editMessageText")[-1]["text"]
    assert "📄 2 file · +30 −4 dòng" in edited and "⌘ 3 lệnh" in edited and "⏱ 1 phút 5s" in edited and "1.5k tok" in edited


async def test_web_messages_are_shown_as_from_the_web(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    await office.chat.post_user(dm.id, "nhắn từ web")
    await until(lambda: any("nhắn từ web" in p["text"] for p in bot.sent()))
    assert any("(trên web)" in p["text"] for p in bot.sent() if "nhắn từ web" in p["text"])


# ---- decision cards -------------------------------------------------------------------------------


async def test_ticket_proposal_card_button_creates_the_ticket_and_updates_the_card(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    item = await office.interactions.create(
        InteractionKind.CONFIRM_TICKET,
        {"title": "Báo cáo tuần", "description": "CSV theo vùng", "assignee": "Hoa", "assignee_id": hoa.id,
         "priority": "medium", "proposed_by": "Hoa", "conversation_id": dm.id},
        actor_id=hoa.id,
    )
    await until(lambda: any(p.get("reply_markup") for p in bot.sent()))
    card = [p for p in bot.sent() if p.get("reply_markup")][-1]
    buttons = [b["callback_data"] for row in card["reply_markup"]["inline_keyboard"] for b in row]
    assert f"i:{item.id}:y" in buttons and "Báo cáo tuần" in card["text"]
    await office.telegram.handle_update({"update_id": 2, "callback_query": {"id": "cb1", "data": f"i:{item.id}:y", "message": {"chat": {"id": GROUP}}}})
    tickets = await office.tickets.list()
    assert [t.title for t in tickets] == ["Báo cáo tuần"]
    await until(lambda: any(tickets[0].key in p["text"] for p in bot.sent("editMessageText")))
    # a second press (or a press after answering on the web) does nothing
    await office.telegram.handle_update({"update_id": 3, "callback_query": {"id": "cb2", "data": f"i:{item.id}:y", "message": {"chat": {"id": GROUP}}}})
    assert len(await office.tickets.list()) == 1
    assert "đã được trả lời" in bot.sent("answerCallbackQuery")[-1]["text"]


async def test_a_question_is_answered_by_replying_to_its_card(office, bot):
    hoa = await hire(office, "ba", "Hoa")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    item = await office.interactions.create(
        InteractionKind.QUESTION, {"asked_by": "Hoa", "question": "Gửi lúc mấy giờ?", "options": ["8h", "9h"], "conversation_id": dm.id}, actor_id=hoa.id
    )
    await until(lambda: any("Gửi lúc mấy giờ" in p["text"] for p in bot.sent()))
    row = await office.db.fetchone("SELECT message_id FROM telegram_cards WHERE interaction_id = ?", (item.id,))
    thread = await office.telegram.topic_for(dm.id)
    await office.telegram.handle_update(tg_message("7h30 nhé", thread, reply_to=row["message_id"]))
    answered = await office.interactions.get(item.id)
    assert answered.status.value == "answered" and answered.response == {"answer": "7h30 nhé"}
    assert all(m.body != "7h30 nhé" for m in await office.chat.messages(dm.id))  # an answer, not a chat message


async def test_tool_approval_buttons_offer_always_allow_rules(office, bot):
    minh = await hire(office, "dev", "Minh")
    await bind(office, bot)
    item = await office.interactions.create(
        InteractionKind.APPROVE_TOOL,
        {"actor": "Minh", "tool": "Bash", "target": "uv run pytest", "reason": "chạy test", "input_hash": "h1",
         "suggestions": [{"rule": "Bash(uv run pytest*)", "scope": "prefix"}]},
        actor_id=minh.id,
    )
    await until(lambda: any(p.get("reply_markup") for p in bot.sent()))
    await office.telegram.handle_update({"update_id": 4, "callback_query": {"id": "cb", "data": f"i:{item.id}:r0", "message": {"chat": {"id": GROUP}}}})
    assert bot.sent("answerCallbackQuery")[-1]["text"] == "Đã ghi nhận"
    assert "Bash(uv run pytest*)" in (await office.org.get_actor(minh.id)).allow_rules


# ---- deliverables -----------------------------------------------------------------------------------


async def test_done_report_attaches_files_from_the_workspace_only(office, bot, tmp_path):
    hoa = await hire(office, "writer", "Lan")
    await bind(office, bot)
    dm = await office.chat.dm(hoa.id)
    ticket = await office.tickets.create("Bài đăng", assignee_actor_id=hoa.id, origin_conversation_id=dm.id)
    ws = office.settings.workspaces_dir / ticket.key
    ws.mkdir(parents=True)
    (ws / "bai-viet.md").write_text("Nội dung", encoding="utf-8")
    outside = tmp_path / "secret.txt"
    outside.write_text("x", encoding="utf-8")
    await office.tickets.add_work_product(ticket.id, "file", "bai-viet.md")
    await office.tickets.add_work_product(ticket.id, "file", str(outside))
    await office.chat.post_actor(dm.id, hoa, f"✅ {ticket.key} xong", meta={"ticket_key": ticket.key, "report": True}, wake=False)
    await until(lambda: bot.documents)
    await asyncio.sleep(0.1)
    assert [name for _thread, name in bot.documents] == ["bai-viet.md"]


# ---- formatting -----------------------------------------------------------------------------------


def test_markdown_to_telegram_html():
    out = fmt.md_to_html("# Tiêu đề\nGiá <39.000đ> & **mua 2 tặng 1**, xem [link](https://x.vn/a?b=1)\n\n```py\nprint(1<2)\n```\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<b>Tiêu đề</b>" in out and "&lt;39.000đ&gt; &amp;" in out and "<b>mua 2 tặng 1</b>" in out
    assert '<a href="https://x.vn/a?b=1">link</a>' in out and "<pre>print(1&lt;2)</pre>" in out and "<pre>| a | b |" in out
    assert fmt.md_to_html("file_name_here và snake_case") == "file_name_here và snake_case"


def test_split_keeps_pieces_under_the_limit():
    text = "\n\n".join(f"Đoạn {i} " + "x" * 300 for i in range(40))
    pieces = fmt.split(text, 1000)
    assert all(len(p) <= 1000 for p in pieces) and "".join(pieces).replace("\n", "") == text.replace("\n", "")


async def test_bot_api_retries_rate_limits_and_never_leaks_the_token(monkeypatch):
    import httpx

    from actoroffice.channels.telegram import BotApi, TelegramError

    token = "123456789:" + "A" * 35
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if len(seen) == 1:
            return httpx.Response(429, json={"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 0}})
        if request.url.path.endswith("/getMe"):
            return httpx.Response(200, json={"ok": True, "result": {"username": "office_bot"}})
        raise httpx.ConnectError(f"cannot reach {request.url}")

    api = BotApi(token, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert (await api.call("getMe"))["username"] == "office_bot" and len(seen) == 2
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    with pytest.raises(TelegramError) as err:
        await api.call("sendMessage", chat_id=1, text="x")
    assert token not in str(err.value) and err.value.__cause__ is None
    await api.close()


async def _no_sleep(_seconds: float) -> None:
    return None


def test_token_comes_from_env_secrets_file_or_dotenv(tmp_path, monkeypatch):
    from actoroffice.channels.telegram import load_token

    for key in ("ACTOROFFICE_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    settings = Settings(data_dir=tmp_path / "home")
    assert load_token(settings) is None
    (tmp_path / ".env").write_text("# bot\nOTHER=1\nTELEGRAM='111:from-dotenv'\n", encoding="utf-8")
    assert load_token(settings) == "111:from-dotenv"
    (tmp_path / "home" / "secrets").mkdir(parents=True)
    (tmp_path / "home" / "secrets" / "telegram_token").write_text("222:from-secrets\n", encoding="utf-8")
    assert load_token(settings) == "222:from-secrets"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "333:from-env")
    assert load_token(settings) == "333:from-env"


async def test_projects_menu_opens_topics_for_each_actor_in_that_project(office, bot):
    await hire(office, "ba", "Hoa")
    await bind(office, bot)
    project = await office.projects.create("GoRoute", path=None, access=ProjectAccess.READ_ONLY)
    group = await office.chat.create_group("Nhóm GoRoute", [(await office.org.find_actor("Hoa")).id], created_by="Bạn", project_id=project.id)
    await office.telegram.handle_update(tg_message("/projects"))
    menu = [p for p in bot.sent() if p.get("reply_markup")][-1]
    choices = {b["text"]: b["callback_data"] for row in menu["reply_markup"]["inline_keyboard"] for b in row}
    assert "📁 GoRoute" in choices and any("(mặc định)" in t for t in choices)
    await office.telegram.handle_update({"update_id": 5, "callback_query": {"id": "cb", "data": choices["📁 GoRoute"], "message": {"chat": {"id": GROUP}}}})
    names = [p["name"] for p in bot.sent("createForumTopic")]
    assert any(n.startswith("💬 Hoa · ") and n.endswith(" · GoRoute") for n in names)
    assert "👥 Nhóm GoRoute · GoRoute" in names
    assert "Đã mở dự án GoRoute" in bot.sent("answerCallbackQuery")[-1]["text"]
    dm = await office.chat.dm((await office.org.find_actor("Hoa")).id, project.id)
    assert dm.project_id == project.id and await office.telegram.topic_for(group.id)
