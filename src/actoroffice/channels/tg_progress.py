"""Live progress on Telegram: one message per actor run, edited as the actor works.

"⏳ Minh · AO-3 · …" with the latest steps (plan, what it is doing and why, tool calls, errors) in an expandable
quote, then "✅ … xong" when the run ends. Chat replies that need no tools get no progress message (the
reply itself is the progress). Edits are throttled per run to stay under Telegram's rate limits.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

from actoroffice.core.activity import event_line
from actoroffice.domain.errors import OfficeError

from . import tg_format as fmt

if TYPE_CHECKING:
    from .telegram import TelegramBridge

EDIT_EVERY_S = 10.0
SHOW_LINES = 15
FINAL_LINES = 30


@dataclass
class Progress:
    run_id: str
    thread: int | None
    title: str  # "<b>Minh</b> · AO-3 · Báo cáo tuần"
    chat: bool  # a chat reply: only shown once the actor uses a tool
    lines: list[str] = field(default_factory=list)
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

    async def started(self, run: dict[str, Any]) -> None:
        if not self.enabled or run.get("kind") not in ("chat", "ticket"):
            return
        office = self.office
        try:
            actor = await office.org.get_actor(run["actor_id"])
            if run.get("ticket_id"):
                ticket = await office.tickets.get(run["ticket_id"])
                conv_id = await office.notifier._ticket_conv(ticket.model_dump())
                title = f"<b>{fmt.esc(actor.name)}</b> · {ticket.key} · {fmt.esc(ticket.title[:80])}"
            else:
                conv_id = run.get("conversation_id")
                title = f"<b>{fmt.esc(actor.name)}</b> đang trả lời"
        except OfficeError:
            return
        thread = await self.bridge.topic_for(conv_id) if conv_id else None
        if conv_id and thread is None:
            return
        self.runs[run["id"]] = Progress(run["id"], thread, title, chat=run.get("kind") == "chat")

    async def event(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        p = self.runs.get(run_id)
        if p is None:
            return
        line = event_line(kind, payload)
        if line is None:
            return
        p.lines.append(f"{line[0]} {fmt.esc(line[1])}")
        if p.message_id is None:
            if p.chat and kind != "tool_use":
                return  # plain replies need no progress message
            p.message_id = await self.bridge.send(p.thread, self._text(p))
            p.last_edit = time.monotonic()
            return
        wait = EDIT_EVERY_S - (time.monotonic() - p.last_edit)
        if wait <= 0:
            await self._edit(p)
        elif p.flush is None or p.flush.done():
            p.flush = asyncio.create_task(self._later(p, wait))

    async def finished(self, run_id: str, run: dict[str, Any] | None, status: str) -> None:
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
        icon = {"succeeded": "✅", "deferred": "⏸", "cancelled": "✖️", "rate_limited": "⏸"}.get(status, "⚠️")
        verb = {"succeeded": "xong lượt", "deferred": "chờ bạn duyệt", "cancelled": "đã dừng", "rate_limited": "tạm dừng (hết quota)"}.get(status, "gặp lỗi")
        head = f"{icon} {p.title} — {verb} · {len(p.lines)} bước" + (f" · {fmt.duration(secs)}" if secs else "")
        if run:
            head += f"\n<i>{fmt.esc(fmt.usage_line(run.get('usage') or {}, run.get('stats') or {}, None, run.get('cost_usd')))}</i>"
        with contextlib.suppress(Exception):
            await self.bridge._edit(p.message_id, head + self._quote(p.lines[-FINAL_LINES:]))

    def _text(self, p: Progress) -> str:
        secs = int(time.monotonic() - p.started)
        return f"⏳ {p.title} · {fmt.duration(secs)}" + self._quote(p.lines[-SHOW_LINES:])

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
            await self.bridge._edit(p.message_id or 0, self._text(p))
