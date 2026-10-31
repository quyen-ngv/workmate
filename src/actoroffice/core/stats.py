"""What a run actually did: files changed, lines added/removed, commands, web reads — shown under each message.

Counted from the tool calls the gate allowed (so a denied call is not counted). Line counts come from a real
line diff between the old and the new content of each write or edit.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_DIFF_LINES = 20_000  # bigger files are counted as all-new lines instead of diffed
MAX_COMMANDS_KEPT = 12


@dataclass
class RunStats:
    workspace: Path | None = None
    files: dict[str, dict[str, int]] = field(default_factory=dict)  # path → {"added": n, "removed": n}
    commands: list[str] = field(default_factory=list)
    command_count: int = 0
    web: int = 0
    reads: int = 0
    office_tools: int = 0
    tool_calls: int = 0

    def record(self, tool: str, args: dict[str, Any]) -> None:
        self.tool_calls += 1
        if tool in ("Write",):
            target = self._abs(args.get("file_path"))
            self._add(self._key(target), _read(target), str(args.get("content", "")))
        elif tool in ("Edit",):
            target = self._abs(args.get("file_path"))
            times = 1
            if args.get("replace_all"):
                times = max(1, _read(target).count(str(args.get("old_string", ""))))
            self._add(self._key(target), str(args.get("old_string", "")), str(args.get("new_string", "")), times=times)
        elif tool in ("MultiEdit",):
            path = self._key(self._abs(args.get("file_path")))
            for edit in args.get("edits") or []:
                self._add(path, str(edit.get("old_string", "")), str(edit.get("new_string", "")))
        elif tool in ("NotebookEdit",):
            self._add(self._key(self._abs(args.get("notebook_path"))), "", str(args.get("new_source", "")))
        elif tool in ("Bash", "PowerShell"):
            self.command_count += 1
            if len(self.commands) < MAX_COMMANDS_KEPT:
                self.commands.append(str(args.get("command", ""))[:160])
        elif tool in ("WebFetch", "WebSearch"):
            self.web += 1
        elif tool in ("Read", "Glob", "Grep", "LS", "NotebookRead"):
            self.reads += 1
        elif tool.startswith("mcp__office__"):
            self.office_tools += 1

    def _abs(self, raw: Any) -> Path:
        path = Path(str(raw or "?"))
        if not path.is_absolute() and self.workspace is not None:
            path = self.workspace / path
        return path

    def _key(self, path: Path) -> str:
        if self.workspace is not None:
            try:
                return path.resolve().relative_to(self.workspace.resolve()).as_posix()
            except (ValueError, OSError):
                pass
        return path.as_posix()

    def _add(self, path: str, old: str, new: str, *, times: int = 1) -> None:
        added, removed = _diff(old, new)
        entry = self.files.setdefault(path, {"added": 0, "removed": 0})
        entry["added"] += added * times
        entry["removed"] += removed * times

    def summary(self) -> dict[str, Any]:
        return {
            "files_changed": len(self.files),
            "lines_added": sum(f["added"] for f in self.files.values()),
            "lines_removed": sum(f["removed"] for f in self.files.values()),
            "files": dict(list(self.files.items())[:50]),
            "commands": self.command_count,
            "command_samples": self.commands,
            "web": self.web,
            "reads": self.reads,
            "office_tools": self.office_tools,
            "tool_calls": self.tool_calls,
        }


def merge(total: dict[str, Any], one: dict[str, Any] | None) -> dict[str, Any]:
    """Add a run's summary into a running total (for a ticket and its sub-tickets)."""
    if not one:
        return total
    for key in ("lines_added", "lines_removed", "commands", "web", "reads", "office_tools", "tool_calls", "duration_s"):
        total[key] = total.get(key, 0) + (one.get(key) or 0)
    files = total.setdefault("files", {})
    for path, counts in (one.get("files") or {}).items():
        entry = files.setdefault(path, {"added": 0, "removed": 0})
        entry["added"] += counts.get("added", 0)
        entry["removed"] += counts.get("removed", 0)
    total["files_changed"] = len(files)
    return total


def _read(p: Path) -> str:
    try:
        if p.is_file() and p.stat().st_size < 5_000_000:
            return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def _diff(old: str, new: str) -> tuple[int, int]:
    old_lines, new_lines = old.splitlines(), new.splitlines()
    if len(old_lines) + len(new_lines) > MAX_DIFF_LINES:
        return len(new_lines), len(old_lines)
    added = removed = 0
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed
