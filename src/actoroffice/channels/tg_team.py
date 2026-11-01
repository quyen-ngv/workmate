"""Team mode on Telegram: who is pressing a button or writing (Telegram id → person → roles), the people
commands (/whoami, /people, /adduser) and the commands that steer a ticket from its topic
(/task, /status, /pause, /resume, /retry, /stop, /steer, /merged, /assume, /wait).

Before anyone is registered the office stays single-user (everyone in the bound group acts as the owner).
Once people exist, only registered people can act; a role-gated card only accepts people holding the role.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import Conflict, NotFound, OfficeError
from actoroffice.domain.team import LOCAL_OWNER_ID, ROLE_VI

from . import tg_format as fmt

if TYPE_CHECKING:
    from .telegram import TelegramBridge

TEAM_COMMANDS = [
    ("task", "Tạo việc cho AI: /task [code|research|doc|automation] tiêu đề"),
    ("status", "Trong topic ticket: AI đang ở đâu, đang làm gì"),
    ("steer", "Trong topic ticket: chỉ dẫn thêm cho AI"),
    ("pause", "Trong topic ticket: tạm dừng AI"),
    ("resume", "Trong topic ticket: cho AI làm tiếp"),
    ("retry", "Trong topic ticket: làm lại giai đoạn hiện tại"),
    ("merged", "Trong topic ticket: báo PR đã merge"),
    ("whoami", "Xem Telegram id và vai của bạn"),
    ("people", "Danh sách người và vai"),
    ("adduser", "Admin: reply tin của ai đó + /adduser Tên vai1 vai2"),
]
TEMPLATES = ("code", "research", "doc", "automation")
TEAM_HELP = (
    "👥 <b>Làm việc cùng AI</b>\n"
    "• /task code Tên việc — tạo việc, AI tìm hiểu rồi hỏi đúng người (BA/Dev/QE).\n"
    "• Mỗi việc có một topic riêng: nhắn trong đó là AI nghe; thẻ ✋ là cổng duyệt cho đúng vai.\n"
    "• Trong topic: /status · /steer chỉ dẫn · /pause · /resume · /retry · /merged.\n"
    "• /whoami xem vai của bạn · /people danh sách người."
)


class TeamCommands:
    def __init__(self, bridge: TelegramBridge) -> None:
        self.bridge = bridge
        self.office = bridge.office

    # ---- identity -------------------------------------------------------------------------------------

    async def who(self, sender: dict[str, Any] | None) -> str | None:
        """The acting person's id: "user" (owner) in single-user mode, a registered person, or None (unknown)."""
        if not await self.office.teams.has_people():
            return LOCAL_OWNER_ID
        if not sender or "id" not in sender:
            return None
        user = await self.office.teams.user_by_telegram(int(sender["id"]))
        return user.id if user else None

    async def is_admin(self, user_id: str | None) -> bool:
        if user_id is None:
            return False
        if user_id == LOCAL_OWNER_ID:
            return True
        return "admin" in await self.office.teams.roles_for(user_id, None)

    # ---- commands -----------------------------------------------------------------------------------

    async def command(self, command: str, args: str, msg: dict[str, Any], thread: int | None) -> bool:
        """Handle a team command; False when it is not one of ours."""
        name = command.lstrip("/")
        if name not in {c for c, _ in TEAM_COMMANDS}:
            return False
        sender = msg.get("from") or {}
        if name == "whoami":
            await self._whoami(sender, thread)
            return True
        if name == "people":
            await self._people(thread)
            return True
        who = await self.who(sender)
        if who is None:
            await self.bridge.send(thread, f"<i>Bạn chưa có trong danh sách (Telegram id <code>{sender.get('id')}</code>). Nhờ admin reply tin của bạn với /adduser Tên vai.</i>")
            return True
        try:
            if name == "adduser":
                await self._adduser(who, args, msg, thread)
            elif name == "task":
                await self._task(who, args, thread)
            else:
                await self._ticket_command(name, who, args, thread)
        except OfficeError as exc:
            await self.bridge.send(thread, f"<i>Không thực hiện được: {fmt.esc(exc)}</i>")
        return True

    async def _whoami(self, sender: dict[str, Any], thread: int | None) -> None:
        tid = sender.get("id")
        user = await self.office.teams.user_by_telegram(int(tid)) if tid else None
        if user is None:
            await self.bridge.send(thread, f"Telegram id của bạn: <code>{tid}</code>. Bạn chưa được đăng ký.")
            return
        lines = [f"👤 <b>{fmt.esc(user.name)}</b> · Telegram id <code>{tid}</code>"]
        if user.default_roles:
            lines.append("Vai chung: " + ", ".join(ROLE_VI.get(r, r) for r in user.default_roles))  # type: ignore[call-overload]
        for team in await self.office.teams.teams_of(user.id):
            member = next((m for m in await self.office.teams.members(team.id) if m.user.id == user.id), None)
            roles = ", ".join(ROLE_VI.get(r, r) for r in (member.roles if member else []))  # type: ignore[call-overload]
            lines.append(f"Team {fmt.esc(team.name)}: {fmt.esc(roles)}")
        await self.bridge.send(thread, "\n".join(lines))

    async def _people(self, thread: int | None) -> None:
        teams = await self.office.teams.list_teams()
        lines = ["👥 <b>Người và vai</b>"]
        for team in teams:
            lines.append(f"\n<b>{fmt.esc(team.name)}</b>" + (f" · Jira {fmt.esc(team.jira_project_key)}" if team.jira_project_key else ""))
            for m in await self.office.teams.members(team.id):
                roles = ", ".join(ROLE_VI.get(r, r) for r in m.roles)  # type: ignore[call-overload]
                lines.append(f"• {fmt.esc(m.user.name)}: {fmt.esc(roles)}")
        loose = [u for u in await self.office.teams.list_users() if u.id != LOCAL_OWNER_ID and not await self.office.teams.teams_of(u.id)]
        if loose:
            lines.append("\n<b>Chưa vào team</b>")
            lines += [f"• {fmt.esc(u.name)}" for u in loose]
        if len(lines) == 1:
            lines.append("Chưa có ai. Admin dùng /adduser (reply tin của người đó).")
        await self.bridge.send(thread, "\n".join(lines))

    async def _adduser(self, who: str, args: str, msg: dict[str, Any], thread: int | None) -> None:
        """/adduser Tên vai… as a reply to the person's message (their Telegram id comes from that message)."""
        if await self.office.teams.has_people() and not await self.is_admin(who):
            raise OfficeError("Chỉ admin được thêm người")
        target = (msg.get("reply_to_message") or {}).get("from") or {}
        parts = args.split()
        if not parts:
            raise OfficeError("Cách dùng: reply tin của người đó, gõ /adduser Tên ba|dev|qe|lead|admin")
        if not target.get("id") or target.get("is_bot") or (msg.get("reply_to_message") or {}).get("forum_topic_created"):
            target = msg.get("from") or {}  # no reply: register yourself (first person = admin)
        name, roles = parts[0], [r.lower() for r in parts[1:]]
        first = not await self.office.teams.has_people()
        if first and "admin" not in roles:
            roles.append("admin")  # the first registered person administers the office
        existing = await self.office.teams.user_by_telegram(int(target["id"]))
        if existing:
            user = await self.office.teams.update_user(existing.id, name=name, roles=roles or existing.default_roles)
        else:
            user = await self.office.teams.create_user(name, roles=roles, telegram_id=int(target["id"]))
        teams = await self.office.teams.list_teams()
        if len(teams) == 1:
            team_roles = [r for r in roles if r != "admin"]
            if team_roles:
                await self.office.teams.add_member(teams[0].id, user.id, team_roles)
        label = ", ".join(ROLE_VI.get(r, r) for r in roles) or "chưa có vai"  # type: ignore[call-overload]
        await self.bridge.send(thread, f"✅ Đã đăng ký <b>{fmt.esc(user.name)}</b> ({fmt.esc(label)})")

    async def _task(self, who: str, args: str, thread: int | None) -> None:
        parts = args.split(maxsplit=1)
        template = "code"
        if parts and parts[0].lower() in TEMPLATES:
            template, args = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
        title = args.strip()
        if not title:
            raise OfficeError("Cách dùng: /task [code|research|doc|automation] tiêu đề việc")
        team_id = None
        if who != LOCAL_OWNER_ID:
            teams = await self.office.teams.teams_of(who)
            team_id = teams[0].id if teams else None
        elif teams := await self.office.teams.list_teams():
            team_id = teams[0].id
        ticket = await self.office.tickets.create(title[:200], description=title, created_by=who)
        ticket = await self.office.stages.start(
            ticket.id, template=template, by=who, team_id=team_id, owner_user_id=who if who != LOCAL_OWNER_ID else None
        )
        conv = ticket.origin_conversation_id
        topic = await self.bridge.topic_for(conv) if conv else None
        await self.bridge.send(thread, f"🎫 Đã tạo <b>{fmt.esc(ticket.key)}</b> · {fmt.esc(ticket.title)} — trao đổi trong topic riêng của việc này.")
        if topic:
            await self.bridge.send(topic, f"🎫 <b>{fmt.esc(ticket.key)} · {fmt.esc(ticket.title)}</b>\nQuy trình: {fmt.esc(template)}. Nhắn ở đây là AI nghe.\n" + TEAM_HELP)
        self.office.scheduler.kick()

    async def ticket_of_thread(self, thread: int | None) -> Any:
        conv_id = await self.bridge._conversation_of(thread)
        if conv_id is None:
            return None
        conv = await self.office.chat.get(conv_id)
        if not conv.ticket_id:
            return None
        with contextlib.suppress(NotFound):
            return await self.office.tickets.get(conv.ticket_id)
        return None

    async def _ticket_command(self, name: str, who: str, args: str, thread: int | None) -> None:
        ticket = await self.ticket_of_thread(thread)
        if ticket is None or not ticket.stage:
            raise OfficeError("Lệnh này dùng trong topic của một việc (ticket)")
        stages = self.office.stages
        if name == "status":
            await self.bridge.send(thread, await self.status_text(ticket.id, viewer=who))
            return
        if name == "pause":
            await stages.pause(ticket.id, by=who)
        elif name == "resume":
            await stages.resume(ticket.id, by=who)
        elif name == "retry":
            await stages.retry(ticket.id, by=who)
        elif name == "steer":
            await stages.steer(ticket.id, args, by=who)
        elif name == "merged":
            if not await self.office.teams.can_act(who, ticket, ["dev", "lead"]) and who != LOCAL_OWNER_ID:
                raise OfficeError("Cần vai Dev hoặc Lead")
            await stages.external_passed(ticket.id, "merge", by=who, note=args.strip() or "PR đã merge")
        await self.bridge.send(thread, f"<i>{fmt.esc(name)}: đã ghi nhận.</i>")
        self.office.scheduler.kick()

    async def status_text(self, ticket_id: int, *, viewer: str | None = None) -> str:
        st = await self.office.stages.status(ticket_id, viewer=viewer)
        lines = [f"📍 <b>{fmt.esc(st['ticket'])}</b> · {fmt.esc(st['title'])}", f"Giai đoạn: <b>{fmt.esc(st['stage_title'])}</b> (lần {st['attempt']}) · {fmt.esc(st['status_label'])}"]
        if st["who"]:
            lines.append("Đang chờ: " + fmt.esc(", ".join(st["who"])))
        steps = st["steps"]
        if steps:
            lines.append(f"Các bước {st['steps_done']}/{len(steps)}:")
            lines += [("☑" if s["done"] else "☐") + " " + fmt.esc(s["text"]) for s in steps]
        if st["run_id"]:
            last = await self.office.runs.last_activity(st["run_id"])
            if last:
                lines.append("Đang: " + fmt.esc(last["title"]))
        usage = st["usage"] or {}
        tokens = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        if tokens:
            lines.append(f"<i>Giai đoạn này: {tokens / 1000:.1f}k tok</i>")
        if st["pr_url"]:
            lines.append(f"PR: {fmt.esc(st['pr_url'])}")
        return "\n".join(lines)

    # ---- buttons on progress cards ----------------------------------------------------------------------

    async def button(self, data: str, sender: dict[str, Any], thread: int | None) -> str:
        """Callback "t:<ticket id>:<action>" from a progress card."""
        _prefix, ticket_id, action = data.split(":", 2)
        who = await self.who(sender)
        if who is None:
            return "Bạn chưa được đăng ký"
        stages = self.office.stages
        if action == "pause":
            await stages.pause(int(ticket_id), by=who)
            return "Đã tạm dừng"
        if action == "resume":
            await stages.resume(int(ticket_id), by=who)
            return "Đã cho làm tiếp"
        if action == "status":
            await self.bridge.send(thread, await self.status_text(int(ticket_id), viewer=who))
            return "Đã gửi trạng thái"
        raise Conflict(f"không rõ thao tác {action}")
