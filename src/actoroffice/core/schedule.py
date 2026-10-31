"""Tiny schedule language shared by reminders and automations (local time of this machine).

    every:N            every N minutes
    daily@HH:MM        every day at HH:MM
    weekdays@HH:MM     Monday to Friday at HH:MM
    weekly:mon@HH:MM   once a week (mon tue wed thu fri sat sun)

`when` (first firing) accepts "2026-10-12 09:00", "09:00" (next occurrence), "in 30m" / "in 2h" / "in 1d".
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_REPEAT = re.compile(r"^(every:(\d+)|daily@(\d{1,2}):(\d{2})|weekdays@(\d{1,2}):(\d{2})|weekly:(mon|tue|wed|thu|fri|sat|sun)@(\d{1,2}):(\d{2}))$")
_RELATIVE = re.compile(r"^(?:in\s+|\+)(\d+)\s*(m|min|mins|minutes?|h|hours?|d|days?)$")


class ScheduleError(ValueError):
    pass


def local_now() -> datetime:
    return datetime.now().astimezone()


def to_utc_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(text: str) -> datetime:
    moment = datetime.fromisoformat(text)
    return moment if moment.tzinfo else moment.astimezone()


def describe_now(now: datetime | None = None) -> str:
    now = now or local_now()
    return now.strftime("%A %Y-%m-%d %H:%M") + f" (UTC{now.strftime('%z')[:3]}:{now.strftime('%z')[3:]})"


def normalize_repeat(text: str | None) -> str | None:
    """Accept a few friendly spellings; return the canonical form or raise ScheduleError."""
    if text is None:
        return None
    value = text.strip().lower().replace(" ", "")
    if value in ("", "none", "once", "no"):
        return None
    aliases = {"hourly": "every:60"}
    value = aliases.get(value, value)
    if m := re.fullmatch(r"every(\d+)(m|min|minutes?)?", value):
        value = f"every:{m.group(1)}"
    if m := re.fullmatch(r"every(\d+)(h|hours?)", value):
        value = f"every:{int(m.group(1)) * 60}"
    m = _REPEAT.match(value)
    if not m:
        raise ScheduleError(
            f"Unknown schedule '{text}'. Use every:N (minutes), daily@HH:MM, weekdays@HH:MM or weekly:mon@HH:MM."
        )
    if m.group(2) and int(m.group(2)) < 1:
        raise ScheduleError("every:N needs N >= 1 minute")
    hour = next((int(g) for g in (m.group(3), m.group(5), m.group(8)) if g), 0)
    minute = next((int(g) for g in (m.group(4), m.group(6), m.group(9)) if g), 0)
    if hour > 23 or minute > 59:
        raise ScheduleError(f"Invalid time in '{text}'")
    return value


def parse_when(text: str, now: datetime | None = None) -> datetime:
    """First firing time, as an aware local datetime."""
    now = now or local_now()
    value = text.strip().lower()
    if m := _RELATIVE.match(value):
        amount, unit = int(m.group(1)), m.group(2)[0]
        delta = {"m": timedelta(minutes=amount), "h": timedelta(hours=amount), "d": timedelta(days=amount)}[unit]
        return now + delta
    if m := re.fullmatch(r"(\d{1,2}):(\d{2})", value):
        candidate = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        return candidate if candidate > now else candidate + timedelta(days=1)
    try:
        moment = datetime.fromisoformat(text.strip().replace("/", "-"))
    except ValueError as exc:
        raise ScheduleError(f"Cannot read time '{text}'. Use 'YYYY-MM-DD HH:MM', 'HH:MM' or 'in 30m'.") from exc
    return moment if moment.tzinfo else moment.replace(tzinfo=now.tzinfo)


def next_after(repeat: str, after: datetime) -> datetime:
    """The first occurrence of `repeat` strictly after `after` (local time)."""
    after = after.astimezone()
    if repeat.startswith("every:"):
        return after + timedelta(minutes=int(repeat.split(":", 1)[1]))
    kind, clock = repeat.split("@", 1)
    hour, minute = (int(x) for x in clock.split(":"))
    candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    for _ in range(8):
        weekday = candidate.weekday()
        if kind == "daily":
            return candidate
        if kind == "weekdays" and weekday < 5:
            return candidate
        if kind.startswith("weekly:") and DAYS[weekday] == kind.split(":", 1)[1]:
            return candidate
        candidate += timedelta(days=1)
    raise ScheduleError(f"Cannot compute next time for '{repeat}'")


def describe(repeat: str | None) -> str:
    if not repeat:
        return "một lần"
    if repeat.startswith("every:"):
        minutes = int(repeat.split(":", 1)[1])
        return f"mỗi {minutes // 60} giờ" if minutes % 60 == 0 else f"mỗi {minutes} phút"
    kind, clock = repeat.split("@", 1)
    if kind == "daily":
        return f"hằng ngày lúc {clock}"
    if kind == "weekdays":
        return f"thứ 2–6 lúc {clock}"
    day = {"mon": "thứ 2", "tue": "thứ 3", "wed": "thứ 4", "thu": "thứ 5", "fri": "thứ 6", "sat": "thứ 7", "sun": "chủ nhật"}
    return f"{day[kind.split(':', 1)[1]]} hằng tuần lúc {clock}"
