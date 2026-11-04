"""The one way the office stops a process together with everything it started (SI-38 BR-3)."""

from __future__ import annotations

import contextlib
import subprocess
from pathlib import Path

import psutil


def kill_tree(pid: int, wait_s: float = 0) -> bool:
    """Kill process `pid` and all its children; with `wait_s` > 0 wait up to that long until they are gone (their
    files are released). False when the process no longer existed."""
    try:
        parent = psutil.Process(pid)
        procs = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return False
    for proc in reversed(procs):  # children first: a parent killed first may let a child be re-parented and missed
        with contextlib.suppress(psutil.Error):
            proc.kill()
    if wait_s > 0:
        psutil.wait_procs(procs, timeout=wait_s)
    return True


def kill_msys_group(kill_exe: Path, pgid: int) -> None:
    """Kill an MSYS (Git Bash) process group with MSYS's own kill. MSYS programs a shell started (`sleep 60 &`) have
    an exited fork stub as their Windows parent, so `kill_tree` of the shell misses them; their group does not."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([str(kill_exe), "-9", "--", f"-{pgid}"], capture_output=True, timeout=10, check=False)


def alive(pid: int | None) -> bool | None:
    """Whether process `pid` still runs (zombies count as gone); None when the pid is not known."""
    if pid is None:
        return None
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False
