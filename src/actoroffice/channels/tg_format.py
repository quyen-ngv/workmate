"""Telegram rendering: Markdown → Telegram HTML, decision cards with buttons, per-message usage lines.

Telegram's HTML supports only b/i/u/s/code/pre/a/blockquote, so tables and headings are flattened. Messages
are capped at 4096 characters; long texts are split on paragraph boundaries before converting.
"""

from __future__ import annotations

import html
import re
from typing import Any

LIMIT = 3500  # raw characters per message; HTML tags add some, Telegram's hard limit is 4096

_FENCE = re.compile(r"```[^\n]*\n(.*?)(?:```|\Z)", re.DOTALL)
_INLINE = re.compile(r"`([^`\n]+)`")
_LINK = re.compile(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*|__(.+?)__")
_ITALIC = re.compile(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?!\w)|(?<!\w)_(?!\s)([^_\n]+?)_(?!\w)")
_HEADING = re.compile(r"(?m)^#{1,6}\s+(.+?)\s*#*$")
_BULLET = re.compile(r"(?m)^(\s*)[-*]\s+")


def md_to_html(text: str) -> str:
    """Convert the Markdown actors write into Telegram HTML. Code and tables become <pre> blocks."""
    keep: list[str] = []

    def stash(fragment: str) -> str:
        keep.append(fragment)
        return f"\x00{len(keep) - 1}\x00"

    text = _FENCE.sub(lambda m: stash(f"<pre>{html.escape(m.group(1).rstrip())}</pre>"), text)
    text = _tables(text, stash)
    text = _INLINE.sub(lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)
    text = _LINK.sub(lambda m: stash(f'<a href="{html.escape(m.group(2), quote=True)}">{html.escape(m.group(1))}</a>'), text)
    text = html.escape(text, quote=False)
    text = _HEADING.sub(lambda m: f"<b>{m.group(1)}</b>", text)
    text = _BOLD.sub(lambda m: f"<b>{m.group(1) or m.group(2)}</b>", text)
    text = _ITALIC.sub(lambda m: f"<i>{m.group(1) or m.group(2)}</i>", text)
    text = _BULLET.sub(lambda m: f"{m.group(1)}• ", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: keep[int(m.group(1))], text)


def _tables(text: str, stash: Any) -> str:
    out: list[str] = []
    block: list[str] = []
    for line in text.split("\n"):
        if line.strip().startswith("|"):
            block.append(line)
            continue
        if block:
            out.append(stash("<pre>" + html.escape("\n".join(block)) + "</pre>"))
            block = []
        out.append(line)
    if block:
        out.append(stash("<pre>" + html.escape("\n".join(block)) + "</pre>"))
    return "\n".join(out)


def split(text: str, limit: int = LIMIT) -> list[str]:
    """Split raw text into pieces of at most `limit` characters, preferring paragraph and line breaks
    (never inside a ``` block when it can be helped)."""
    if len(text) <= limit:
        return [text]
    pieces: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = -1
        for sep in ("\n\n", "\n", " "):
            cut = rest.rfind(sep, limit // 2, limit)
            if cut > 0 and rest[:cut].count("```") % 2 == 0:
                break
            cut = -1
        if cut <= 0:
            cut = limit
        pieces.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        pieces.append(rest)
    return pieces


def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


# ---- usage ----------------------------------------------------------------------------------------


def tokens(n: Any) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1e6:.1f}M"
    if n >= 1000:
        return f"{n / 1e3:.1f}k"
    return str(n)


def duration(secs: Any) -> str:
    if secs is None:
        return ""
    secs = int(secs)
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60} phút" + (f" {secs % 60}s" if secs % 60 else "")
    return f"{secs // 3600} giờ {round((secs % 3600) / 60)} phút"


def usage_line(usage: dict[str, Any], stats: dict[str, Any], duration_s: Any, cost_usd: Any) -> str:
    """Same facts as the web's line under each message: files, lines, commands, web, reads, time, tokens."""
    parts = []
    if stats.get("files_changed"):
        parts.append(f"📄 {stats['files_changed']} file · +{stats.get('lines_added', 0)} −{stats.get('lines_removed', 0)} dòng")
    if stats.get("commands"):
        parts.append(f"⌘ {stats['commands']} lệnh")
    if stats.get("web"):
        parts.append(f"🌐 {stats['web']} lần đọc web")
    if stats.get("reads"):
        parts.append(f"🔎 {stats['reads']} lần đọc file")
    if duration_s:
        parts.append(f"⏱ {duration(duration_s)}")
    total = sum(int(usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
    parts.append(f"{tokens(total)} tok · ≈${float(cost_usd or 0):.3f}")
    return " · ".join(parts)


def work_line(work: dict[str, Any]) -> str:
    """Totals for a whole ticket tree, shown under a "done" report."""
    head = f"Σ {work.get('runs', 0)} lượt chạy · {work.get('actors', 0)} người"
    return head + " · " + usage_line(work, work, work.get("duration_s"), work.get("cost_usd"))


# ---- decision cards ---------------------------------------------------------------------------------

Button = dict[str, str]


def step_label(step: dict[str, Any]) -> str:
    if step.get("type") == "notify":
        return f"💬 nhắn: {step.get('text', '')}"
    if step.get("type") == "script":
        return f"⚙️ chạy script {step.get('file', '')}"
    return f"🤖 giao {step.get('actor', '')}: {step.get('prompt', '')}"


def card(item: dict[str, Any]) -> tuple[str, list[list[Button]]]:
    """HTML text and inline keyboard for a pending decision. Callback data: "i:<interaction id>:<choice>"."""
    p = item.get("payload") or {}
    iid = item["id"]

    def btn(text: str, choice: str) -> Button:
        return {"text": text, "callback_data": f"i:{iid}:{choice}"}

    kind = item["kind"]
    if kind == "approve_tool":
        target = p.get("target") or str(p.get("input") or "")
        reason = " · ".join(x for x in (p.get("reason"), p.get("ticket")) if x)
        text = f"🔐 <b>{esc(p.get('actor'))} xin phép: {esc(p.get('tool'))}</b>\n<pre>{esc(target[:1500])}</pre>"
        if reason:
            text += f"\n<i>{esc(reason)}</i>"
        rows = [[btn("✅ Cho phép 1 lần", "y"), btn("⛔ Từ chối", "n")]]
        for i, s in enumerate((p.get("suggestions") or [])[:4]):
            short = re.sub(r"^\w+\(|\)$", "", str(s.get("rule", "")))  # "Bash(ls -a*)" → "ls -a*"
            labels = {"exact": "Luôn cho phép lệnh này", "tool": f"Luôn cho phép mọi {s.get('rule', '')}"}
            label = labels.get(str(s.get("scope")), f"Luôn cho phép {short}")
            rows.append([btn(("♾ " + label)[:60], f"r{i}")])
        return text, rows
    if kind == "question":
        text = f"❓ <b>{esc(p.get('asked_by'))} hỏi bạn</b>\n{md_to_html(str(p.get('question', ''))[:3000])}"
        rows = [[btn(str(o)[:60], f"o{i}")] for i, o in enumerate((p.get("options") or [])[:8])]
        return text + "\n\n<i>↩️ Reply tin này để trả lời bằng chữ.</i>", rows
    if kind == "confirm_ticket":
        desc = str(p.get("description") or "")
        text = (
            f"🎫 <b>{esc(p.get('proposed_by'))} đề xuất ticket → {esc(p.get('assignee'))}</b>\n<b>{esc(p.get('title'))}</b>"
            + (f"\n{md_to_html(desc[:2500])}" if desc else "")
        )
        return text, [[btn("✅ Tạo ticket", "y"), btn("Bỏ", "n")]]
    if kind == "approve_automation":
        steps = "\n".join(f"{i}. {esc(step_label(s))}" for i, s in enumerate(p.get("steps") or [], 1))
        code = p.get("code") or {}
        code_text = "".join(f"\n<b>{esc(name)}</b>\n<pre>{esc(body[:1800])}</pre>" for name, body in list(code.items())[:2])
        more = "\n<i>(code dài, xem đủ trên web)</i>" if any(len(b) > 1800 for b in code.values()) or len(code) > 2 else ""
        text = (
            f"⚙️ <b>{esc(p.get('owner'))} xin duyệt automation: {esc(p.get('name'))}</b>\n{md_to_html(str(p.get('description') or ''))}\n"
            f"Lịch: <b>{esc(p.get('schedule_text') or 'chạy tay')}</b>\n{steps}{code_text}{more}"
        )
        return text, [[btn("✅ Duyệt & chạy thử", "y"), btn("Duyệt, chưa chạy", "a")], [btn("⛔ Từ chối", "n")]]
    if kind == "approve_access":
        text = (
            f"🔓 <b>{esc(p.get('actor'))} xin quyền sửa dự án {esc(p.get('project'))}</b>\n<code>{esc(p.get('path'))}</code>\n"
            f"{esc(p.get('reason'))}" + (f"\n<i>{esc(p.get('ticket'))}</i>" if p.get("ticket") else "")
        )
        labels = {"worktree": "🌿 Git worktree (an toàn, merge sau)", "direct": "✏️ Sửa trực tiếp"}
        rows = [[btn(labels[o], f"g{o[0]}")] for o in p.get("options") or [] if o in labels]
        return text, rows + [[btn("⛔ Không cho phép", "n")]]
    if kind == "ask_colleague":
        text = f"🗣 <b>{esc(p.get('asked_by'))} hỏi {esc(p.get('to'))}</b> ({esc(p.get('ticket'))})\n{md_to_html(str(p.get('question', ''))[:3000])}"
        return text + "\n\n<i>↩️ Reply tin này để trả lời thay.</i>", []
    return f"<b>{esc(kind)}</b>", []


def response_for(item: dict[str, Any], choice: str) -> dict[str, Any]:
    """Turn a button press into the same answer the web UI sends."""
    p = item.get("payload") or {}
    kind = item["kind"]
    if choice == "n":
        return {"confirmed": False} if kind == "confirm_ticket" else {"approved": False}
    if kind == "approve_tool":
        if choice.startswith("r"):
            return {"approved": True, "remember": (p.get("suggestions") or [])[int(choice[1:])]["rule"]}
        return {"approved": True}
    if kind == "confirm_ticket":
        return {"confirmed": True}
    if kind == "approve_automation":
        return {"approved": True, "run_now": choice == "y"}
    if kind == "approve_access" and choice in ("gw", "gd"):
        return {"approved": True, "access": "worktree" if choice == "gw" else "direct"}
    if kind == "question" and choice.startswith("o"):
        return {"answer": str((p.get("options") or [])[int(choice[1:])])}
    raise ValueError(f"unknown choice {choice!r} for {kind}")


def verdict(item: dict[str, Any]) -> str:
    """One line shown on a card once it is answered (here, on the web or by a colleague)."""
    r = item.get("response") or {}
    kind, status = item["kind"], item.get("status")
    if status in ("expired", "cancelled"):
        return "⌛ Đã hết hạn" if status == "expired" else "✖️ Đã huỷ"
    if kind == "approve_tool":
        if not r.get("approved"):
            return "⛔ Đã từ chối"
        return f"✅ Đã cho phép và ghi nhớ: {r['remember']}" if r.get("remember") else "✅ Đã cho phép 1 lần"
    if kind == "confirm_ticket":
        return f"✅ Đã tạo {r['ticket_key']}" if r.get("ticket_key") else "Đã bỏ"
    if kind == "approve_automation":
        return "✅ Đã duyệt và bật" if r.get("approved") else "⛔ Đã từ chối"
    if kind == "approve_access":
        mode = {"worktree": "git worktree", "direct": "sửa trực tiếp"}.get(str(r.get("access")), "")
        return f"✅ Đã cho phép ({mode})" if r.get("approved") else "⛔ Không cho phép"
    who = r.get("answered_by") or "Bạn"
    return f"💬 {who} trả lời: {r.get('answer', '')}"
