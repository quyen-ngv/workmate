"""One HTTP helper for every adapter: error kinds, retries with backoff, a cap on concurrent requests, a host guard.

Nothing here logs. Error messages carry a status code and a path only: never a response body, a header or a token.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from actoroffice.ports.connection import ConnectorError

MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 0.5
MAX_SLEEP_S = 60.0
DEFAULT_CONCURRENCY = 4
TIMEOUT_S = 30.0
# 5xx and transport errors are retried only when repeating the request cannot do the work twice. 429 is always retried.
IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})

Sleep = Callable[[float], Awaitable[None]]

_REASONS = {
    "auth": "xác thực thất bại, kiểm tra tài khoản/token của kết nối",
    "forbidden": "không có quyền",
    "not_found": "không tìm thấy",
    "rate_limited": "vượt hạn mức, thử lại sau",
    "network": "hệ thống ngoài đang lỗi",
    "invalid": "yêu cầu không hợp lệ",
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def host_of(url: str) -> str:
    """Lower-case host of a URL ("" when it has none)."""
    try:
        return (urlsplit(url.strip()).hostname or "").lower()
    except ValueError:
        return ""


def kind_for_status(status: int) -> str:
    if status == 401:
        return "auth"
    if status == 403:
        return "forbidden"
    if status == 404:
        return "not_found"
    if status == 429:
        return "rate_limited"
    if status >= 500:
        return "network"
    return "invalid"


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after", "").strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


class Http:
    """Wraps an authenticated `httpx.AsyncClient` bound to one base URL. Adapters call `request` / `json` with paths."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        label: str,
        max_concurrent: int = DEFAULT_CONCURRENCY,
        sleep: Sleep = asyncio.sleep,
        jitter: Callable[[], float] = random.random,
        blocked: ConnectorError | None = None,
    ) -> None:
        self.client = client
        self.label = label
        self.sleep = sleep
        self.jitter = jitter
        self.blocked = blocked  # set when the connection cannot work at all (no secret, unsupported auth)
        self._semaphore = asyncio.Semaphore(max(1, max_concurrent))

    async def close(self) -> None:
        await self.client.aclose()

    def _guard(self, url: str) -> str:
        """Only the connection's own host is ever called. Returns the path used in messages."""
        target = httpx.URL(url)
        base = self.client.base_url
        if target.is_absolute_url and (target.scheme, target.host, target.port) != (base.scheme, base.host, base.port):
            raise ConnectorError("invalid", f"{self.label}: từ chối gọi một host khác với host của kết nối")
        return target.path

    def _backoff(self, attempt: int) -> float:
        return min(MAX_SLEEP_S, BACKOFF_BASE_S * (2**attempt) * (0.5 + self.jitter()))

    async def request(self, method: str, url: str, *, allow_redirect: bool = False, **kwargs: Any) -> httpx.Response:
        """One call with retries. Returns the response for 2xx (and 3xx when `allow_redirect`); raises ConnectorError otherwise.
        Redirects are never followed here."""
        if self.blocked is not None:
            raise self.blocked
        method = method.upper()
        path = self._guard(url)
        where = f"{method} {path}"
        for attempt in range(MAX_ATTEMPTS):
            last = attempt == MAX_ATTEMPTS - 1
            try:
                async with self._semaphore:
                    response = await self.client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                if last or method not in IDEMPOTENT:
                    raise ConnectorError("network", f"{self.label}: lỗi mạng ({type(exc).__name__}) khi gọi {where}") from None
                await self.sleep(self._backoff(attempt))
                continue
            status = response.status_code
            if status < 300 or (status < 400 and allow_redirect):
                return response
            kind = kind_for_status(status) if status >= 400 else "invalid"
            wait = _retry_after(response)
            if status == 403 and (wait is not None or response.headers.get("x-ratelimit-remaining") == "0"):
                kind = "rate_limited"  # GitHub answers 403 when the quota is used up
            retry = kind == "rate_limited" or (kind == "network" and method in IDEMPOTENT)
            if retry and not last:
                await self.sleep(min(MAX_SLEEP_S, wait) if wait is not None else self._backoff(attempt))
                continue
            reason = "bị chuyển hướng, không đi theo" if status < 400 else _REASONS[kind]
            raise ConnectorError(kind, f"{self.label}: {reason} ({status} {where})", retry_after=wait if kind == "rate_limited" else None)
        raise ConnectorError("network", f"{self.label}: không gọi được {where}")  # pragma: no cover - the loop always returns or raises

    async def json(self, method: str, url: str, **kwargs: Any) -> Any:
        response = await self.request(method, url, **kwargs)
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise ConnectorError("invalid", f"{self.label}: phản hồi không phải JSON ({method.upper()} {httpx.URL(url).path})") from None
