"""Live progress on Telegram: one message per AI run, edited in place as it works.

For a ticket running through stages the card shows the stage, the checklist of steps (☑ ▶ ☐), the latest
activity lines (normalized by engine/activity.py: same for Claude and Kiro) and buttons (status, pause). Other
runs keep the classic card ("⏳ Minh · AO-3 · …" with the latest steps). Chat replies that need no tools get no
card (the reply itself is the progress). Edits are throttled per run to stay under Telegram's rate limits.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from actoroffice.core.activity import event_line
from actoroffice.domain.errors import OfficeError
from actoroffice.services.stages import stage_title

from . import tg_format as fmt

if TYPE_CHECKING:
    from .telegram import TelegramBridge

log = logging.getLogger(__name__)
RETRIED_KINDS = {"network", "runtime_start"}  # engine/failures.py RETRIED: a failed run of these used up its retries
EDIT_EVERY_S = 10.0
SHOW_LINES = 15
FINAL_LINES = 30
ICONS = {
    "narration": "💬",
    "thinking": "💭",
    "read": "📖",
    "search": "🔎",
    "source": "🌐",
    "edit": "✏️",
    "command": "⌘",
    "office": "🗂",
    "question": "❓",
    "step": "☑",
    "check": "🧪",
    "deviation": "↪️",
    "stage": "📍",
    "usage": "⏱",
    "health": "⚠️",
    "error": "⚠️",
    "compacted": "🗜",
}


@dataclass
class Progress:
    run_id: str
    thread: int | None
    title: str  # "<b>Minh</b> · AO-3 · Báo cáo tuần"
    chat: bool  # a chat reply: only shown once the actor uses a tool
    ticket_id: int | None = None
    staged: bool = False
    lines: list[str] = field(default_factory=list)
    calls: dict[str, int] = field(default_factory=dict)  # call id → index in lines (replaced when the call ends)
    steps: list[dict[str, Any]] = field(default_factory=list)
    message_id: int | None = None
    last_edit: float = 0.0
    flush: asyncio.Task[None] | None = None
    started: float = field(default_factory=time.monotonic)


class LiveProgress:
    def __init__(self, bridge: TelegramBridge) -> None:
        self.bridge = bridge
        self.office = bridge.office
        self.runs: dict[str, Progress] = {}
        self.enabled = True

    def reset(self) -> None:
        """Forget every live message (the bot left their group): pending edits are cancelled."""
        for p in self.runs.values():
            if p.flush and not p.flush.done():
                p.flush.cancel()
        self.runs.clear()

    async def started(self, run: dict[str, Any]) -> None:
        if not self.enabled or run.get("kind") not in ("chat", "ticket"):
            return
        office = self.office
        staged = False
        try:
            actor = await office.org.get_actor(run["actor_id"])
            if run.get("ticket_id"):
                ticket = await office.tickets.get(run["ticket_id"])
                conv_id = await office.notifier._ticket_conv(ticket.model_dump())
                staged = bool(ticket.stage)
                stage = f" · <b>{fmt.esc(stage_title(ticket.stage))}</b>" if staged else ""
                title = f"<b>{fmt.esc(actor.name)}</b> · {ticket.key}{stage} · {fmt.esc(ticket.title[:80])}"
            else:
                conv_id = run.get("conversation_id")
                title = f"<b>{fmt.esc(actor.name)}</b> đang trả lời"
        except OfficeError:
            return
        thread = await self.bridge.topic_for(conv_id) if conv_id else None
        if conv_id and thread is None:
            return
        p = Progress(run["id"], thread, title, chat=run.get("kind") == "chat", ticket_id=run.get("ticket_id"), staged=staged)
        if staged and p.ticket_id:
            p.steps = await self._steps(p.ticket_id)
        self.runs[run["id"]] = p

    async def event(self, run_id: str, kind: str, payload: dict[str, Any], activity: dict[str, Any] | None = None) -> None:
        p = self.runs.get(run_id)
        if p is None:
            return
        if activity is not None:
            if activity.get("kind") in ("thinking", "usage"):
                return
            line = f"{ICONS.get(str(activity.get('kind')), '•')} {fmt.esc(str(activity.get('title', '')))}"
            call = activity.get("call_id")
            if call and call in p.calls and activity.get("status") != "running":
                p.lines[p.calls.pop(call)] = line  # the call ended: replace its "running" line
            else:
                if call and activity.get("status") == "running":
                    p.calls[call] = len(p.lines)
                p.lines.append(line)
            if activity.get("kind") == "step" and p.ticket_id:
                p.steps = await self._steps(p.ticket_id)
            new_tool = activity.get("kind") not in ("narration",)
        else:
            plain = event_line(kind, payload)
            if plain is None:
                return
            p.lines.append(f"{plain[0]} {fmt.esc(plain[1])}")
            new_tool = kind == "tool_use"
        await self._show(p, new_tool)

    async def steps_changed(self, ticket_id: int) -> None:
        for p in self.runs.values():
            if p.ticket_id == ticket_id and p.staged:
                p.steps = await self._steps(ticket_id)
                await self._show(p, True)

    async def _show(self, p: Progress, new_tool: bool) -> None:
        if p.message_id is None:
            if p.chat and not new_tool:
                return  # plain replies need no progress message
            p.message_id = await self.bridge.send(p.thread, self._text(p), self._buttons(p))
            p.last_edit = time.monotonic()
            return
        wait = EDIT_EVERY_S - (time.monotonic() - p.last_edit)
        if wait <= 0:
            await self._edit(p)
        elif p.flush is None or p.flush.done():
            p.flush = asyncio.create_task(self._later(p, wait))

    async def finished(self, run_id: str, run: dict[str, Any] | None, status: str) -> None:
        if status == "failed" and run and run.get("error_kind") in RETRIED_KINDS and run.get("ticket_id"):
            await self._out_of_retries(run)
        p = self.runs.pop(run_id, None)
        if p is None:
            return
        if p.flush and not p.flush.done():
            p.flush.cancel()
        if p.message_id is None:
            return
        secs = None
        if run and run.get("started_at") and run.get("finished_at"):
            secs = int((datetime.fromisoformat(run["finished_at"]) - datetime.fromisoformat(run["started_at"])).total_seconds())
        icon = {"succeeded": "✅", "parked": "⏸", "deferred": "⏸", "cancelled": "✖️", "rate_limited": "⏸"}.get(status, "⚠️")
        verb = {
            "succeeded": "xong lượt",
            "parked": "đang chờ người",
            "deferred": "chờ bạn duyệt",
            "cancelled": "đã dừng",
            "rate_limited": "tạm dừng (hết quota)",
        }.get(status, "gặp lỗi")
        head = f"{icon} {p.title} — {verb} · {len(p.lines)} bước" + (f" · {fmt.duration(secs)}" if secs else "")
        if p.staged and p.ticket_id:
            with contextlib.suppress(OfficeError):
                st = await self.office.stages.status(p.ticket_id)
                head += f"\n📍 {fmt.esc(st['stage_title'])}: {fmt.esc(st['status_label'])}" + (f" · chờ {fmt.esc(', '.join(st['who']))}" if st["who"] else "")
        if run:
            head += f"\n<i>{fmt.esc(fmt.usage_line(run.get('usage') or {}, run.get('stats') or {}, None, run.get('cost_usd')))}</i>"
        with contextlib.suppress(Exception):
            await self.bridge._edit(p.message_id, head + self._steps_block(p) + self._quote(p.lines[-FINAL_LINES:]), self._buttons(p, final=True))

    async def _out_of_retries(self, run: dict[str, Any]) -> None:
        """A run failed for a passing reason after all its automatic retries (SI-38 BR-8): a new message in the
        ticket's topic (an edit of the progress card notifies nobody), even with progress cards turned off."""
        try:
            ticket = await self.office.tickets.get(run["ticket_id"])
            conv_id = await self.office.notifier._ticket_conv(ticket.model_dump())
            thread = await self.bridge.topic_for(conv_id) if conv_id else None
            if conv_id and thread is None:
                return
            text = f"⚠️ {ticket.key} · {fmt.esc(ticket.title[:80])} — hết lượt tự thử lại\n{fmt.esc(str(run.get('error') or '')[:600])}"
            await self.bridge.send(thread, text)
        except Exception:
            log.exception("telegram: could not report run %s out of retries", run.get("id"))

    def _text(self, p: Progress) -> str:
        secs = int(time.monotonic() - p.started)
        return f"⏳ {p.title} · {fmt.duration(secs)}" + self._steps_block(p) + self._quote(p.lines[-SHOW_LINES:])

    def _steps_block(self, p: Progress) -> str:
        if not p.steps:
            return ""
        done = sum(1 for s in p.steps if s.get("done"))
        current = next((i for i, s in enumerate(p.steps) if not s.get("done")), None)
        rows = []
        for i, s in enumerate(p.steps):
            mark = "☑" if s.get("done") else ("▶" if i == current else "☐")
            rows.append(f"{mark} {fmt.esc(str(s.get('text', ''))[:80])}")
        return f"\nCác bước {done}/{len(p.steps)}:\n" + "\n".join(rows)

    def _buttons(self, p: Progress, *, final: bool = False) -> list[list[dict[str, str]]] | None:
        if not (p.staged and p.ticket_id):
            return None
        status = {"text": "📍 Trạng thái", "callback_data": f"t:{p.ticket_id}:status"}
        if final:
            return [[status]]
        return [[status, {"text": "⏸ Tạm dừng", "callback_data": f"t:{p.ticket_id}:pause"}]]

    async def _steps(self, ticket_id: int) -> list[dict[str, Any]]:
        with contextlib.suppress(OfficeError):
            ticket = await self.office.tickets.get(ticket_id)
            srun = await self.office.stages.current_run(ticket)
            if srun is not None:
                return [s.model_dump() for s in srun.steps]
        return []

    @staticmethod
    def _quote(lines: list[str]) -> str:
        if not lines:
            return ""
        body = "\n".join(lines)
        if len(body) > 3300:
            body = "…" + body[-3300:]
        return f"\n<blockquote expandable>{body}</blockquote>"

    async def _later(self, p: Progress, wait: float) -> None:
        await asyncio.sleep(wait)
        if p.run_id in self.runs:
            await self._edit(p)

    async def _edit(self, p: Progress) -> None:
        p.last_edit = time.monotonic()
        with contextlib.suppress(Exception):  # "message is not modified" and rate limits are not worth failing for
            await self.bridge._edit(p.message_id or 0, self._text(p), self._buttons(p))
