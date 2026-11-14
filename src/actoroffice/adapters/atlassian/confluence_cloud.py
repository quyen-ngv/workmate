"""Confluence Cloud: pages as documents (`DocSource`) and as a wiki (`Wiki`).

A reference (`DocRef.ref`) is a page id, a page URL of the connected site, or `"<SPACE>:<title>"` (how storage format
links to a page). `Wiki.search(text, space=, limit=)` and `DocSource.search(query, scope, limit)` share one name in the
ports, so `search` looks at its second argument: a `Scope` means document hits; otherwise page references.
`search_pages` and `search_docs` are the two explicit forms.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from actoroffice.adapters._http import Http, host_of, now_iso
from actoroffice.adapters.atlassian import CONFLUENCE_KIND, JIRA_KIND
from actoroffice.adapters.atlassian.auth import CLOUD_ID_HELP, SCOPED_TOKEN_HINT, SECRET_FIELDS, build_client, check_cloud_id, has_credentials, missing_secret_error, normalize_site
from actoroffice.adapters.atlassian.storage_html import INCLUDE_RE, include_note, markdown_to_storage, storage_to_markdown
from actoroffice.ports.connection import Capability, ConnectorError, Health, Scope
from actoroffice.ports.docs import Attachment, DocContent, DocHit, DocRef, Page, PageRef

LABEL = "Confluence"
MAX_INCLUDE_DEPTH = 2
MAX_PAGES_OF_RESULTS = 10
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+")
_SPACE_PAGE_RE = re.compile(r"^/wiki/spaces/([^/]+)/pages/(\d+)(?:/|$)")
_TINY_RE = re.compile(r"^/wiki/x/([A-Za-z0-9_-]+)/?$")
_ISSUE_URL_RE = re.compile(r"^/browse/([A-Z][A-Z0-9_]*-\d+)/?$")

# ---- Checked on 2026-11-14 against the Confluence Cloud REST API v2 and v1 references (developer.atlassian.com/cloud/confluence/rest/v2
# and /rest/v1) and their OpenAPI documents (spec V-1, V-3). The scopes matter for a token with scopes (see auth.py) and for
# OAuth. REST v2 only takes granular scopes; the two v1 calls kept here take a classic one each (their granular form,
# still marked beta, is read:content-details:confluence for both).
_READ_SCOPES = ["read:page:confluence", "read:space:confluence", "read:attachment:confluence", "search:confluence", "read:confluence-user"]
REQUIRED_SCOPES: dict[Capability, list[str]] = {
    Capability.DOCS: _READ_SCOPES,
    # read:hierarchical-content is for the children of a page; write:page only when publishing
    Capability.WIKI: [*_READ_SCOPES, "read:hierarchical-content:confluence", "write:page:confluence"],
}
# Paths are relative to <site>/wiki. Pages come from REST v2 with the storage body (v1 `content/{id}` is gone); searching by
# CQL and the current user exist in v1 only.
PAGE_PATH = "/api/v2/pages/{id}"
PAGE_BODY_PARAMS = {"body-format": "storage"}
PAGES_PATH = "/api/v2/pages"  # ?space-id=&title= finds a page by title
CHILDREN_PATH = "/api/v2/pages/{id}/direct-children"  # every kind of child, with a `type`; `/children` (pages only) is deprecated
ATTACHMENTS_PATH = "/api/v2/pages/{id}/attachments"
SPACE_PATH = "/api/v2/spaces/{id}"
SPACES_PATH = "/api/v2/spaces"  # ?keys=
SEARCH_PATH = "/rest/api/content/search"
IDENTITY_PATH = "/rest/api/user/current"
# Not an API, so not in Atlassian's reference: the short link is expected to answer with a redirect to the page URL. The
# Location header is read, never followed blindly. Through the gateway (a token with scopes) it cannot be resolved.
TINY_PATH = "/x/{code}"
MAX_TINY_HOPS = 3


class FollowLinks(BaseModel):
    depth: int = Field(default=1, ge=0, description="Đi theo liên kết mấy cấp")
    max: int = Field(default=20, ge=0, description="Tối đa bao nhiêu tài liệu được thêm theo liên kết")


class ConfluenceCloudConfig(BaseModel):
    site: str = Field(description="Địa chỉ site, ví dụ https://acme.atlassian.net")
    cloud_id: str = Field(default="", description=CLOUD_ID_HELP)
    spaces: list[str] = Field(default_factory=list, description="Khoá space được phép đọc (PAY, ARCH). Để trống: mọi space token đọc được")
    follow_links: FollowLinks = Field(default_factory=FollowLinks)
    mirror: bool = Field(default=False, description="Tải trang về để tìm kiếm cục bộ")
    write: bool = Field(default=False, description="Cho phép xuất bản trang")

    @field_validator("site")
    @classmethod
    def _site(cls, value: str) -> str:
        site = normalize_site(value)
        if not host_of(site):
            raise ValueError("site phải là địa chỉ như https://acme.atlassian.net")
        return site

    @field_validator("cloud_id")
    @classmethod
    def _cloud_id(cls, value: str) -> str:
        return check_cloud_id(value)

    @field_validator("spaces")
    @classmethod
    def _spaces(cls, value: list[str]) -> list[str]:
        keys = [v.strip() for v in value if v.strip()]
        bad = [k for k in keys if not re.fullmatch(r"~?[A-Za-z0-9_.-]+", k)]
        if bad:
            raise ValueError(f"khoá space không hợp lệ: {', '.join(bad)}")
        return keys


class ConfluenceCloud:
    name: ClassVar[str] = CONFLUENCE_KIND
    title: ClassVar[str] = "Confluence Cloud"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS, Capability.WIKI})
    config_schema: ClassVar[type[BaseModel]] = ConfluenceCloudConfig
    secret_fields: ClassVar[tuple[str, ...]] = SECRET_FIELDS
    required_scopes: ClassVar[dict[Capability, list[str]]] = REQUIRED_SCOPES

    def __init__(self, config: ConfluenceCloudConfig, secret: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.site = config.site
        self.host = host_of(config.site)
        self.base = config.site + "/wiki"
        self.http = Http(
            build_client(config.site, secret, product="confluence", cloud_id=config.cloud_id, transport=transport),
            label=LABEL,
            blocked=None if has_credentials(secret) else missing_secret_error(LABEL),
        )
        self._space_keys: dict[str, str] = {}  # space id → key
        self._space_ids: dict[str, str] = {}  # space key → id
        self._page_spaces: dict[str, str] = {}  # page id → space key, learnt from URLs and from pages read

    async def close(self) -> None:
        await self.http.close()

    async def test(self) -> Health:
        if self.http.blocked is not None:
            return Health(state="unconfigured", checked_at=now_iso(), detail=str(self.http.blocked))
        try:
            me = await self.http.json("GET", IDENTITY_PATH)
        except ConnectorError as exc:
            hint = SCOPED_TOKEN_HINT if exc.kind == "auth" and not self.config.cloud_id else ""
            return Health(state="error", checked_at=now_iso(), detail=str(exc) + hint)
        identity = str((me or {}).get("email") or (me or {}).get("displayName") or (me or {}).get("publicName") or "") or None
        return Health(state="ok", checked_at=now_iso(), detail="Đã kết nối Confluence. API token (Basic) không cho biết quyền, nên chưa đối chiếu được quyền thiếu.", identity=identity)

    # ---- references

    def _parse_url(self, url: str) -> tuple[str, str, str] | None:
        """A page URL of this site → ("id", page id, space key) or ("tiny", code, ""). None for anything else."""
        if host_of(url) != self.host:
            return None
        parts = urlsplit(url)
        if m := _SPACE_PAGE_RE.match(parts.path):
            return "id", m.group(2), m.group(1)
        if m := _TINY_RE.match(parts.path):
            return "tiny", m.group(1), ""
        if parts.path.rstrip("/").endswith("/pages/viewpage.action"):
            page_id = (parse_qs(parts.query).get("pageId") or [""])[0]
            if page_id.isdigit():
                return "id", page_id, ""
        return None

    def _space_allowed(self, space: str) -> bool:
        return not self.config.spaces or space in self.config.spaces

    def _require_space(self, space: str) -> None:
        if not self._space_allowed(space):
            raise ConnectorError("forbidden", f"{LABEL}: space «{space}» nằm ngoài các space được phép của kết nối ({', '.join(self.config.spaces)})")

    def recognize(self, text: str) -> list[DocRef]:
        refs: list[DocRef] = []
        for m in _URL_RE.finditer(text):
            url = m.group(0).rstrip(".,;:!?")
            parsed = self._parse_url(url)
            if parsed is None:
                continue
            form, value, space = parsed
            if form == "id" and space:
                self._page_spaces[value] = space
            ref = DocRef(ref=value if form == "id" else url, kind=CONFLUENCE_KIND)
            if ref not in refs:
                refs.append(ref)
        return refs

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        """False when the page is known to be outside the allowed spaces. A bare page id or a tiny link whose space is
        not known yet passes here; reading it still refuses a page of another space."""
        value = ref.ref.strip()
        if "://" in value:
            parsed = self._parse_url(value)
            if parsed is None:
                return False
            form, found, space = parsed
            space = space or (self._page_spaces.get(found, "") if form == "id" else "")
        elif value.isdigit():
            space = self._page_spaces.get(value, "")
        elif ":" in value:
            space = value.split(":", 1)[0]
        else:
            return False
        return self._space_allowed(space) if space else True

    async def resolve(self, url: str) -> str:
        """A page URL of this site (any of the three forms) → page id. A URL of another host is refused without a request."""
        parsed = self._parse_url(url)
        if parsed is None:
            raise ConnectorError("invalid", f"{LABEL}: địa chỉ không phải trang của site đã kết nối ({self.host})")
        form, value, space = parsed
        if form == "id":
            if space:
                self._page_spaces[value] = space
            return value
        path = TINY_PATH.format(code=value)
        for _ in range(MAX_TINY_HOPS):
            response = await self.http.request("GET", path, allow_redirect=True)
            location = response.headers.get("location", "")
            if not (300 <= response.status_code < 400) or not location:
                break
            target = urljoin(f"{self.base}{path}", location)
            parsed = self._parse_url(target)
            if parsed is not None and parsed[0] == "id":
                if parsed[2]:
                    self._page_spaces[parsed[1]] = parsed[2]
                return parsed[1]
            target_path = urlsplit(target).path
            if host_of(target) != self.host or not target_path.startswith("/wiki/"):
                break
            path = target_path[len("/wiki") :]
        raise ConnectorError("not_found", f"{LABEL}: không tìm ra trang từ link rút gọn /wiki/x/{value}")

    async def _page_id(self, ref: str) -> str:
        ref = ref.strip()
        if ref.isdigit():
            return ref
        if "://" in ref:
            return await self.resolve(ref)
        if ":" in ref:
            space, title = ref.split(":", 1)
            found = await self._find_by_title(space, title)
            if found is None:
                raise ConnectorError("not_found", f"{LABEL}: không thấy trang «{title[:80]}» trong space {space or '?'}")
            return str(found["id"])
        raise ConnectorError("invalid", f"{LABEL}: «{ref[:80]}» không phải id trang, địa chỉ trang hay «SPACE:tiêu đề»")

    # ---- spaces

    async def _space_key(self, space_id: str) -> str:
        if not space_id:
            return ""
        if space_id not in self._space_keys:
            data = await self.http.json("GET", SPACE_PATH.format(id=space_id)) or {}
            self._space_keys[space_id] = str(data.get("key") or "")
            self._space_ids.setdefault(self._space_keys[space_id], space_id)
        return self._space_keys[space_id]

    async def _space_id(self, key: str) -> str:
        if key not in self._space_ids:
            data = await self.http.json("GET", SPACES_PATH, params={"keys": key}) or {}
            results = data.get("results") or []
            if not results:
                raise ConnectorError("not_found", f"{LABEL}: không thấy space {key}")
            self._space_ids[key] = str(results[0]["id"])
            self._space_keys[self._space_ids[key]] = key
        return self._space_ids[key]

    # ---- reading

    def _url(self, data: dict[str, Any]) -> str:
        return self.base + str((data.get("_links") or {}).get("webui") or "")

    async def _find_by_title(self, space: str, title: str) -> dict[str, Any] | None:
        if not space or not title:
            return None
        self._require_space(space)
        data = await self.http.json("GET", PAGES_PATH, params={"space-id": await self._space_id(space), "title": title, **PAGE_BODY_PARAMS}) or {}
        return (data.get("results") or [None])[0]

    def _classify(self, links: list[DocRef]) -> list[DocRef]:
        """Plain hyperlinks: keep those to this site (a page or an issue), drop other hosts."""
        out: list[DocRef] = []
        for link in links:
            if not link.kind:
                parsed = self._parse_url(link.ref)
                issue = _ISSUE_URL_RE.match(urlsplit(link.ref).path) if host_of(link.ref) == self.host else None
                if parsed is not None:
                    link = DocRef(ref=parsed[1] if parsed[0] == "id" else link.ref, kind=CONFLUENCE_KIND)
                elif issue:
                    link = DocRef(ref=issue.group(1), kind=JIRA_KIND)
                else:
                    continue
            if link not in out:
                out.append(link)
        return out

    async def _convert(self, storage: str, space: str, level: int, seen: set[tuple[str, str]]) -> tuple[str, list[DocRef], list[str], list[str]]:
        """Storage format → markdown, with include / excerpt-include macros replaced by the page they name (at most
        `MAX_INCLUDE_DEPTH` levels, allowed spaces only). What cannot be included stays as a link and a note."""
        markdown, links, notes, files = storage_to_markdown(storage, space=space)
        for m in list(INCLUDE_RE.finditer(markdown)):
            inc_space, inc_title = m.group(1) or space, m.group(2)
            target = (inc_space, inc_title)
            if level >= MAX_INCLUDE_DEPTH or target in seen or not self._space_allowed(inc_space):
                continue
            try:
                found = await self._find_by_title(inc_space, inc_title)
            except ConnectorError as exc:
                if exc.kind in ("not_found", "forbidden"):
                    continue
                raise
            if found is None:
                continue
            inner = str(((found.get("body") or {}).get("storage") or {}).get("value") or "")
            sub_md, sub_links, sub_notes, sub_files = await self._convert(inner, inc_space, level + 1, seen | {target})
            markdown = markdown.replace(m.group(0), sub_md or f"(trang «{inc_title}» trống)", 1)
            notes = [n for n in notes if n != include_note(inc_title)] + [n for n in sub_notes if n not in notes]
            links += [link for link in sub_links if link not in links]
            files += [f for f in sub_files if f not in files]
        return markdown, links, notes, files

    async def _attachments(self, page_id: str, notes: list[str]) -> list[Attachment]:
        try:
            data = await self.http.json("GET", ATTACHMENTS_PATH.format(id=page_id)) or {}
        except ConnectorError as exc:
            if exc.kind not in ("forbidden", "not_found"):
                raise
            notes.append("Không liệt kê được tệp đính kèm của trang.")
            return []
        out = []
        for item in data.get("results") or []:
            download = str(item.get("downloadLink") or (item.get("_links") or {}).get("download") or "")
            out.append(Attachment(name=str(item.get("title") or ""), media_type=str(item.get("mediaType") or ""), size=int(item.get("fileSize") or 0), url=self.base + download if download else ""))
        return out

    async def _to_page(self, data: dict[str, Any], *, body: bool = True) -> Page:
        page_id = str(data.get("id") or "")
        space = await self._space_key(str(data.get("spaceId") or ""))
        self._require_space(space)
        self._page_spaces[page_id] = space
        title = str(data.get("title") or "")
        markdown, links, notes, attachments = "", [], [], []
        if body:
            storage = str(((data.get("body") or {}).get("storage") or {}).get("value") or "")
            markdown, links, notes, _files = await self._convert(storage, space, 0, {(space, title)})
            attachments = await self._attachments(page_id, notes)
        return Page(
            id=page_id,
            title=title,
            space=space,
            version=int((data.get("version") or {}).get("number") or 0),
            url=self._url(data),
            body=markdown,
            links=self._classify(links),
            attachments=attachments,
            notes=notes,
        )

    async def page(self, page_id: str) -> Page:
        page_id = str(page_id).strip()
        if not page_id.isdigit():
            raise ConnectorError("invalid", f"{LABEL}: id trang phải là số")
        data = await self.http.json("GET", PAGE_PATH.format(id=page_id), params=PAGE_BODY_PARAMS)
        return await self._to_page(data or {})

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        if not self.in_scope(ref, scope):
            raise ConnectorError("forbidden", f"{LABEL}: tài liệu nằm ngoài phạm vi của kết nối")
        page = await self.page(await self._page_id(ref.ref))
        return DocContent(title=page.title, text=f"# {page.title}\n\n{page.body}".rstrip(), version=str(page.version), url=page.url, links=page.links, attachments=page.attachments, notes=page.notes)

    async def children(self, page_id: str) -> list[PageRef]:
        page_id = str(page_id).strip()
        if not page_id.isdigit():
            raise ConnectorError("invalid", f"{LABEL}: id trang phải là số")
        out: list[PageRef] = []
        path: str | None = CHILDREN_PATH.format(id=page_id)
        for _ in range(MAX_PAGES_OF_RESULTS):
            if not path:
                break
            data = await self.http.json("GET", path) or {}
            for item in data.get("results") or []:
                if (item.get("type") or "page") != "page":
                    continue  # a folder, whiteboard, database or embed: not a document this adapter reads
                space = await self._space_key(str(item.get("spaceId") or ""))
                if self._space_allowed(space):
                    child_id = str(item.get("id") or "")
                    out.append(PageRef(id=child_id, title=str(item.get("title") or ""), space=space, url=f"{self.base}/pages/viewpage.action?pageId={child_id}"))
            nxt = str((data.get("_links") or {}).get("next") or "")
            path = (nxt[len("/wiki") :] if nxt.startswith("/wiki/") else nxt) if nxt.startswith("/") else None
        return out

    def _cql(self, text: str, space: str | None) -> str:
        escaped = text.replace("\\", " ").replace('"', " ").strip()
        spaces = [space] if space else self.config.spaces
        cql = f'type = page AND text ~ "{escaped}"'
        if spaces:
            cql += " AND space in (" + ", ".join(f'"{s}"' for s in spaces) + ")"
        return cql

    async def search_pages(self, text: str, *, space: str | None = None, limit: int = 10) -> list[PageRef]:
        if space:
            if not re.fullmatch(r"~?[A-Za-z0-9_.-]+", space):
                raise ConnectorError("invalid", f"{LABEL}: khoá space không hợp lệ")
            self._require_space(space)
        if not text.strip():
            return []
        data = await self.http.json("GET", SEARCH_PATH, params={"cql": self._cql(text, space), "limit": limit, "expand": "space"}) or {}
        return [PageRef(id=str(r.get("id") or ""), title=str(r.get("title") or ""), space=str((r.get("space") or {}).get("key") or ""), url=self._url(r)) for r in (data.get("results") or [])[:limit]]

    async def search_docs(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]:
        return [DocHit(ref=DocRef(ref=p.id, kind=CONFLUENCE_KIND), title=p.title, snippet=f"Space {p.space}" if p.space else "", url=p.url) for p in await self.search_pages(query, limit=limit)]

    async def search(self, text: str, scope: Scope | None = None, limit: int = 10, *, space: str | None = None) -> list[Any]:
        """`search(text, space=, limit=)` → page references (Wiki); `search(query, Scope, limit)` → hits (DocSource)."""
        if isinstance(scope, Scope):
            return await self.search_docs(text, scope, limit)
        return await self.search_pages(text, space=space, limit=limit)

    # ---- writing

    async def upsert_page(self, space: str, title: str, body_md: str, *, parent_id: str | None = None) -> Page:
        """Create the page, or update the page with the same title in the space (publishing twice gives one page)."""
        if not self.config.write:
            raise ConnectorError("forbidden", f"{LABEL}: kết nối đang ở chế độ chỉ đọc, chưa được phép xuất bản trang (bật «write» cho kết nối)")
        self._require_space(space)
        if parent_id is not None and not str(parent_id).isdigit():
            raise ConnectorError("invalid", f"{LABEL}: id trang cha phải là số")
        space_id = await self._space_id(space)
        found = await self.http.json("GET", PAGES_PATH, params={"space-id": space_id, "title": title}) or {}
        body = {"representation": "storage", "value": markdown_to_storage(body_md)}
        existing = (found.get("results") or [None])[0]
        if existing:
            version = int((existing.get("version") or {}).get("number") or 1) + 1
            payload: dict[str, Any] = {"id": existing["id"], "status": "current", "title": title, "body": body, "version": {"number": version}}
            data = await self.http.json("PUT", PAGE_PATH.format(id=existing["id"]), json=payload)
        else:
            payload = {"spaceId": space_id, "status": "current", "title": title, "body": body}
            if parent_id:
                payload["parentId"] = str(parent_id)
            data = await self.http.json("POST", PAGES_PATH, json=payload)
        page = await self._to_page(data or {}, body=False)
        return page.model_copy(update={"body": body_md})
