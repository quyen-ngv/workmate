"""Authentication for one Atlassian Cloud site. Adapters only see an `httpx.AsyncClient` with the right base URL.

Built: email + API token (Basic), in the two forms Atlassian has (checked 2026-11-14, spec V-2):
- a token without scopes calls the site itself: `https://<site>/rest/api/3/…`, `https://<site>/wiki/…`;
- a token with scopes only works through the gateway, `https://api.atlassian.com/ex/<jira|confluence>/<cloudId>/…`, with the
  same Basic header. The cloud id is what `https://<site>/_edge/tenant_info` answers; it is configuration (`cloud_id`).
OAuth 2.0 (3LO) uses the same gateway URL with a Bearer token; it is not built, and `site_auth` is where it would go.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field

import httpx

from actoroffice.adapters._http import TIMEOUT_S
from actoroffice.ports.connection import ConnectorError

SECRET_FIELDS: tuple[str, ...] = ("email", "token")
# Path of each product under the site URL, and under the gateway URL: Atlassian's example for a scoped token is
# https://api.atlassian.com/ex/confluence/<cloudId>/wiki/rest/api/… (support.atlassian.com/confluence/kb/scoped-api-tokens-in-confluence-cloud).
PRODUCT_PATHS = {"jira": "", "confluence": "/wiki"}
GATEWAY = "https://api.atlassian.com/ex/{product}/{cloud_id}"
CLOUD_ID_HELP = "Cloud ID của site. Chỉ khai khi API token có phạm vi (scoped): token loại này phải gọi qua api.atlassian.com. Xem tại https://<site>/_edge/tenant_info"
SCOPED_TOKEN_HINT = " Nếu API token có phạm vi (scoped), hãy khai cloud_id cho kết nối: token loại này chỉ gọi được qua api.atlassian.com."


@dataclass(frozen=True)
class SiteAuth:
    base_url: str
    headers: dict[str, str] = field(default_factory=dict)


def normalize_site(site: str) -> str:
    """ "acme.atlassian.net/wiki/" → "https://acme.atlassian.net"."""
    site = site.strip().rstrip("/")
    if site and "://" not in site:
        site = "https://" + site
    if site.endswith("/wiki"):
        site = site[: -len("/wiki")]
    return site


def check_cloud_id(value: str) -> str:
    """Validator for the `cloud_id` of a connection: empty, or the id Atlassian gives (a UUID)."""
    value = value.strip()
    if value and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", value):
        raise ValueError("cloud_id không hợp lệ: đó là mã dạng 11223344-a1b2-3b33-c444-def123456789")
    return value


def has_credentials(secret: dict[str, str]) -> bool:
    return all(secret.get(name) for name in SECRET_FIELDS)


def base_url(site: str, *, product: str, cloud_id: str = "") -> str:
    """The site for a token without scopes; the gateway, addressed by cloud id, for a token with scopes."""
    root = GATEWAY.format(product=product, cloud_id=cloud_id) if cloud_id else normalize_site(site)
    return root + PRODUCT_PATHS[product]


def basic_auth(site: str, secret: dict[str, str], *, product: str, cloud_id: str = "") -> SiteAuth:
    raw = f"{secret.get('email', '')}:{secret.get('token', '')}".encode()
    return SiteAuth(base_url(site, product=product, cloud_id=cloud_id), {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")})


def site_auth(site: str, secret: dict[str, str], *, product: str, cloud_id: str = "") -> SiteAuth:
    """Base URL + headers for a product of a site. Only Basic is built."""
    if secret.get("oauth_refresh_token") or secret.get("oauth_access_token"):
        raise ConnectorError("invalid", "Atlassian: đăng nhập OAuth 2.0 chưa được hỗ trợ, hãy dùng email + API token")
    if not has_credentials(secret):
        return SiteAuth(base_url(site, product=product, cloud_id=cloud_id))
    return basic_auth(site, secret, product=product, cloud_id=cloud_id)


def build_client(site: str, secret: dict[str, str], *, product: str, cloud_id: str = "", transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    auth = site_auth(site, secret, product=product, cloud_id=cloud_id)
    return httpx.AsyncClient(base_url=auth.base_url, headers={"Accept": "application/json", **auth.headers}, timeout=TIMEOUT_S, transport=transport)


def missing_secret_error(label: str) -> ConnectorError:
    return ConnectorError("unconfigured", f"{label}: chưa có email và API token. Vào Cài đặt › Kết nối để nhập.")
