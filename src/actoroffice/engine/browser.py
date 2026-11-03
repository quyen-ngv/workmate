"""Headless browser for UI evidence (SI-23): open a page of the ticket's temporary app, run a few actions
(click, fill, wait) and save a PNG. Only loopback addresses are opened; requests to other hosts are blocked.

`PlaywrightBrowser` drives the Chrome or Edge installed on the machine (playwright channel, nothing is
downloaded). playwright is an optional extra (`ui`): without it the browser reports an environment error.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

LOOPBACK_NAMES = {"localhost"}
CHANNELS = ("chrome", "msedge")
# Name resolution for any host but loopback fails: covers what routing cannot see (WebSocket, preconnect, DNS prefetch)
HOST_RULES = "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1"
MAX_ACTIONS = 20
MAX_WAIT_MS = 30_000
ACTION_TIMEOUT_MS = 10_000
GOTO_TIMEOUT_MS = 30_000


class BrowserError(Exception):
    """The machine cannot take the screenshot (no playwright, no Chrome/Edge): an environment problem."""


def is_loopback(url: str) -> bool:
    """http(s) URL whose host is 127.0.0.0/8, ::1 or localhost."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.scheme not in ("http", "https", "ws", "wss") or not host:
        return False
    if host.lower() in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _allowed_request(url: str) -> bool:
    return url.startswith(("data:", "blob:", "about:")) or is_loopback(url)


@dataclass(frozen=True)
class Action:
    kind: str  # click | fill | wait
    selector: str = ""
    text: str = ""
    ms: int = 0


def parse_actions(raw: Any) -> list[Action]:
    """[{"click": sel}, {"fill": sel, "text": t}, {"wait": sel | ms}] → actions; ValueError on anything else."""
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise ValueError("actions must be a list")
    if len(raw) > MAX_ACTIONS:
        raise ValueError(f"at most {MAX_ACTIONS} actions")
    actions = []
    for i, item in enumerate(raw, 1):
        if not isinstance(item, dict):
            raise ValueError(f"action {i}: an object like {{\"click\": \"#save\"}}")
        kinds = [k for k in ("click", "fill", "wait") if k in item]
        if len(kinds) != 1:
            raise ValueError(f"action {i}: exactly one of click, fill, wait")
        kind = kinds[0]
        value = item[kind]
        if kind == "wait" and isinstance(value, int | float) and not isinstance(value, bool):
            if not 0 <= value <= MAX_WAIT_MS:
                raise ValueError(f"action {i}: wait between 0 and {MAX_WAIT_MS} ms")
            actions.append(Action("wait", ms=int(value)))
            continue
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"action {i}: {kind} needs a CSS selector")
        if kind == "fill":
            if not isinstance(item.get("text"), str):
                raise ValueError(f"action {i}: fill needs text")
            actions.append(Action("fill", selector=value, text=item["text"]))
        else:
            actions.append(Action(kind, selector=value))
    return actions


@dataclass
class ShotResult:
    blocked: list[str]  # requests to other hosts that were stopped


class Browser(Protocol):
    async def screenshot(
        self, entry: str, target: str, out: Path, *, width: int, height: int, full_page: bool, actions: list[Action]
    ) -> ShotResult:
        """Open `entry` (e.g. a sign-in URL), then `target` when it differs, run the actions, save a PNG to `out`."""
        ...


class PlaywrightBrowser:
    def __init__(self, channels: tuple[str, ...] = CHANNELS) -> None:
        self.channels = channels

    async def screenshot(
        self, entry: str, target: str, out: Path, *, width: int, height: int, full_page: bool, actions: list[Action]
    ) -> ShotResult:
        for url in (entry, target):
            if not is_loopback(url):
                raise ValueError(f"only loopback addresses may be opened: {url}")
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserError("không có gói playwright (cài: uv sync --extra ui)") from exc
        blocked: list[str] = []
        async with async_playwright() as pw:
            browser = None
            errors = []
            for channel in self.channels:
                try:
                    browser = await pw.chromium.launch(channel=channel, headless=True, args=[HOST_RULES])
                    break
                except Exception as exc:  # not installed: try the next channel
                    errors.append(f"{channel}: {str(exc).splitlines()[0][:160]}")
            if browser is None:
                raise BrowserError("không tìm thấy Chrome hoặc Edge trên máy (" + "; ".join(errors) + ")")
            try:
                context = await browser.new_context(viewport={"width": width, "height": height}, service_workers="block")

                async def guard(route: Any) -> None:
                    url = route.request.url
                    if _allowed_request(url):
                        await route.continue_()
                    else:
                        blocked.append(url[:200])
                        await route.abort("blockedbyclient")

                async def guard_ws(ws: Any) -> None:
                    if is_loopback(ws.url):
                        ws.connect_to_server()
                    else:
                        blocked.append(ws.url[:200])
                        await ws.close()

                await context.route("**/*", guard)
                await context.route_web_socket(re.compile(".*"), guard_ws)
                page = await context.new_page()
                page.set_default_timeout(ACTION_TIMEOUT_MS)
                await page.goto(entry, wait_until="load", timeout=GOTO_TIMEOUT_MS)
                if target != entry:
                    await page.goto(target, wait_until="load", timeout=GOTO_TIMEOUT_MS)
                for action in actions:
                    if action.kind == "click":
                        await page.click(action.selector)
                    elif action.kind == "fill":
                        await page.fill(action.selector, action.text)
                    elif action.selector:
                        await page.wait_for_selector(action.selector)
                    else:
                        await page.wait_for_timeout(action.ms)
                out.parent.mkdir(parents=True, exist_ok=True)
                await page.screenshot(path=str(out), full_page=full_page)
            finally:
                await browser.close()
        return ShotResult(blocked=blocked)
