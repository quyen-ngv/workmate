"""Telegram menus: what the web's side panels do, as commands with buttons.

Projects, groups, tickets, automations, team, skills, memory, inbox and usage. A form with several fields
("wizard") asks one thing at a time in the topic where it was started; the next text there is the answer, and
/cancel stops it. Navigation buttons edit the menu message in place; results are new messages.

Callback data (Telegram allows 64 bytes): "m:<action>[:<arg>…]" with ids, ticket keys or list indexes.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from actoroffice.core.activity import event_line
from actoroffice.domain.automation import AutomationStatus
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.org import ActorStatus
from actoroffice.domain.work import ProjectAccess, TicketStatus
from actoroffice.services.automations import schedule_text

from . import tg_format as fmt

if TYPE_CHECKING:
    from .telegram import TelegramBridge

Rows = list[list[dict[str, str]]]

COMMANDS = [
    ("menu", "Menu chính"),
    ("activity", "Ai đang làm gì (xem từng bước, dừng)"),
    ("new_ticket", "Tạo ticket"),
    ("tickets", "Ticket đang mở"),
    ("new_group", "Tạo nhóm chat"),
    ("new_project", "Tạo dự án"),
    ("projects", "Mở topic chat theo dự án"),
    ("automations", "Automation"),
    ("team", "Nhân sự"),
    ("hire", "Tuyển nhân viên"),
    ("skills", "Xem skill"),
    ("inbox", "Việc đang chờ bạn"),
    ("usage", "Usage hôm nay / 7 ngày"),
    ("progress", "Bật/tắt tin tiến độ trực tiếp"),
    ("sync", "Tạo lại topic còn thiếu"),
    ("cancel", "Huỷ thao tác đang làm"),
    ("help", "Hướng dẫn"),
]
STATUS = {
    "backlog": "🗂 backlog", "todo": "⚪ chờ làm", "in_progress": "🔵 đang làm", "in_review": "🟡 chờ duyệt",
    "blocked": "🔴 bị chặn", "done": "✅ xong", "cancelled": "✖️ huỷ",
}
AUTO_STATUS = {"pending_review": "🟡 chờ duyệt", "enabled": "🟢 đang bật", "disabled": "⏸ đã tắt", "finished": "✔️ đã chạy xong"}
ACCESS = {"read_only": "📖 Chỉ đọc", "worktree": "🌿 Git worktree", "direct": "✏️ Sửa trực tiếp"}
OPEN = [s for s in TicketStatus if s not in (TicketStatus.DONE, TicketStatus.CANCELLED)]
WIZARD_TTL = 15 * 60


def b(text: str, data: str) -> dict[str, str]:
    return {"text": text[:60], "callback_data": data[:64]}


@dataclass
class Wizard:
    kind: str
    expects: str | None = None  # field the next text message fills
    data: dict[str, Any] = field(default_factory=dict)
    message_id: int | None = None  # the menu message being edited
    started: float = field(default_factory=time.monotonic)


class Menus:
    def __init__(self, bridge: TelegramBridge) -> None:
        self.bridge = bridge
        self.office = bridge.office
        self.wizards: dict[int | None, Wizard] = {}
        self._skills: list[Any] = []

    # ---- entry points (called by the bridge) --------------------------------------------------------

    async def command(self, command: str, thread: int | None) -> bool:
        handler = {
            "/menu": self.main_menu, "/start": None, "/new_ticket": self.new_ticket, "/tickets": self.tickets,
            "/new_group": self.new_group, "/new_project": self.new_project, "/automations": self.automations,
            "/team": self.team, "/hire": self.hire, "/skills": self.skills, "/inbox": self.inbox, "/usage": self.usage,
            "/cancel": self.cancel, "/activity": self.activity, "/progress": self.toggle_progress,
        }.get(command)
        if handler is None:
            return False
        await handler(thread)
        return True

    async def text(self, thread: int | None, text: str) -> bool:
        """The answer to a wizard's question. False when no wizard waits for text here."""
        wizard = self.wizards.get(thread)
        if wizard is None or wizard.expects is None:
            return False
        if time.monotonic() - wizard.started > WIZARD_TTL:
            self.wizards.pop(thread, None)
            return False
        wizard.data[wizard.expects] = text.strip()
        wizard.expects = None
        await self._advance(thread, wizard)
        return True

    async def button(self, data: str, thread: int | None, message_id: int) -> str:
        parts = data.split(":")[1:]
        action, args = parts[0], parts[1:]
        handler = getattr(self, f"_b_{action}", None)
        if handler is None:
            return "Nút này không còn dùng được."
        return await handler(thread, message_id, *args) or "OK"

    # ---- main menu ------------------------------------------------------------------------------

    async def main_menu(self, thread: int | None) -> None:
        rows = [
            [b("🔄 Ai đang làm gì", "m:go:activity")],
            [b("🎫 Tạo ticket", "m:go:new_ticket"), b("📋 Ticket đang mở", "m:go:tickets")],
            [b("👥 Tạo nhóm", "m:go:new_group"), b("📁 Tạo dự án", "m:go:new_project")],
            [b("📁 Topic theo dự án", "m:go:projects"), b("⚙️ Automation", "m:go:automations")],
            [b("🧑‍💼 Nhân sự", "m:go:team"), b("➕ Tuyển nhân viên", "m:go:hire")],
            [b("📘 Skill", "m:go:skills"), b("📥 Đang chờ bạn", "m:go:inbox"), b("📊 Usage", "m:go:usage")],
        ]
        await self.bridge.send(thread, "🏢 <b>Văn phòng AI</b> — chọn việc:", rows)

    async def _b_go(self, thread: int | None, _message_id: int, name: str) -> str:
        if name == "projects":
            await self.bridge._command("/projects", thread)
        else:
            await self.command(f"/{name}", thread)
        return "OK"

    async def cancel(self, thread: int | None) -> None:
        had = self.wizards.pop(thread, None)
        await self.bridge.send(thread, "Đã huỷ." if had else "Không có thao tác nào đang làm.")

    # ---- wizards ------------------------------------------------------------------------------------

    async def _start(self, thread: int | None, kind: str, **data: Any) -> Wizard:
        wizard = Wizard(kind, data=dict(data))
        self.wizards[thread] = wizard
        await self._advance(thread, wizard)
        return wizard

    async def _ask(self, thread: int | None, wizard: Wizard, field_name: str, question: str, rows: Rows | None = None) -> None:
        wizard.expects = field_name
        await self.bridge.send(thread, question + "\n<i>(/cancel để huỷ)</i>", (rows or []) + [[b("✖️ Huỷ", "m:x")]])

    async def _choose(self, thread: int | None, wizard: Wizard, question: str, rows: Rows) -> None:
        wizard.expects = None
        rows = rows + [[b("✖️ Huỷ", "m:x")]]
        if wizard.message_id:
            await self.bridge._edit(wizard.message_id, question, rows)
        else:
            wizard.message_id = await self.bridge.send(thread, question, rows)

    async def _b_x(self, thread: int | None, _message_id: int) -> str:
        self.wizards.pop(thread, None)
        await self.bridge.send(thread, "Đã huỷ.")
        return "Đã huỷ"

    async def _b_w(self, thread: int | None, _message_id: int, key: str, *value: str) -> str:
        """A choice inside a wizard: store it and move on."""
        wizard = self.wizards.get(thread)
        if wizard is None:
            return "Thao tác này đã hết hạn, hãy bắt đầu lại."
        joined = ":".join(value)
        if key == "toggle":
            chosen = set(wizard.data.get("actors", []))
            chosen ^= {joined}
            wizard.data["actors"] = sorted(chosen)
        else:
            wizard.data[key] = joined
        await self._advance(thread, wizard)
        return "OK"

    async def _advance(self, thread: int | None, wizard: Wizard) -> None:
        try:
            await getattr(self, f"_wiz_{wizard.kind}")(thread, wizard)
        except OfficeError as exc:
            self.wizards.pop(thread, None)
            await self.bridge.send(thread, f"⚠️ {fmt.esc(exc)}")

    def _done(self, thread: int | None) -> None:
        self.wizards.pop(thread, None)

    async def _project_rows(self, key: str = "project") -> Rows:
        default = self.office.chat.default_project_id
        return [[b(f"📁 {p.name}" + (" (mặc định)" if p.id == default else ""), f"m:w:{key}:{p.id}")] for p in await self.office.projects.list()]

    async def _need_project(self, thread: int | None, wizard: Wizard, question: str) -> bool:
        """Ask for the project unless there is only one. True when the wizard has to wait."""
        if "project" in wizard.data:
            return False
        projects = await self.office.projects.list()
        if len(projects) == 1:
            wizard.data["project"] = projects[0].id
            return False
        await self._choose(thread, wizard, question, await self._project_rows())
        return True

    # new ticket: project → assignee → title → description

    async def new_ticket(self, thread: int | None) -> None:
        await self._start(thread, "ticket")

    async def _wiz_ticket(self, thread: int | None, w: Wizard) -> None:
        if await self._need_project(thread, w, "🎫 <b>Ticket mới</b> — thuộc dự án nào?"):
            return
        if "assignee" not in w.data:
            actors = [a for a in await self.office.org.list_actors() if a.status == ActorStatus.ACTIVE]
            rows = [[b(f"👤 {a.name}", f"m:w:assignee:{a.id}")] for a in actors] + [[b("Chưa giao ai", "m:w:assignee:-")]]
            await self._choose(thread, w, "🎫 <b>Ticket mới</b> — giao cho ai?", rows)
            return
        if "title" not in w.data:
            await self._ask(thread, w, "title", "🎫 Gõ <b>tiêu đề</b> ticket:")
            return
        if "description" not in w.data:
            await self._ask(thread, w, "description", "Gõ <b>mô tả</b> (yêu cầu, tiêu chí xong…), hoặc bấm Bỏ qua:", [[b("Bỏ qua", "m:w:description:")]])
            return
        self._done(thread)
        assignee = w.data["assignee"] if w.data["assignee"] != "-" else None
        ticket = await self.office.tickets.create(
            w.data["title"], description=w.data["description"], assignee_actor_id=assignee, project_id=w.data["project"]
        )
        self.office.scheduler.kick()
        who = (await self.office.org.get_actor(assignee)).name if assignee else "chưa giao ai"
        await self.bridge.send(thread, f"✅ Đã tạo <b>{ticket.key}</b> · {fmt.esc(ticket.title)} → {fmt.esc(who)}", [[b("Xem ticket", f"m:t:{ticket.key}")]])

    # new group: project → members → name

    async def new_group(self, thread: int | None) -> None:
        await self._start(thread, "group")

    async def _wiz_group(self, thread: int | None, w: Wizard) -> None:
        if await self._need_project(thread, w, "👥 <b>Nhóm mới</b> — thuộc dự án nào?"):
            return
        if not w.data.get("members_done"):
            chosen = set(w.data.get("actors", []))
            actors = [a for a in await self.office.org.list_actors() if a.status == ActorStatus.ACTIVE]
            rows = [[b(("✅ " if a.id in chosen else "▫️ ") + a.name, f"m:w:toggle:{a.id}")] for a in actors]
            if chosen:
                rows.append([b(f"Xong ({len(chosen)} người)", "m:w:members_done:1")])
            await self._choose(thread, w, "👥 <b>Nhóm mới</b> — chọn thành viên rồi bấm Xong:", rows)
            return
        if "title" not in w.data:
            await self._ask(thread, w, "title", "👥 Gõ <b>tên nhóm</b>:")
            return
        self._done(thread)
        conv = await self.office.chat.create_group(w.data["title"], w.data["actors"], created_by="Bạn", project_id=w.data["project"])
        topic = await self.bridge.topic_for(conv.id)
        await self.bridge.send(thread, f"✅ Đã tạo nhóm <b>{fmt.esc(conv.title)}</b>." + self._topic_link(topic))

    # new project: name → access → folder

    async def new_project(self, thread: int | None) -> None:
        await self._start(thread, "project")

    async def _wiz_project(self, thread: int | None, w: Wizard) -> None:
        if "name" not in w.data:
            await self._ask(thread, w, "name", "📁 Gõ <b>tên dự án</b>:")
            return
        if "access" not in w.data:
            rows = [[b(label, f"m:w:access:{key}")] for key, label in ACCESS.items()]
            await self._choose(
                thread, w,
                "📁 Nhân viên được làm gì với thư mục dự án?\n"
                "• <b>Chỉ đọc</b>: làm trong thư mục riêng của ticket, chỉ đọc thư mục dự án.\n"
                "• <b>Git worktree</b>: mỗi việc một nhánh git, bạn duyệt rồi mới merge (an toàn nhất cho code).\n"
                "• <b>Sửa trực tiếp</b>: sửa thẳng vào thư mục, mỗi lúc một việc.",
                rows,
            )
            return
        if "path" not in w.data:
            skip = [[b("Bỏ qua (không gắn thư mục)", "m:w:path:")]] if w.data["access"] == "read_only" else []
            await self._ask(thread, w, "path", "📁 Gõ <b>đường dẫn thư mục</b> dự án (ví dụ <code>D:\\code\\goroute</code>):", skip)
            return
        self._done(thread)
        project = await self.office.projects.create(w.data["name"], path=w.data["path"] or None, access=ProjectAccess(w.data["access"]))
        await self.bridge.send(
            thread, f"✅ Đã tạo dự án <b>{fmt.esc(project.name)}</b> ({ACCESS[project.access.value]}).", [[b("Mở topic chat của dự án", f"p:{project.id}")]]
        )

    # hire: position → name

    async def hire(self, thread: int | None) -> None:
        await self._start(thread, "hire")

    async def _wiz_hire(self, thread: int | None, w: Wizard) -> None:
        if "position" not in w.data:
            rows = [[b(p.title, f"m:w:position:{p.id}")] for p in await self.office.org.list_positions()]
            await self._choose(thread, w, "➕ <b>Tuyển nhân viên</b> — vị trí nào?", rows)
            return
        if "name" not in w.data:
            suggested = await self.office.org.suggest_name()
            await self._ask(
                thread, w, "name", "➕ Gõ <b>tên</b> (một từ, dùng để @gọi) hoặc dùng tên gợi ý:", [[b(f"Dùng tên {suggested}", f"m:w:name:{suggested}")]]
            )
            return
        name = w.data["name"].strip()
        if not name or " " in name:
            w.data.pop("name")
            await self._ask(thread, w, "name", "Tên phải là một từ, không dấu cách. Gõ lại:")
            return
        self._done(thread)
        actor = await self.office.org.hire(w.data["position"], name)
        self.office.bus.publish("team.changed")
        position = await self.office.org.get_position(actor.position_id)
        dm = await self.office.chat.dm(actor.id)
        topic = await self.bridge.topic_for(dm.id)
        await self.bridge.send(thread, f"✅ Đã tuyển <b>{fmt.esc(actor.name)}</b> · {fmt.esc(position.title)}." + self._topic_link(topic))

    # ---- tickets ------------------------------------------------------------------------------------

    async def tickets(self, thread: int | None) -> None:
        text, rows = await self._ticket_list()
        await self.bridge.send(thread, text, rows)

    async def _ticket_list(self) -> tuple[str, Rows]:
        tickets = await self.office.tickets.list(status=OPEN, limit=40)
        if not tickets:
            return "📋 Không có ticket nào đang mở.", [[b("🎫 Tạo ticket", "m:go:new_ticket")]]
        names = {a.id: a.name for a in await self.office.org.list_actors()}
        rows = [
            [b(f"{STATUS.get(t.status.value, '')[:2]} {t.key} · {t.title} · {names.get(t.assignee_actor_id or '', '—')}", f"m:t:{t.key}")]
            for t in tickets[:40]
        ]
        return f"📋 <b>{len(tickets)} ticket đang mở</b> — chọn để xem:", rows

    async def _b_tl(self, thread: int | None, message_id: int) -> str:
        text, rows = await self._ticket_list()
        await self.bridge._edit(message_id, text, rows)
        return "OK"

    async def _b_t(self, thread: int | None, message_id: int, key: str) -> str:
        t = await self.office.tickets.get(key)
        names = {a.id: a.name for a in await self.office.org.list_actors()}
        project = await self.office.projects.get(t.project_id) if t.project_id else None
        children = await self.office.tickets.list(parent_id=t.id)
        docs = await self.office.tickets.list_documents(t.id)
        products = await self.office.tickets.work_products(t.id)
        lines = [
            f"🎫 <b>{t.key} · {fmt.esc(t.title)}</b>",
            f"{STATUS.get(t.status.value, t.status.value)} · 👤 {fmt.esc(names.get(t.assignee_actor_id or '', 'chưa giao'))}"
            + (f" · 📁 {fmt.esc(project.name)}" if project else "") + (f" · ⏰ {t.due_at[:16].replace('T', ' ')}" if t.due_at else ""),
        ]
        if t.description:
            lines += ["", fmt.md_to_html(t.description[:1200]) + ("…" if len(t.description) > 1200 else "")]
        if children:
            lines += ["", "<b>Ticket con:</b> " + ", ".join(f"{c.key} {STATUS.get(c.status.value, '')[:2]}" for c in children[:15])]
        if products:
            lines += ["<b>Bàn giao:</b> " + ", ".join(fmt.esc(f"{p['kind']}: {p['ref']}") for p in products[:6])]
        rows: Rows = []
        doc_buttons = [b(f"📄 {d.key} (rev {d.revision})", f"m:td:{t.key}:{d.key}") for d in docs[:8]]
        rows += [doc_buttons[i : i + 2] for i in range(0, len(doc_buttons), 2)]
        rows.append([b("💬 Bình luận", f"m:tc:{t.key}"), b("🔁 Thay đổi yêu cầu", f"m:tx:{t.key}")])
        if t.status.value not in ("done", "cancelled"):
            rows.append([b("👤 Giao lại", f"m:tr:{t.key}"), b("✖️ Huỷ ticket", f"m:tk:{t.key}")])
        rows.append([b("⬅️ Danh sách ticket", "m:tl")])
        await self.bridge._edit(message_id, "\n".join(lines), rows)
        return "OK"

    async def _b_td(self, thread: int | None, _message_id: int, key: str, doc_key: str) -> str:
        t = await self.office.tickets.get(key)
        doc = next((d for d in await self.office.tickets.list_documents(t.id) if d.key == doc_key), None)
        if doc is None:
            return "Không thấy tài liệu này."
        await self.bridge.send_long(thread, f"📄 <b>{t.key} · {fmt.esc(doc.key)}</b> (rev {doc.revision}, {fmt.esc(doc.updated_by)})\n", doc.body)
        return "OK"

    async def _b_tc(self, thread: int | None, _message_id: int, key: str) -> str:
        w = Wizard("comment", data={"key": key})
        self.wizards[thread] = w
        await self._ask(thread, w, "body", f"💬 Gõ bình luận cho <b>{fmt.esc(key)}</b> (người làm ticket sẽ đọc và tiếp tục):")
        return "OK"

    async def _wiz_comment(self, thread: int | None, w: Wizard) -> None:
        self._done(thread)
        await self.office.tickets.add_comment(w.data["key"], w.data["body"], author_kind="user", author_id="user")
        self.office.scheduler.kick()
        await self.bridge.send(thread, f"✅ Đã bình luận vào {fmt.esc(w.data['key'])}.")

    async def _b_tx(self, thread: int | None, _message_id: int, key: str) -> str:
        w = Wizard("change", data={"key": key})
        self.wizards[thread] = w
        await self._ask(
            thread, w, "summary",
            f"🔁 Mô tả <b>thay đổi yêu cầu</b> cho {fmt.esc(key)}. Mọi ticket liên quan trong cây sẽ được báo, việc đang làm dừng để lập kế hoạch lại:",
        )
        return "OK"

    async def _wiz_change(self, thread: int | None, w: Wizard) -> None:
        self._done(thread)
        result = await self.office.tickets.change_notice(w.data["key"], w.data["summary"], by="Bạn")
        self.office.scheduler.kick()
        told = ", ".join([*result.get("notified", []), *result.get("reopened", [])])
        await self.bridge.send(thread, f"✅ Đã ghi nhận thay đổi yêu cầu cho {fmt.esc(w.data['key'])}." + (f" Đã báo: {fmt.esc(told)}" if told else ""))

    async def _b_tr(self, thread: int | None, message_id: int, key: str) -> str:
        actors = [a for a in await self.office.org.list_actors() if a.status == ActorStatus.ACTIVE]
        rows = [[b(f"👤 {a.name}", f"m:ta:{key}:{a.id}")] for a in actors] + [[b("⬅️ Quay lại", f"m:t:{key}")]]
        await self.bridge._edit(message_id, f"👤 Giao <b>{fmt.esc(key)}</b> cho ai?", rows)
        return "OK"

    async def _b_ta(self, thread: int | None, message_id: int, key: str, actor_id: str) -> str:
        await self.office.tickets.assign(key, actor_id, by="user")
        self.office.scheduler.kick()
        await self._b_t(thread, message_id, key)
        return f"Đã giao cho {(await self.office.org.get_actor(actor_id)).name}"

    async def _b_tk(self, thread: int | None, message_id: int, key: str, confirm: str = "") -> str:
        if confirm != "y":
            rows = [[b("✖️ Huỷ ticket này", f"m:tk:{key}:y"), b("⬅️ Không", f"m:t:{key}")]]
            await self.bridge._edit(message_id, f"Huỷ <b>{fmt.esc(key)}</b>? Việc đang làm sẽ dừng.", rows)
            return "Xác nhận?"
        await self.office.tickets.transition(key, TicketStatus.CANCELLED, by="user")
        await self._b_t(thread, message_id, key)
        return "Đã huỷ ticket"

    # ---- automations --------------------------------------------------------------------------------

    async def automations(self, thread: int | None) -> None:
        text, rows = await self._automation_list()
        await self.bridge.send(thread, text, rows)

    async def _automation_list(self) -> tuple[str, Rows]:
        items = [a for a in await self.office.automations.list() if a.status != AutomationStatus.FINISHED]
        if not items:
            return "⚙️ Chưa có automation nào. Nhờ một nhân viên “làm việc X mỗi sáng” để họ thiết kế automation.", []
        rows = [[b(f"{AUTO_STATUS.get(a.status.value, '')[:2]} {a.name} · {schedule_text(a.schedule, a.next_at)}", f"m:a:{a.id}")] for a in items[:40]]
        return f"⚙️ <b>{len(items)} automation</b> — chọn để xem:", rows

    async def _b_al(self, thread: int | None, message_id: int) -> str:
        text, rows = await self._automation_list()
        await self.bridge._edit(message_id, text, rows)
        return "OK"

    async def _b_a(self, thread: int | None, message_id: int, automation_id: str) -> str:
        a = await self.office.automations.get(automation_id)
        owner = (await self.office.org.get_actor(a.owner_actor_id)).name if a.owner_actor_id else "—"
        runs = await self.office.automations.runs(a.id, limit=1)
        lines = [
            f"⚙️ <b>{fmt.esc(a.name)}</b> · {AUTO_STATUS.get(a.status.value, a.status.value)}",
            f"👤 {fmt.esc(owner)} · 🗓 {fmt.esc(schedule_text(a.schedule, a.next_at))}" + (f" · ❗ lỗi liên tiếp: {a.failures}" if a.failures else ""),
        ]
        if a.description:
            lines += ["", fmt.esc(a.description)]
        lines += ["", *(f"{i}. {fmt.esc(fmt.step_label(s.model_dump()))}" for i, s in enumerate(a.steps, 1))]
        if runs:
            r = runs[0]
            lines += ["", f"<b>Lần chạy gần nhất:</b> {fmt.esc(r.status)} · {r.started_at[:16].replace('T', ' ')} UTC"]
            detail = (r.error or r.output or "").strip()
            if detail:
                lines.append(f"<pre>{fmt.esc(detail[-700:])}</pre>")
        rows: Rows = []
        if a.status != AutomationStatus.PENDING_REVIEW:
            toggle = b("⏸ Tắt", f"m:ae:{a.id}:0") if a.status == AutomationStatus.ENABLED else b("▶️ Bật", f"m:ae:{a.id}:1")
            rows.append([b("▶️ Chạy ngay", f"m:ar:{a.id}"), toggle])
        rows.append([b("🔄 Làm mới", f"m:a:{a.id}"), b("⬅️ Danh sách", "m:al")])
        await self.bridge._edit(message_id, "\n".join(lines), rows)
        return "OK"

    async def _b_ar(self, thread: int | None, message_id: int, automation_id: str) -> str:
        a = await self.office.automations.get(automation_id)
        if a.status == AutomationStatus.PENDING_REVIEW:
            return "Automation chưa được duyệt."
        if a.id in self.office.automations.running_ids:
            return "Automation đang chạy rồi."
        self.office.automations.start(a, "manual")
        return "Đã bắt đầu chạy; bấm Làm mới để xem kết quả"

    async def _b_ae(self, thread: int | None, message_id: int, automation_id: str, on: str) -> str:
        await self.office.automations.set_enabled(automation_id, on == "1", by="user")
        await self._b_a(thread, message_id, automation_id)
        return "Đã bật" if on == "1" else "Đã tắt"

    # ---- team -------------------------------------------------------------------------------------

    async def team(self, thread: int | None) -> None:
        text, rows = await self._team_list()
        await self.bridge.send(thread, text, rows)

    async def _team_list(self) -> tuple[str, Rows]:
        positions = {p.id: p.title for p in await self.office.org.list_positions()}
        actors = await self.office.org.list_actors()
        rows = [[b(("⏸ " if a.status != ActorStatus.ACTIVE else "🧑 ") + f"{a.name} · {positions.get(a.position_id, '')}", f"m:u:{a.id}")] for a in actors]
        rows.append([b("➕ Tuyển nhân viên", "m:go:hire")])
        return f"🧑‍💼 <b>{len(actors)} nhân viên</b>:", rows

    async def _b_ul(self, thread: int | None, message_id: int) -> str:
        text, rows = await self._team_list()
        await self.bridge._edit(message_id, text, rows)
        return "OK"

    async def _b_u(self, thread: int | None, message_id: int, actor_id: str) -> str:
        office = self.office
        actor = await office.org.get_actor(actor_id)
        position = await office.org.get_position(actor.position_id)
        today = await office.runs.usage_summary(actor_id=actor.id, since=_since(1))
        week = await office.runs.usage_summary(actor_id=actor.id, since=_since(7))
        memories = await office.memory.visible(actor)
        autos = await office.automations.list(owner_actor_id=actor.id, active_only=True)
        open_tickets = await office.tickets.list(status=OPEN, assignee_actor_id=actor.id)
        lines = [
            f"🧑 <b>{fmt.esc(actor.name)}</b> · {fmt.esc(position.title)}" + (f" · {fmt.esc(actor.specialty)}" if actor.specialty else ""),
            f"{'🟢 đang làm việc' if actor.status == ActorStatus.ACTIVE else '⏸ tạm dừng'} · model: {fmt.esc(actor.model or position.model or 'mặc định')}",
            f"📊 Hôm nay: {_usage(today)}",
            f"📊 7 ngày: {_usage(week)}",
            f"🎫 {len(open_tickets)} ticket đang mở · ⚙️ {len(autos)} automation · 🧠 {len(memories)} memory · 📘 {len(office.skills.for_actor(actor, position))} skill",
        ]
        if open_tickets:
            lines.append("Đang làm: " + ", ".join(f"{t.key}" for t in open_tickets[:8]))
        pause = b("⏸ Tạm dừng", f"m:up:{actor.id}:paused") if actor.status == ActorStatus.ACTIVE else b("▶️ Làm việc lại", f"m:up:{actor.id}:active")
        rows = [[b("🧠 Memory", f"m:um:{actor.id}"), pause], [b("✏️ Đổi tên", f"m:un:{actor.id}"), b("⬅️ Nhân sự", "m:ul")]]
        dm = await office.chat.dm(actor.id)
        topic = await self.bridge._existing_topic(dm.id)
        link = self._topic_url(topic)
        if link:
            rows.insert(0, [{"text": "💬 Mở chat", "url": link}])
        await self.bridge._edit(message_id, "\n".join(lines), rows)
        return "OK"

    async def _b_un(self, thread: int | None, _message_id: int, actor_id: str) -> str:
        actor = await self.office.org.get_actor(actor_id)
        w = Wizard("rename", data={"actor": actor_id})
        self.wizards[thread] = w
        await self._ask(thread, w, "name", f"✏️ Tên mới cho <b>{fmt.esc(actor.name)}</b> (một từ, dùng để @gọi):")
        return "OK"

    async def _wiz_rename(self, thread: int | None, w: Wizard) -> None:
        before = await self.office.org.get_actor(w.data["actor"])
        try:
            actor = await self.office.rename_actor(w.data["actor"], w.data["name"], by="Bạn")
        except OfficeError as exc:
            w.data.pop("name", None)
            await self._ask(thread, w, "name", f"⚠️ {fmt.esc(exc)}. Gõ tên khác:")
            return
        self._done(thread)
        await self.bridge.send(thread, f"✅ Đã đổi tên <b>{fmt.esc(before.name)}</b> → <b>{fmt.esc(actor.name)}</b>. Từ giờ gọi @{fmt.esc(actor.name)}.")

    async def _b_up(self, thread: int | None, message_id: int, actor_id: str, status: str) -> str:
        await self.office.org.set_actor_status(actor_id, ActorStatus(status))
        self.office.scheduler.kick()
        self.office.bus.publish("team.changed")
        await self._b_u(thread, message_id, actor_id)
        return "Đã cập nhật"

    async def _b_um(self, thread: int | None, _message_id: int, actor_id: str) -> str:
        actor = await self.office.org.get_actor(actor_id)
        memories = await self.office.memory.visible(actor, limit=60)
        if not memories:
            await self.bridge.send(thread, f"🧠 {fmt.esc(actor.name)} chưa nhớ gì.")
            return "OK"
        body = "\n".join(f"- {'📌 ' if m.pinned else ''}[{m.kind.value}, {m.scope.value}] {m.content}" for m in memories)
        await self.bridge.send_long(thread, f"🧠 <b>Memory của {fmt.esc(actor.name)}</b> ({len(memories)})\n", body)
        return "OK"

    # ---- skills ------------------------------------------------------------------------------------

    async def skills(self, thread: int | None) -> None:
        self._skills = self.office.skills.list()
        if not self._skills:
            await self.bridge.send(thread, "📘 Chưa có skill nào.")
            return
        rows = [
            [b(f"📘 {s.ref.name}" + (f" · {s.ref.owner}" if s.ref.owner else " · chung"), f"m:s:{i}")] for i, s in enumerate(self._skills[:60])
        ]
        await self.bridge.send(thread, f"📘 <b>{len(self._skills)} skill</b> — chọn để đọc:", rows)

    async def _b_s(self, thread: int | None, _message_id: int, index: str) -> str:
        try:
            ref = self._skills[int(index)].ref
        except (IndexError, ValueError):
            return "Danh sách đã cũ, gõ /skills lại."
        skill = self.office.skills.get(ref)
        head = f"📘 <b>{fmt.esc(skill.ref.name)}</b> ({fmt.esc(skill.ref.scope.value)}{' · ' + fmt.esc(skill.ref.owner) if skill.ref.owner else ''})\n<i>{fmt.esc(skill.description)}</i>\n"
        files = f"\n\nFile kèm: {', '.join(skill.files)}" if skill.files else ""
        await self.bridge.send_long(thread, head, skill.body + files)
        return "OK"

    # ---- activity: what actors are doing ----------------------------------------------------------------

    async def activity(self, thread: int | None) -> None:
        office = self.office
        names = {a.id: a.name for a in await office.org.list_actors()}
        recent = await office.runs.recent(limit=50)
        running = [r for r in recent if r["status"] == "running"]
        lines, rows = ["🔄 <b>Đang làm</b>"], []
        if not running:
            lines.append("Không ai đang chạy.")
        for r in running:
            name = names.get(r["actor_id"], "?")
            secs = int((datetime.now(UTC) - datetime.fromisoformat(r["started_at"])).total_seconds()) if r.get("started_at") else None
            last = await self._last_step(r["id"])
            lines.append(f"🔵 <b>{fmt.esc(name)}</b> · {fmt.esc(_what(r))} · {fmt.duration(secs)}" + (f"\n    {fmt.esc(last)}" if last else ""))
            rows.append([b(f"📜 Xem {name}", f"m:rv:{r['id']}"), b(f"✖️ Dừng {name}", f"m:rs:{r['id']}")])
        done = [r for r in recent if r["status"] != "running"][:6]
        if done:
            lines += ["", "🕘 <b>Gần đây</b>"]
        for r in done:
            name = names.get(r["actor_id"], "?")
            icon = {"succeeded": "✅", "failed": "⚠️", "cancelled": "✖️"}.get(r["status"], "⏸")
            lines.append(f"{icon} {fmt.esc(name)} · {fmt.esc(_what(r))} · {(r.get('started_at') or '')[11:16]} UTC")
            rows.append([b(f"📜 {name} · {_what(r)}", f"m:rv:{r['id']}")])
        rows.append([b("🔄 Làm mới", "m:go:activity")])
        await self.bridge.send(thread, "\n".join(lines), rows)

    async def _last_step(self, run_id: str) -> str:
        for event in reversed(await self.office.runs.events(run_id)):
            line = event_line(event["kind"], event["payload"])
            if line:
                return f"{line[0]} {line[1]}"
        return ""

    async def _b_rv(self, thread: int | None, _message_id: int, run_id: str) -> str:
        """The whole step-by-step log of one run."""
        events = await self.office.runs.events(run_id)
        steps = [(e["at"], event_line(e["kind"], e["payload"])) for e in events]
        body = "\n".join(f"{at[11:19]} {line[0]} {line[1]}" for at, line in steps if line) or "(chưa có bước nào)"
        run = next((r for r in await self.office.runs.recent(limit=200) if r["id"] == run_id), None)
        name = (await self.office.org.get_actor(run["actor_id"])).name if run else "?"
        head = f"📜 <b>{fmt.esc(name)}</b> · {fmt.esc(_what(run) if run else run_id)}\n"
        for i, piece in enumerate(fmt.split(body)):
            await self.bridge.send(thread, (head if i == 0 else "") + fmt.esc(piece))
        return "OK"

    async def _b_rs(self, thread: int | None, _message_id: int, run_id: str) -> str:
        await self.office.scheduler.stop_run(run_id)
        return "Đã yêu cầu dừng"

    async def toggle_progress(self, thread: int | None) -> None:
        progress = self.bridge.progress
        progress.enabled = not progress.enabled
        await self.bridge._set_state("progress", "1" if progress.enabled else "0")
        await self.bridge.send(
            thread,
            "🔔 Đã bật tin tiến độ trực tiếp: mỗi lượt làm việc có một tin cập nhật từng bước."
            if progress.enabled else "🔕 Đã tắt tin tiến độ trực tiếp. Gõ /activity khi muốn xem ai đang làm gì.",
        )

    # ---- inbox and usage -----------------------------------------------------------------------------

    async def inbox(self, thread: int | None) -> None:
        items = [i for i in await self.office.interactions.pending() if i.kind.value != "ask_colleague"]
        if not items:
            await self.bridge.send(thread, "📥 Không có việc nào đang chờ bạn.")
            return
        await self.bridge.send(thread, f"📥 <b>{len(items)} việc đang chờ bạn:</b>")
        for item in items[:15]:
            await self.bridge._send_card(thread, item.id)

    async def usage(self, thread: int | None) -> None:
        names = {a.id: a.name for a in await self.office.org.list_actors()}
        lines = ["📊 <b>Usage</b>"]
        for label, days in (("Hôm nay", 1), ("7 ngày", 7)):
            rows = sorted(await self.office.runs.usage_by_actor(since=_since(days)), key=lambda r: -float(r["cost_usd"] or 0))
            total = sum(float(r["cost_usd"] or 0) for r in rows)
            lines += ["", f"<b>{label}</b> · ≈${total:.2f}"]
            lines += [f"• {fmt.esc(names.get(r['actor_id'], r['actor_id']))}: {r['runs']} lượt · {_usage(r)}" for r in rows] or ["• chưa có"]
        await self.bridge.send(thread, "\n".join(lines))

    # ---- helpers -----------------------------------------------------------------------------------

    def _topic_url(self, thread: int | None) -> str | None:
        chat = str(self.bridge.chat_id or "")
        if not thread or not chat.startswith("-100"):
            return None
        return f"https://t.me/c/{chat[4:]}/{thread}"

    def _topic_link(self, thread: int | None) -> str:
        url = self._topic_url(thread)
        return f' <a href="{url}">Mở topic</a>' if url else ""


def _what(run: dict[str, Any]) -> str:
    if run.get("ticket_key"):
        return f"{run['ticket_key']} {run.get('ticket_title') or ''}".strip()[:50]
    if run.get("conversation_title"):
        return f"chat {run['conversation_title']}"[:50]
    return str(run.get("kind") or "")


def _since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def _usage(u: dict[str, Any]) -> str:
    total = sum(int(u.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
    return f"{fmt.tokens(total)} tok · ≈${float(u.get('cost_usd') or 0):.3f}"
