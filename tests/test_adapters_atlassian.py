"""Adapters for Jira Cloud and Confluence Cloud, the shared HTTP helper, and the two format converters.
No network: every adapter gets an `httpx.MockTransport`."""

from __future__ import annotations

import ast
import asyncio
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from actoroffice.adapters._http import Http
from actoroffice.adapters.atlassian.adf import adf_to_markdown, text_to_adf
from actoroffice.adapters.atlassian.confluence_cloud import ConfluenceCloud, ConfluenceCloudConfig
from actoroffice.adapters.atlassian.jira_cloud import JiraCloud, JiraCloudConfig
from actoroffice.adapters.atlassian.storage_html import MERGED_CELLS_NOTE, include_note, markdown_to_storage, storage_to_markdown
from actoroffice.ports.connection import Capability, ConnectorError, Scope
from actoroffice.ports.docs import DocRef

SITE = "https://acme.atlassian.net"
CLOUD_ID = "11223344-a1b2-3b33-c444-def123456789"
SECRET = {"email": "bot@acme.test", "token": "tok-123"}
SCOPE = Scope(team_id="pay")
J = "jira_cloud"
C = "confluence_cloud"


class Fake:
    """Routes by (method, path). A value is a callable(request) → Response, a Response, or JSON for a 200."""

    def __init__(self, routes: dict[tuple[str, str], Any] | None = None) -> None:
        self.routes = routes or {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        value = self.routes.get((request.method, request.url.path))
        if value is None:
            return httpx.Response(404, json={"message": "no route"})
        if callable(value):
            value = value(request)
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if method is None or r.method == method]

    def body(self, method: str, path: str) -> Any:
        return next(json.loads(r.content) for r in self.requests if r.method == method and r.url.path == path)


def para(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "paragraph", "content": list(content)}


def text(value: str, *marks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "text", "text": value, **({"marks": list(marks)} if marks else {})}


def doc(*content: dict[str, Any]) -> dict[str, Any]:
    return {"type": "doc", "version": 1, "content": list(content)}


# ---- the shared HTTP helper --------------------------------------------------------------------------------


def make_http(handler: Callable[..., Any], **kwargs: Any) -> tuple[Http, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    client = httpx.AsyncClient(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return Http(client, label="Thử", sleep=sleep, jitter=lambda: 0.5, **kwargs), sleeps


@pytest.mark.parametrize(
    ("status", "kind", "calls"), [(401, "auth", 1), (403, "forbidden", 1), (404, "not_found", 1), (400, "invalid", 1), (409, "invalid", 1), (500, "network", 4), (503, "network", 4)]
)
async def test_http_maps_status_to_kind(status: int, kind: str, calls: int) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, text="secret-body tok-123")

    http, _ = make_http(handler)
    with pytest.raises(ConnectorError) as err:
        await http.json("GET", "/thing/1")
    assert err.value.kind == kind
    assert len(seen) == calls
    assert "secret-body" not in str(err.value) and "tok-123" not in str(err.value)
    assert f"{status} GET /thing/1" in str(err.value)


async def test_http_retries_429_honouring_retry_after_then_succeeds() -> None:
    answers = [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(429), httpx.Response(200, json={"ok": True})]
    http, sleeps = make_http(lambda request: answers.pop(0))
    assert await http.json("POST", "/x") == {"ok": True}
    assert sleeps == [7.0, 1.0]  # the header first, then 0.5 * 2**1 * (0.5 + 0.5)


async def test_http_gives_up_after_four_attempts_with_retry_after_on_the_error() -> None:
    calls = []
    http, sleeps = make_http(lambda request: calls.append(1) or httpx.Response(429, headers={"Retry-After": "2"}))
    with pytest.raises(ConnectorError) as err:
        await http.json("GET", "/x")
    assert (err.value.kind, err.value.retry_after, len(calls), sleeps) == ("rate_limited", 2.0, 4, [2.0, 2.0, 2.0])


async def test_http_does_not_call_again_when_the_named_wait_is_longer_than_one_sleep() -> None:
    # Atlassian: "Retry-After: 1847" on a used-up quota. No request is spent before that time; the error says how long.
    calls = []
    http, sleeps = make_http(lambda request: calls.append(1) or httpx.Response(429, headers={"Retry-After": "1847", "X-RateLimit-Reset": "2026-11-14T15:00:00Z"}))
    with pytest.raises(ConnectorError) as err:
        await http.json("GET", "/x")
    assert (err.value.kind, err.value.retry_after, len(calls), sleeps) == ("rate_limited", 1847.0, 1, [])


async def test_http_waits_until_the_reset_time_github_names() -> None:
    # GitHub: 403 or 429 with x-ratelimit-remaining 0 and x-ratelimit-reset in UTC epoch seconds, often without Retry-After.
    now = 1_800_000_000.0
    answers = [httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now) + 20)}), httpx.Response(200, json={"ok": True})]
    http, sleeps = make_http(lambda request: answers.pop(0), clock=lambda: now)
    assert await http.json("GET", "/x") == {"ok": True}
    assert sleeps == [20.0]

    calls = []
    far = httpx.Response(429, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(now) + 1800)})
    http, sleeps = make_http(lambda request: calls.append(1) or far, clock=lambda: now)
    with pytest.raises(ConnectorError) as err:
        await http.json("GET", "/x")
    assert (err.value.kind, err.value.retry_after, len(calls), sleeps) == ("rate_limited", 1800.0, 1, [])


async def test_http_does_not_repeat_a_post_on_5xx_and_maps_transport_errors() -> None:
    calls = []
    http, sleeps = make_http(lambda request: calls.append(1) or httpx.Response(502))
    with pytest.raises(ConnectorError) as err:
        await http.json("POST", "/x", json={})
    assert (err.value.kind, len(calls), sleeps) == ("network", 1, [])

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host tok-123")

    http, sleeps = make_http(broken)
    with pytest.raises(ConnectorError) as err:
        await http.json("GET", "/x")
    assert err.value.kind == "network" and len(sleeps) == 3 and "tok-123" not in str(err.value)


async def test_http_refuses_another_host_without_a_request() -> None:
    seen = []
    http, _ = make_http(lambda request: seen.append(request) or httpx.Response(200, json={}))
    with pytest.raises(ConnectorError) as err:
        await http.json("GET", "https://evil.example/steal")
    assert err.value.kind == "invalid" and seen == []
    assert await http.json("GET", "https://api.test/ok") == {}


async def test_http_caps_concurrent_requests() -> None:
    running = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        for _ in range(5):
            await asyncio.sleep(0)
        running -= 1
        return httpx.Response(200, json={})

    http, _ = make_http(handler, max_concurrent=2)
    await asyncio.gather(*(http.json("GET", f"/{i}") for i in range(8)))
    assert peak == 2


# ---- ADF ---------------------------------------------------------------------------------------------------

ADF_CASES: list[tuple[str, Any, str]] = [
    ("none", None, ""),
    ("string", "  đã là chữ  ", "đã là chữ"),
    (
        "heading, marks, link",
        doc(
            {"type": "heading", "attrs": {"level": 2}, "content": [text("Mục tiêu")]},
            para(text("Hoàn "), text("tiền", {"type": "strong"}), text(" qua "), text("ví", {"type": "link", "attrs": {"href": "https://x.test/v"}})),
        ),
        "## Mục tiêu\n\nHoàn **tiền** qua [ví](https://x.test/v)",
    ),
    (
        "nested list",
        doc(
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [para(text("A")), {"type": "bulletList", "content": [{"type": "listItem", "content": [para(text("B"))]}]}]},
                    {"type": "listItem", "content": [para(text("C"))]},
                ],
            }
        ),
        "- A\n  - B\n- C",
    ),
    (
        "ordered list",
        doc({"type": "orderedList", "content": [{"type": "listItem", "content": [para(text("một"))]}, {"type": "listItem", "content": [para(text("hai"))]}]}),
        "1. một\n2. hai",
    ),
    ("code block", doc({"type": "codeBlock", "attrs": {"language": "java"}, "content": [text("int a;\nint b;")]}), "```java\nint a;\nint b;\n```"),
    (
        "table",
        doc(
            {
                "type": "table",
                "content": [
                    {"type": "tableRow", "content": [{"type": "tableHeader", "content": [para(text("Mã"))]}, {"type": "tableHeader", "content": [para(text("Ý nghĩa"))]}]},
                    {"type": "tableRow", "content": [{"type": "tableCell", "content": [para(text("E1"))]}, {"type": "tableCell", "content": [para(text("Hết | mức"))]}]},
                ],
            }
        ),
        "| Mã | Ý nghĩa |\n| --- | --- |\n| E1 | Hết \\| mức |",
    ),
    ("mention and inline code", doc(para({"type": "mention", "attrs": {"text": "@Lan"}}, text(" xem "), text("limit()", {"type": "code"}))), "@Lan xem `limit()`"),
    ("panel", doc({"type": "panel", "attrs": {"panelType": "warning"}, "content": [para(text("Chú ý"))]}), "> **Cảnh báo:**\n>\n> Chú ý"),
    ("inline card and hard break", doc(para(text("Xem"), {"type": "hardBreak"}, {"type": "inlineCard", "attrs": {"url": "https://x.test/p"}})), "Xem\nhttps://x.test/p"),
]


@pytest.mark.parametrize(("node", "expected"), [c[1:] for c in ADF_CASES], ids=[c[0] for c in ADF_CASES])
def test_adf_to_markdown(node: Any, expected: str) -> None:
    assert adf_to_markdown(node) == expected


def test_text_to_adf_keeps_paragraphs_lines_and_code() -> None:
    out = text_to_adf("dòng 1\ndòng 2\n\n```\ncode\n```")
    assert [b["type"] for b in out["content"]] == ["paragraph", "codeBlock"]
    assert [c["type"] for c in out["content"][0]["content"]] == ["text", "hardBreak", "text"]
    assert out["content"][1]["content"][0]["text"] == "code"
    assert text_to_adf("")["content"] == [{"type": "paragraph", "content": []}]


# ---- storage format ----------------------------------------------------------------------------------------


def macro(name: str, inner: str) -> str:
    return f'<ac:structured-macro ac:name="{name}">{inner}</ac:structured-macro>'


def param(name: str, value: str) -> str:
    return f'<ac:parameter ac:name="{name}">{value}</ac:parameter>'


STORAGE_CASES: list[tuple[str, str, str, list[DocRef], list[str], list[str]]] = [
    ("heading and inline marks", "<h1>Tiêu đề</h1><p>Một <strong>đậm</strong> và <em>nghiêng</em>, <code>mã</code>.</p>", "# Tiêu đề\n\nMột **đậm** và *nghiêng*, `mã`.", [], [], []),
    ("nested list", "<ul><li>A<ul><li>B</li><li>C</li></ul></li><li>D</li></ul><ol><li>x</li><li>y</li></ol>", "- A\n  - B\n  - C\n- D\n\n1. x\n2. y", [], [], []),
    (
        "table",
        "<table><tbody><tr><th>Mã</th><th>Nghĩa</th></tr><tr><td>E1</td><td><p>Hết</p><p>hạn</p></td></tr></tbody></table>",
        "| Mã | Nghĩa |\n| --- | --- |\n| E1 | Hết<br>hạn |",
        [],
        [],
        [],
    ),
    ("merged cells", '<table><tr><td colspan="2">Gộp</td></tr><tr><td>a</td><td>b</td></tr></table>', "| Gộp |  |\n| --- | --- |\n| a | b |", [], [MERGED_CELLS_NOTE], []),
    (
        "code macro",
        macro("code", param("language", "java") + '<ac:plain-text-body><![CDATA[if (a < b) {\n  return "x";\n}]]></ac:plain-text-body>'),
        '```java\nif (a < b) {\n  return "x";\n}\n```',
        [],
        [],
        [],
    ),
    (
        "warning panel",
        macro("warning", param("title", "Chú ý") + "<ac:rich-text-body><p>Không gọi hai lần.</p></ac:rich-text-body>"),
        "> **Cảnh báo: Chú ý**\n>\n> Không gọi hai lần.",
        [],
        [],
        [],
    ),
    (
        "page links",
        '<p>Xem <ac:link><ri:page ri:content-title="Thiết kế hạn mức" ri:space-key="ARCH"/></ac:link> và '
        '<ac:link ac:anchor="s2"><ri:page ri:content-title="Trang cùng space"/><ac:plain-text-link-body><![CDATA[mục 2]]></ac:plain-text-link-body></ac:link>.</p>',
        "Xem [[Thiết kế hạn mức]] và mục 2 ([[Trang cùng space]]).",
        [DocRef(ref="ARCH:Thiết kế hạn mức", kind=C), DocRef(ref="PAY:Trang cùng space", locator="#s2", kind=C)],
        [],
        [],
    ),
    ("jira issue macro", "<p>Theo " + macro("jira", param("key", "PAY-120")) + " nhé</p>", "Theo PAY-120 nhé", [DocRef(ref="PAY-120", kind=J)], [], []),
    (
        "draw.io diagram",
        "<p>" + macro("drawio", param("diagramName", "luong-hoan-tien")) + "</p>",
        "*[Sơ đồ draw.io: luong-hoan-tien — AI không đọc được]*",
        [],
        ["Sơ đồ draw.io «luong-hoan-tien»: AI không đọc được."],
        ["luong-hoan-tien"],
    ),
    ("gliffy diagram", macro("gliffy", param("name", "so-do")), "*[Sơ đồ Gliffy: so-do — AI không đọc được]*", [], ["Sơ đồ Gliffy «so-do»: AI không đọc được."], ["so-do"]),
    (
        "image",
        '<p>Màn hình: <ac:image><ri:attachment ri:filename="man-hinh.png"/></ac:image></p>',
        "Màn hình: *[Ảnh: man-hinh.png — AI không đọc được]*",
        [],
        ["Ảnh «man-hinh.png»: AI không đọc được."],
        ["man-hinh.png"],
    ),
    (
        "include",
        macro("include", '<ac:parameter ac:name=""><ac:link><ri:page ri:content-title="Quy ước chung"/></ac:link></ac:parameter>'),
        "[[include:PAY:Quy ước chung]]",
        [DocRef(ref="PAY:Quy ước chung", kind=C)],
        [include_note("Quy ước chung")],
        [],
    ),
    ("hyperlink", '<p><a href="https://example.com/a">ngoài</a></p>', "[ngoài](https://example.com/a)", [DocRef(ref="https://example.com/a", kind="")], [], []),
    (
        "task list",
        "<ac:task-list><ac:task><ac:task-status>complete</ac:task-status><ac:task-body>xong</ac:task-body></ac:task>"
        "<ac:task><ac:task-status>incomplete</ac:task-status><ac:task-body>chưa</ac:task-body></ac:task></ac:task-list>",
        "- [x] xong\n- [ ] chưa",
        [],
        [],
        [],
    ),
]


@pytest.mark.parametrize(("storage", "markdown", "links", "notes", "files"), [c[1:] for c in STORAGE_CASES], ids=[c[0] for c in STORAGE_CASES])
def test_storage_to_markdown(storage: str, markdown: str, links: list[DocRef], notes: list[str], files: list[str]) -> None:
    assert storage_to_markdown(storage, space="PAY") == (markdown, links, notes, files)


def test_markdown_to_storage() -> None:
    out = markdown_to_storage("# T\n\n- a\n- b\n\n1. x\n\n```\nc<d\n```\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nđoạn **đậm** & `mã`")
    assert "<h1>T</h1>" in out
    assert "<ul>\n<li>a</li>\n<li>b</li>\n</ul>" in out
    assert "<ol>\n<li>x</li>\n</ol>" in out
    assert "<![CDATA[c<d]]>" in out
    assert "<tr><th>A</th><th>B</th></tr>\n<tr><td>1</td><td>2</td></tr>\n</tbody></table>" in out
    assert "<p>đoạn <strong>đậm</strong> &amp; <code>mã</code></p>" in out
    assert storage_to_markdown(out)[0].startswith("# T\n\n- a\n- b\n\n1. x\n\n```\nc<d\n```")


# ---- Jira Cloud --------------------------------------------------------------------------------------------

WIKI_URL = f"{SITE}/wiki/spaces/PAY/pages/111/Thiet-ke"
ISSUE = {
    "key": "PAY-120",
    "fields": {
        "summary": "Hoàn tiền một phần",
        "issuetype": {"name": "Story"},
        "status": {"name": "In Progress"},
        "labels": ["ai"],
        "updated": "2026-11-10T09:00:00.000+0700",
        "parent": {"key": "PAY-100"},
        "description": doc(para(text("Xem thiết kế "), {"type": "inlineCard", "attrs": {"url": WIKI_URL}})),
        "customfield_10042": doc(para(text("Hoàn tối đa bằng số đã trả"))),
        "issuelinks": [{"type": {"name": "Blocks", "outward": "blocks", "inward": "is blocked by"}, "outwardIssue": {"key": "CORE-7", "fields": {"summary": "Sổ cái"}}}],
        "subtasks": [{"key": "PAY-121", "fields": {"summary": "API"}}],
        "attachment": [{"filename": "spec.pdf", "mimeType": "application/pdf", "size": 2048, "content": f"{SITE}/rest/api/3/attachment/content/1"}],
    },
}
COMMENTS = {
    "total": 3,
    "comments": [
        {"author": {"displayName": "Lan"}, "created": "2026-11-09", "body": doc(para(text("Mới nhất")))},
        {"author": {"displayName": "Minh"}, "created": "2026-11-08", "body": "cũ hơn"},
    ],
}
REMOTE = [{"object": {"url": f"{SITE}/wiki/x/AbC1", "title": "Trang wiki"}}, {"object": {"url": "https://evil.example/wiki/spaces/X/pages/9", "title": "Ngoài"}}]
FOUND = {"issues": [{"key": "PAY-122", "fields": {"summary": "Con", "status": {"name": "To Do"}, "description": doc(para(text("mô tả con")))}}], "isLast": True}


def jira(fake: Fake, secret: dict[str, str] | None = None, **config: Any) -> JiraCloud:
    merged = {"site": SITE, "projects": ["PAY", "CORE"], "fields": {"acceptance": "customfield_10042"}, "max_comments": 2, **config}
    return JiraCloud(JiraCloudConfig(**merged), SECRET if secret is None else secret, transport=fake.transport)


def jira_fake() -> Fake:
    return Fake(
        {
            ("GET", "/rest/api/3/issue/PAY-120"): ISSUE,
            ("GET", "/rest/api/3/issue/PAY-120/comment"): COMMENTS,
            ("GET", "/rest/api/3/issue/PAY-120/remotelink"): REMOTE,
            ("POST", "/rest/api/3/search/jql"): FOUND,
            ("POST", "/rest/api/3/issue/PAY-120/comment"): {"id": "1"},
            ("GET", "/rest/api/3/myself"): {"emailAddress": "bot@acme.test", "displayName": "Bot"},
        }
    )


def test_jira_declares_what_the_registry_needs() -> None:
    assert (JiraCloud.name, JiraCloud.config_schema, JiraCloud.secret_fields) == ("jira_cloud", JiraCloudConfig, ("email", "token"))
    assert JiraCloud.capabilities == frozenset({Capability.DOCS, Capability.TRACKER}) and JiraCloud.title
    assert JiraCloud.required_scopes == {Capability.DOCS: ["read:jira-work", "read:jira-user"], Capability.TRACKER: ["read:jira-work", "read:jira-user", "write:jira-work"]}
    assert JiraCloudConfig(site="acme.atlassian.net/").site == SITE
    assert JiraCloudConfig(site=SITE).write is False
    with pytest.raises(ValueError):
        JiraCloudConfig(site=SITE, projects=["pay; drop"])
    assert JiraCloudConfig(site=SITE).cloud_id == "" and JiraCloudConfig(site=SITE, cloud_id=f" {CLOUD_ID} ").cloud_id == CLOUD_ID
    with pytest.raises(ValueError):
        JiraCloudConfig(site=SITE, cloud_id="../../evil")


async def test_jira_token_with_scopes_goes_through_the_gateway() -> None:
    gateway = f"/ex/jira/{CLOUD_ID}"
    fake = Fake({("GET", f"{gateway}/rest/api/3/issue/PAY-120"): ISSUE, ("GET", f"{gateway}/rest/api/3/issue/PAY-120/remotelink"): [], ("GET", f"{gateway}/rest/api/3/myself"): {"displayName": "Bot"}})
    adapter = jira(fake, cloud_id=CLOUD_ID, max_comments=0)
    issue = await adapter.issue("PAY-120")
    assert (issue.key, issue.url) == ("PAY-120", f"{SITE}/browse/PAY-120")  # links for people stay on the site
    assert (await adapter.test()).state == "ok"
    assert {r.url.host for r in fake.requests} == {"api.atlassian.com"}
    assert all(r.headers["authorization"].startswith("Basic ") for r in fake.requests)


async def test_jira_search_follows_next_page_tokens() -> None:
    def page(request: httpx.Request) -> dict[str, Any]:
        sent = json.loads(request.content)
        if sent.get("nextPageToken") == "tok-2":
            return {"issues": [{"key": "PAY-3", "fields": {"summary": "ba"}}], "isLast": True}
        return {"issues": [{"key": "PAY-1", "fields": {"summary": "một"}}, {"key": "PAY-2", "fields": {"summary": "hai"}}], "nextPageToken": "tok-2", "isLast": False}

    fake = Fake({("POST", "/rest/api/3/search/jql"): page})
    assert [i.key for i in await jira(fake).search("status = Open", 10)] == ["PAY-1", "PAY-2", "PAY-3"]
    first, second = (json.loads(r.content) for r in fake.requests)
    assert ("nextPageToken" in first, second["nextPageToken"], first["fields"]) == (False, "tok-2", second["fields"])
    assert "summary" in first["fields"]  # the search returns ids only unless fields are named


async def test_jira_issue_is_still_read_when_issue_linking_is_off() -> None:
    fake = jira_fake()
    fake.routes[("GET", "/rest/api/3/issue/PAY-120/remotelink")] = httpx.Response(403)  # what Jira answers when issue linking is disabled
    issue = await jira(fake).issue("PAY-120")
    assert issue.key == "PAY-120" and DocRef(ref=f"{SITE}/wiki/x/AbC1", kind=C) not in issue.links


async def test_jira_fetch_returns_markdown_links_and_version() -> None:
    fake = jira_fake()
    adapter = jira(fake)
    content = await adapter.fetch(DocRef(ref="PAY-120"), SCOPE)
    await adapter.close()

    assert (content.title, content.version, content.url) == ("PAY-120: Hoàn tiền một phần", "2026-11-10T09:00:00.000+0700", f"{SITE}/browse/PAY-120")
    assert content.text.startswith("# PAY-120: Hoàn tiền một phần\n\nLoại: Story · Trạng thái: In Progress · Thuộc: PAY-100 · Nhãn: ai\n\n## Mô tả\n\nXem thiết kế " + WIKI_URL)
    assert "## Tiêu chí chấp nhận\n\nHoàn tối đa bằng số đã trả" in content.text
    assert "## Issue con\n\n- PAY-122: Con (To Do)" in content.text
    assert "- blocks CORE-7: Sổ cái\n- việc con PAY-121: API\n- [Trang wiki](" in content.text
    assert "- spec.pdf (application/pdf, 2048 byte)" in content.text
    assert "## Bình luận (2 mới nhất trong 3)\n\n### Lan · 2026-11-09\n\nMới nhất\n\n### Minh · 2026-11-08\n\ncũ hơn" in content.text
    assert content.truncated is True
    assert content.links == [
        DocRef(ref="CORE-7", kind=J),
        DocRef(ref="PAY-121", kind=J),
        DocRef(ref=WIKI_URL, kind=C),  # from the description
        DocRef(ref=f"{SITE}/wiki/x/AbC1", kind=C),  # a remote link; the one on another host is not a document reference
        DocRef(ref="PAY-122", kind=J),
    ]
    assert [a.name for a in content.attachments] == ["spec.pdf"]
    assert {r.url.host for r in fake.requests} == {"acme.atlassian.net"}
    assert all(r.headers["authorization"].startswith("Basic ") for r in fake.requests)
    comment_request = next(r for r in fake.requests if r.url.path.endswith("/comment"))
    assert (comment_request.url.params["orderBy"], comment_request.url.params["maxResults"]) == ("-created", "2")
    assert "customfield_10042" in fake.requests[0].url.params["fields"]
    assert fake.body("POST", "/rest/api/3/search/jql")["jql"] == 'project in ("PAY", "CORE") AND (parent = PAY-120)'


async def test_jira_tracker_methods() -> None:
    fake = jira_fake()
    adapter = jira(fake)
    issue = await adapter.issue(f"{SITE}/browse/PAY-120")
    assert (issue.key, issue.type, issue.status, issue.parent, issue.labels) == ("PAY-120", "Story", "In Progress", "PAY-100", ["ai"])
    assert issue.fields == {"acceptance": "Hoàn tối đa bằng số đã trả"}
    assert [c["author"] for c in issue.comments] == ["Lan", "Minh"]

    found = await adapter.search("status = Open ORDER BY created ASC", 5)
    assert [(i.key, i.title, i.description) for i in found] == [("PAY-122", "Con", "mô tả con")]
    sent = [json.loads(r.content) for r in fake.requests if r.method == "POST"][-1]
    assert (sent["jql"], sent["maxResults"]) == ('project in ("PAY", "CORE") AND (status = Open) ORDER BY created ASC', 5)

    assert [i.key for i in await adapter.children("PAY-120")] == ["PAY-122"]

    hits = await adapter.search('hoàn "tiền"', SCOPE, 3)
    assert [(h.ref, h.title, h.url) for h in hits] == [(DocRef(ref="PAY-122", kind=J), "PAY-122: Con", f"{SITE}/browse/PAY-122")]
    sent = [json.loads(r.content) for r in fake.requests if r.method == "POST"][-1]
    assert sent["jql"] == 'project in ("PAY", "CORE") AND (text ~ "hoàn  tiền") ORDER BY updated DESC'


RECOGNIZE_TEXT = f"Làm theo PAY-120 và CORE-7, không phải HR-9 hay UTF-8. Xem {SITE}/browse/OPS-3. Bỏ https://evil.example/browse/PAY-999, lặp PAY-120."


def test_jira_recognize_and_in_scope() -> None:
    adapter = jira(Fake())
    assert adapter.recognize(RECOGNIZE_TEXT) == [DocRef(ref="OPS-3", kind=J), DocRef(ref="PAY-120", kind=J), DocRef(ref="CORE-7", kind=J)]
    table = {"PAY-120": True, "CORE-7": True, "OPS-3": False, f"{SITE}/browse/PAY-5": True, f"{SITE}/browse/OPS-3": False, "https://evil.example/browse/PAY-5": False, "không phải khoá": False}
    assert {ref: adapter.in_scope(DocRef(ref=ref), SCOPE) for ref in table} == table
    anything = jira(Fake(), projects=[])
    assert anything.in_scope(DocRef(ref="OPS-3"), SCOPE) is True
    assert anything.recognize("OPS-3 và UTF-8") == []  # bare keys need the list of projects


async def test_jira_refuses_out_of_scope_and_foreign_urls_without_a_request() -> None:
    fake = jira_fake()
    adapter = jira(fake)
    for ref, kind in (("OPS-3", "forbidden"), ("https://evil.example/browse/PAY-120", "invalid"), ("../../admin", "invalid")):
        with pytest.raises(ConnectorError) as err:
            await adapter.fetch(DocRef(ref=ref), SCOPE)
        assert err.value.kind == kind
    assert fake.requests == []


async def test_jira_comment_needs_write() -> None:
    fake = jira_fake()
    with pytest.raises(ConnectorError) as err:
        await jira(fake).comment("PAY-120", "chào")
    assert err.value.kind == "forbidden" and fake.requests == []

    await jira(fake, write=True).comment("PAY-120", "chào\n\n```\nmã\n```")
    body = fake.body("POST", "/rest/api/3/issue/PAY-120/comment")["body"]
    assert [b["type"] for b in body["content"]] == ["paragraph", "codeBlock"]


async def test_jira_health_and_missing_secret() -> None:
    health = await jira(jira_fake()).test()
    assert (health.state, health.identity) == ("ok", "bot@acme.test") and health.checked_at

    fake = jira_fake()
    bare = jira(fake, secret={})
    assert (await bare.test()).state == "unconfigured"
    with pytest.raises(ConnectorError) as err:
        await bare.issue("PAY-120")
    assert err.value.kind == "unconfigured" and fake.requests == []

    denied = Fake({("GET", "/rest/api/3/myself"): httpx.Response(401, text="tok-123 is wrong")})
    health = await jira(denied).test()
    assert health.state == "error" and "tok-123" not in health.detail and "401" in health.detail
    assert "cloud_id" in health.detail  # a token with scopes is refused on the site URL: the message says what to set
    fake = Fake({("GET", f"/ex/jira/{CLOUD_ID}/rest/api/3/myself"): httpx.Response(401)})
    assert "cloud_id" not in (await jira(fake, cloud_id=CLOUD_ID).test()).detail


async def test_jira_error_kinds_and_retry() -> None:
    answers = [httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json=ISSUE)]
    fake = jira_fake()
    fake.routes[("GET", "/rest/api/3/issue/PAY-120")] = lambda request: answers.pop(0)
    adapter = jira(fake, max_comments=0)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    adapter.http.sleep = sleep
    assert (await adapter.issue("PAY-120")).key == "PAY-120" and sleeps == [3.0]

    with pytest.raises(ConnectorError) as err:
        await adapter.issue("PAY-404")
    assert err.value.kind == "not_found"


# ---- Confluence Cloud --------------------------------------------------------------------------------------

PAGE_STORAGE = (
    "<h2>Luồng</h2><p>Theo "
    + macro("jira", param("key", "PAY-120"))
    + "</p>"
    + macro("include", '<ac:parameter ac:name=""><ac:link><ri:page ri:content-title="Quy ước chung"/></ac:link></ac:parameter>')
    + macro("drawio", param("diagramName", "luong"))
    + f'<p><a href="{SITE}/wiki/spaces/ARCH/pages/222/X">kiến trúc</a>, <a href="{SITE}/browse/CORE-7">sổ cái</a>, <a href="https://evil.example/x">ngoài</a></p>'
)
PAGE = {"id": "111", "title": "Thiết kế hoàn tiền", "spaceId": "900", "version": {"number": 7}, "_links": {"webui": "/spaces/PAY/pages/111/Thiet+ke"}, "body": {"storage": {"value": PAGE_STORAGE}}}
INCLUDED = {"id": "333", "title": "Quy ước chung", "spaceId": "900", "body": {"storage": {"value": "<p>Số tiền tính bằng đồng.</p>"}}}
ATTACHMENTS = {"results": [{"title": "luong.drawio.png", "mediaType": "image/png", "fileSize": 10, "downloadLink": "/download/attachments/111/luong.drawio.png"}]}


def wiki(fake: Fake, secret: dict[str, str] | None = None, **config: Any) -> ConfluenceCloud:
    return ConfluenceCloud(ConfluenceCloudConfig(**{"site": SITE, "spaces": ["PAY", "ARCH"], **config}), SECRET if secret is None else secret, transport=fake.transport)


def wiki_fake() -> Fake:
    def children(request: httpx.Request) -> dict[str, Any]:
        # REST v2 "Get direct children of a page": every kind of child with its `type`, paged by `_links.next` (a relative URL).
        if "cursor" in request.url.params:
            more = [{"id": "113", "status": "current", "title": "Con 2", "type": "page", "spaceId": "900", "childPosition": 2}]
            return {"results": [*more, {"id": "114", "status": "current", "title": "Của HR", "type": "page", "spaceId": "901", "childPosition": 3}]}
        first = [{"id": "112", "status": "current", "title": "Con 1", "type": "page", "spaceId": "900", "childPosition": 0}]
        folder = {"id": "115", "status": "current", "title": "Thư mục", "type": "folder", "spaceId": "900", "childPosition": 1}
        return {"results": [*first, folder], "_links": {"next": "/wiki/api/v2/pages/111/direct-children?cursor=abc", "base": f"{SITE}/wiki"}}

    def pages(request: httpx.Request) -> dict[str, Any]:
        return {"results": [INCLUDED] if request.url.params.get("title") == "Quy ước chung" else []}

    return Fake(
        {
            ("GET", "/wiki/api/v2/pages/111"): PAGE,
            ("GET", "/wiki/api/v2/pages/555"): {"id": "555", "title": "Lương", "spaceId": "901", "version": {"number": 1}, "body": {"storage": {"value": "<p>bí mật</p>"}}},
            ("GET", "/wiki/api/v2/spaces/900"): {"id": "900", "key": "PAY"},
            ("GET", "/wiki/api/v2/spaces/901"): {"id": "901", "key": "HR"},
            ("GET", "/wiki/api/v2/spaces"): {"results": [{"id": "900", "key": "PAY"}]},
            ("GET", "/wiki/api/v2/pages"): pages,
            ("GET", "/wiki/api/v2/pages/111/attachments"): ATTACHMENTS,
            ("GET", "/wiki/api/v2/pages/111/direct-children"): children,
            ("GET", "/wiki/x/AbC1"): httpx.Response(302, headers={"Location": "/wiki/spaces/PAY/pages/111/Thiet+ke"}),
            ("GET", "/wiki/x/Evil"): httpx.Response(302, headers={"Location": "https://evil.example/wiki/spaces/PAY/pages/111"}),
            ("GET", "/wiki/rest/api/content/search"): {"results": [{"id": "111", "title": "Thiết kế hoàn tiền", "space": {"key": "PAY"}, "_links": {"webui": "/spaces/PAY/pages/111"}}]},
            ("GET", "/wiki/rest/api/user/current"): {"email": "bot@acme.test"},
        }
    )


def test_confluence_declares_what_the_registry_needs() -> None:
    assert (ConfluenceCloud.name, ConfluenceCloud.config_schema, ConfluenceCloud.secret_fields) == ("confluence_cloud", ConfluenceCloudConfig, ("email", "token"))
    assert ConfluenceCloud.capabilities == frozenset({Capability.DOCS, Capability.WIKI}) and ConfluenceCloud.title
    config = ConfluenceCloudConfig(site=SITE + "/wiki")
    assert (config.site, config.spaces, config.follow_links.depth, config.follow_links.max, config.mirror, config.write) == (SITE, [], 1, 20, False, False)
    read = ["read:page:confluence", "read:space:confluence", "read:attachment:confluence", "search:confluence", "read:confluence-user"]
    assert ConfluenceCloud.required_scopes == {Capability.DOCS: read, Capability.WIKI: [*read, "read:hierarchical-content:confluence", "write:page:confluence"]}


async def test_confluence_token_with_scopes_goes_through_the_gateway() -> None:
    gateway = f"/ex/confluence/{CLOUD_ID}/wiki"
    fake = Fake({("GET", f"{gateway}/api/v2/pages/111"): {**PAGE, "body": {"storage": {"value": "<p>chào</p>"}}}, ("GET", f"{gateway}/api/v2/spaces/900"): {"id": "900", "key": "PAY"}})
    fake.routes[("GET", f"{gateway}/api/v2/pages/111/attachments")] = {"results": []}
    page = await wiki(fake, cloud_id=CLOUD_ID).page("111")
    assert (page.body, page.url) == ("chào", f"{SITE}/wiki/spaces/PAY/pages/111/Thiet+ke")  # links for people stay on the site
    assert {r.url.host for r in fake.requests} == {"api.atlassian.com"}
    assert all(r.headers["authorization"].startswith("Basic ") for r in fake.requests)


async def test_confluence_page_and_fetch_embed_includes_and_keep_notes() -> None:
    fake = wiki_fake()
    adapter = wiki(fake)
    content = await adapter.fetch(DocRef(ref="111"), SCOPE)
    await adapter.close()

    assert (content.title, content.version, content.url) == ("Thiết kế hoàn tiền", "7", f"{SITE}/wiki/spaces/PAY/pages/111/Thiet+ke")
    assert content.text.startswith("# Thiết kế hoàn tiền\n\n## Luồng\n\nTheo PAY-120\n\nSố tiền tính bằng đồng.\n\n*[Sơ đồ draw.io: luong")
    assert "[[include:" not in content.text
    assert content.notes == ["Sơ đồ draw.io «luong»: AI không đọc được."]  # the include was embedded, so its note is gone
    assert content.links == [DocRef(ref="PAY-120", kind=J), DocRef(ref="PAY:Quy ước chung", kind=C), DocRef(ref="222", kind=C), DocRef(ref="CORE-7", kind=J)]
    assert [(a.name, a.media_type, a.size, a.url) for a in content.attachments] == [("luong.drawio.png", "image/png", 10, f"{SITE}/wiki/download/attachments/111/luong.drawio.png")]
    assert {r.url.host for r in fake.requests} == {"acme.atlassian.net"}
    assert fake.requests[0].url.params["body-format"] == "storage"

    page = await wiki(wiki_fake()).page("111")
    assert (page.id, page.space, page.version) == ("111", "PAY", 7)


async def test_confluence_include_that_cannot_be_read_stays_a_link_and_a_note() -> None:
    fake = wiki_fake()
    fake.routes[("GET", "/wiki/api/v2/pages")] = {"results": []}
    page = await wiki(fake).page("111")
    assert "[[include:PAY:Quy ước chung]]" in page.body
    assert include_note("Quy ước chung") in page.notes and DocRef(ref="PAY:Quy ước chung", kind=C) in page.links


async def test_confluence_includes_stop_at_two_levels() -> None:
    def include(title: str) -> str:
        return macro("include", f'<ac:parameter ac:name=""><ac:link><ri:page ri:content-title="{title}"/></ac:link></ac:parameter>')

    bodies = {"A": "<p>a</p>" + include("B"), "B": "<p>b</p>" + include("C"), "C": "<p>c</p>"}
    fake = wiki_fake()
    fake.routes[("GET", "/wiki/api/v2/pages/111")] = {**PAGE, "body": {"storage": {"value": "<p>gốc</p>" + include("A")}}}
    fake.routes[("GET", "/wiki/api/v2/pages")] = lambda r: {"results": [{"id": "9", "title": r.url.params["title"], "spaceId": "900", "body": {"storage": {"value": bodies[r.url.params["title"]]}}}]}
    page = await wiki(fake).page("111")
    assert page.body == "gốc\n\na\n\nb\n\n[[include:PAY:C]]"
    assert page.notes == [include_note("C")]


CONFLUENCE_TEXT = (
    f"Đọc {SITE}/wiki/spaces/PAY/pages/111/Thiet+ke, {SITE}/wiki/x/AbC1 và {SITE}/wiki/pages/viewpage.action?pageId=222. "
    f"Không phải https://evil.example/wiki/spaces/PAY/pages/9 hay {SITE}/browse/PAY-1; lặp {SITE}/wiki/spaces/PAY/pages/111."
)


def test_confluence_recognize_and_in_scope() -> None:
    adapter = wiki(Fake())
    assert adapter.recognize(CONFLUENCE_TEXT) == [DocRef(ref="111", kind=C), DocRef(ref=f"{SITE}/wiki/x/AbC1", kind=C), DocRef(ref="222", kind=C)]
    table = {
        "111": True,  # its space was seen in the URL above
        "222": True,  # space not known yet: decided when the page is read
        f"{SITE}/wiki/spaces/ARCH/pages/5/X": True,
        f"{SITE}/wiki/spaces/HR/pages/6/Luong": False,
        f"{SITE}/wiki/x/AbC1": True,
        "PAY:Quy ước chung": True,
        "HR:Lương": False,
        "https://evil.example/wiki/spaces/PAY/pages/9": False,
        "linh tinh": False,
    }
    assert {ref: adapter.in_scope(DocRef(ref=ref), SCOPE) for ref in table} == table
    adapter.recognize(f"{SITE}/wiki/spaces/HR/pages/777/Luong")
    assert adapter.in_scope(DocRef(ref="777"), SCOPE) is False
    assert wiki(Fake(), spaces=[]).in_scope(DocRef(ref="HR:Lương"), SCOPE) is True


async def test_confluence_resolve_three_url_forms_and_never_calls_another_host() -> None:
    fake = wiki_fake()
    adapter = wiki(fake)
    assert await adapter.resolve(f"{SITE}/wiki/spaces/PAY/pages/111/Thiet+ke") == "111"
    assert await adapter.resolve(f"{SITE}/wiki/pages/viewpage.action?pageId=222") == "222"
    assert fake.requests == []
    assert await adapter.resolve(f"{SITE}/wiki/x/AbC1") == "111"
    assert fake.paths() == ["/wiki/x/AbC1"]

    for url, kind in ((f"{SITE}/wiki/x/Evil", "not_found"), ("https://evil.example/wiki/x/AbC1", "invalid"), (f"{SITE}/wiki/display/PAY/Home", "invalid")):
        with pytest.raises(ConnectorError) as err:
            await adapter.resolve(url)
        assert err.value.kind == kind
    assert {r.url.host for r in fake.requests} == {"acme.atlassian.net"}

    with pytest.raises(ConnectorError) as err:
        await adapter.fetch(DocRef(ref="https://evil.example/wiki/spaces/PAY/pages/111"), SCOPE)
    assert err.value.kind == "forbidden" and {r.url.host for r in fake.requests} == {"acme.atlassian.net"}


async def test_confluence_refuses_a_page_of_another_space() -> None:
    adapter = wiki(wiki_fake())
    for call in (adapter.page("555"), adapter.fetch(DocRef(ref="555"), SCOPE), adapter.fetch(DocRef(ref="HR:Lương"), SCOPE)):
        with pytest.raises(ConnectorError) as err:
            await call
        assert err.value.kind == "forbidden" and "bí mật" not in str(err.value)
    with pytest.raises(ConnectorError) as err:
        await adapter.page("111/../../admin")
    assert err.value.kind == "invalid"


async def test_confluence_fetch_by_space_and_title() -> None:
    fake = wiki_fake()
    fake.routes[("GET", "/wiki/api/v2/pages/333")] = {**INCLUDED, "version": {"number": 2}}
    fake.routes[("GET", "/wiki/api/v2/pages/333/attachments")] = httpx.Response(403)
    content = await wiki(fake).fetch(DocRef(ref="PAY:Quy ước chung"), SCOPE)
    assert (content.version, content.text) == ("2", "# Quy ước chung\n\nSố tiền tính bằng đồng.")
    assert content.notes == ["Không liệt kê được tệp đính kèm của trang."]


async def test_confluence_children_follow_pages_and_drop_other_spaces() -> None:
    fake = wiki_fake()
    kids = await wiki(fake).children("111")
    assert [(k.id, k.title, k.space) for k in kids] == [("112", "Con 1", "PAY"), ("113", "Con 2", "PAY")]  # no folder, no page of HR
    assert kids[0].url == f"{SITE}/wiki/pages/viewpage.action?pageId=112"
    assert fake.paths().count("/wiki/api/v2/pages/111/direct-children") == 2


async def test_confluence_search_is_limited_to_the_allowed_spaces() -> None:
    fake = wiki_fake()
    adapter = wiki(fake)
    pages = await adapter.search('hoàn "tiền"', limit=5)
    assert [(p.id, p.title, p.space, p.url) for p in pages] == [("111", "Thiết kế hoàn tiền", "PAY", f"{SITE}/wiki/spaces/PAY/pages/111")]
    assert fake.requests[-1].url.params["cql"] == 'type = page AND text ~ "hoàn  tiền" AND space in ("PAY", "ARCH")'
    assert fake.requests[-1].url.params["limit"] == "5"

    await adapter.search("x", space="ARCH")
    assert fake.requests[-1].url.params["cql"] == 'type = page AND text ~ "x" AND space in ("ARCH")'

    hits = await adapter.search("hoàn tiền", SCOPE, 3)
    assert [(h.ref, h.title) for h in hits] == [(DocRef(ref="111", kind=C), "Thiết kế hoàn tiền")]

    before = len(fake.requests)
    with pytest.raises(ConnectorError) as err:
        await adapter.search("x", space="HR")
    assert err.value.kind == "forbidden" and len(fake.requests) == before


async def test_confluence_upsert_needs_write_then_creates_or_updates() -> None:
    fake = wiki_fake()
    with pytest.raises(ConnectorError) as err:
        await wiki(fake).upsert_page("PAY", "Mới", "# Mới")
    assert err.value.kind == "forbidden" and fake.requests == []

    created = {"id": "444", "title": "Mới", "spaceId": "900", "version": {"number": 1}, "_links": {"webui": "/spaces/PAY/pages/444"}}
    fake.routes[("POST", "/wiki/api/v2/pages")] = created
    adapter = wiki(fake, write=True)
    page = await adapter.upsert_page("PAY", "Mới", "# Mới\n\nnội dung", parent_id="111")
    assert (page.id, page.space, page.version, page.url, page.body) == ("444", "PAY", 1, f"{SITE}/wiki/spaces/PAY/pages/444", "# Mới\n\nnội dung")
    sent = fake.body("POST", "/wiki/api/v2/pages")
    assert (sent["spaceId"], sent["title"], sent["parentId"], sent["body"]) == ("900", "Mới", "111", {"representation": "storage", "value": "<h1>Mới</h1>\n<p>nội dung</p>"})

    fake.routes[("GET", "/wiki/api/v2/pages")] = {"results": [{"id": "444", "version": {"number": 3}}]}
    fake.routes[("PUT", "/wiki/api/v2/pages/444")] = {**created, "version": {"number": 4}}
    assert (await adapter.upsert_page("PAY", "Mới", "khác")).version == 4
    assert fake.body("PUT", "/wiki/api/v2/pages/444")["version"] == {"number": 4}

    with pytest.raises(ConnectorError) as err:
        await adapter.upsert_page("HR", "Lương", "x")
    assert err.value.kind == "forbidden"


async def test_confluence_health_and_missing_secret() -> None:
    health = await wiki(wiki_fake()).test()
    assert (health.state, health.identity) == ("ok", "bot@acme.test")
    fake = wiki_fake()
    bare = wiki(fake, secret={"email": "bot@acme.test"})
    assert (await bare.test()).state == "unconfigured"
    with pytest.raises(ConnectorError) as err:
        await bare.page("111")
    assert err.value.kind == "unconfigured" and fake.requests == []


# ---- the dependency rule -----------------------------------------------------------------------------------

ADAPTERS = Path(__file__).resolve().parents[1] / "src" / "actoroffice" / "adapters"
OWNED = ["_http.py", "bitbucket_cloud.py", "github.py", *(f"atlassian/{n}.py" for n in ("__init__", "auth", "adf", "storage_html", "jira_cloud", "confluence_cloud"))]
ALLOWED_THIRD_PARTY = {"httpx", "pydantic"}


@pytest.mark.parametrize("relative", OWNED)
def test_adapters_import_only_ports_stdlib_httpx_and_pydantic(relative: str) -> None:
    tree = ast.parse((ADAPTERS / relative).read_text(encoding="utf-8"))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:  # a relative import stays inside adapters/
            modules.append(node.module or "")
    for module in modules:
        top = module.split(".")[0]
        if top == "actoroffice":
            assert module.startswith(("actoroffice.ports", "actoroffice.adapters")), f"{relative} imports {module}"
        else:
            assert top in sys.stdlib_module_names or top in ALLOWED_THIRD_PARTY, f"{relative} imports {module}"
