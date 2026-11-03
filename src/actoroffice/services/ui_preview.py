"""The ticket's temporary app for UI screenshots (SI-23).

On the first screenshot of a run the office runs the project's `ui.seed` then `ui.start` commands in the ticket's
worktree, on a free port of the ticket's block (AO_PORT_9 … AO_PORT_0), with a fresh data folder and a random token,
and waits for `ui.url` to answer. The app lives as long as the run: the executor stops it (whole process tree) and
deletes the data folder when the run ends.

The commands get AO_UI_PORT, AO_UI_DATA and AO_UI_TOKEN in their environment; `{port}` and `{data}` are replaced in
their argv. The token is only put in the URL the office opens itself: never in argv, the profile or a tool output.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import psutil

from actoroffice.engine.browser import Browser, PlaywrightBrowser, is_loopback
from actoroffice.engine.workspace import PORTS_PER_TICKET, ticket_env

from .checks import command_list, run_argv, test_env

if TYPE_CHECKING:
    from actoroffice.app import Office
    from actoroffice.domain.work import Project, Ticket

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 60.0
TAIL_LINES = 15


class UiEnvError(Exception):
    """The app could not be started or did not answer: an environment problem, with the end of its output."""

    def __init__(self, reason: str, tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.tail = tail


def ui_spec(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    """profile["ui"] when it can start an app (a start command and a URL), else None."""
    ui = (profile or {}).get("ui")
    if not isinstance(ui, dict) or not command_list(ui, "start") or not str(ui.get("url") or "").strip():
        return None
    return ui


def ui_timeout(profile: dict[str, Any] | None) -> float:
    timeouts = (profile or {}).get("timeouts")
    return float(timeouts.get("ui", DEFAULT_TIMEOUT)) if isinstance(timeouts, dict) else DEFAULT_TIMEOUT


@dataclass
class Preview:
    run_id: str
    root: Path  # temp folder: data/ (AO_UI_DATA) and start.log
    port: int
    token: str
    url: str = ""  # ui.url with port and token filled in (never shown)
    proc: asyncio.subprocess.Process | None = None
    log_file: Any = None
    ready: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def shots(self) -> Path:
        return self.root / "shots"

    def mask(self, text: str) -> str:
        return text.replace(self.token, "***") if self.token else text

    def tail(self) -> str:
        path = self.root / "start.log"
        with contextlib.suppress(OSError):
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            return self.mask("\n".join(lines[-TAIL_LINES:]))
        return ""


def free_port(env: dict[str, str]) -> int | None:
    """The first port of the ticket's block that nothing listens on, from AO_PORT_9 down (low ports are the ones
    the project's own services usually take)."""
    for i in reversed(range(PORTS_PER_TICKET)):
        port = int(env[f"AO_PORT_{i}"])
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    return None


def _kill_and_wait(pid: int, timeout: float = 10) -> None:
    """Kill a process and all its children, then wait until they are gone (their files are released)."""
    try:
        parent = psutil.Process(pid)
        procs = [parent, *parent.children(recursive=True)]
    except psutil.Error:
        return
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.kill()
    psutil.wait_procs(procs, timeout=timeout)


def rmtree(path: Path) -> None:
    """Delete a folder, read-only files included (git objects of a temporary office are read-only on Windows)."""

    def writable(func: Any, target: str, _exc: BaseException) -> None:
        with contextlib.suppress(OSError):
            os.chmod(target, stat.S_IWRITE)
            func(target)

    with contextlib.suppress(OSError):
        shutil.rmtree(path, onexc=writable)


def fill(value: str, preview: Preview, *, token: bool = False) -> str:
    out = value.replace("{port}", str(preview.port)).replace("{data}", str(preview.data))
    return out.replace("{token}", preview.token) if token else out


class UiPreviewService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self.browser: Browser = PlaywrightBrowser()
        self._apps: dict[str, Preview] = {}

    def running(self, run_id: str) -> Preview | None:
        return self._apps.get(run_id)

    async def ensure(self, run_id: str, ticket: Ticket, project: Project | None, workspace: Path) -> Preview:
        """The run's app, started on first use. Raises UiEnvError when it cannot start or does not answer."""
        profile = project.profile if project else {}
        ui = ui_spec(profile)
        if ui is None:
            raise UiEnvError("dự án chưa khai báo profile ui (lệnh start và URL)")
        preview = self._apps.get(run_id)
        if preview is None:
            env = ticket_env(ticket, project)
            port = free_port(env)
            if port is None:
                raise UiEnvError(f"không còn cổng trống trong khối cổng của {ticket.key} ({env['AO_PORT_0']}–{env[f'AO_PORT_{PORTS_PER_TICKET - 1}']})")
            base = self.office.settings.data_dir / "ui-previews"  # inside the office's own folder, never the system temp
            base.mkdir(parents=True, exist_ok=True)
            root = Path(tempfile.mkdtemp(prefix=f"{ticket.key.lower()}-", dir=base))
            # registered before any await: stop_run cleans up even when the start is interrupted (R-3)
            preview = Preview(run_id, root, port, secrets.token_urlsafe(32))
            self._apps[run_id] = preview
            preview.data.mkdir()
            self.office.redact.register(preview.token)
        async with preview.lock:
            if preview.ready and preview.proc is not None and preview.proc.returncode is None:
                return preview
            try:
                if preview.ready:
                    assert preview.proc is not None
                    raise UiEnvError(f"app đã dừng (exit {preview.proc.returncode})", preview.tail())
                await self._start(preview, ticket, project, workspace, ui, ui_timeout(profile))
            except BaseException:
                # nothing half-started stays behind; the next call starts a fresh app
                await self.stop_run(run_id)
                raise
            return preview

    async def _start(self, preview: Preview, ticket: Ticket, project: Project | None, workspace: Path, ui: dict[str, Any], timeout: float) -> None:
        env = {
            **test_env(self.office),
            **ticket_env(ticket, project),
            "AO_UI_PORT": str(preview.port),
            "AO_UI_DATA": str(preview.data),
            "AO_UI_TOKEN": preview.token,
        }
        url = fill(str(ui["url"]).strip(), preview, token=True)
        if not is_loopback(url):
            raise UiEnvError("ui.url phải là địa chỉ loopback (127.0.0.1, localhost, ::1)")
        preview.url = url
        for argv in command_list(ui, "seed"):
            code, out, _secs = await run_argv([fill(a, preview) for a in argv], workspace, timeout=timeout, env=env)
            if code != 0:
                tail = "\n".join(preview.mask(out).splitlines()[-TAIL_LINES:])
                raise UiEnvError(f"lệnh seed lỗi (exit {code})", tail)
        argv = [fill(a, preview) for a in command_list(ui, "start")[0]]
        exe = (shutil.which(argv[0]) or argv[0]) if sys.platform == "win32" else argv[0]  # resolves uv.exe, npm.cmd …
        preview.log_file = (preview.root / "start.log").open("wb")
        # own process group / session: the whole tree can be killed when the run ends
        extra: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if sys.platform == "win32" else {"start_new_session": True}
        try:
            preview.proc = await asyncio.create_subprocess_exec(
                exe, *argv[1:], cwd=str(workspace), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=preview.log_file, stderr=subprocess.STDOUT, **extra
            )
        except OSError as exc:
            raise UiEnvError(f"không chạy được lệnh start {argv[0]}: {exc}") from exc
        await self._wait(preview, timeout)
        preview.ready = True

    async def _wait(self, preview: Preview, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        async with httpx.AsyncClient(timeout=2.0, trust_env=False, follow_redirects=False) as client:
            while True:
                assert preview.proc is not None
                if preview.proc.returncode is not None:
                    raise UiEnvError(f"app thoát sớm (exit {preview.proc.returncode})", preview.tail())
                with contextlib.suppress(httpx.HTTPError):
                    await client.get(preview.url)
                    return  # any HTTP answer: the app is up
                if time.monotonic() > deadline:
                    raise UiEnvError(f"URL không trả lời sau {timeout:.0f}s", preview.tail())
                await asyncio.sleep(0.3)

    async def stop_run(self, run_id: str) -> None:
        """Stop the run's app (whole process tree) and delete its temp folder. Safe to call when nothing runs."""
        preview = self._apps.pop(run_id, None)
        if preview is None:
            return
        if preview.proc is not None and preview.proc.returncode is None:
            await asyncio.to_thread(_kill_and_wait, preview.proc.pid)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(preview.proc.wait(), 10)
        if preview.log_file is not None:
            preview.log_file.close()
        for _ in range(10):  # Windows releases files of a killed process a moment later
            await asyncio.to_thread(rmtree, preview.root)
            if not preview.root.exists():
                break
            await asyncio.sleep(0.3)
        if preview.root.exists():
            log.warning("UI preview folder %s could not be removed", preview.root)

    async def close(self) -> None:
        for run_id in list(self._apps):
            await self.stop_run(run_id)
