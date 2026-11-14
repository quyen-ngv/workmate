"""Server-side Atlassian Cloud access for the AI's tools: Confluence (read, search, publish) and Bitbucket Cloud
(pull requests, comments). Credentials stay in <data>/secrets and are never shown to the AI.

- Confluence uses the Jira site credentials (same Atlassian Cloud site: <base>/wiki), REST v2 for pages and v1
  CQL search.
- Bitbucket Cloud uses an Atlassian API token (email + token, Basic auth) on https://api.bitbucket.org/2.0,
  in <data>/secrets/bitbucket.json ({"email", "token", "workspace"}).

Both are thin and pluggable: the tools call `CodeHost` / `Wiki` methods, so another provider (Bitbucket Data
Center, GitHub, GitLab) only needs another class with the same methods.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Awaitable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import httpx

from actoroffice.domain.errors import OfficeError
from actoroffice.ports.code import RepoRef
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Scope

from .jira import load_credentials

if TYPE_CHECKING:
    from actoroffice.app import Office


class AtlassianError(OfficeError):
    code = 502
    kind = ""  # the ConnectorError kind when the failure came from a connection ("forbidden", "not_found"…)


class Wiki(Protocol):
    async def search(self, text: str, *, space: str | None = None, limit: int = 10) -> list[dict[str, Any]]: ...
    async def page(self, page_id: str) -> dict[str, Any]: ...
    async def upsert_page(self, space: str, title: str, html: str, *, parent_id: str | None = None) -> dict[str, Any]: ...


class CodeHost(Protocol):
    async def pr_create(self, repo: str, source: str, destination: str, title: str, description: str) -> dict[str, Any]: ...
    async def pr_get(self, repo: str, pr_id: int) -> dict[str, Any]: ...
    async def pr_comments(self, repo: str, pr_id: int) -> list[dict[str, Any]]: ...
    async def pr_reply(self, repo: str, pr_id: int, text: str, *, parent_id: int | None = None) -> dict[str, Any]: ...


async def _call(http: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> Any:
    try:
        response = await http.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise AtlassianError(f"{type(exc).__name__} khi gọi {url}") from exc
    if response.status_code >= 400:
        raise AtlassianError(f"{method} {url} → {response.status_code}: {response.text[:300]}")
    return response.json() if response.content else None


class Confluence:
    def __init__(self, base_url: str, email: str, token: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base = base_url.rstrip("/") + "/wiki"
        self.http = httpx.AsyncClient(base_url=self.base, auth=(email, token), timeout=30, transport=transport, headers={"Accept": "application/json"})

    async def close(self) -> None:
        await self.http.aclose()

    async def search(self, text: str, *, space: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        safe = text.replace('"', " ")
        cql = f'type = page AND text ~ "{safe}"' + (f' AND space = "{space}"' if space else "")
        data = await _call(self.http, "GET", "/rest/api/content/search", params={"cql": cql, "limit": limit, "expand": "version,space"})
        return [
            {
                "id": r["id"],
                "title": r.get("title"),
                "space": (r.get("space") or {}).get("key"),
                "updated": ((r.get("version") or {}).get("when") or "")[:10],
                "url": self.base + ((r.get("_links") or {}).get("webui") or ""),
            }
            for r in (data or {}).get("results", [])
        ]

    async def page(self, page_id: str) -> dict[str, Any]:
        data = await _call(self.http, "GET", f"/api/v2/pages/{page_id}", params={"body-format": "storage"})
        html = ((data.get("body") or {}).get("storage") or {}).get("value") or ""
        return {
            "id": data["id"],
            "title": data.get("title"),
            "version": (data.get("version") or {}).get("number"),
            "updated": ((data.get("version") or {}).get("createdAt") or "")[:10],
            "text": html_to_text(html),
            "url": self.base + ((data.get("_links") or {}).get("webui") or ""),
        }

    async def upsert_page(self, space: str, title: str, html: str, *, parent_id: str | None = None) -> dict[str, Any]:
        """Create the page, or update the page with the same title in the space (idempotent publishing)."""
        space_data = await _call(self.http, "GET", "/api/v2/spaces", params={"keys": space})
        spaces = (space_data or {}).get("results") or []
        if not spaces:
            raise AtlassianError(f"Không thấy space Confluence {space}")
        space_id = spaces[0]["id"]
        found = await _call(self.http, "GET", "/api/v2/pages", params={"space-id": space_id, "title": title})
        body = {"representation": "storage", "value": html}
        existing = ((found or {}).get("results") or [None])[0]
        if existing:
            version = int((existing.get("version") or {}).get("number") or 1) + 1
            data = await _call(
                self.http, "PUT", f"/api/v2/pages/{existing['id']}", json={"id": existing["id"], "status": "current", "title": title, "body": body, "version": {"number": version}}
            )
        else:
            payload: dict[str, Any] = {"spaceId": space_id, "status": "current", "title": title, "body": body}
            if parent_id:
                payload["parentId"] = parent_id
            data = await _call(self.http, "POST", "/api/v2/pages", json=payload)
        return {"id": data["id"], "url": self.base + ((data.get("_links") or {}).get("webui") or "")}


def bitbucket_secrets(data_dir: Path) -> Path:
    return data_dir / "secrets" / "bitbucket.json"


def load_bitbucket(data_dir: Path) -> dict[str, str] | None:
    try:
        data = json.loads(bitbucket_secrets(data_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if all(data.get(k) for k in ("email", "token", "workspace")) else None


def save_bitbucket(data_dir: Path, email: str, token: str, workspace: str) -> None:
    path = bitbucket_secrets(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"email": email, "token": token, "workspace": workspace}), encoding="utf-8")
    with contextlib.suppress(OSError):
        path.chmod(0o600)


class BitbucketCloud:
    def __init__(self, email: str, token: str, workspace: str, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.workspace = workspace
        self.http = httpx.AsyncClient(
            base_url="https://api.bitbucket.org/2.0", auth=(email, token), timeout=30, transport=transport, headers={"Accept": "application/json"}
        )

    async def close(self) -> None:
        await self.http.aclose()

    def _repo(self, repo: str) -> str:
        repo = repo.strip().strip("/")
        if "/" in repo:  # "workspace/slug" or a full URL
            m = re.search(r"bitbucket\.org[/:]([^/]+)/([^/.]+)", repo)
            return f"{m.group(1)}/{m.group(2)}" if m else repo
        return f"{self.workspace}/{repo}"

    async def pr_create(self, repo: str, source: str, destination: str, title: str, description: str) -> dict[str, Any]:
        data = await _call(
            self.http,
            "POST",
            f"/repositories/{self._repo(repo)}/pullrequests",
            json={
                "title": title,
                "description": description,
                "source": {"branch": {"name": source}},
                "destination": {"branch": {"name": destination}},
                "close_source_branch": True,
            },
        )
        return {"id": data["id"], "url": ((data.get("links") or {}).get("html") or {}).get("href"), "state": data.get("state")}

    async def pr_get(self, repo: str, pr_id: int) -> dict[str, Any]:
        data = await _call(self.http, "GET", f"/repositories/{self._repo(repo)}/pullrequests/{pr_id}")
        return {
            "id": data["id"],
            "state": data.get("state"),  # OPEN | MERGED | DECLINED | SUPERSEDED
            "title": data.get("title"),
            "url": ((data.get("links") or {}).get("html") or {}).get("href"),
            "approvals": [p["user"].get("display_name") for p in data.get("participants") or [] if p.get("approved")],
        }

    async def pr_comments(self, repo: str, pr_id: int) -> list[dict[str, Any]]:
        data = await _call(self.http, "GET", f"/repositories/{self._repo(repo)}/pullrequests/{pr_id}/comments", params={"pagelen": 100})
        out = []
        for c in (data or {}).get("values", []):
            if c.get("deleted"):
                continue
            inline = c.get("inline") or {}
            out.append(
                {
                    "id": c["id"],
                    "by": (c.get("user") or {}).get("display_name"),
                    "text": ((c.get("content") or {}).get("raw") or "")[:4000],
                    "file": inline.get("path"),
                    "line": inline.get("to") or inline.get("from"),
                    "parent": (c.get("parent") or {}).get("id"),
                    "resolved": bool(c.get("resolution")),
                    "at": (c.get("created_on") or "")[:16],
                }
            )
        return out

    async def pr_reply(self, repo: str, pr_id: int, text: str, *, parent_id: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"content": {"raw": text}}
        if parent_id:
            body["parent"] = {"id": parent_id}
        data = await _call(self.http, "POST", f"/repositories/{self._repo(repo)}/pullrequests/{pr_id}/comments", json=body)
        return {"id": data["id"]}


class Atlassian:
    """Lazily built clients (None when not configured)."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self._wiki: Wiki | None = None
        self._code: CodeHost | None = None

    def wiki(self) -> Wiki | None:
        if self._wiki is None:
            creds = load_credentials(self.data_dir)
            if creds:
                self._wiki = Confluence(creds["base_url"], creds["email"], creds["token"])
        return self._wiki

    def code_host(self) -> CodeHost | None:
        if self._code is None:
            creds = load_bitbucket(self.data_dir)
            if creds:
                self._code = BitbucketCloud(creds["email"], creds["token"], creds["workspace"])
        return self._code

    def use(self, *, wiki: Wiki | None = None, code_host: CodeHost | None = None) -> None:
        """Tests and other providers plug their own clients in."""
        if wiki is not None:
            self._wiki = wiki
        if code_host is not None:
            self._code = code_host

    async def close(self) -> None:
        for client in (self._wiki, self._code):
            close = getattr(client, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):
                    await close()


# ---- the old configuration next to connections -----------------------------------------------------------------
# Until it is imported (`ConnectionService.import_legacy`), the configuration from before connections keeps working:
# it counts as the capability it gives, and it is what the tools fall back to when no connection serves a call.


def legacy_capability(office: Office, capability: Capability) -> bool:
    """Whether the old configuration gives a capability: `secrets/jira.json` (Jira and, on the same site, Confluence),
    `secrets/bitbucket.json`, or a client plugged in with `Atlassian.use` / `JiraMirror(client=…)`."""
    if capability == Capability.TRACKER:
        return office.jira.ready()
    if capability == Capability.WIKI:
        return office.atlassian.wiki() is not None
    if capability == Capability.CODE_PR:
        return office.atlassian.code_host() is not None
    return False


def repo_slug(repo: str) -> str:
    """ "https://host/acme/billing.git", "git@host:acme/billing.git" or "acme/billing" → "acme/billing"."""
    repo = repo.strip().rstrip("/").removesuffix(".git")
    m = re.match(r"^(?:[a-z+]+://[^/]+/|[\w.-]+@[\w.-]+:)(.+)$", repo)
    return m.group(1) if m else repo


def parse_pr(url: str, adapters: Iterable[Any] = ()) -> tuple[str, int] | None:
    """("workspace/repo", number) of a pull request URL: a URL one of the given code-host adapters owns (GitHub,
    Bitbucket… by their `parse_pr_url`), else a bitbucket.org URL as the old client takes it."""
    for adapter in adapters:
        found = adapter.parse_pr_url(url or "")
        if found is not None:
            return found[0].slug, found[1]
    m = re.search(r"bitbucket\.org/([^/]+)/([^/]+)/pull-requests/(\d+)", url or "")
    return (f"{m.group(1)}/{m.group(2)}", int(m.group(3))) if m else None


class ConnectedCodeHost:
    """A code-host connection (`ports.code.CodeHost`) behind the methods and shapes of the old client, so the tools and
    the pull request watcher treat both the same. A failure is an AtlassianError carrying the connector's `kind`.
    What the port does not tell stays unknown: `approvals` is None, no comment is `resolved`, a reply has no id."""

    def __init__(self, spec: ConnectionSpec, adapter: Any) -> None:
        self.spec = spec
        self.adapter = adapter
        self.label = spec.use  # who says "merged" / "new review" in the ticket

    @staticmethod
    async def _do(call: Awaitable[Any]) -> Any:
        try:
            return await call
        except ConnectorError as exc:
            error = AtlassianError(str(exc))
            error.kind = exc.kind
            raise error from exc

    def parse_pr(self, url: str) -> tuple[str, int] | None:
        found = self.adapter.parse_pr_url(url or "")
        return (found[0].slug, found[1]) if found is not None else None

    async def pr_create(self, repo: str, source: str, destination: str, title: str, description: str) -> dict[str, Any]:
        pr = await self._do(self.adapter.pr_create(RepoRef(slug=repo_slug(repo)), source, destination, title, description))
        return {"id": pr.id, "url": pr.url, "state": pr.state.upper()}

    async def pr_get(self, repo: str, pr_id: int) -> dict[str, Any]:
        pr = await self._do(self.adapter.pr_get(RepoRef(slug=repo_slug(repo)), pr_id))
        return {"id": pr.id, "state": pr.state.upper(), "title": pr.title, "url": pr.url, "approvals": None}

    async def pr_comments(self, repo: str, pr_id: int) -> list[dict[str, Any]]:
        comments = await self._do(self.adapter.pr_comments(RepoRef(slug=repo_slug(repo)), pr_id))
        return [
            {"id": c.id, "by": c.author, "text": c.body[:4000], "file": c.path, "line": c.line, "parent": c.parent_id, "resolved": False, "at": c.created[:16]}
            for c in comments
        ]

    async def pr_reply(self, repo: str, pr_id: int, text: str, *, parent_id: int | None = None) -> dict[str, Any]:
        await self._do(self.adapter.pr_reply(RepoRef(slug=repo_slug(repo)), pr_id, text, parent_id=parent_id))
        return {}


async def code_hosts(office: Office, scope: Scope | None, *, write: bool = False) -> list[Any]:
    """The code hosts a ticket can use, best first: its connections (project, team, office; only those with `write` on
    when the call writes), then the client of the old configuration."""
    hosts: list[Any] = []
    for spec in await office.connections.candidates(Capability.CODE_PR, scope):
        if write and not spec.write:
            continue
        try:
            hosts.append(ConnectedCodeHost(spec, await office.connections.adapter(spec.id)))
        except ConnectorError:
            continue  # no secret yet: as good as not connected
    old = office.atlassian.code_host()
    if old is not None:
        hosts.append(old)
    return hosts


async def pr_host(office: Office, scope: Scope | None, url: str, *, write: bool = False) -> tuple[Any, str, int] | None:
    """Who serves a pull request URL, as (client, "workspace/repo", number): the nearest connection whose adapter says
    the URL is one of its pull requests, else the old Bitbucket client for a bitbucket.org URL. None: nobody."""
    for host in await code_hosts(office, scope, write=write):
        parsed = host.parse_pr(url) if isinstance(host, ConnectedCodeHost) else parse_pr(url)
        if parsed is not None:
            return host, parsed[0], parsed[1]
    return None


def html_to_text(html: str) -> str:
    """Confluence storage XHTML → readable text (headings, list items and table cells keep their structure)."""
    text = re.sub(r"(?is)<(script|style).*?</\1>", "", html)
    text = re.sub(r"(?i)<h([1-6])[^>]*>", lambda m: "\n" + "#" * int(m.group(1)) + " ", text)
    text = re.sub(r"(?i)</h[1-6]>|</p>|<br\s*/?>|</tr>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = re.sub(r"(?i)</t[dh]>", " | ", text)
    text = re.sub(r"<[^>]+>", "", text)
    import html as _html

    text = _html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def markdown_to_storage(md: str) -> str:
    """Small markdown → Confluence storage converter: headings, lists, code fences, tables, bold, inline code."""
    import html as _html

    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    in_list = in_code = in_table = False
    code: list[str] = []

    def inline(text: str) -> str:
        t = _html.escape(text)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", t)

    for line in lines:
        if line.strip().startswith("```"):
            if in_code:
                out.append('<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[' + "\n".join(code) + "]]></ac:plain-text-body></ac:structured-macro>")
                code, in_code = [], False
            else:
                in_code = True
            continue
        if in_code:
            code.append(line)
            continue
        if in_list and not re.match(r"^\s*[-*] ", line):
            out.append("</ul>")
            in_list = False
        if in_table and not line.strip().startswith("|"):
            out.append("</tbody></table>")
            in_table = False
        if m := re.match(r"^(#{1,6})\s+(.*)$", line):
            out.append(f"<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>")
        elif m := re.match(r"^\s*[-*]\s+(.*)$", line):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{inline(m.group(1))}</li>")
        elif line.strip().startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
                continue
            if not in_table:
                out.append("<table><tbody>")
                in_table = True
            out.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in cells) + "</tr>")
        elif line.strip():
            out.append(f"<p>{inline(line)}</p>")
    if in_code:
        out.append('<ac:structured-macro ac:name="code"><ac:plain-text-body><![CDATA[' + "\n".join(code) + "]]></ac:plain-text-body></ac:structured-macro>")
    if in_list:
        out.append("</ul>")
    if in_table:
        out.append("</tbody></table>")
    return "\n".join(out)
