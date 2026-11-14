"""`use: http`: any system with a REST API as a document source, by configuration only.

The config says what a reference looks like (`recognize`), which request fetches one (`fetch`: a path under `base_url`
with `{ref}`), and where title, text and version are in the answer. Only `base_url`'s host is ever called, with GET or
POST, and the credential comes from the connection's secret, never from the config.
"""

from __future__ import annotations

from typing import Any, ClassVar, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from actoroffice.adapters._http import TIMEOUT_S, Http, host_of, now_iso
from actoroffice.adapters.generic.common import (
    SECRET_FIELDS,
    DocMapping,
    HitMapping,
    Recognizer,
    SourceConfig,
    Strict,
    allowed,
    check_header_name,
    check_plain_headers,
    check_url,
    document,
    fill,
    hits,
    missing_secrets,
    parse_json,
    require_placeholder,
)
from actoroffice.ports.connection import Capability, ConnectorError, Health, Scope
from actoroffice.ports.docs import DocContent, DocHit, DocRef

NAME = "http"
LABEL = "HTTP"
MAX_HITS = 50
AUTH_SECRETS: dict[str, tuple[str, ...]] = {"none": (), "bearer": ("token",), "header": ("token",), "basic": ("username", "password")}


class HttpRequest(Strict):
    method: Literal["GET", "POST"] = Field(default="GET", description="GET hoặc POST (kết nối này chỉ đọc)")
    path: str = Field(description="Đường dẫn dưới base_url, ví dụ /documents/{ref}")
    query: dict[str, str | int | float | bool] = Field(default_factory=dict, description='Tham số trên địa chỉ, ví dụ {q: "{query}"}')
    body: Any = Field(default=None, description="Thân JSON của yêu cầu POST")

    @field_validator("method", mode="before")
    @classmethod
    def _method(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    @field_validator("path")
    @classmethod
    def _path(cls, value: str) -> str:
        return check_path_template(value)


def check_path_template(value: str) -> str:
    """A path under the connection's base URL. A full URL here would be a way to send the credential to another host."""
    value = value.strip()
    if "://" in value or value.startswith("//"):
        raise ValueError("phải là đường dẫn dưới base_url (ví dụ /documents/{ref}), không phải một địa chỉ đầy đủ: kết nối chỉ gọi host của chính nó")
    if not value.startswith("/") or any(c in value for c in "?# \t\r\n") or any(part in (".", "..") for part in value.split("/")):
        raise ValueError("phải bắt đầu bằng '/', không có '..', khoảng trắng, '?' hay '#' (tham số ghi ở `query`)")
    return value


class HttpFetch(HttpRequest, DocMapping):
    @model_validator(mode="after")
    def _has_ref(self) -> HttpFetch:
        require_placeholder([self.path, self.query, self.body], "ref", "path, query hoặc body")
        return self


class HttpSearch(HttpRequest, HitMapping):
    @model_validator(mode="after")
    def _has_query(self) -> HttpSearch:
        require_placeholder([self.path, self.query, self.body], "query", "path, query hoặc body")
        return self


class GenericHttpConfig(SourceConfig):
    base_url: str = Field(description="Địa chỉ gốc của API, ví dụ https://docs.acme.example/api. Kết nối chỉ gọi host này")
    auth: Literal["none", "bearer", "basic", "header"] = Field(
        default="none", description="none: không xác thực; bearer: token trong Authorization; basic: username + password; header: token trong header tên ở auth_header"
    )
    auth_header: str = Field(default="", validate_default=True, description="Tên header mang token khi auth: header, ví dụ X-Api-Key")
    headers: dict[str, str] = Field(default_factory=dict, description="Header không bí mật gửi kèm mọi yêu cầu, ví dụ Accept")
    fetch: HttpFetch = Field(description="Cách đọc một tài liệu theo tham chiếu")
    search: HttpSearch | None = Field(default=None, description="Cách tìm tài liệu theo từ khoá (không khai thì kết nối không tìm kiếm)")
    health: str = Field(default="", description="Đường dẫn GET nhẹ để kiểm tra kết nối, ví dụ /ping")
    timeout_s: float = Field(default=TIMEOUT_S, gt=0, le=300, description="Thời gian chờ mỗi yêu cầu (giây)")

    @field_validator("base_url")
    @classmethod
    def _base_url(cls, value: str) -> str:
        return check_url(value, sends_secret=False, what="base_url").rstrip("/")

    @field_validator("auth_header")
    @classmethod
    def _auth_header(cls, value: str, info: ValidationInfo) -> str:
        if info.data.get("auth") != "header":
            return value.strip()
        if not value.strip():
            raise ValueError("cần tên header khi auth: header, ví dụ X-Api-Key")
        return check_header_name(value)

    @field_validator("headers")
    @classmethod
    def _headers(cls, value: dict[str, str]) -> dict[str, str]:
        return check_plain_headers(value)

    @field_validator("health")
    @classmethod
    def _health(cls, value: str) -> str:
        return check_path_template(value) if value.strip() else ""

    @model_validator(mode="after")
    def _no_secret_over_plain_http(self) -> GenericHttpConfig:
        check_url(self.base_url, sends_secret=self.auth != "none", what="base_url")
        return self


class GenericHttp:
    name: ClassVar[str] = NAME
    title: ClassVar[str] = "HTTP (REST API bất kỳ)"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS})
    config_schema: ClassVar[type[BaseModel]] = GenericHttpConfig
    secret_fields: ClassVar[tuple[str, ...]] = SECRET_FIELDS
    optional_secret_fields: ClassVar[tuple[str, ...]] = SECRET_FIELDS  # which one is needed depends on `auth`

    def __init__(self, config: GenericHttpConfig, secret: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.recognizer = Recognizer(config.recognize)
        label = f"{LABEL} {host_of(config.base_url)}"
        headers = dict(config.headers)
        auth: httpx.Auth | None = None
        blocked: ConnectorError | None = None
        missing = missing_secrets(AUTH_SECRETS[config.auth], secret)
        if missing:
            blocked = ConnectorError("unconfigured", f"{label}: chưa có bí mật ({', '.join(missing)}) cho auth: {config.auth}. Vào Cài đặt › Kết nối để nhập.")
        elif config.auth == "bearer":
            headers["Authorization"] = f"Bearer {secret['token']}"
        elif config.auth == "header":
            headers[config.auth_header] = secret["token"]
        elif config.auth == "basic":
            auth = httpx.BasicAuth(secret["username"], secret["password"])
        client = httpx.AsyncClient(base_url=config.base_url, headers=headers, auth=auth, timeout=config.timeout_s, transport=transport)
        self.http = Http(client, label=label, blocked=blocked)

    async def close(self) -> None:
        await self.http.close()

    async def test(self) -> Health:
        blocked = self.http.blocked
        if blocked is not None:
            return Health(state="unconfigured", checked_at=now_iso(), detail=str(blocked))
        if not self.config.health:
            return Health(state="degraded", checked_at=now_iso(), detail="Chưa khai `health` (một đường dẫn GET nhẹ, ví dụ /ping) nên chưa gọi thử được hệ thống.")
        try:
            await self.http.request("GET", self.config.health)
        except ConnectorError as exc:
            return Health(state="error", checked_at=now_iso(), detail=str(exc))
        return Health(state="ok", checked_at=now_iso(), detail=f"Đã gọi được {host_of(self.config.base_url)} (GET {self.config.health}).")

    # ---- references

    def recognize(self, text: str) -> list[DocRef]:
        return [DocRef(ref=ref, kind=NAME) for ref in self.recognizer.find(text)]

    def _ref(self, text: str) -> str:
        value = self.recognizer.canonical(text)
        if value is None or value in (".", ".."):
            raise ConnectorError("invalid", f"{self.http.label}: «{text[:80]}» không phải tham chiếu mà kết nối này nhận ra")
        return value

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        value = self.recognizer.canonical(ref.ref)
        return value is not None and allowed(value, self.config.allow)

    # ---- reading

    async def _send(self, request: HttpRequest, values: dict[str, Any]) -> httpx.Response:
        """One configured request. A value goes into the path percent-encoded, so it stays one path segment."""
        path = fill(request.path, {name: quote(str(value), safe="") for name, value in values.items()})
        kwargs: dict[str, Any] = {}
        if request.query:
            kwargs["params"] = fill(dict(request.query), values)
        if request.body is not None:
            kwargs["json"] = fill(request.body, values)
        return await self.http.request(request.method, path, **kwargs)

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        value = self._ref(ref.ref)
        if not allowed(value, self.config.allow):
            raise ConnectorError("forbidden", f"{self.http.label}: {value} nằm ngoài các tham chiếu được phép của kết nối (allow)")
        response = await self._send(self.config.fetch, {"ref": value})
        raw = response.text
        return document(NAME, self.recognizer, value, raw, parse_json(raw), self.config.fetch, self.http.label)

    async def search(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]:
        query = query.strip()
        if self.config.search is None or not query:
            return []
        limit = max(1, min(limit, MAX_HITS))
        response = await self._send(self.config.search, {"query": query, "limit": limit})
        data = parse_json(response.text)
        if data is None:
            raise ConnectorError("invalid", f"{self.http.label}: kết quả tìm kiếm không phải JSON")
        return hits(NAME, self.recognizer, self.config.allow, data, self.config.search, limit, self.http.label)
