"""`use: http`: any system with a REST API as a document source, by configuration only.

The config says what a reference looks like (`recognize`), which request fetches one (`fetch`: a path under `base_url`
with `{ref}`), and where title, text and version are in the answer. Only `base_url`'s host is ever called, with GET or
POST, and the credential comes from the connection's secret, never from the config.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, ClassVar, Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from actoroffice.adapters._http import TIMEOUT_S, Http, host_of, kind_for_status, now_iso, tls
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
OAUTH = "oauth2_password"
AUTH_SECRETS: dict[str, tuple[str, ...]] = {
    "none": (),
    "bearer": ("token",),
    "header": ("token",),
    "basic": ("username", "password"),
    OAUTH: ("username", "password"),  # client_id / client_secret are optional
}
HTTP_SECRET_FIELDS = (*SECRET_FIELDS, "client_id", "client_secret")
TOKEN_EARLY_S = 60.0  # a token is replaced this long before the system says it ends
TOKEN_DEFAULT_S = 300.0  # its lifetime when the system does not say (`expires_in` absent)


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
    auth: Literal["none", "bearer", "basic", "header", "oauth2_password"] = Field(
        default="none",
        description=(
            "none: không xác thực; bearer: token trong Authorization; basic: username + password; header: token trong header tên ở auth_header; "
            "oauth2_password: đăng nhập ở token_url bằng username + password (client_id, client_secret nếu hệ thống cần) rồi gửi token nhận được"
        ),
    )
    auth_header: str = Field(default="", validate_default=True, description="Tên header mang token khi auth: header, ví dụ X-Api-Key")
    token_url: str = Field(default="", description="Nơi đăng nhập khi auth: oauth2_password: một đường dẫn dưới base_url (/oauth/token) hoặc một địa chỉ trên cùng host")
    scope: str = Field(default="", description="Scope xin khi đăng nhập (auth: oauth2_password), có thể để trống")
    token_field: str = Field(default="access_token", description="Tên trường chứa token trong câu trả lời đăng nhập")
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

    @model_validator(mode="after")
    def _token_url_on_the_same_host(self) -> GenericHttpConfig:
        """The password is only ever sent to the connection's own host."""
        self.token_url, self.token_field = self.token_url.strip(), self.token_field.strip()
        if self.auth != OAUTH:
            return self
        if not self.token_url:
            raise ValueError("token_url: cần nơi đăng nhập khi auth: oauth2_password, ví dụ /oauth/token")
        if not self.token_field:
            raise ValueError("token_field: cần tên trường chứa token, ví dụ access_token")
        if "://" not in self.token_url:
            try:
                check_path_template(self.token_url)
            except ValueError as exc:
                raise ValueError(f"token_url: {exc}") from None
            return self
        check_url(self.token_url, sends_secret=True, what="token_url")
        if any(c in self.token_url for c in "?# \t\r\n"):
            raise ValueError("token_url: không có khoảng trắng, '?' hay '#'")
        if _origin(self.token_url) != _origin(self.base_url):
            raise ValueError("token_url phải ở cùng host với base_url (hoặc là một đường dẫn dưới base_url): mật khẩu chỉ gửi tới host của kết nối")
        return self


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = httpx.URL(url)
    return parsed.scheme, parsed.host, parsed.port


class GenericHttp:
    name: ClassVar[str] = NAME
    title: ClassVar[str] = "HTTP (REST API bất kỳ)"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS})
    config_schema: ClassVar[type[BaseModel]] = GenericHttpConfig
    secret_fields: ClassVar[tuple[str, ...]] = HTTP_SECRET_FIELDS
    optional_secret_fields: ClassVar[tuple[str, ...]] = HTTP_SECRET_FIELDS  # which one is needed depends on `auth`

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
        client = httpx.AsyncClient(base_url=config.base_url, headers=headers, auth=auth, timeout=config.timeout_s, transport=transport, verify=tls(transport))
        self.http = Http(client, label=label, blocked=blocked)
        # auth: oauth2_password. The secret stays in this object only; nothing here is ever put in a message.
        self._secret = dict(secret) if config.auth == OAUTH else {}
        self._token: str | None = None
        self._token_until = 0.0
        self._sign_in_lock = asyncio.Lock()  # one sign-in at a time; the others wait and use its token
        self.clock: Callable[[], float] = time.monotonic

    def __repr__(self) -> str:
        return f"GenericHttp({self.http.label!r}, auth={self.config.auth!r})"

    async def close(self) -> None:
        await self.http.close()

    async def test(self) -> Health:
        blocked = self.http.blocked
        if blocked is not None:
            return Health(state="unconfigured", checked_at=now_iso(), detail=str(blocked))
        signed_in = ""
        if self.config.auth == OAUTH:
            try:
                await self._bearer(fresh=True)
            except ConnectorError as exc:
                return Health(state="error", checked_at=now_iso(), detail=str(exc))
            signed_in = "Đã đăng nhập được (OAuth2). "
        if not self.config.health:
            if signed_in:
                return Health(state="ok", checked_at=now_iso(), detail=f"{signed_in}Chưa khai `health` nên chưa gọi thử một đường dẫn của API.")
            return Health(state="degraded", checked_at=now_iso(), detail="Chưa khai `health` (một đường dẫn GET nhẹ, ví dụ /ping) nên chưa gọi thử được hệ thống.")
        try:
            await self._request("GET", self.config.health)
        except ConnectorError as exc:
            return Health(state="error", checked_at=now_iso(), detail=str(exc))
        return Health(state="ok", checked_at=now_iso(), detail=f"{signed_in}Đã gọi được {host_of(self.config.base_url)} (GET {self.config.health}).")

    # ---- OAuth2 password grant

    async def _bearer(self, *, fresh: bool = False, not_this: str | None = None) -> str:
        """The token to send: the cached one while it lasts, else a new sign-in. `not_this` is a token the system
        just refused: whoever holds the lock first replaces it, the others get the new one without signing in again."""
        async with self._sign_in_lock:
            usable = self._token is not None and self.clock() < self._token_until and self._token != not_this
            if usable and not fresh:
                return self._token  # type: ignore[return-value]
            self._token = None
            return await self._sign_in()

    async def _sign_in(self) -> str:
        """POST grant_type=password to token_url (form-encoded). No message from here holds the password, the client
        secret, the token or what the system answered."""
        label, secret = self.http.label, self._secret
        self.http._guard(self.config.token_url)  # the own-host rule, once more at the door
        form = {"grant_type": "password", "username": secret["username"], "password": secret["password"]}
        if self.config.scope:
            form["scope"] = self.config.scope
        client_auth: httpx.Auth | None = None
        if secret.get("client_secret"):
            client_auth = httpx.BasicAuth(secret.get("client_id", ""), secret["client_secret"])
        elif secret.get("client_id"):
            form["client_id"] = secret["client_id"]
        try:
            kwargs: dict[str, Any] = {"auth": client_auth} if client_auth else {}
            response = await self.http.client.post(self.config.token_url, data=form, **kwargs)
        except httpx.HTTPError as exc:
            raise ConnectorError("network", f"{label}: lỗi mạng ({type(exc).__name__}) khi đăng nhập OAuth2") from None
        status = response.status_code
        if status in (400, 401, 403):
            raise ConnectorError("auth", f"{label}: đăng nhập OAuth2 bị từ chối ({status}). Kiểm tra username, password và client_id/client_secret của kết nối.")
        if status >= 300:
            kind = kind_for_status(status) if status >= 400 else "invalid"
            raise ConnectorError(kind, f"{label}: không đăng nhập OAuth2 được ({status})")
        data = parse_json(response.text)
        token = data.get(self.config.token_field) if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip() or any(c in token for c in "\r\n"):
            raise ConnectorError("auth", f"{label}: câu trả lời đăng nhập OAuth2 không có token ở trường `{self.config.token_field}`")
        try:
            lifetime = float(data.get("expires_in"))
        except (TypeError, ValueError):
            lifetime = TOKEN_DEFAULT_S
        self._token = token.strip()
        self._token_until = self.clock() + max(0.0, lifetime - TOKEN_EARLY_S)
        return self._token

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """One call of the API. With oauth2_password: the token is sent, and a 401 means one new sign-in and one retry."""
        if self.config.auth != OAUTH:
            return await self.http.request(method, path, **kwargs)
        if self.http.blocked is not None:
            raise self.http.blocked
        token = await self._bearer()
        try:
            return await self.http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)
        except ConnectorError as exc:
            if exc.kind != "auth":
                raise
        token = await self._bearer(not_this=token)
        return await self.http.request(method, path, headers={"Authorization": f"Bearer {token}"}, **kwargs)

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
        return await self._request(request.method, path, **kwargs)

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
