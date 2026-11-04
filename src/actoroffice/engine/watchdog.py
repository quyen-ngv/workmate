"""Watchdog for a running ticket run (instead of token budgets): it notices when the AI is slow, stalled or
going round in circles, says so in the activity stream, and stops loops by making the next tool call ask a
developer instead.

- slow:     one command has been running longer than `slow_command_s`
- stalled:  no event at all for `stall_after_s` (per stage) while no command is running within its limit; the
            executor then stops the runtime and retries the turn as a network/API failure (SI-38 BR-4). Time the
            machine slept is not counted (engine/clock.py)
- editing:  10 different edits of one file with no command (test/build run) in between: a warning, and the next
            edit of that file is held back once with a reminder to run a syntax check or the related tests
- looping:  the same failing command 3 times, the same part of a file read 4 times, the same edit made twice
            (so an edit redone after it was undone), or 2 undone edits of one file with no command in between.
            Different edits of one file are normal work, however many, and so is undoing one misplaced edit
            once: neither is a loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .activity import describe_call
from .clock import AwakeClock

if TYPE_CHECKING:
    from actoroffice.app import Office

FAILS_FOR_LOOP = 3
READS_FOR_LOOP = 4
EDITS_FOR_WARNING = 10
UNDOS_FOR_LOOP = 2


def normalize_command(cmd: str) -> str:
    cmd = re.sub(r"\s+", " ", cmd.strip().lower())
    return re.sub(r"\d{3,}", "N", cmd)  # ports, pids, timestamps


@dataclass
class Watchdog:
    office: Office
    run_id: str
    stall_after_s: float
    slow_command_s: float
    command_limit_s: float = 900  # a command running longer is not a reason to wait: the guard stops it
    clock: AwakeClock = field(default_factory=AwakeClock)
    on_stall: Callable[[], None] | None = None  # the executor's hook: stop the runtime and retry
    last_event: float = 0.0
    stalled: bool = False
    failures: Counter[str] = field(default_factory=Counter)
    reads: Counter[str] = field(default_factory=Counter)
    edits_since_command: Counter[str] = field(default_factory=Counter)
    edit_sigs: dict[str, set[str]] = field(default_factory=dict)  # path → edits made since the last command
    undos: Counter[str] = field(default_factory=Counter)  # path → edits undone since the last command
    running: dict[str, tuple[str, float]] = field(default_factory=dict)  # call id → (command, started)
    reported: set[str] = field(default_factory=set)
    loop: str | None = None  # set when a loop is detected; the gate turns it into a stop-and-ask
    nudge: str | None = None  # a file edited many times with no command: its next edit is held back once
    nudged_sig: str | None = None  # the edit held back, so making it again is not counted as a repeat
    _task: asyncio.Task[None] | None = None

    def __post_init__(self) -> None:
        self.last_event = self.clock.now()

    def reset(self) -> None:
        """A new try of the turn: nothing is running, the stall clock starts again."""
        self.last_event = self.clock.now()
        self.stalled = False
        self.running.clear()
        self.reported = {r for r in self.reported if r == "told"}

    def start(self) -> None:
        self._task = asyncio.create_task(self._watch(), name=f"watchdog-{self.run_id}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def observe(self, activity: dict[str, Any] | None) -> None:
        self.last_event = self.clock.now()
        self.reported.discard("stalled")
        if not activity:
            return
        kind, status, call = activity.get("kind"), activity.get("status"), activity.get("call_id")
        fields = activity.get("fields") or {}
        if kind == "command":
            command = normalize_command(str(fields.get("command") or ""))
            if status == "running" and call:
                self.running[call] = (command, self.clock.now())
                return
            self.running.pop(str(call), None)
            self.edits_since_command.clear()
            self.edit_sigs.clear()
            self.undos.clear()
            self.nudge = self.nudged_sig = None
            if status == "error":
                self.failures[command] += 1
                if self.failures[command] >= FAILS_FOR_LOOP:
                    await self._loop(f"lệnh `{command[:80]}` lỗi {self.failures[command]} lần liền")
            else:
                self.failures.pop(command, None)
        elif kind == "read" and status == "running":
            path = str(fields.get("path") or fields.get("command") or "")
            path = f"{path}@{fields['part']}" if fields.get("part") not in (None, "", ":") else path
            if path:
                self.reads[path] += 1
                if self.reads[path] >= READS_FOR_LOOP:
                    await self._loop(f"đọc lại `{path[-80:]}` {self.reads[path]} lần")
        elif kind == "edit" and status == "running":
            path = str(fields.get("path") or "")
            seen = self.edit_sigs.setdefault(path, set())
            sig, undo = fields.get("sig"), fields.get("undo")
            if sig and sig in seen and sig == self.nudged_sig:
                self.nudged_sig = None  # the edit the nudge held back, made again: not a repeat
            elif sig and sig in seen:
                await self._loop(f"lặp lại đúng một chỗ sửa trong `{path[-80:]}`")
            elif undo and undo in seen and undo != sig:
                self.undos[path] += 1  # one undo fixes a misplaced edit; a second one is going back and forth
                if self.undos[path] >= UNDOS_FOR_LOOP:
                    await self._loop(f"hoàn tác {self.undos[path]} lần trong `{path[-80:]}`")
            if sig:
                seen.add(sig)
            self.edits_since_command[path] += 1
            if self.edits_since_command[path] == EDITS_FOR_WARNING:  # different edits: a nudge, not a stop (SI-22)
                await self._health(
                    "editing",
                    f"Đã sửa `{path[-80:]}` {EDITS_FOR_WARNING} lần mà chưa chạy lệnh nào: nên kiểm tra cú pháp hoặc chạy test liên quan",
                )
                self.nudge = path

    async def _loop(self, reason: str) -> None:
        if self.loop is None:
            self.loop = reason
            await self._health("looping", f"Có vẻ đang lặp: {reason}. Mình sẽ dừng và hỏi Dev.")

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(min(15.0, max(1.0, self.stall_after_s / 4)))
            await self.check()

    async def check(self) -> None:
        now = self.clock.now()
        slept = self.clock.take_unreported()
        if slept:
            await self._health("sleep", f"Máy ngủ {int(slept)}s: không tính vào thời gian kẹt hay thời gian của lệnh")
        # a running command counts as activity up to its limit: once past it the guard stops it, and the runtime then
        # gets the whole stall threshold to answer (the CLI's own timeout is the limit + 60 s), not a stall at once
        quiet_since = max([self.last_event, *(started + self.command_limit_s for _command, started in self.running.values())])
        if now - quiet_since > self.stall_after_s and "stalled" not in self.reported:
            self.reported.add("stalled")
            self.stalled = True
            await self._health("stalled", f"Có vẻ bị kẹt: {int(now - quiet_since)}s không có hoạt động; dừng lượt này rồi tự thử lại")
            if self.on_stall is not None:
                self.on_stall()
        for call, (command, started) in list(self.running.items()):
                key = f"slow:{call}"
                if now - started > self.slow_command_s and key not in self.reported:
                    self.reported.add(key)
                    await self._health("slow", f"Lệnh `{command[:80]}` chạy đã {int(now - started)}s")

    async def _health(self, state: str, title: str) -> None:
        with contextlib.suppress(Exception):
            await self.office.runs.event(self.run_id, "health", {"state": state, "title": title})

    def stop_message(self) -> str | None:
        """Once per loop: the text the gate returns instead of running the next tool call."""
        if self.loop is None or "told" in self.reported:
            return None
        self.reported.add("told")
        return (
            f"STOP: you seem to be going round in circles ({self.loop}). This tool call was NOT run. "
            "Summarize what you tried and what you found, then ask a developer with ask_human (roles [dev]) how to "
            "proceed, and end your turn."
        )

    def nudge_message(self, tool: str, args: dict[str, Any]) -> str | None:
        """Once per warning: the text the gate returns instead of the next edit of the file edited many times."""
        if self.nudge is None:
            return None
        activity = describe_call(tool, args)
        if activity is None or activity.kind != "edit" or activity.fields.get("path") != self.nudge:
            return None
        path, self.nudge, self.nudged_sig = self.nudge, None, activity.fields.get("sig")
        return (
            f"CHECK FIRST: you edited `{path[-80:]}` {self.edits_since_command[path]} times without running anything. "
            "This edit was NOT run. Run a syntax check (e.g. `node --check`, `python -m py_compile`) or its related "
            "tests, then make this edit again and continue."
        )
