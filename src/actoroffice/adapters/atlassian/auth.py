"""Authentication for one Atlassian Cloud site. Adapters only see an `httpx.AsyncClient` with the right base URL.

Built: email + API token (Basic). The seam for OAuth 2.0 (3LO) / scoped tokens is `site_auth`: it returns the base URL and
the headers, so another scheme only changes this file (spec V-2).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field

import httpx

from actoroffice.adapters._http import TIMEOUT_S
from actoroffice.ports.connection import ConnectorError

SECRET_FIELDS: tuple[str, ...] = ("email", "token")
# Path of each product under the site URL when calling with Basic auth. NEEDS CHECKING (V-2): scoped API tokens and OAuth
# are said to go through https://api.atlassian.com/ex/<product>/<cloudId> instead.
PRODUCT_PATHS = {"jira": "", "confluence": "/wiki"}


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


def has_credentials(secret: dict[str, str]) -> bool:
    return all(secret.get(name) for name in SECRET_FIELDS)


def basic_auth(site: str, secret: dict[str, str], *, product: str) -> SiteAuth:
    raw = f"{secret.get('email', '')}:{secret.get('token', '')}".encode()
    return SiteAuth(normalize_site(site) + PRODUCT_PATHS[product], {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")})


def site_auth(site: str, secret: dict[str, str], *, product: str) -> SiteAuth:
    """Base URL + headers for a product of a site. Only Basic is built."""
    if secret.get("oauth_refresh_token") or secret.get("oauth_access_token"):
        raise ConnectorError("invalid", "Atlassian: đăng nhập OAuth 2.0 chưa được hỗ trợ, hãy dùng email + API token")
    if not has_credentials(secret):
        return SiteAuth(normalize_site(site) + PRODUCT_PATHS[product])
    return basic_auth(site, secret, product=product)


def build_client(site: str, secret: dict[str, str], *, product: str, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    auth = site_auth(site, secret, product=product)
    return httpx.AsyncClient(base_url=auth.base_url, headers={"Accept": "application/json", **auth.headers}, timeout=TIMEOUT_S, transport=transport)


def missing_secret_error(label: str) -> ConnectorError:
    return ConnectorError("unconfigured", f"{label}: chưa có email và API token. Vào Cài đặt › Kết nối để nhập.")
