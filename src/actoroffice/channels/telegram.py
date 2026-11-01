"""Telegram channel: one bot, one private forum group ("Topics" on); every conversation is a topic.

- DMs with actors, groups and ticket meeting rooms each get a topic, created on demand.
- Messages are mirrored both ways. What the user types in a topic goes to that conversation, so @mentions
  work exactly like on the web. Messages the user sends on the web are shown too, marked "(web)".
- Decisions (tool approvals, ticket proposals, automation reviews, questions) are cards with buttons; free-text
  answers are a reply to the card. Answering on the web updates the card here, and the other way round.
- After an actor's run finishes, its last message gets the usage line (files, lines, commands, time, tokens).

The office is single-user for now: the first forum group the bot is added to (where someone writes) becomes
the office group; everything else is ignored. Long polling, so no public URL or webhook is needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from actoroffice.domain.errors import Conflict, OfficeError

from . import tg_format as fmt
from .tg_menu import COMMANDS, Menus
from .tg_progress import LiveProgress
from .tg_team import TEAM_COMMANDS, TEAM_HELP, TeamCommands

if TYPE_CHECKING:
    from actoroffice.app import Office
    from actoroffice.config import Settings

log = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)  # httpx logs request URLs, which contain the bot token

TOKEN_RE = re.compile(r"^\d{6,12}:[A-Za-z0-9_\-]{30,}$")
MAX_FILE = 20 * 1024 * 1024
FILE_KINDS = {"file", "document", "dataset", "report"}
HELP = (
    "🏢 <b>Văn phòng AI</b>\n"
    "• Mỗi topic là một cuộc trò chuyện: 💬 chat riêng với một nhân viên, 👥 nhóm, 🏛 phòng họp của ticket.\n"
    "• Nhắn trong topic nào thì người trong topic đó nhận; trong nhóm dùng @tên để gọi ai đó.\n"
    "• Thẻ duyệt có nút bấm; câu hỏi thì reply vào thẻ để trả lời.\n"
    "• /menu mở menu chính: tạo ticket, nhóm, dự án, tuyển nhân viên; xem ticket, automation, nhân sự, skill, usage.\n"
    "• /projects chọn dự án để mở topic chat với nhân viên trong dự án đó (như chọn dự án trên web).\n"
    "• /sync tạo lại topic còn thiếu · /cancel huỷ thao tác đang làm · /help xem hướng dẫn này.\n"
    "Gõ “/” để xem mọi lệnh.\n\n" + TEAM_HELP
)


class TelegramError(Exception):
    def __init__(self, description: str, code: int = 0) -> None:
        super().__init__(description)
        self.code = code


ENV_KEYS = ("ACTOROFFICE_TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "TELEGRAM")


def load_token(settings: Settings) -> str | None:
    """Environment first, then secrets/telegram_token, then a .env file (working folder or office home)."""
    for key in ENV_KEYS:
        if os.environ.get(key, "").strip():
            return os.environ[key].strip()
    path = token_path(settings)
    if path.exists() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    for env_file in (Path.cwd() / ".env", settings.data_dir / ".env"):
        values = _dotenv(env_file)
        for key in ENV_KEYS:
            if values.get(key):
                return values[key]
    return None


def _dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def token_path(settings: Settings) -> Path:
    return settings.data_dir / "secrets" / "telegram_token"


class BotApi:
    """The few Bot API methods we need, over plain HTTPS. Errors never include the URL (it holds the token)."""

    def __init__(self, token: str, *, base: str = "https://api.telegram.org", client: httpx.AsyncClient | None = None) -> None:
        self._url = f"{base}/bot{token}/"
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(60, connect=15))

    async def call(self, method: str, **params: Any) -> Any:
        params = {k: v for k, v in params.items() if v is not None}
        for attempt in range(4):
            try:
                resp = await self._client.post(self._url + method, json=params)
                data = resp.json()
            except (httpx.HTTPError, ValueError) as exc:
                if attempt == 3:
                    raise TelegramError(f"network error ({type(exc).__name__})") from None
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if data.get("ok"):
                return data.get("result")
            retry = (data.get("parameters") or {}).get("retry_after")
            if resp.status_code == 429 and retry is not None and attempt < 3:
                await asyncio.sleep(min(float(retry), 60))
                continue
            raise TelegramError(str(data.get("description") or "error"), int(data.get("error_code") or resp.status_code))
        raise TelegramError("too many retries")

    async def send_document(self, chat_id: int, thread_id: int | None, path: Path, caption: str = "") -> Any:
        data = {"chat_id": str(chat_id), "caption": caption[:1000]}
        if thread_id:
            data["message_thread_id"] = str(thread_id)
        try:
            resp = await self._client.post(self._url + "sendDocument", data=data, files={"document": (path.name, path.read_bytes())})
            body = resp.json()
        except (httpx.HTTPError, ValueError, OSError) as exc:
            raise TelegramError(f"upload failed ({type(exc).__name__})") from None
        if not body.get("ok"):
            raise TelegramError(str(body.get("description") or "error"), int(body.get("error_code") or resp.status_code))
        return body.get("result")

    async def close(self) -> None:
        await self._client.aclose()


class TelegramBridge:
    def __init__(self, office: Office, api: Any) -> None:
        self.office = office
        self.api = api
        self.chat_id: int | None = None
        self._echo: list[tuple[str, str]] = []  # (conversation, text) the user just sent from Telegram
        self._runs: dict[str, tuple[int, int, str]] = {}  # run id → (chat, message id, html) of its last message
        self._topic_lock = asyncio.Lock()
        self._warned = False
        self.menus = Menus(self)
        self.progress = LiveProgress(self)
        self.team = TeamCommands(self)
        self._hinted: set[int] = set()  # unregistered senders already told how to join

    # ---- lifecycle --------------------------------------------------------------------------------

    async def run(self, ready: asyncio.Event | None = None) -> None:
        self.chat_id = await self._state_int("chat_id")
        self.progress.enabled = await self._state_int("progress") != 0
        async with self.office.bus.subscribe() as queue:
            if ready:
                ready.set()
            if self.chat_id is not None:
                await self._set_commands()
                with contextlib.suppress(TelegramError, OfficeError):
                    await self.sync_all()  # topics follow the office's current data (e.g. after a reset)
            await asyncio.gather(self._poll(), self._events(queue))

    async def _events(self, queue: asyncio.Queue[Any]) -> None:
        while True:
            event = await queue.get()
            try:
                await self.handle_event(event.kind, event.data)
            except TelegramError as exc:
                log.warning("telegram: %s while mirroring %s", exc, event.kind)
            except Exception:
                log.exception("telegram: failed on %s", event.kind)

    async def _poll(self) -> None:
        offset = await self._state_int("offset") or 0
        backoff = 1.0
        while True:
            try:
                updates = await self.api.call("getUpdates", offset=offset, timeout=30, allowed_updates=["message", "callback_query"])
            except TelegramError as exc:
                if exc.code in (401, 404):
                    log.error("telegram: the bot token was rejected; fix it with `actoroffice telegram <token>`")
                    return
                if exc.code == 409:  # a webhook is set (or another copy is polling)
                    with contextlib.suppress(TelegramError):
                        await self.api.call("deleteWebhook")
                log.warning("telegram: %s; retrying in %.0fs", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
                continue
            backoff = 1.0
            for update in updates or []:
                offset = int(update["update_id"]) + 1
                try:
                    await self.handle_update(update)
                except TelegramError as exc:
                    log.warning("telegram: %s while handling an update", exc)
                except Exception:
                    log.exception("telegram: failed on update %s", update.get("update_id"))
            if updates:
                await self._set_state("offset", str(offset))

    # ---- office → Telegram -------------------------------------------------------------------------

    async def handle_event(self, kind: str, data: dict[str, Any]) -> None:
        if self.chat_id is None:
            return
        if kind == "chat.message":
            await self._on_message(data["message"])
        elif kind in ("interaction.resolved", "interaction.updated"):
            await self._refresh_card(data["interaction"])
        elif kind == "run.finished":
            await self._on_run_finished(data)
            await self.progress.finished(str(data.get("run_id")), data.get("run"), str(data.get("status")))
        elif kind == "run.event":
            await self.progress.event(str(data.get("run_id")), str(data.get("kind")), data.get("payload") or {}, data.get("activity"))
        elif kind == "ticket.steps":
            await self.progress.steps_changed(int(data.get("ticket_id") or 0))
        elif kind == "run.started":
            run = data.get("run") or {}
            if run.get("conversation_id"):
                thread = await self._existing_topic(run["conversation_id"])
                if thread:
                    with contextlib.suppress(TelegramError):
                        await self.api.call("sendChatAction", chat_id=self.chat_id, message_thread_id=thread, action="typing")
            await self.progress.started(run)
        elif kind == "chat.conversation":
            await self._on_conversation(data["conversation"])
        elif kind == "team.changed":
            await self.sync_dms()

    async def _on_message(self, m: dict[str, Any]) -> None:
        conv_id = m["conversation_id"]
        if m["author_kind"] == "user" and (conv_id, m["body"]) in self._echo:
            self._echo.remove((conv_id, m["body"]))
            return
        thread = await self.topic_for(conv_id)
        if thread is None:
            return
        meta = m.get("meta") or {}
        if m.get("kind") == "interaction" and meta.get("interaction_id"):
            await self._send_card(thread, meta["interaction_id"])
            return
        head, body = await self._head(m), m["body"]
        if m.get("kind") == "ticket":
            note = str(meta.get("note") or "")
            text = f"🎫 {fmt.esc(body)}" + (f"\n<i>{fmt.esc(note[:400])}</i>" if note else "")
            await self.send(thread, text)
            return
        if m.get("kind") == "memory":
            await self.send(thread, f"📌 <i>{fmt.esc(body)}</i>")
            return
        if m["author_kind"] == "system":
            await self.send(thread, f"<i>{fmt.esc(body)}</i>")
            return
        pieces = fmt.split(body)
        last_id, last_html = 0, ""
        for i, piece in enumerate(pieces):
            last_html = (head if i == 0 else "") + fmt.md_to_html(piece)
            last_id = await self.send(thread, last_html)
        if meta.get("report") and meta.get("ticket_key"):
            await self._report_extras(thread, str(meta["ticket_key"]), last_id, last_html)
        elif meta.get("run_id") and last_id:
            self._runs[str(meta["run_id"])] = (self.chat_id or 0, last_id, last_html)
            while len(self._runs) > 500:
                self._runs.pop(next(iter(self._runs)))

    async def _head(self, m: dict[str, Any]) -> str:
        if m["author_kind"] == "actor" and m.get("author_id"):
            try:
                actor = await self.office.org.get_actor(m["author_id"])
                position = await self.office.org.get_position(actor.position_id)
            except OfficeError:
                return ""
            emoji = f"{actor.emoji} " if actor.emoji else ""
            return f"<b>{fmt.esc(emoji + actor.name)}</b> · <i>{fmt.esc(position.title)}</i>\n"
        if m["author_kind"] == "user":
            author = m.get("author_id")
            if author and author != "user":
                with contextlib.suppress(OfficeError):
                    return f"<b>🧑 {fmt.esc((await self.office.teams.get_user(author)).name)}</b>\n"
            return "<b>🧑 Bạn</b> <i>(trên web)</i>\n"
        return ""

    async def _report_extras(self, thread: int, ticket_key: str, message_id: int, text: str) -> None:
        """A "done" report gets the whole ticket tree's totals and the deliverable files."""
        try:
            ticket = await self.office.tickets.get(ticket_key)
            work = await self.office.runs.work_summary(ticket.id)
        except OfficeError:
            return
        if work.get("runs") and message_id:
            with contextlib.suppress(TelegramError):
                await self._edit(message_id, text + f"\n\n<i>{fmt.esc(fmt.work_line(work))}</i>")
        for product in (await self.office.tickets.work_products(ticket.id))[:5]:
            if product["kind"] not in FILE_KINDS:
                continue
            path = await self._deliverable(ticket, str(product["ref"]))
            if path:
                note = (product.get("metadata") or {}).get("note") or ""
                with contextlib.suppress(TelegramError):
                    await self.api.send_document(self.chat_id or 0, thread, path, caption=f"{ticket_key} · {path.name}" + (f" — {note}" if note else ""))

    async def _deliverable(self, ticket: Any, ref: str) -> Path | None:
        """Only files inside the ticket tree's workspace or the project folder are ever sent."""
        office = self.office
        root = await office.tickets.root_of(ticket.id)
        bases = [(office.settings.workspaces_dir / root.key).resolve(), (office.settings.workspaces_dir / ticket.key).resolve()]
        if ticket.project_id:
            with contextlib.suppress(OfficeError):
                project = await office.projects.get(ticket.project_id)
                if project.path:
                    bases.append(Path(project.path).resolve())
        raw = Path(ref.strip().strip("`"))
        candidates = [raw] if raw.is_absolute() else [base / raw for base in bases]
        secrets_dir = (office.settings.data_dir / "secrets").resolve()
        for candidate in candidates:
            try:
                path = candidate.resolve()
            except OSError:
                continue
            inside = any(path.is_relative_to(base) for base in bases) and not path.is_relative_to(secrets_dir)
            if inside and path.is_file() and 0 < path.stat().st_size <= MAX_FILE:
                return path
        return None

    async def _on_run_finished(self, data: dict[str, Any]) -> None:
        entry = self._runs.pop(str(data.get("run_id")), None)
        run = data.get("run") or {}
        if not entry or not run:
            return
        secs = None
        if run.get("started_at") and run.get("finished_at"):
            secs = int((datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds())
        line = fmt.usage_line(run.get("usage") or {}, run.get("stats") or {}, secs, run.get("cost_usd"))
        _chat, message_id, text = entry
        with contextlib.suppress(TelegramError):
            await self._edit(message_id, text + f"\n\n<i>{fmt.esc(line)}</i>")

    async def _on_conversation(self, conv: dict[str, Any]) -> None:
        row = await self.office.db.fetchone(
            "SELECT thread_id, title, closed FROM telegram_topics WHERE conversation_id = ? AND chat_id = ?", (conv["id"], self.chat_id)
        )
        if conv.get("archived"):
            if row and not row["closed"]:
                with contextlib.suppress(TelegramError):
                    await self.api.call("closeForumTopic", chat_id=self.chat_id, message_thread_id=row["thread_id"])
                await self._mark_closed(conv["id"], True)
            return
        if not row:
            await self.topic_for(conv["id"])
            return
        if row["closed"]:
            with contextlib.suppress(TelegramError):
                await self.api.call("reopenForumTopic", chat_id=self.chat_id, message_thread_id=row["thread_id"])
            await self._mark_closed(conv["id"], False)
        name = await self._topic_name(await self.office.chat.get(conv["id"]))
        if name != row["title"]:
            with contextlib.suppress(TelegramError):
                await self.api.call("editForumTopic", chat_id=self.chat_id, message_thread_id=row["thread_id"], name=name)
                await self.office.db.execute(
                    "UPDATE telegram_topics SET title = ? WHERE conversation_id = ? AND chat_id = ?", (name, conv["id"], self.chat_id)
                )

    # ---- cards ------------------------------------------------------------------------------------

    async def _send_card(self, thread: int, interaction_id: str) -> None:
        try:
            item = (await self.office.interactions.get(interaction_id)).model_dump()
        except OfficeError:
            return
        text, rows = fmt.card(item)
        if item["status"] != "pending":
            text, rows = text + f"\n\n<b>{fmt.esc(fmt.verdict(item))}</b>", []
        message_id = await self.send(thread, text, rows)
        await self.office.db.execute(
            "INSERT OR REPLACE INTO telegram_cards(interaction_id, chat_id, message_id) VALUES (?, ?, ?)",
            (interaction_id, self.chat_id, message_id),
        )

    async def _refresh_card(self, item: dict[str, Any]) -> None:
        row = await self.office.db.fetchone(
            "SELECT message_id FROM telegram_cards WHERE interaction_id = ? AND chat_id = ?", (item["id"], self.chat_id)
        )
        if not row:
            return
        text, rows = fmt.card(item)
        with contextlib.suppress(TelegramError):  # "message is not modified" when nothing changed
            if item.get("status") == "pending":  # e.g. one of two approvals came in: keep the buttons
                await self._edit(row["message_id"], text, rows)
            else:
                await self._edit(row["message_id"], text + f"\n\n<b>{fmt.esc(fmt.verdict(item))}</b>", [])

    # ---- Telegram → office -------------------------------------------------------------------------

    async def handle_update(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self._on_button(update["callback_query"])
        elif "message" in update:
            await self._on_telegram_message(update["message"])

    async def _on_button(self, cq: dict[str, Any]) -> None:
        note, alert = "Đã ghi nhận", False
        try:
            message = cq.get("message") or {}
            chat = (message.get("chat") or {}).get("id")
            data = str(cq.get("data", ""))
            if chat != self.chat_id:
                note = "Bot này chỉ nhận thao tác trong group văn phòng."
            elif data.startswith("p:"):
                note = await self._open_project_button(data[2:], message.get("message_thread_id"))
            elif data.startswith("m:"):
                note = await self.menus.button(data, message.get("message_thread_id"), int(message.get("message_id") or 0))
            elif data.startswith("t:"):
                note = await self.team.button(data, cq.get("from") or {}, message.get("message_thread_id"))
            else:
                _prefix, interaction_id, choice = data.split(":", 2)
                item = await self.office.interactions.get(interaction_id)
                who = await self.team.who(cq.get("from"))
                if item.status.value != "pending":
                    note = "Việc này đã được trả lời rồi."
                    await self._refresh_card(item.model_dump())
                elif who is None:
                    note, alert = "Bạn chưa được đăng ký: nhờ admin reply tin của bạn với /adduser Tên vai.", True
                else:
                    await self.office.answer(interaction_id, fmt.response_for(item.model_dump(), choice), by=who)
                    self.office.scheduler.kick()
        except Conflict:
            note = "Việc này đã được trả lời rồi."
        except (OfficeError, ValueError, IndexError, KeyError) as exc:
            note, alert = f"Không thực hiện được: {exc}"[:190], True
        with contextlib.suppress(TelegramError):
            await self.api.call("answerCallbackQuery", callback_query_id=cq["id"], text=note, show_alert=alert)

    async def _open_project_button(self, project_id: str, thread: int | None) -> str:
        project = await self.office.projects.get(project_id)
        created = await self.open_project(project.id)
        await self.send(thread, f"📁 <b>{fmt.esc(project.name)}</b>: các topic chat của dự án đã sẵn sàng.")
        return f"Đã mở dự án {project.name}" + (f": {created} topic mới" if created else "")

    async def _on_telegram_message(self, msg: dict[str, Any]) -> None:
        chat = msg.get("chat") or {}
        if (msg.get("from") or {}).get("is_bot"):
            return
        text = (msg.get("text") or msg.get("caption") or "").strip()
        if chat.get("type") == "private":
            await self.api.call(
                "sendMessage",
                chat_id=chat["id"],
                text="Bot này làm việc trong group văn phòng: tạo một group, bật Topics, thêm bot làm admin "
                "(quyền quản lý topic) rồi nhắn /start trong group.",
            )
            return
        if self.chat_id is None:
            if chat.get("is_forum"):
                await self._bind(int(chat["id"]))
            elif chat.get("type") in ("group", "supergroup"):
                await self.api.call("sendMessage", chat_id=chat["id"], text="Hãy bật Topics cho group này (Cài đặt group → Topics) rồi nhắn /start lại.")
            return
        if chat.get("id") != self.chat_id:
            return
        thread = msg.get("message_thread_id") if msg.get("is_topic_message") else None
        if text.startswith("/"):
            command = text.split()[0].split("@")[0].lower()
            args = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
            if await self.team.command(command, args, msg, thread):
                return
            if not await self.menus.command(command, thread):
                await self._command(command, thread)
            return
        if not text:
            await self.send(thread, "<i>Hiện bot chỉ nhận tin nhắn chữ.</i>")
            return
        if await self.menus.text(thread, text):  # the answer to a form's question (ticket title, group name…)
            return
        who = await self.team.who(msg.get("from"))
        if who is None:
            sender = int((msg.get("from") or {}).get("id") or 0)
            if sender not in self._hinted:
                self._hinted.add(sender)
                await self.send(thread, f"<i>Bạn chưa được đăng ký (Telegram id <code>{sender}</code>): nhờ admin reply tin của bạn với /adduser Tên vai.</i>")
            return
        reply = msg.get("reply_to_message") or {}
        if reply and reply.get("message_id") != thread and not reply.get("forum_topic_created"):
            if await self._answer_card(int(reply["message_id"]), text, who):
                return
        conv_id = await self._conversation_of(thread)
        if conv_id is None:
            await self.send(thread, HELP)
            return
        self._echo.append((conv_id, text))
        del self._echo[:-50]
        try:
            await self.office.post_user_message(conv_id, text, user_id=who)
        except OfficeError as exc:
            self._echo.remove((conv_id, text))
            await self.send(thread, f"<i>Không gửi được: {fmt.esc(exc)}</i>")
            return
        self.office.scheduler.kick()

    async def _answer_card(self, message_id: int, text: str, who: str = "user") -> bool:
        row = await self.office.db.fetchone(
            "SELECT interaction_id FROM telegram_cards WHERE chat_id = ? AND message_id = ?", (self.chat_id, message_id)
        )
        if not row:
            return False
        try:
            item = await self.office.interactions.get(row["interaction_id"])
            if item.status.value != "pending" or item.kind.value not in ("question", "ask_colleague", "approve_stage"):
                return False
            if item.kind.value == "approve_stage":  # a text reply to a gate card sends the stage back with that note
                await self.office.answer(item.id, {"approved": False, "note": text}, by=who)
            else:
                await self.office.answer(item.id, {"answer": text}, by=who)
        except Conflict:
            return False
        except OfficeError as exc:
            await self.send(None, f"<i>Không trả lời được: {fmt.esc(exc)}</i>")
            return True
        self.office.scheduler.kick()
        return True

    async def _command(self, command: str, thread: int | None) -> None:
        if command == "/sync":
            created = await self.sync_all()
            await self.send(thread, f"Đã đồng bộ: {created} topic mới.")
        elif command in ("/projects", "/project"):
            default = self.office.chat.default_project_id
            rows = [
                [{"text": f"📁 {p.name}" + (" (mặc định)" if p.id == default else ""), "callback_data": f"p:{p.id}"}]
                for p in (await self.office.projects.list())[:30]
            ]
            await self.send(thread, "Chọn dự án để mở topic chat với từng nhân viên trong dự án đó:", rows)
        else:
            await self.send(thread, HELP)

    async def _bind(self, chat_id: int) -> None:
        self.chat_id = chat_id
        await self._set_state("chat_id", str(chat_id))
        await self.send(None, "✅ Đã kết nối văn phòng với group này.\n\n" + HELP)
        await self._set_commands()
        await self.sync_all()

    async def _set_commands(self) -> None:
        """The "/" menu in Telegram lists every command with a short description."""
        with contextlib.suppress(TelegramError):
            commands = [*COMMANDS, *[c for c in TEAM_COMMANDS if c[0] not in {x for x, _ in COMMANDS}]]
            await self.api.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in commands])

    # ---- topics -----------------------------------------------------------------------------------

    async def sync_all(self) -> int:
        before = await self._topic_count()
        await self.sync_dms()
        for conv in await self.office.chat.list():
            if conv["kind"] == "group":
                await self.topic_for(conv["id"])
        return await self._topic_count() - before

    async def sync_dms(self, project_id: str | None = None) -> None:
        if self.chat_id is None:
            return
        for actor in await self.office.org.list_actors():
            if actor.status.value == "active":
                dm = await self.office.chat.dm(actor.id, project_id or self.office.chat.default_project_id)
                await self.topic_for(dm.id)

    async def open_project(self, project_id: str) -> int:
        """A topic for chatting with every actor inside this project, plus the project's groups."""
        before = await self._topic_count()
        await self.sync_dms(project_id)
        for conv in await self.office.chat.list(project_id):
            if conv["kind"] == "group":
                await self.topic_for(conv["id"])
        return await self._topic_count() - before

    async def topic_for(self, conversation_id: str) -> int | None:
        async with self._topic_lock:
            row = await self.office.db.fetchone(
                "SELECT thread_id, closed FROM telegram_topics WHERE conversation_id = ? AND chat_id = ?", (conversation_id, self.chat_id)
            )
            if row and not row["closed"]:
                return int(row["thread_id"])
            try:
                conv = await self.office.chat.get(conversation_id)
            except OfficeError:
                return None
            if conv.archived:
                return None
            if row:  # closed earlier, the conversation is active again
                with contextlib.suppress(TelegramError):
                    await self.api.call("reopenForumTopic", chat_id=self.chat_id, message_thread_id=row["thread_id"])
                await self._mark_closed(conversation_id, False)
                return int(row["thread_id"])
            name = await self._topic_name(conv)
            try:
                topic = await self.api.call("createForumTopic", chat_id=self.chat_id, name=name)
            except TelegramError as exc:
                await self._warn(exc)
                return None
            thread = int(topic["message_thread_id"])
            await self.office.db.execute(
                "INSERT OR REPLACE INTO telegram_topics(conversation_id, chat_id, thread_id, title, closed) VALUES (?, ?, ?, ?, 0)",
                (conversation_id, self.chat_id, thread, name),
            )
            return thread

    async def _existing_topic(self, conversation_id: str) -> int | None:
        row = await self.office.db.fetchone(
            "SELECT thread_id FROM telegram_topics WHERE conversation_id = ? AND chat_id = ? AND closed = 0", (conversation_id, self.chat_id)
        )
        return int(row["thread_id"]) if row else None

    async def _topic_name(self, conv: Any) -> str:
        if conv.kind.value == "dm" and conv.actor_ids:
            try:
                actor = await self.office.org.get_actor(conv.actor_ids[0])
                position = await self.office.org.get_position(actor.position_id)
                name = f"💬 {actor.name} · {position.title}"
            except OfficeError:
                name = f"💬 {conv.title}"
            if conv.project_id and conv.project_id != self.office.chat.default_project_id:
                with contextlib.suppress(OfficeError):
                    name += f" · {(await self.office.projects.get(conv.project_id)).name}"
            return name[:128]
        name = ("🏛 " if conv.ticket_id else "👥 ") + conv.title
        if conv.project_id and conv.project_id != self.office.chat.default_project_id:
            with contextlib.suppress(OfficeError):
                name += f" · {(await self.office.projects.get(conv.project_id)).name}"
        return name[:128]

    async def _conversation_of(self, thread: int | None) -> str | None:
        if not thread:
            return None
        row = await self.office.db.fetchone(
            "SELECT conversation_id FROM telegram_topics WHERE chat_id = ? AND thread_id = ? AND closed = 0", (self.chat_id, thread)
        )
        return row["conversation_id"] if row else None

    async def _topic_count(self) -> int:
        row = await self.office.db.fetchone("SELECT COUNT(*) AS n FROM telegram_topics WHERE chat_id = ?", (self.chat_id,))
        return int(row["n"]) if row else 0

    async def _mark_closed(self, conversation_id: str, closed: bool) -> None:
        await self.office.db.execute(
            "UPDATE telegram_topics SET closed = ? WHERE conversation_id = ? AND chat_id = ?", (int(closed), conversation_id, self.chat_id)
        )

    async def _warn(self, exc: TelegramError) -> None:
        log.warning("telegram: cannot create a topic: %s", exc)
        if not self._warned:
            self._warned = True
            with contextlib.suppress(TelegramError):
                await self.send(None, f"⚠️ Bot không tạo được topic ({fmt.esc(exc)}). Hãy cho bot quyền admin “Quản lý topic”, rồi gõ /sync.")

    # ---- sending ------------------------------------------------------------------------------------

    async def send(self, thread: int | None, text: str, rows: list[list[dict[str, str]]] | None = None) -> int:
        markup = {"inline_keyboard": rows} if rows else None
        params = dict(chat_id=self.chat_id, message_thread_id=thread, reply_markup=markup, link_preview_options={"is_disabled": True})
        try:
            result = await self.api.call("sendMessage", text=text, parse_mode="HTML", **params)
        except TelegramError as exc:
            if "parse" not in str(exc).lower():
                raise
            result = await self.api.call("sendMessage", text=_plain(text)[:4096], **params)  # broken HTML: send it as text
        return int(result["message_id"])

    async def send_long(self, thread: int | None, head: str, body: str) -> None:
        """Markdown that may be longer than one Telegram message; `head` (HTML) goes on the first piece."""
        for i, piece in enumerate(fmt.split(body or "(trống)")):
            await self.send(thread, (head if i == 0 else "") + fmt.md_to_html(piece))

    async def _edit(self, message_id: int, text: str, rows: list[list[dict[str, str]]] | None = None) -> None:
        markup = {"inline_keyboard": rows} if rows is not None else None
        try:
            await self.api.call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=text, parse_mode="HTML", reply_markup=markup)
        except TelegramError as exc:
            if "parse" not in str(exc).lower():
                raise
            await self.api.call("editMessageText", chat_id=self.chat_id, message_id=message_id, text=_plain(text)[:4096], reply_markup=markup)

    # ---- state ------------------------------------------------------------------------------------

    async def _state_int(self, key: str) -> int | None:
        row = await self.office.db.fetchone("SELECT value FROM telegram_state WHERE key = ?", (key,))
        return int(row["value"]) if row else None

    async def _set_state(self, key: str, value: str) -> None:
        await self.office.db.execute(
            "INSERT INTO telegram_state(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value)
        )


def _plain(text: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", text))
