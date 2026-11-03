"""UI evidence (SI-23): the ticket's temporary app, the ui_screenshot tool, the check ui_evidence, the [ui] tag,
the artifact file API, the PR description section, the prompts and the seed of actor-office's own temporary office."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import zlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.stage import default_templates
from actoroffice.domain.work import ProjectAccess
from actoroffice.engine import prompt_pack
from actoroffice.engine.browser import Action, BrowserError, PlaywrightBrowser, ShotResult, is_loopback, parse_actions
from actoroffice.engine.workspace import ticket_env
from actoroffice.services import ui_preview
from actoroffice.services.checks import review_ids, ui_evidence_markdown, ui_ids
from actoroffice.services.ui_preview import listeners
from actoroffice.tools.registry import ToolCtx, ToolError
from actoroffice.tools.stage_tools import ui_screenshot

from .fakes import FakeRuntime
from .seed import copy_repo, seed_db
from .test_quality import run_checks, set_doc
from .test_web import HEADERS, TOKEN, client, login  # noqa: F401  (fixture re-export)
from .test_web_team import office_client  # noqa: F401  (fixture re-export)

ROOT = Path(__file__).resolve().parents[1]
APP = Path(__file__).with_name("ui_fake_app.py")
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
UI_REQUIREMENTS = """\
AC-1: Tổng tiền
- Given giỏ hàng When tính Then đúng tổng
AC-2 [ui]: Nút hoàn tiền
- Given trang đơn When mở Then thấy nút
AC-3 [ui] [review]: Hộp xác nhận
- Given trang đơn When bấm Then hộp xác nhận hiện
- Cách kiểm: xem ảnh
"""


def ui_profile(mode: str = "serve", *, seed: bool = True, timeout: float | None = None) -> dict[str, Any]:
    ui: dict[str, Any] = {
        "start": [sys.executable, str(APP), mode, "--port={port}", "--data={data}"],
        "url": "http://127.0.0.1:{port}/login?t={token}",
        "paths": ["web/**"],
    }
    if seed:
        ui["seed"] = [sys.executable, str(APP), "seed", "{port}"]
    profile: dict[str, Any] = {"ui": ui}
    if timeout is not None:
        profile["timeouts"] = {"ui": timeout}
    return profile


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


class FakeBrowser:
    def __init__(self, fail: Exception | None = None, blocked: list[str] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = fail
        self.blocked = blocked or []

    async def screenshot(self, entry: str, target: str, out: Path, *, width: int, height: int, full_page: bool, actions: list[Action]) -> ShotResult:
        self.calls.append({"entry": entry, "target": target, "width": width, "height": height, "full_page": full_page, "actions": actions})
        if self.fail is not None:
            raise self.fail
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(PNG)
        return ShotResult(blocked=list(self.blocked))


async def ui_ticket(office: Office, tmp_path: Path, profile: dict[str, Any] | None, *, template: str = "code", stage: str = "build", files: dict[str, str] | None = None):
    repo = copy_repo(tmp_path / "svc", files or {"src/app.py": "def total(x):\n    return x\n"})
    project = await office.projects.create(f"svc-{tmp_path.name}", path=str(repo), access=ProjectAccess.WORKTREE)
    if profile:
        project = await office.projects.configure(project.id, profile=profile)
    # Each test DB starts at ticket 1: a ticket id of its own gives each test its own port block (parallel workers)
    seq = 100 + zlib.crc32(str(tmp_path).encode()) % 1800
    await office.db.execute("INSERT OR REPLACE INTO sqlite_sequence(name, seq) VALUES ('tickets', ?)", (seq,))
    ticket = await office.tickets.create("Hoàn tiền", description="x", project_id=project.id)
    await office.db.execute("UPDATE tickets SET stage = ?, stage_template = ? WHERE id = ?", (stage, template, ticket.id))
    return await office.tickets.get(ticket.id), project, repo


async def tool_ctx(office: Office, ticket_id: int, workspace: Path, run_id: str = "run-ui-1") -> ToolCtx:
    ai = await office.ensure_ai()
    position = await office.org.get_position(ai.position_id)
    return ToolCtx(office, ai, position, run_id, ticket_id=ticket_id, workspace=workspace)


def gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def listening(port: int) -> list[str]:
    """Addresses something listens on at this TCP port (machine-wide: a venv launcher on Windows hides its child's sockets)."""
    return listeners(port)


def has_chrome() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    if sys.platform == "win32":
        bases = [os.environ.get(k, "") for k in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        return any(Path(b, "Google/Chrome/Application/chrome.exe").is_file() or Path(b, "Microsoft/Edge/Application/msedge.exe").is_file() for b in bases if b)
    return any(shutil.which(n) for n in ("google-chrome", "google-chrome-stable", "microsoft-edge", "chrome"))


real_chrome = pytest.mark.skipif(not has_chrome(), reason="needs playwright and Chrome/Edge on this machine")


# ---- AC-1 profile ui in Settings ---------------------------------------------------------------------------


def test_ac1_setup_saves_and_clears_ui_profile(client, tmp_path):  # noqa: F811
    login(client)
    repo = copy_repo(tmp_path / "web")
    body = {
        "name": "web", "path": str(repo), "test": "", "build": "", "lint": "",
        "ui_seed": "uv run python scripts/ui_seed.py",
        "ui_start": "uv run actoroffice --home {data} serve --port {port} --token-env AO_UI_TOKEN",
        "ui_url": "http://127.0.0.1:{port}/login?t={token}&next=/team",
        "ui_paths": "src/**/static/**, *.css\nweb/**",
    }
    r = client.post("/api/setup/project", json=body, headers=HEADERS)
    assert r.status_code == 200, r.text
    assert r.json()["project"]["profile"]["ui"] == {
        "seed": ["uv", "run", "python", "scripts/ui_seed.py"],
        "start": ["uv", "run", "actoroffice", "--home", "{data}", "serve", "--port", "{port}", "--token-env", "AO_UI_TOKEN"],
        "url": "http://127.0.0.1:{port}/login?t={token}&next=/team",
        "paths": ["src/**/static/**", "*.css", "web/**"],
    }
    project = client.get("/api/setup").json()["projects"][0]
    assert project["ui_seed"] == "uv run python scripts/ui_seed.py"
    assert project["ui_start"] == body["ui_start"] and project["ui_url"] == body["ui_url"]
    assert project["ui_paths"] == "src/**/static/**, *.css, web/**"

    # start without url is refused and nothing changes
    bad = client.post("/api/setup/project", json={**body, "id": project["id"], "ui_url": ""}, headers=HEADERS)
    assert bad.status_code == 422 and "start và URL" in bad.json()["detail"]
    assert client.get("/api/setup").json()["projects"][0]["profile"]["ui"]["url"] == body["ui_url"]

    cleared = {**body, "id": project["id"], "ui_seed": "", "ui_start": "", "ui_url": "", "ui_paths": "", "test": "pytest"}
    r = client.post("/api/setup/project", json=cleared, headers=HEADERS)
    assert r.json()["project"]["profile"] == {"test": ["pytest"]}
    assert client.get("/api/setup").json()["projects"][0]["ui_start"] == ""


# ---- AC-2 temporary app ------------------------------------------------------------------------------------


async def test_ac2_preview_starts_seed_then_start_on_ticket_port(office: Office, tmp_path: Path) -> None:
    ticket, project, repo = await ui_ticket(office, tmp_path, ui_profile())
    block = {int(v) for k, v in ticket_env(ticket, project).items() if k.startswith("AO_PORT_") and k != "AO_PORT_BASE"}
    first = await office.ui.ensure("run-a", ticket, project, repo)
    try:
        info = json.loads((first.data / "serve.json").read_text(encoding="utf-8"))
        seeded = json.loads((first.data / "seed.json").read_text(encoding="utf-8"))
        assert info["seeded"] is True  # seed ran before start
        assert Path(seeded["cwd"]).resolve() == repo.resolve() and seeded["argv"] == [str(first.port)]
        assert first.port in block and info["port"] == first.port
        assert info["argv"] == [f"--port={first.port}", f"--data={first.data}"]  # {port}/{data} filled, never the token
        assert info["token"] == first.token and len(first.token) >= 32
        assert first.token not in json.dumps(project.profile)
        assert listening(first.port) == ["127.0.0.1"]
        assert (await office.ui.ensure("run-a", ticket, project, repo)) is first  # same run: same app
    finally:
        await office.ui.stop_run("run-a")
    second = await office.ui.ensure("run-b", ticket, project, repo)
    try:
        assert second.token != first.token and second.root != first.root
        assert json.loads((second.data / "serve.json").read_text(encoding="utf-8"))["token"] == second.token
    finally:
        await office.ui.stop_run("run-b")


async def test_ac2_app_listening_beyond_loopback_is_refused(office: Office, tmp_path: Path, monkeypatch) -> None:
    """The office checks the address itself: an app the start command opened to the network is stopped (R3)."""
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile())
    browser = FakeBrowser()
    office.ui.browser = browser
    seen: list[int] = []

    def exposed(port: int) -> list[str]:
        seen.append(port)
        return ["0.0.0.0", "127.0.0.1"]  # what an app started with --host 0.0.0.0 shows (no real socket opened to the LAN)

    monkeypatch.setattr(ui_preview, "listeners", exposed)
    with pytest.raises(ToolError, match=r"app nghe trên 0\.0\.0\.0 thay vì chỉ 127\.0\.0\.1"):
        await ui_screenshot({"ac": "AC-2", "caption": "x"}, await tool_ctx(office, ticket.id, repo))
    assert seen and browser.calls == [] and office.ui.running("run-ui-1") is None
    [error] = await ui_errors(office, ticket.id)
    assert error["metadata"]["ac"] == "AC-2" and "0.0.0.0" in error["metadata"]["reason"]
    assert ui_preview.is_loopback_ip("::1") and ui_preview.is_loopback_ip("127.0.0.1") and not ui_preview.is_loopback_ip("::")
    assert not ui_preview.is_loopback_ip("192.168.1.5") and not ui_preview.is_loopback_ip("")


async def test_ac2_stop_run_kills_app_and_removes_data(office: Office, tmp_path: Path) -> None:
    ticket, project, repo = await ui_ticket(office, tmp_path, ui_profile())
    preview = await office.ui.ensure("run-a", ticket, project, repo)
    pid = json.loads((preview.data / "serve.json").read_text(encoding="utf-8"))["pid"]
    assert psutil.pid_exists(pid)
    assert preview.root.parent == office.settings.data_dir / "ui-previews"  # never the system temp folder
    locked = preview.data / "objects" / "0b" / "read-only"  # like a temporary office's git objects
    locked.parent.mkdir(parents=True)
    locked.write_text("x", encoding="utf-8")
    locked.chmod(0o444)
    await office.ui.stop_run("run-a")
    assert not preview.root.exists()
    assert gone(pid)
    assert office.ui.running("run-a") is None
    await office.ui.stop_run("run-a")  # twice is fine


async def test_ac2_run_end_stops_the_app(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    ticket, project, _repo = await ui_ticket(office, tmp_path, ui_profile())
    office.ui.browser = FakeBrowser()
    await office.stages.ensure_run(ticket)
    seen: dict[str, Any] = {}

    async def shoot(ctx):
        out = await ctx.office("ui_screenshot", {"path": "/team", "ac": "AC-2", "caption": "nút"})
        preview = office.ui.running(ctx.spec.run_id)
        seen.update(out=out, root=preview.root, pid=preview.proc.pid)
        assert preview.proc.returncode is None

    runtime.scripts.append(shoot)
    ai = await office.ensure_ai()
    await office.tickets.assign(ticket.id, ai.id, by="user")
    await office.scheduler.wait_idle(timeout=60)
    assert "Saved screenshot" in seen["out"]
    assert not seen["root"].exists()
    assert gone(seen["pid"])


# ---- AC-3 screenshot saved as an artifact of the AC ------------------------------------------------------


async def test_ac3_screenshot_runs_actions_in_order_and_saves_artifact(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile())
    browser = FakeBrowser()
    office.ui.browser = browser
    ctx = await tool_ctx(office, ticket.id, repo)
    actions = [{"fill": "#name", "text": "hello"}, {"click": "#go"}, {"wait": "#out"}, {"wait": 50}]
    try:
        async with office.bus.subscribe() as events:
            out = await ui_screenshot({"path": "/team?x=1", "ac": "ac 3", "caption": "Hộp xác nhận", "width": 1024, "height": 700, "actions": actions}, ctx)
            event = await asyncio.wait_for(events.get(), 5)
        preview = office.ui.running("run-ui-1")
        assert preview is not None and preview.token not in out and "***" not in out
    finally:
        await office.ui.stop_run("run-ui-1")
    [call] = browser.calls
    assert call["actions"] == [Action("fill", selector="#name", text="hello"), Action("click", selector="#go"), Action("wait", selector="#out"), Action("wait", ms=50)]
    assert call["target"] == f"http://127.0.0.1:{preview.port}/team?x=1" and call["entry"].endswith(f"/login?t={preview.token}")
    assert (call["width"], call["height"], call["full_page"]) == (1024, 700, False)
    [product] = [p for p in await office.tickets.work_products(ticket.id) if p["kind"] == "screenshot"]
    saved = Path(product["ref"])
    assert saved.parent == office.settings.data_dir / "artifacts" / ticket.key and saved.read_bytes() == PNG
    assert product["metadata"] == {"note": "Hộp xác nhận", "ac": "AC-3"}
    assert saved.name in out and "AC-3" in out and saved.name == "ac-3-hop-xac-nhan.png"
    assert event.kind == "ticket.artifact" and event.data["kind"] == "screenshot" and event.data["path"] == str(saved)
    assert not list(repo.glob("**/*.png"))  # nothing left in the worktree


async def test_ac3_bad_actions_are_refused(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile())
    office.ui.browser = FakeBrowser()
    ctx = await tool_ctx(office, ticket.id, repo)
    for bad in ([{"eval": "alert(1)"}], [{"click": "#a", "fill": "#b"}], [{"fill": "#a"}], [{"wait": 999_999}], "click"):
        with pytest.raises(ToolError, match="actions"):
            await ui_screenshot({"ac": "AC-2", "caption": "x", "actions": bad}, ctx)
    assert office.ui.running("run-ui-1") is None  # refused before the app starts
    assert parse_actions(None) == [] and parse_actions([{"wait": 0}]) == [Action("wait", ms=0)]


@real_chrome
async def test_ac3_real_chrome_screenshot_png(office: Office, tmp_path: Path) -> None:
    ticket, project, repo = await ui_ticket(office, tmp_path, ui_profile())
    preview = await office.ui.ensure("run-real", ticket, project, repo)
    try:
        out = tmp_path / "shot.png"
        actions = parse_actions([{"fill": "#name", "text": "xin chào"}, {"click": "#go"}, {"wait": "#out:has-text('xin chào')"}])
        await PlaywrightBrowser().screenshot(preview.url, preview.url, out, width=800, height=600, full_page=False, actions=actions)
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and out.stat().st_size > 1000
    finally:
        await office.ui.stop_run("run-real")


# ---- AC-4 loopback only ----------------------------------------------------------------------------------


async def test_ac4_rejects_non_loopback_url_without_browser(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile())
    browser = FakeBrowser()
    office.ui.browser = browser
    ctx = await tool_ctx(office, ticket.id, repo)
    for url in ("http://example.com/", "https://10.0.0.5:8443/x", "http://127.0.0.1.evil.com/", "file:///C:/Windows/win.ini"):
        with pytest.raises(ToolError, match="only loopback"):
            await ui_screenshot({"path": url, "ac": "AC-2", "caption": "x"}, ctx)
    assert browser.calls == [] and office.ui.running("run-ui-1") is None
    assert all(is_loopback(u) for u in ("http://127.0.0.1:8770/", "http://localhost/x", "http://[::1]:80/", "http://127.1.2.3/"))
    assert not any(is_loopback(u) for u in ("http://example.com", "ftp://127.0.0.1/", "http://0.0.0.0/", "javascript:alert(1)", ""))


async def test_ac4_profile_url_must_be_loopback(office: Office, tmp_path: Path) -> None:
    profile = ui_profile()
    profile["ui"]["url"] = "http://example.com:{port}/"
    ticket, _project, repo = await ui_ticket(office, tmp_path, profile)
    browser = FakeBrowser()
    office.ui.browser = browser
    with pytest.raises(ToolError, match="loopback"):
        await ui_screenshot({"ac": "AC-2", "caption": "x"}, await tool_ctx(office, ticket.id, repo))
    assert browser.calls == []


@real_chrome
async def test_ac4_real_chrome_blocks_external_request(office: Office, tmp_path: Path) -> None:
    ticket, project, repo = await ui_ticket(office, tmp_path, ui_profile())
    preview = await office.ui.ensure("run-real", ticket, project, repo)
    try:
        out = tmp_path / "shot.png"
        result = await PlaywrightBrowser().screenshot(preview.url, preview.url, out, width=800, height=600, full_page=True, actions=[])
        assert any("example.com" in u for u in result.blocked)
        assert out.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        with pytest.raises(ValueError, match="loopback"):
            await PlaywrightBrowser().screenshot("http://example.com/", "http://example.com/", out, width=800, height=600, full_page=False, actions=[])
    finally:
        await office.ui.stop_run("run-real")


# ---- AC-5 environment errors ----------------------------------------------------------------------------


async def ui_errors(office: Office, ticket_id: int) -> list[dict[str, Any]]:
    return [p for p in await office.tickets.work_products(ticket_id) if p["kind"] == "ui_error"]


async def test_ac5_missing_browser_records_ui_error(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile())
    office.ui.browser = FakeBrowser(fail=BrowserError("không tìm thấy Chrome hoặc Edge trên máy"))
    with pytest.raises(ToolError) as err:
        await ui_screenshot({"ac": "AC-2", "caption": "x"}, await tool_ctx(office, ticket.id, repo))
    await office.ui.stop_run("run-ui-1")
    assert "Environment error" in str(err.value) and "không tìm thấy Chrome" in str(err.value) and "không chụp được" in str(err.value)
    [error] = await ui_errors(office, ticket.id)
    assert error["metadata"] == {"ac": "AC-2", "reason": "không tìm thấy Chrome hoặc Edge trên máy"}
    assert not [p for p in await office.tickets.work_products(ticket.id) if p["kind"] == "screenshot"]


async def test_ac5_playwright_without_a_browser_is_an_environment_error(tmp_path: Path) -> None:
    pytest.importorskip("playwright")
    with pytest.raises(BrowserError, match="không tìm thấy Chrome hoặc Edge"):
        await PlaywrightBrowser(channels=("no-such-channel",)).screenshot(
            "http://127.0.0.1:9/", "http://127.0.0.1:9/", tmp_path / "x.png", width=800, height=600, full_page=False, actions=[]
        )


async def test_ac5_start_exits_early_reports_tail_masked(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile("crash"))
    browser = FakeBrowser()
    office.ui.browser = browser
    seen: list[str] = []
    original = office.ui._start

    async def spy(preview, *args):
        seen.append(preview.token)
        seen.append(str(preview.root))
        await original(preview, *args)

    office.ui._start = spy  # type: ignore[method-assign]
    with pytest.raises(ToolError) as err:
        await ui_screenshot({"ac": "AC-2", "caption": "x"}, await tool_ctx(office, ticket.id, repo))
    text = str(err.value)
    assert "app thoát sớm (exit 3)" in text and "boom: port already in use" in text
    assert seen[0] not in text and "starting with token ***" in text
    assert browser.calls == [] and office.ui.running("run-ui-1") is None and not Path(seen[1]).exists()
    [error] = await ui_errors(office, ticket.id)
    assert error["metadata"]["ac"] == "AC-2" and error["metadata"]["reason"] == "app thoát sớm (exit 3)"


async def test_ac5_url_timeout(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile("silent", seed=False, timeout=1.5))
    office.ui.browser = FakeBrowser()
    before = {p.pid for p in psutil.Process().children(recursive=True)}
    started = time.monotonic()
    with pytest.raises(ToolError, match="URL không trả lời sau 2s"):
        await ui_screenshot({"ac": "ac-2", "caption": "x"}, await tool_ctx(office, ticket.id, repo))
    assert time.monotonic() - started < 20
    left = [p for p in psutil.Process().children(recursive=True) if p.pid not in before and p.is_running() and p.status() != psutil.STATUS_ZOMBIE]
    assert left == []  # the app that never answered was killed
    [error] = await ui_errors(office, ticket.id)
    assert error["metadata"]["ac"] == "AC-2"


async def test_ac5_seed_failure_is_an_environment_error(office: Office, tmp_path: Path) -> None:
    profile = ui_profile()
    profile["ui"]["seed"] = [sys.executable, "-c", "import sys; print('seed broke'); sys.exit(4)"]
    ticket, _project, repo = await ui_ticket(office, tmp_path, profile)
    office.ui.browser = FakeBrowser()
    with pytest.raises(ToolError, match=r"lệnh seed lỗi \(exit 4\)[\s\S]*seed broke"):
        await ui_screenshot({"ac": "AC-2", "caption": "x"}, await tool_ctx(office, ticket.id, repo))


async def test_ac5_no_ui_profile_is_not_recorded(office: Office, tmp_path: Path) -> None:
    ticket, _project, repo = await ui_ticket(office, tmp_path, None)
    with pytest.raises(ToolError, match="no UI profile"):
        await ui_screenshot({"ac": "AC-2", "caption": "x"}, await tool_ctx(office, ticket.id, repo))
    assert await ui_errors(office, ticket.id) == []


# ---- AC-6 check ui_evidence ------------------------------------------------------------------------------


async def evidence_ticket(office: Office, tmp_path: Path, *, profile: dict[str, Any] | None = None, requirements: str = UI_REQUIREMENTS, ui_change: bool = True):
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile() if profile is None else profile)
    await set_doc(office, ticket.id, "requirements", requirements)
    await set_doc(office, ticket.id, "handoff", "| AC-1 | test_ac1 |\n")
    (repo / "web").mkdir()
    (repo / ("web/app.js" if ui_change else "README2.md")).write_text("x\n", encoding="utf-8")
    return ticket, repo


async def shot(office: Office, ticket_id: int, ac: str) -> None:
    await office.tickets.add_work_product(ticket_id, "screenshot", f"/x/{ac.lower()}.png", {"note": "ảnh", "ac": ac})


async def test_ac6_ui_evidence_skipped_cases(office: Office, tmp_path: Path) -> None:
    ticket, repo = await evidence_ticket(office, tmp_path / "a", profile={})
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "skipped" and "ui.paths" in result.summary
    ticket, repo = await evidence_ticket(office, tmp_path / "b", ui_change=False)
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "skipped" and result.summary == "thay đổi không chạm file giao diện"
    ticket, repo = await evidence_ticket(office, tmp_path / "c", requirements="AC-1: Given a When b Then c\n")
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "skipped" and result.summary == "không có AC [ui]"


async def test_ac6_failed_lists_missing_acs(office: Office, tmp_path: Path) -> None:
    ticket, repo = await evidence_ticket(office, tmp_path)
    await shot(office, ticket.id, "AC-1")  # a screenshot of an AC that is not [ui] does not count
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "failed" and result.data["missing"] == ["AC-2", "AC-3"]
    assert "AC [ui] chưa có ảnh: AC-2, AC-3" in result.summary and result.data["ui_files"] == ["web/app.js"]


async def test_ac6_passed_with_shots(office: Office, tmp_path: Path) -> None:
    ticket, repo = await evidence_ticket(office, tmp_path)
    await shot(office, ticket.id, "AC-2")
    await shot(office, ticket.id, "ac 3")
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "passed" and result.summary == "2/2 AC [ui] có ảnh" and result.data["warnings"] == []


async def test_ac6_passed_warning_with_env_error_or_handoff_line(office: Office, tmp_path: Path) -> None:
    ticket, repo = await evidence_ticket(office, tmp_path)
    await office.tickets.add_work_product(ticket.id, "ui_error", "không có Chrome", {"ac": "AC-2", "reason": "không có Chrome"})
    await set_doc(office, ticket.id, "handoff", "| AC-1 | test |\n| AC-3 | không chụp được: máy CI không có màn hình |\n| AC-31 | không chụp được: khác |\n")
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "passed" and result.summary.startswith("⚠️")
    assert result.data["warnings"] == ["AC-2: không có Chrome", "AC-3: máy CI không có màn hình"]
    # a line that only names the AC without the reason phrase is no reason
    await set_doc(office, ticket.id, "handoff", "| AC-3 | xem ảnh |\n")
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "failed" and result.data["missing"] == ["AC-3"]


async def test_ac6_check_in_code_and_quick_build(office: Office, tmp_path: Path) -> None:
    templates = default_templates()
    code_build = templates["code"].stage("build").checks
    assert code_build.index("ui_evidence") == code_build.index("ac_coverage") + 1
    assert "ui_evidence" in templates["quick"].stage("build").checks
    # quick: no requirements, the ACs are in the plan
    ticket, _project, repo = await ui_ticket(office, tmp_path, ui_profile(), template="quick")
    await set_doc(office, ticket.id, "plan", "Problem: x\nAC-1 [ui]: Given trang When mở Then thấy nút\n")
    (repo / "web").mkdir()
    (repo / "web" / "a.css").write_text("a{}\n", encoding="utf-8")
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "failed" and result.data["missing"] == ["AC-1"]
    await shot(office, ticket.id, "AC-1")
    result, _ = await run_checks(office, ticket.id, repo, "ui_evidence")
    assert result.status == "passed"


# ---- AC-7 tag [ui] ------------------------------------------------------------------------------------------


async def test_ac7_ui_tag_parsed_and_review_any_order(office: Office, tmp_path: Path) -> None:
    text = "AC-1 [ui]: a\nAC-2 [ui] [review]: b\nAC-3 [review] [UI]: c\nAC-4 [review]: d\nAC-5: nhắc [ui] sau chữ\nAC-6 [uix]: e\n"
    assert ui_ids(text) == {"AC-1", "AC-2", "AC-3"}
    assert review_ids(text) == {"AC-2", "AC-3", "AC-4"}
    requirements = (
        "AC-1 [ui]: Given a When b Then c\n"
        "AC-2 [ui] [review]: Given a When b Then c\n  Cách kiểm: xem ảnh\n"
        "AC-3 [review] [ui]: Given a When b Then c\n  Cách kiểm: xem ảnh\n"
        "AC-4: Given a When b Then c\n"
    )
    ticket, _project, repo = await ui_ticket(office, tmp_path, None)
    await set_doc(office, ticket.id, "requirements", requirements)
    await set_doc(office, ticket.id, "handoff", "| AC-2 | ảnh |\n| AC-3 | ảnh |\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_x.py").write_text("def test_ac4_x():\n    pass\n", encoding="utf-8")
    testable, _ = await run_checks(office, ticket.id, repo, "ac_testable")
    assert testable.status == "passed" and testable.data["review"] == ["AC-2", "AC-3"]
    coverage, _ = await run_checks(office, ticket.id, repo, "ac_coverage")
    assert coverage.status == "failed" and coverage.data["missing"] == ["AC-1"]  # [ui] alone still needs a test
    (repo / "tests" / "test_x.py").write_text("def test_ac4_x():\n    pass\n\n\ndef test_ac1_x():\n    pass\n", encoding="utf-8")
    coverage, _ = await run_checks(office, ticket.id, repo, "ac_coverage")
    assert coverage.status == "passed", coverage.summary


# ---- AC-8 artifact file API and /stage ----------------------------------------------------------------------


def test_ac8_artifact_file_served_and_denied(office_client, tmp_path) -> None:  # noqa: F811
    office, c = office_client
    login(c)
    task = c.post("/api/tasks", json={"title": "Hoàn tiền", "template": "code"}, headers=HEADERS).json()
    other = c.post("/api/tasks", json={"title": "Khác", "template": "code"}, headers=HEADERS).json()
    ids: dict[str, int] = {}

    async def prepare() -> None:
        ticket = await office.tickets.get(task["key"])
        folder = office.settings.data_dir / "artifacts" / ticket.key
        folder.mkdir(parents=True)
        (folder / "ac-2.png").write_bytes(PNG)
        await office.tickets.put_document(ticket.id, "requirements", UI_REQUIREMENTS, author="Copilot", base_revision=None)
        await office.tickets.put_document(ticket.id, "handoff", "| AC-3 | không chụp được: không có Chrome |\n", author="Copilot", base_revision=None)
        await office.tickets.add_work_product(ticket.id, "screenshot", str(folder / "ac-2.png"), {"note": "Nút", "ac": "AC-2"})
        outside = tmp_path / "secret.png"
        outside.write_bytes(PNG)
        await office.tickets.add_work_product(ticket.id, "screenshot", str(outside), {"ac": "AC-2"})
        await office.tickets.add_work_product(ticket.id, "screenshot", str(folder / "../" / other["key"] / "x.png"), {"ac": "AC-2"})
        products = await office.tickets.work_products(ticket.id)
        ids.update(ok=products[0]["id"], outside=products[1]["id"], sibling=products[2]["id"])

    c.portal.call(prepare)
    r = c.get(f"/api/tickets/{task['key']}/artifacts/{ids['ok']}/file")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content == PNG
    assert c.get(f"/api/tickets/{other['key']}/artifacts/{ids['ok']}/file").status_code == 404  # another ticket's id
    assert c.get(f"/api/tickets/{task['key']}/artifacts/999999/file").status_code == 404
    assert c.get(f"/api/tickets/{task['key']}/artifacts/{ids['outside']}/file").status_code == 403
    assert c.get(f"/api/tickets/{task['key']}/artifacts/{ids['sibling']}/file").status_code == 403

    evidence = c.get(f"/api/tickets/{task['key']}/stage").json()["ui_evidence"]
    assert [e["id"] for e in evidence] == ["AC-2", "AC-3"]
    assert evidence[0]["shots"][0] == {"id": ids["ok"], "name": "ac-2.png", "caption": "Nút"}
    assert [e["title"] for e in evidence] == ["Nút hoàn tiền", "Hộp xác nhận"]  # id and tags are not repeated
    assert evidence[1]["shots"] == [] and evidence[1]["reasons"] == ["không có Chrome"]

    c.cookies.clear()
    assert c.get(f"/api/tickets/{task['key']}/artifacts/{ids['ok']}/file").status_code == 401


async def test_ac8_gate_card_payload_has_ui_evidence(office: Office, tmp_path: Path) -> None:
    ticket, _repo = await evidence_ticket(office, tmp_path)
    await shot(office, ticket.id, "AC-2")
    stage = (await office.stages.template_of(ticket)).stage("build")
    card = await office.stages._open_gate(ticket, stage, "rules", "xong", [], run_id=None)
    assert [e["id"] for e in card.payload["ui_evidence"]] == ["AC-2", "AC-3"]
    assert card.payload["ui_evidence"][0]["shots"][0]["name"] == "ac-2.png"


# ---- AC-10 PR description ----------------------------------------------------------------------------------


async def test_ac10_pr_create_appends_ui_evidence_section(office: Office, tmp_path: Path) -> None:
    from actoroffice.tools.atlassian_tools import pr_create

    from .test_atlassian import FakeHost, git

    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(remote)], check=True)
    ticket, project, repo = await ui_ticket(office, tmp_path, ui_profile())
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "checkout", "-q", "-b", "t/si-x")
    await office.projects.configure(project.id, repo_url="acme/web")
    host = FakeHost()
    office.atlassian.use(code_host=host)
    await set_doc(office, ticket.id, "requirements", UI_REQUIREMENTS)
    await set_doc(office, ticket.id, "handoff", "| AC-3 | không chụp được: máy không có Chrome |\n")
    await office.tickets.add_work_product(ticket.id, "screenshot", "/a/artifacts/X/ac-2-nut.png", {"note": "Nút hoàn tiền", "ac": "AC-2"})
    out = await pr_create({"title": "Hoàn tiền", "description": "Tóm tắt\n"}, await tool_ctx(office, ticket.id, repo))
    assert "Opened PR #7" in out
    assert host.created[0]["description"] == (
        "Tóm tắt\n\n## Bằng chứng giao diện\n- AC-2: `ac-2-nut.png` (Nút hoàn tiền)\n- AC-3: ⚠️ không chụp được: máy không có Chrome"
    )
    assert ui_evidence_markdown([]) == ""


# ---- AC-11 prompts -------------------------------------------------------------------------------------------


def _system(template_name: str, stage_id: str) -> str:
    from .test_prompt_pack import _prompt

    return _prompt(template_name, True, stage_id).system


def test_ac11_prompts_mention_ui_tag_and_screenshot_tool() -> None:
    understand = _system("code", "understand")
    assert 'Tag an AC "[ui]" right after its id' in understand and "still needs a test unless it is also [review]" in understand
    assert "[ui]" in _system("quick", "scope")
    for template_name in ("code", "quick"):
        build = _system(template_name, "build")
        assert "ui_screenshot" in build and "ac = its id" in build and "không chụp được: <lý do>" in build and "`handoff`" in build
        assert "screenshot if you can run the page" not in build
    for template_name, stage_id in (("code", "plan"), ("code", "pr"), ("code", "close")):
        assert "ui_screenshot" not in _system(template_name, stage_id)
    assert "screenshot if you can run the page" not in "".join(prompt_pack.STAGE_INSTRUCTIONS.values())


# ---- AC-12 actor-office's own temporary office ----------------------------------------------------------------


def free_local_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def isolated_env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "userhome"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k not in ("ACTOROFFICE_HOME", "VIRTUAL_ENV")}
    env.update(HOME=str(home), USERPROFILE=str(home), PYTHONUTF8="1", AO_UI_DATA=str(tmp_path / "data"), AO_UI_TOKEN="k" * 43)
    return env


def test_ac12_seed_script_fills_temp_home(tmp_path: Path) -> None:
    env = isolated_env(tmp_path)
    (tmp_path / "data").mkdir()
    for _ in range(2):  # a second run adds nothing
        done = subprocess.run([sys.executable, str(ROOT / "scripts" / "ui_seed.py")], env=env, capture_output=True, text=True, timeout=120, cwd=ROOT)
        assert done.returncode == 0, done.stderr
    db = sqlite3.connect(tmp_path / "data" / "office.db")
    try:
        rows = db.execute("SELECT stage, status FROM tickets WHERE stage IS NOT NULL").fetchall()
        assert len(rows) == 5
        assert {r[0] for r in rows} >= {"understand", "build", "pr"}
        assert {r[1] for r in rows} >= {"done", "cancelled", "in_review"}
        assert db.execute("SELECT COUNT(*) FROM teams").fetchone()[0] >= 1
        assert db.execute("SELECT COUNT(*) FROM users WHERE id != 'owner'").fetchone()[0] >= 1
        assert db.execute("SELECT COUNT(*) FROM interactions WHERE kind = 'approve_stage' AND status = 'pending'").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM work_products WHERE kind = 'screenshot'").fetchone()[0] == 2
    finally:
        db.close()
    assert not (tmp_path / "userhome" / ".actoroffice").exists()
    assert not list((tmp_path / "data").glob("secrets/web_token"))
    missing = subprocess.run([sys.executable, str(ROOT / "scripts" / "ui_seed.py")], env={**env, "AO_UI_DATA": ""}, capture_output=True, text=True, timeout=60, cwd=ROOT)
    assert missing.returncode == 2 and "AO_UI_DATA" in missing.stderr


def test_ac12_serve_uses_token_env_and_writes_no_token(tmp_path: Path) -> None:
    env = isolated_env(tmp_path)
    port = free_local_port()
    data = tmp_path / "data"
    argv = [sys.executable, "-m", "actoroffice", "--home", str(data), "serve", "--host", "127.0.0.1", "--port", str(port), "--view-only", "--no-browser", "--token-env", "AO_UI_TOKEN"]
    log = (tmp_path / "serve.log").open("wb")
    proc = subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT, cwd=ROOT)
    try:
        deadline = time.monotonic() + 60
        while True:
            assert proc.poll() is None, (tmp_path / "serve.log").read_text(encoding="utf-8", errors="replace")
            try:
                r = httpx.get(f"http://127.0.0.1:{port}/login?t={env['AO_UI_TOKEN']}&next=/team", follow_redirects=False, timeout=2, trust_env=False)
                break
            except httpx.HTTPError:
                assert time.monotonic() < deadline
                time.sleep(0.3)
        assert r.status_code == 303
        assert httpx.get(f"http://127.0.0.1:{port}/login?t=wrong", timeout=2, trust_env=False).status_code == 401
        assert listening(port) == ["127.0.0.1"]
        assert not (data / "secrets" / "web_token").exists()
    finally:
        proc.kill()
        proc.wait(30)
        log.close()
    output = (tmp_path / "serve.log").read_text(encoding="utf-8", errors="replace")
    assert env["AO_UI_TOKEN"] not in output and "$AO_UI_TOKEN" in output
    assert not (tmp_path / "userhome" / ".actoroffice").exists()
