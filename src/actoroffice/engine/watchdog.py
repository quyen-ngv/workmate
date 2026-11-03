"""Watchdog for a running ticket run (instead of token budgets): it notices when the AI is slow, stalled or
going round in circles, says so in the activity stream, and stops loops by making the next tool call ask a
developer instead.

- slow:     one command has been running longer than `slow_command_s`
- stalled:  no event at all for `stall_after_s`
- looping:  the same failing command 3 times, the same part of a file read 4 times, the same edit made twice
            (so an edit redone after it was undone), or 2 undone edits / 10 edits of one file with no command
            (test/build run) in between. Several different edits of one file are normal work, and so is
            undoing one misplaced edit once: neither is a loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from actoroffice.app import Office

FAILS_FOR_LOOP = 3
READS_FOR_LOOP = 4
EDITS_FOR_LOOP = 10
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
    last_event: float = field(default_factory=time.monotonic)
    failures: Counter[str] = field(default_factory=Counter)
    reads: Counter[str] = field(default_factory=Counter)
    edits_since_command: Counter[str] = field(default_factory=Counter)
    edit_sigs: dict[str, set[str]] = field(default_factory=dict)  # path → edits made since the last command
    undos: Counter[str] = field(default_factory=Counter)  # path → edits undone since the last command
    running: dict[str, tuple[str, float]] = field(default_factory=dict)  # call id → (command, started)
    reported: set[str] = field(default_factory=set)
    loop: str | None = None  # set when a loop is detected; the gate turns it into a stop-and-ask
    _task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._watch(), name=f"watchdog-{self.run_id}")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def observe(self, activity: dict[str, Any] | None) -> None:
        self.last_event = time.monotonic()
        self.reported.discard("stalled")
        if not activity:
            return
        kind, status, call = activity.get("kind"), activity.get("status"), activity.get("call_id")
        fields = activity.get("fields") or {}
        if kind == "command":
            command = normalize_command(str(fields.get("command") or ""))
            if status == "running" and call:
                self.running[call] = (command, time.monotonic())
                return
            self.running.pop(str(call), None)
            self.edits_since_command.clear()
            self.edit_sigs.clear()
            self.undos.clear()
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
            if sig and sig in seen:
                await self._loop(f"lặp lại đúng một chỗ sửa trong `{path[-80:]}`")
            elif undo and undo in seen and undo != sig:
                self.undos[path] += 1  # one undo fixes a misplaced edit; a second one is going back and forth
                if self.undos[path] >= UNDOS_FOR_LOOP:
                    await self._loop(f"hoàn tác {self.undos[path]} lần trong `{path[-80:]}`")
            if sig:
                seen.add(sig)
            self.edits_since_command[path] += 1
            if self.edits_since_command[path] >= EDITS_FOR_LOOP:
                await self._loop(f"sửa `{path[-80:]}` {self.edits_since_command[path]} lần mà không chạy lại test")

    async def _loop(self, reason: str) -> None:
        if self.loop is None:
            self.loop = reason
            await self._health("looping", f"Có vẻ đang lặp: {reason}. Mình sẽ dừng và hỏi Dev.")

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(min(15.0, max(1.0, self.stall_after_s / 4)))
            now = time.monotonic()
            if now - self.last_event > self.stall_after_s and "stalled" not in self.reported:
                self.reported.add("stalled")
                await self._health("stalled", f"Có vẻ bị kẹt: {int(now - self.last_event)}s không có hoạt động")
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
