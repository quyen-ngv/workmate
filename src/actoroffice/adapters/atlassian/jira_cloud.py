"""Jira Cloud: issues as documents (`DocSource`) and as a tracker (`Tracker`). REST API v3 with ADF bodies.

`Tracker.search(jql, limit)` and `DocSource.search(query, scope, limit)` share one name in the ports, so `search` looks
at its second argument: a `Scope` means a text search for documents; anything else means JQL. `search_issues` and
`search_docs` are the two explicit forms.
"""

from __future__ import annotations

import re
from typing import Any, ClassVar
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field, field_validator

from actoroffice.adapters._http import Http, host_of, now_iso
from actoroffice.adapters.atlassian import CONFLUENCE_KIND, JIRA_KIND
from actoroffice.adapters.atlassian.adf import adf_to_markdown, text_to_adf
from actoroffice.adapters.atlassian.auth import CLOUD_ID_HELP, SCOPED_TOKEN_HINT, SECRET_FIELDS, build_client, check_cloud_id, has_credentials, missing_secret_error, normalize_site
from actoroffice.ports.connection import Capability, ConnectorError, Health, Scope
from actoroffice.ports.docs import Attachment, DocContent, DocHit, DocRef, Issue

LABEL = "Jira"
PROJECT_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*")
ISSUE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]*-\d+")
_KEY_IN_TEXT_RE = re.compile(r"(?<![A-Za-z0-9_/-])([A-Z][A-Z0-9_]*)-\d+(?![A-Za-z0-9_])")
_URL_RE = re.compile(r"https?://[^\s<>\"'\]\)]+")

# ---- Checked on 2026-11-14 against the Jira Cloud platform REST API v3 reference (developer.atlassian.com/cloud/jira/platform/rest/v3)
# and its OpenAPI document (spec V-1). Classic scopes, which Atlassian recommends where they exist; they matter for a token
# with scopes (see auth.py) and for OAuth. A token without scopes can do whatever its user can.
REQUIRED_SCOPES: dict[Capability, list[str]] = {
    Capability.DOCS: ["read:jira-work", "read:jira-user"],  # read:jira-user is for /myself, which `test()` calls
    Capability.TRACKER: ["read:jira-work", "read:jira-user", "write:jira-work"],  # write:jira-work only when the connection may comment
}
API = "/rest/api/3"
# "Search for issues using JQL enhanced search": paged by `nextPageToken`, returns only ids unless `fields` is given, and
# refuses an unbounded JQL (one with no restriction, such as a bare ORDER BY). GET/POST /search are deprecated.
SEARCH_PATH = f"{API}/search/jql"
COMMENT_ORDER = "-created"  # `orderBy` value that returns the newest comment first
CHILDREN_JQL = "parent = {key}"  # issues under an epic, and sub-tasks
IDENTITY_PATH = f"{API}/myself"

BASE_FIELDS = ["summary", "description", "status", "issuetype", "labels", "updated", "parent", "issuelinks", "subtasks", "attachment"]
SEARCH_FIELDS = ["summary", "description", "status", "issuetype", "labels", "updated", "parent"]
FIELD_TITLES = {"acceptance": "Tiêu chí chấp nhận"}
SEARCH_PAGE = 50


class JiraCloudConfig(BaseModel):
    site: str = Field(description="Địa chỉ site, ví dụ https://acme.atlassian.net")
    cloud_id: str = Field(default="", description=CLOUD_ID_HELP)
    projects: list[str] = Field(default_factory=list, description="Khoá dự án được phép đọc (PAY, CORE). Để trống: mọi dự án token đọc được")
    fields: dict[str, str] = Field(default_factory=dict, description="Trường tuỳ biến cần lấy, ví dụ acceptance → customfield_10042")
    poll_s: int = Field(default=120, ge=0, description="Chu kỳ hỏi thay đổi (giây); 0 là tắt")
    max_comments: int = Field(default=20, ge=0, description="Số bình luận mới nhất đưa vào tài liệu")
    write: bool = Field(default=False, description="Cho phép bình luận lên issue")

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

    @field_validator("projects")
    @classmethod
    def _projects(cls, value: list[str]) -> list[str]:
        keys = [v.strip().upper() for v in value if v.strip()]
        bad = [k for k in keys if not PROJECT_KEY_RE.fullmatch(k)]
        if bad:
            raise ValueError(f"khoá dự án không hợp lệ: {', '.join(bad)}")
        return keys

    @field_validator("fields")
    @classmethod
    def _fields(cls, value: dict[str, str]) -> dict[str, str]:
        bad = [v for v in value.values() if not re.fullmatch(r"[A-Za-z0-9_]+", v)]
        if bad:
            raise ValueError(f"mã trường không hợp lệ: {', '.join(bad)}")
        return value


def _field_text(value: Any) -> str:
    """A custom field as text: ADF, a string, a number, an option or a list of them."""
    if value is None:
        return ""
    if isinstance(value, dict):
        if value.get("type") == "doc":
            return adf_to_markdown(value)
        return str(value.get("value") or value.get("name") or value.get("displayName") or "")
    if isinstance(value, list):
        return "\n".join(f"- {t}" for t in (_field_text(v) for v in value) if t)
    return str(value).strip()


class _Extra(BaseModel):
    relations: list[tuple[str, str, str]] = Field(default_factory=list)  # (relation, key, title)
    remote: list[tuple[str, str]] = Field(default_factory=list)  # (title, url)
    total_comments: int = 0


class JiraCloud:
    name: ClassVar[str] = JIRA_KIND
    title: ClassVar[str] = "Jira Cloud"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS, Capability.TRACKER})
    config_schema: ClassVar[type[BaseModel]] = JiraCloudConfig
    secret_fields: ClassVar[tuple[str, ...]] = SECRET_FIELDS
    required_scopes: ClassVar[dict[Capability, list[str]]] = REQUIRED_SCOPES

    def __init__(self, config: JiraCloudConfig, secret: dict[str, str], *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.config = config
        self.site = config.site
        self.host = host_of(config.site)
        client = build_client(config.site, secret, product="jira", cloud_id=config.cloud_id, transport=transport)
        self.http = Http(client, label=LABEL, blocked=None if has_credentials(secret) else missing_secret_error(LABEL))

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
        identity = str((me or {}).get("emailAddress") or (me or {}).get("displayName") or "") or None
        return Health(state="ok", checked_at=now_iso(), detail="Đã kết nối Jira. API token (Basic) không cho biết quyền, nên chưa đối chiếu được quyền thiếu.", identity=identity)

    # ---- references

    def browse_url(self, key: str) -> str:
        return f"{self.site}/browse/{key}"

    def _key_of_url(self, url: str) -> str | None:
        """Issue key of a browse URL of this site; None for anything else."""
        if host_of(url) != self.host:
            return None
        m = re.match(r"^/browse/([A-Z][A-Z0-9_]*-\d+)/?$", urlsplit(url).path)
        return m.group(1) if m else None

    def _key(self, ref: str) -> str:
        ref = ref.strip()
        if ISSUE_KEY_RE.fullmatch(ref):
            return ref
        key = self._key_of_url(ref) if "://" in ref else None
        if key is None:
            raise ConnectorError("invalid", f"{LABEL}: «{ref[:80]}» không phải khoá issue hay địa chỉ issue của site đã kết nối")
        return key

    def _allowed(self, key: str) -> bool:
        return not self.config.projects or key.split("-", 1)[0] in self.config.projects

    def _require_scope(self, key: str) -> None:
        if not self._allowed(key):
            raise ConnectorError("forbidden", f"{LABEL}: {key} nằm ngoài các dự án được phép của kết nối ({', '.join(self.config.projects)})")

    def recognize(self, text: str) -> list[DocRef]:
        """Browse URLs of this site, and bare keys of the configured projects (bare keys need `projects`: "UTF-8" is not an issue)."""
        found: list[str] = []
        for m in _URL_RE.finditer(text):
            key = self._key_of_url(m.group(0).rstrip(".,;:!?"))
            if key and key not in found:
                found.append(key)
        for m in _KEY_IN_TEXT_RE.finditer(text):
            if m.group(1) in self.config.projects and m.group(0) not in found:
                found.append(m.group(0))
        return [DocRef(ref=key, kind=JIRA_KIND) for key in found]

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        try:
            return self._allowed(self._key(ref.ref))
        except ConnectorError:
            return False

    # ---- reading

    def _site_link(self, url: str) -> DocRef | None:
        """A URL of this Atlassian site as a document reference: a wiki page or an issue. Other hosts: None."""
        if host_of(url) != self.host:
            return None
        key = self._key_of_url(url)
        if key:
            return DocRef(ref=key, kind=JIRA_KIND)
        return DocRef(ref=url, kind=CONFLUENCE_KIND) if urlsplit(url).path.startswith("/wiki/") else None

    def _issue(self, data: dict[str, Any]) -> tuple[Issue, _Extra]:
        fields = data.get("fields") or {}
        key = str(data.get("key") or "")
        extra = _Extra()
        links: list[DocRef] = []

        def add(link: DocRef | None) -> None:
            if link is not None and link not in links and link.ref != key:
                links.append(link)

        for item in fields.get("issuelinks") or []:
            kind = item.get("type") or {}
            for side, relation in (("outwardIssue", kind.get("outward")), ("inwardIssue", kind.get("inward"))):
                other = item.get(side)
                if other and other.get("key"):
                    extra.relations.append((str(relation or kind.get("name") or "liên kết"), str(other["key"]), str((other.get("fields") or {}).get("summary") or "")))
                    add(DocRef(ref=str(other["key"]), kind=JIRA_KIND))
        for sub in fields.get("subtasks") or []:
            if sub.get("key"):
                extra.relations.append(("việc con", str(sub["key"]), str((sub.get("fields") or {}).get("summary") or "")))
                add(DocRef(ref=str(sub["key"]), kind=JIRA_KIND))
        description = adf_to_markdown(fields.get("description"))
        for m in _URL_RE.finditer(description):
            add(self._site_link(m.group(0).rstrip(".,;:!?")))
        issue = Issue(
            key=key,
            title=str(fields.get("summary") or key),
            type=str((fields.get("issuetype") or {}).get("name") or ""),
            status=str((fields.get("status") or {}).get("name") or ""),
            description=description,
            updated=str(fields.get("updated") or ""),
            url=self.browse_url(key),
            parent=(fields.get("parent") or {}).get("key"),
            labels=[str(label) for label in fields.get("labels") or []],
            links=links,
            attachments=[
                Attachment(name=str(a.get("filename") or ""), media_type=str(a.get("mimeType") or ""), size=int(a.get("size") or 0), url=str(a.get("content") or ""))
                for a in fields.get("attachment") or []
            ],
            fields={name: _field_text(fields.get(field_id)) for name, field_id in self.config.fields.items()},
        )
        return issue, extra

    async def _load(self, key: str) -> tuple[Issue, _Extra]:
        key = self._key(key)
        self._require_scope(key)
        wanted = BASE_FIELDS + list(self.config.fields.values())
        data = await self.http.json("GET", f"{API}/issue/{key}", params={"fields": ",".join(wanted)})
        issue, extra = self._issue(data or {})
        if self.config.max_comments:
            page = await self.http.json("GET", f"{API}/issue/{key}/comment", params={"orderBy": COMMENT_ORDER, "maxResults": self.config.max_comments}) or {}
            comments = page.get("comments") or []
            extra.total_comments = int(page.get("total") or len(comments))
            issue.comments = [
                {"author": str((c.get("author") or {}).get("displayName") or ""), "created": str(c.get("created") or ""), "body": adf_to_markdown(c.get("body"))}
                for c in comments[: self.config.max_comments]
            ]
        for item in await self._remote_links(key):
            target = item.get("object") or {}
            url = str(target.get("url") or "")
            if not url:
                continue
            extra.remote.append((str(target.get("title") or url), url))
            link = self._site_link(url)
            if link is not None and link not in issue.links:
                issue.links.append(link)
        return issue, extra

    async def _remote_links(self, key: str) -> list[dict[str, Any]]:
        """Remote links of an issue. Jira answers 403 when issue linking is switched off for the site: that is "none"."""
        try:
            return await self.http.json("GET", f"{API}/issue/{key}/remotelink") or []
        except ConnectorError as exc:
            if exc.kind != "forbidden":
                raise
            return []

    async def issue(self, key: str) -> Issue:
        return (await self._load(key))[0]

    async def children(self, key: str) -> list[Issue]:
        key = self._key(key)
        self._require_scope(key)
        return await self.search_issues(CHILDREN_JQL.format(key=key))

    def _scoped_jql(self, jql: str) -> str:
        """The JQL limited to the allowed projects (the caller's ORDER BY is kept)."""
        m = re.search(r"\border\s+by\b", jql, re.IGNORECASE)
        body, order = (jql[: m.start()].strip(), " " + jql[m.start() :].strip()) if m else (jql.strip(), "")
        if self.config.projects:
            clause = "project in (" + ", ".join(f'"{p}"' for p in self.config.projects) + ")"
            body = f"{clause} AND ({body})" if body else clause
        return body + order

    async def search_issues(self, jql: str, limit: int = 50) -> list[Issue]:
        issues: list[Issue] = []
        token: str | None = None
        scoped = self._scoped_jql(jql)
        while len(issues) < limit:
            body: dict[str, Any] = {"jql": scoped, "fields": SEARCH_FIELDS, "maxResults": min(SEARCH_PAGE, limit - len(issues))}
            if token:
                body["nextPageToken"] = token
            data = await self.http.json("POST", SEARCH_PATH, json=body) or {}
            issues += [self._issue(item)[0] for item in data.get("issues") or []]
            token = data.get("nextPageToken")  # null on the last page
            if not token or data.get("isLast"):
                break
        return issues[:limit]

    async def search_docs(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]:
        escaped = query.replace("\\", " ").replace('"', " ").strip()
        if not escaped:
            return []
        issues = await self.search_issues(f'text ~ "{escaped}" ORDER BY updated DESC', limit)
        return [DocHit(ref=DocRef(ref=i.key, kind=JIRA_KIND), title=f"{i.key}: {i.title}", snippet=i.description[:240], url=i.url) for i in issues]

    async def search(self, query: str, scope: Scope | int | None = None, limit: int | None = None) -> list[Any]:
        """`search(jql, limit)` → issues (Tracker); `search(text, Scope, limit)` → hits (DocSource)."""
        if isinstance(scope, Scope):
            return await self.search_docs(query, scope, limit or 10)
        return await self.search_issues(query, scope if isinstance(scope, int) else (limit or 50))

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        issue, extra = await self._load(ref.ref)
        children = await self.children(issue.key)
        links = list(issue.links)
        for child in children:
            link = DocRef(ref=child.key, kind=JIRA_KIND)
            if link not in links:
                links.append(link)
        return DocContent(
            title=f"{issue.key}: {issue.title}",
            text=self._markdown(issue, extra, children),
            version=issue.updated,
            url=issue.url,
            links=links,
            attachments=issue.attachments,
            truncated=extra.total_comments > len(issue.comments),
        )

    def _markdown(self, issue: Issue, extra: _Extra, children: list[Issue]) -> str:
        facts = [f"Loại: {issue.type or '?'}", f"Trạng thái: {issue.status or '?'}"]
        if issue.parent:
            facts.append(f"Thuộc: {issue.parent}")
        if issue.labels:
            facts.append("Nhãn: " + ", ".join(issue.labels))
        parts = [f"# {issue.key}: {issue.title}", " · ".join(facts), "## Mô tả", issue.description or "(không có mô tả)"]
        for name, value in issue.fields.items():
            if value:
                parts += [f"## {FIELD_TITLES.get(name, name)}", value]
        if children:
            parts += ["## Issue con", "\n".join(f"- {c.key}: {c.title} ({c.status})" for c in children)]
        if extra.relations or extra.remote:
            lines = [f"- {relation} {key}: {title}".rstrip(": ") for relation, key, title in extra.relations]
            lines += [f"- [{title}]({url})" for title, url in extra.remote]
            parts += ["## Liên kết", "\n".join(lines)]
        if issue.attachments:
            parts += ["## Tệp đính kèm", "\n".join(f"- {a.name} ({a.media_type or '?'}, {a.size} byte)" for a in issue.attachments)]
        if issue.comments:
            shown = f"{len(issue.comments)} mới nhất trong {extra.total_comments}" if extra.total_comments > len(issue.comments) else str(len(issue.comments))
            parts.append(f"## Bình luận ({shown})")
            parts += [f"### {c['author'] or '?'} · {c['created']}\n\n{c['body']}" for c in issue.comments]
        return "\n\n".join(parts)

    # ---- writing

    async def comment(self, key: str, text: str) -> None:
        if not self.config.write:
            raise ConnectorError("forbidden", f"{LABEL}: kết nối đang ở chế độ chỉ đọc, chưa được phép bình luận (bật «write» cho kết nối)")
        key = self._key(key)
        self._require_scope(key)
        await self.http.json("POST", f"{API}/issue/{key}/comment", json={"body": text_to_adf(text)})
