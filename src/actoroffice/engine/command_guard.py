"""Hard limit of the AI's shell commands (SI-38 BR-1, BR-2).

Every shell command of a run is watched from the moment it starts until its process is gone, also when it runs in
the background (`run_in_background`, or a foreground command the Claude CLI moved to the background at its own
timeout: the CLI never stops it). Past `limit_s` (awake time, engine/clock.py) the guard kills the command's whole
process tree and queues a note for the AI. When the run ends, every command still alive is killed.

- Claude: the CLI runs the command, so the office only knows its process through a prefix added to the command
  after the gate allowed it (`wrap`): the shell writes its own PID (the Windows PID under Git Bash) to a file.
- Kiro: the office starts the terminal itself and gives its process (`watch`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from actoroffice.proctree import kill_tree

from .clock import AwakeClock

log = logging.getLogger(__name__)

MARK = "# ao:guard"
POLL_S = 1.0
PID_GRACE_S = 10.0  # a command whose shell never wrote its PID file is dropped after this long once its tool ended


@dataclass
class Command:
    call_id: str
    command: str
    started: float
    pid_file: Path | None = None
    proc: psutil.Process | None = None
    tool_done: float | None = None  # when its tool call ended (still alive then: it runs in the background)
    on_stop: Callable[[str], None] | None = None  # Kiro: the note goes into the terminal output


@dataclass
class CommandGuard:
    run_id: str
    limit_s: float
    folder: Path
    clock: AwakeClock = field(default_factory=AwakeClock)
    on_stop: Callable[[str, float], Awaitable[None]] | None = None  # (command, seconds it ran): activity event
    commands: dict[str, Command] = field(default_factory=dict)
    stopped: dict[str, str] = field(default_factory=dict)  # call id → note, told with that call's own result
    notes: list[str] = field(default_factory=list)  # notes of commands stopped in the background
    last_stop: float | None = None  # awake time of the last stop
    _task: asyncio.Task[None] | None = None

    # ---- registering commands -------------------------------------------------------------------

    def wrap(self, call_id: str | None, command: str) -> str:
        """The command the Claude CLI runs instead: a prefix writes the shell's PID, then the command unchanged."""
        if not call_id or not command.strip():
            return command
        pid_file = self.folder / (re.sub(r"[^A-Za-z0-9_.-]", "_", call_id) + ".pid")
        target = pid_file.as_posix()
        if "'" in target:
            log.warning("run %s: cannot watch commands from %s", self.run_id, target)
            return command
        self.folder.mkdir(parents=True, exist_ok=True)
        self.commands[call_id] = Command(call_id, command, self.clock.now(), pid_file=pid_file)
        return f"{{ cat /proc/$$/winpid 2>/dev/null || echo $$; }} > '{target}' {MARK}\n{command}"

    @staticmethod
    def unwrap(command: str) -> str:
        head, sep, rest = command.partition(f" {MARK}\n")
        return rest if sep and head.startswith("{ cat /proc/$$/winpid") else command

    def watch(self, call_id: str, command: str, pid: int, on_stop: Callable[[str], None] | None = None) -> None:
        """A command the office started itself (Kiro terminal)."""
        cmd = Command(call_id, command, self.clock.now(), on_stop=on_stop)
        with contextlib.suppress(psutil.Error):
            cmd.proc = psutil.Process(pid)
        self.commands[call_id] = cmd

    def tool_ended(self, call_id: str | None) -> str | None:
        """The tool call of a command returned: its process may go on in the background. Returns what to tell the
        AI with this result: why this command was stopped, and commands stopped in the background meanwhile."""
        cmd = self.commands.get(call_id or "")
        if cmd is not None:
            self._resolve(cmd)
            cmd.tool_done = self.clock.now()
        own = self.stopped.pop(call_id or "", None)
        told = [own] if own else []
        told += self.notes
        self.notes = []
        return "\n\n".join(told) or None

    # ---- watching ---------------------------------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name=f"command-guard-{self.run_id}")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(POLL_S)
            await self.poll()

    async def poll(self) -> None:
        now = self.clock.now()
        for call_id, cmd in list(self.commands.items()):
            self._resolve(cmd)
            if cmd.proc is not None and not _running(cmd.proc):
                self.commands.pop(call_id, None)
                continue
            if cmd.proc is None and cmd.tool_done is not None and now - cmd.tool_done > PID_GRACE_S:
                log.warning("run %s: no PID for command %s; it is not watched", self.run_id, call_id)
                self.commands.pop(call_id, None)
                continue
            elapsed = now - cmd.started
            if elapsed > self.limit_s and cmd.proc is not None:
                self.commands.pop(call_id, None)
                kill_tree(cmd.proc.pid)
                self.last_stop = now
                note = stop_note(cmd.command, elapsed, self.limit_s)
                if cmd.on_stop is not None:
                    cmd.on_stop(note)
                elif cmd.tool_done is None:
                    self.stopped[call_id] = note
                else:
                    self.notes.append(note)
                if self.on_stop is not None:
                    with contextlib.suppress(Exception):
                        await self.on_stop(cmd.command, elapsed)

    def _resolve(self, cmd: Command) -> None:
        if cmd.proc is not None or cmd.pid_file is None:
            return
        with contextlib.suppress(OSError, ValueError, psutil.Error):
            cmd.proc = psutil.Process(int(cmd.pid_file.read_text(encoding="ascii").strip()))

    def stopped_since(self, mark: float) -> bool:
        return self.last_stop is not None and self.last_stop >= mark

    async def close(self) -> None:
        """The run ended: stop the loop and every command still alive (BR-2), remove the PID files."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        for cmd in list(self.commands.values()):
            self._resolve(cmd)
            if cmd.proc is not None and _running(cmd.proc):
                kill_tree(cmd.proc.pid)
        self.commands.clear()
        for pid_file in self.folder.glob("*.pid") if self.folder.is_dir() else []:
            pid_file.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            self.folder.rmdir()


def stop_note(command: str, elapsed: float, limit_s: float) -> str:
    """What the AI is told about a command the office stopped."""
    short = " ".join(command.split())
    short = short[:200] + ("…" if len(short) > 200 else "")
    return (
        f"Office stopped this command after {int(elapsed)}s (limit {_seconds(limit_s)}, command_timeout_s): `{short}`. "
        "Its whole process tree was killed. Run a narrower command instead, e.g. the tests of one file or one test "
        "(-k), not the whole suite."
    )


def _seconds(value: float) -> str:
    return f"{int(value)}s" if float(value).is_integer() else f"{value}s"


def _running(proc: psutil.Process) -> bool:
    try:
        return proc.is_running() and proc.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def command_limit(profile: dict | None, default: float) -> float:
    """`command_timeout_s` of a project profile; a missing or wrong value gives the office default (G3)."""
    raw = (profile or {}).get("command_timeout_s")
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = 0
    if isinstance(raw, bool) or value <= 0:
        log.warning("command_timeout_s %r in the project profile is not a positive number; using %ss", raw, default)
        return default
    return value
