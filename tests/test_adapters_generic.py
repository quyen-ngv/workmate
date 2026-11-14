"""The generic adapters (`http`, `mcp`, `command`): an outside system connected by configuration only, read through the
same `office.sourcehub` as Jira or Confluence. The `mcp` and `command` tests start real child processes."""

from __future__ import annotations

import ast
import asyncio
import copy
import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
import yaml

from actoroffice.adapters import registry
from actoroffice.adapters.generic.common import Recognizer, content_version, fill, pick
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Scope
from actoroffice.ports.docs import DocRef

from .fakes import FakeRuntime
from .seed import seed_db

SCOPE = Scope()
GENERIC = ("http", "mcp", "command")


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    yield o
    await o.close()


def changed(config: dict[str, Any], changes: dict[str, Any]) -> dict[str, Any]:
    """A copy of a config with `{"fetch.path": value}` style changes; the value `...` removes the key."""
    config = copy.deepcopy(config)
    for dotted, value in changes.items():
        *parents, key = dotted.split(".")
        target = config
        for parent in parents:
            target = target.setdefault(parent, {})
        if value is ...:
            target.pop(key, None)
        else:
            target[key] = value
    return config


def build(use: str, config: dict[str, Any], secret: dict[str, str] | None = None, **kwargs: Any) -> Any:
    return registry.build(ConnectionSpec(id="c", use=use, config=config), secret or {}, **kwargs)


def refused(use: str, config: dict[str, Any]) -> str:
    with pytest.raises(ConnectorError) as err:
        registry.validate(ConnectionSpec(id="c", use=use, config=config))
    assert err.value.kind == "invalid"
    return str(err.value)


async def gone(pid: int, within_s: float = 10.0) -> bool:
    """Whether a process ends within the time given."""
    for _ in range(int(within_s / 0.05)):
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return True
        except psutil.Error:
            return True
        await asyncio.sleep(0.05)
    return False


# ---- registry ---------------------------------------------------------------------------------------------------------


def test_registry_lists_the_generic_adapters_and_describes_their_config() -> None:
    assert set(GENERIC) <= set(registry.names())
    for name in GENERIC:
        info = registry.describe(name)
        schema = info["config_schema"]
        assert info["title"] and info["capabilities"] == ["docs"], name
        assert schema["type"] == "object" and {"recognize", "fetch"} <= set(schema["properties"]) and "fetch" in schema["required"], name
        assert all(prop.get("description") for prop in schema["properties"].values()), f"{name}: every field of the form is explained"
        assert json.loads(json.dumps(info)) == info, "the settings page gets plain JSON"
        assert registry.capabilities(name) == frozenset({Capability.DOCS})
        cls = registry.adapter_class(name)
        assert set(cls.secret_fields) <= set(cls.optional_secret_fields), "which secret is needed depends on the config, so none is required up front"


ADAPTERS = Path(__file__).resolve().parents[1] / "src" / "actoroffice" / "adapters"


@pytest.mark.parametrize("module", ["__init__", "common", "http", "mcp", "command"])
def test_generic_adapters_import_only_ports_adapters_and_declared_libraries(module: str) -> None:
    tree = ast.parse((ADAPTERS / "generic" / f"{module}.py").read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
    for name in names:
        top = name.split(".")[0]
        if top == "actoroffice":
            assert name.startswith(("actoroffice.ports", "actoroffice.adapters", "actoroffice.proctree")), f"{module} imports {name}"  # proctree: the one kill_tree
        else:
            assert top in sys.stdlib_module_names or top in {"httpx", "pydantic", "psutil", "mcp"}, f"{module} imports {name}"


# ---- shared helpers ---------------------------------------------------------------------------------------------------


def test_a_reference_is_the_named_group_and_must_be_the_whole_value_to_pass() -> None:
    rec = Recognizer([r"(?<![\w/-])(?P<ref>DOC-\d+)", r"https://docs\.acme\.test/view/(?P<ref>DOC-\d+)", r"https://wiki\.acme\.test/p/[^\s]+"])
    text = "Xem DOC-12, https://docs.acme.test/view/DOC-13 và https://wiki.acme.test/p/limits. Không phải https://evil.test/view/DOC-99"
    assert rec.find(text) == ["DOC-12", "DOC-13", "https://wiki.acme.test/p/limits"], "a URL gives its key; the sentence's full stop is not part of a URL"
    assert rec.canonical("DOC-12") == "DOC-12" and rec.canonical(" https://docs.acme.test/view/DOC-13 ") == "DOC-13"
    for not_one in ("DOC-12; rm -rf /", "x DOC-12", "DOC-", "https://evil.test/view/DOC-99", "", "../DOC-12"):
        assert rec.canonical(not_one) is None, not_one
    assert rec.resolve("tài liệu DOC-7 (bản nháp)") == "DOC-7"


def test_paths_and_templates() -> None:
    data = {"title": "T", "body": {"content": "nội dung"}, "value": [{"webUrl": "u1", "n": 1}, {"webUrl": "u2"}], "meta": {"rev": 7}}
    assert pick(data, "$.body.content") == pick(data, "body.content") == pick(data, "/body/content") == "nội dung"
    assert pick(data, "$.value[*].webUrl") == ["u1", "u2"] and pick(data, "value[1].webUrl") == pick(data, "value.1.webUrl") == "u2"
    assert pick(data, "$.meta.rev") == 7 and pick(data, "$.nope.x") is None and pick(data, "$") == data
    filled = fill({"q": "{query}", "size": "{limit}", "raw": "a {query} b {other}", "deep": [{"id": "{ref}"}]}, {"query": "hạn mức", "limit": 5, "ref": "DOC-1"})
    assert filled == {"q": "hạn mức", "size": 5, "raw": "a hạn mức b {other}", "deep": [{"id": "DOC-1"}]}, "a lone placeholder keeps its type; unknown braces stay"


# ---- http -------------------------------------------------------------------------------------------------------------

TOKEN = "s3cret-docs-token"
HTTP_YAML = r"""
base_url: https://docs.acme.test/api
auth: bearer
recognize:
  - '(?<![\w/-])(?P<ref>DOC-\d+)'
  - 'https://docs\.acme\.test/view/(?P<ref>DOC-\d+)'
allow: [DOC-1]
fetch:
  path: /documents/{ref}
  query: {expand: body}
  title: $.title
  text: $.body.content
  version: $.meta.rev
  url: $.links.web
search:
  path: /search
  query: {q: "{query}", size: "{limit}"}
  items: $.results
  ref: $.id
  title: $.title
  snippet: $.excerpt
health: /ping
"""
HTTP_CONFIG: dict[str, Any] = yaml.safe_load(HTTP_YAML)
LIMITS = {
    "title": "Quy định hạn mức",
    "body": {"content": "Hạn mức hoàn tiền là 5 lần mỗi ngày. Xem thêm DOC-102."},
    "meta": {"rev": 7},
    "links": {"web": "https://docs.acme.test/view/DOC-101"},
}


class DocsApi:
    """A document system with a REST API at https://docs.acme.test/api, bearer token."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.docs: dict[str, Any] = {"DOC-101": LIMITS}
        self.transport = httpx.MockTransport(self.handle)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.raw_path.decode()}" for r in self.requests]

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        sent = request.headers.get("authorization", "")
        if sent != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"error": f"bad credentials: {sent}"})
        path = request.url.path
        if path == "/api/ping":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/search":
            term = request.url.params["q"]
            results = [{"id": key, "title": doc["title"], "excerpt": doc["body"]["content"][:30]} for key, doc in self.docs.items() if term.lower() in doc["body"]["content"].lower()]
            results += [{"id": "DOC-555", "title": "Ngoài phạm vi", "excerpt": term}, {"id": "bản nháp", "title": "Không có mã", "excerpt": term}]
            return httpx.Response(200, json={"results": results, "size": request.url.params["size"]})
        key = path.removeprefix("/api/documents/")
        if key == "DOC-199":
            return httpx.Response(403, text=f"denied; you sent {sent}")  # a system that echoes the credential
        if key in self.docs:
            return httpx.Response(200, json=self.docs[key])
        return httpx.Response(404, json={"error": "no such document"})


def through(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    """Every adapter the office builds talks to `transport` instead of the network."""
    real = registry.build
    monkeypatch.setattr(registry, "build", lambda spec, secret, **_: real(spec, secret, transport=transport))


async def test_http_source_from_yaml_alone_is_recognised_fetched_and_searched_through_the_office(office: Office, monkeypatch) -> None:
    api = DocsApi()
    through(monkeypatch, api.transport)
    saved = await office.connections.save(ConnectionSpec(id="docs", use="http", config=yaml.safe_load(HTTP_YAML)), secret={"token": TOKEN})
    assert saved.secret_ref == "docs" and TOKEN not in (await office.db.fetchone("SELECT config_json FROM connections WHERE id = 'docs'"))["config_json"]
    ticket = await office.tickets.create("Nâng hạn mức")

    seen = await office.sourcehub.recognize(ticket, "Theo DOC-101 và https://docs.acme.test/view/DOC-102. Không phải https://evil.test/view/DOC-103")
    assert [(spec.id, ref.connection, ref.ref, ref.kind) for spec, _, ref in seen] == [("docs", "docs", "DOC-101", "http"), ("docs", "docs", "DOC-102", "http")]

    fetched = await office.sourcehub.get(ticket, "https://docs.acme.test/view/DOC-101")
    content = fetched.content
    assert (fetched.connection, fetched.use, fetched.ref.ref) == ("docs", "http", "DOC-101"), "a URL and a key are the same document"
    assert (content.title, content.version, content.url) == ("Quy định hạn mức", "7", "https://docs.acme.test/view/DOC-101")
    assert content.text == "Hạn mức hoàn tiền là 5 lần mỗi ngày. Xem thêm DOC-102." and content.links == [DocRef(ref="DOC-102", kind="http")]
    request = api.requests[-1]
    assert (request.method, str(request.url)) == ("GET", "https://docs.acme.test/api/documents/DOC-101?expand=body")
    row = await office.db.fetchone("SELECT version, title FROM source_cache WHERE connection_id = 'docs' AND ref = 'DOC-101'")
    assert (row["version"], row["title"]) == ("7", "Quy định hạn mức")

    found = await office.sourcehub.search(ticket, "hạn mức", limit=5)
    assert found == [{"connection": "docs", "ref": "DOC-101", "title": "Quy định hạn mức", "snippet": "Hạn mức hoàn tiền là 5 lần mỗi", "url": ""}], "hits it could not fetch later are dropped"
    assert dict(api.requests[-1].url.params) == {"q": "hạn mức", "size": "5"}

    health = await office.connections.health("docs")
    assert health.state == "ok" and "docs.acme.test" in health.detail
    assert {r.url.host for r in api.requests} == {"docs.acme.test"}


async def test_http_missing_out_of_scope_and_unknown_references(office: Office, monkeypatch) -> None:
    api = DocsApi()
    through(monkeypatch, api.transport)
    await office.connections.save(ConnectionSpec(id="docs", use="http", config=HTTP_CONFIG), secret={"token": TOKEN})
    ticket = await office.tickets.create("Nâng hạn mức")
    with pytest.raises(ConnectorError) as missing:
        await office.sourcehub.get(ticket, "DOC-104")
    assert missing.value.kind == "not_found" and "404 GET /documents/DOC-104" in str(missing.value)
    before = len(api.requests)
    with pytest.raises(ConnectorError) as outside:
        await office.sourcehub.get(ticket, "DOC-555")
    assert outside.value.kind == "forbidden" and "ngoài phạm vi" in str(outside.value)
    with pytest.raises(ConnectorError) as unknown:
        await office.sourcehub.get(ticket, "https://evil.test/view/DOC-101")
    assert unknown.value.kind == "not_found" and "nhận ra" in str(unknown.value)
    assert len(api.requests) == before, "neither was asked of the outside system"


async def test_http_only_ever_calls_its_own_host() -> None:
    api = DocsApi()
    adapter = build("http", HTTP_CONFIG, {"token": TOKEN}, transport=api.transport)
    assert adapter.recognize("https://evil.test/view/DOC-101 https://docs.acme.test.evil.test/view/DOC-101") == []
    for ref in ("https://evil.test/view/DOC-101", "https://evil.test/api/documents/DOC-101", "DOC-101/../../admin", ".."):
        assert not adapter.in_scope(DocRef(ref=ref), SCOPE)
        with pytest.raises(ConnectorError) as err:
            await adapter.fetch(DocRef(ref=ref), SCOPE)
        assert err.value.kind == "invalid", ref
    with pytest.raises(ConnectorError, match="host khác"):
        await adapter.http.request("GET", "https://evil.test/api/documents/DOC-101")
    assert api.requests == [], "the token never left for another host"
    # a reference goes into the path as one encoded segment: it cannot become another path of the same host either
    loose = build("http", changed(HTTP_CONFIG, {"recognize": [r"(?P<ref>[^\s]+)"], "allow": []}), {"token": TOKEN}, transport=api.transport)
    with pytest.raises(ConnectorError) as err:
        await loose.fetch(DocRef(ref="../admin/users?x=1#y"), SCOPE)
    assert err.value.kind == "not_found" and api.paths() == ["GET docs.acme.test/api/documents/..%2Fadmin%2Fusers%3Fx%3D1%23y?expand=body"]
    await adapter.close()
    await loose.close()


@pytest.mark.parametrize(
    ("changes", "named"),
    [
        ({"recognize": ["(?P<ref>DOC-\\d+"]}, ["recognize", "regex không hợp lệ"]),
        ({"recognize": []}, ["recognize", "ít nhất một mẫu"]),
        ({"recognize": ["(?P<ref>\\d*)"]}, ["recognize", "chuỗi rỗng"]),
        ({"fetch.path": "/documents"}, ["fetch", "{ref}"]),
        ({"fetch.path": "https://evil.test/documents/{ref}"}, ["fetch.path", "host của chính nó"]),
        ({"fetch.path": "/documents/../{ref}"}, ["fetch.path", "'..'"]),
        ({"fetch.method": "DELETE"}, ["fetch.method"]),
        ({"fetch.text": "$.body..[content"}, ["fetch.text", "không đọc được"]),
        ({"fetch.pth": "/x/{ref}"}, ["fetch.pth"]),
        ({"search.query": {"q": "hạn mức"}}, ["search", "{query}"]),
        ({"serach": {"path": "/s"}}, ["serach"]),
        ({"base_url": "docs.acme.test/api"}, ["base_url", "https://"]),
        ({"base_url": "http://docs.acme.test/api"}, ["base_url", "https"]),
        ({"base_url": "https://bot:pw@docs.acme.test/api"}, ["base_url", "bí mật"]),
        ({"auth": "header"}, ["auth_header", "X-Api-Key"]),
        ({"auth": "oauth"}, ["auth"]),
        ({"headers": {"Authorization": "Bearer abc"}}, ["headers", "bí mật"]),
        ({"health": "https://evil.test/ping"}, ["health"]),
    ],
)
def test_http_bad_config_is_refused_with_the_field_named(changes: dict[str, Any], named: list[str]) -> None:
    message = refused("http", changed(HTTP_CONFIG, changes))
    assert all(part in message for part in named), message


async def test_a_bad_generic_config_is_refused_at_save_time_and_what_ran_stays(office: Office) -> None:
    await office.connections.save(ConnectionSpec(id="docs", use="http", config=HTTP_CONFIG), secret={"token": TOKEN})
    with pytest.raises(ConnectorError, match="fetch"):
        await office.connections.save(ConnectionSpec(id="docs", use="http", config=changed(HTTP_CONFIG, {"fetch.path": "/documents"})))
    with pytest.raises(ConnectorError, match="danh sách đối số"):
        await office.connections.save(ConnectionSpec(id="svn", use="command", config={"recognize": ["(?P<ref>\\w+)"], "fetch": "svn cat {ref}"}))
    kept = await office.connections.get("docs")
    assert kept is not None and kept.config == HTTP_CONFIG and await office.connections.get("svn") is None


async def test_http_secret_never_appears_in_errors_or_health(office: Office, monkeypatch) -> None:
    api = DocsApi()
    through(monkeypatch, api.transport)
    await office.connections.save(ConnectionSpec(id="docs", use="http", config=HTTP_CONFIG), secret={"token": TOKEN})
    ticket = await office.tickets.create("Nâng hạn mức")
    said: list[str] = []
    with pytest.raises(ConnectorError) as echoed:
        await office.sourcehub.get(ticket, "DOC-199")
    assert echoed.value.kind == "forbidden"
    said.append(str(echoed.value))
    # the wrong token: the system answers 401 and repeats what it was sent
    await office.connections.save(ConnectionSpec(id="docs", use="http", config=HTTP_CONFIG), secret={"token": "wr0ng-token-value"})
    with pytest.raises(ConnectorError) as denied:
        await office.sourcehub.get(ticket, "DOC-101")
    assert denied.value.kind == "auth"
    health = await office.connections.health("docs")
    assert health.state == "error"
    said += [str(denied.value), health.detail, json.dumps(await office.connections.overview(), ensure_ascii=False)]
    assert all(TOKEN not in text and "wr0ng-token-value" not in text and "Bearer" not in text for text in said), said
    # no secret at all: a normal state that says what is missing, and nothing is called
    before = len(api.requests)
    bare = build("http", HTTP_CONFIG, {}, transport=api.transport)
    health = await bare.test()
    assert health.state == "unconfigured" and "token" in health.detail
    with pytest.raises(ConnectorError) as blocked:
        await bare.fetch(DocRef(ref="DOC-101"), SCOPE)
    assert blocked.value.kind == "unconfigured" and len(api.requests) == before


async def test_http_auth_kinds_html_and_a_content_hash_when_there_is_no_version() -> None:
    seen: list[httpx.Request] = []
    page = "<html><head><title>x</title><script>steal()</script></head><body><h1>Hạn mức</h1><p>Hoàn tiền <b>5 lần</b>.</p></body></html>"

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"value": [{"webUrl": "https://wiki.acme.test/p/limits"}, {"webUrl": "https://elsewhere.test/p/x"}], "body": json.loads(request.content)})
        if request.url.path.startswith("/api/documents/"):
            return httpx.Response(200, json=LIMITS)
        return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})

    config = {
        "base_url": "https://wiki.acme.test",
        "auth": "basic",
        "headers": {"Accept": "text/html"},
        "match": r"https://wiki\.acme\.test/p/[^\s]+",  # the spelling of the Doc Core spec: one pattern, no named group
        "fetch": {"path": "/render", "query": {"page": "{ref}"}, "format": "html"},
        "search": {"method": "post", "path": "/find", "body": {"text": "{query}", "top": "{limit}"}, "items": "$.value[*].webUrl"},
    }
    wiki = build("http", config, {"username": "bot", "password": "pw-123456"}, transport=httpx.MockTransport(handle))
    content = await wiki.fetch(DocRef(ref="https://wiki.acme.test/p/limits"), SCOPE)
    assert content.text == "# Hạn mức\n\nHoàn tiền **5 lần**." and "steal" not in content.text
    assert content.version == content_version(content.text) and content.version.startswith("sha256:"), "no version field: the content's hash"
    assert (content.title, content.url) == ("https://wiki.acme.test/p/limits", "https://wiki.acme.test/p/limits")
    assert seen[-1].headers["authorization"].startswith("Basic ") and seen[-1].headers["accept"] == "text/html"
    assert seen[-1].url.params["page"] == "https://wiki.acme.test/p/limits"
    assert (await wiki.fetch(DocRef(ref="https://wiki.acme.test/p/limits"), SCOPE)).version == content.version
    page = page.replace("5 lần", "9 lần")
    assert (await wiki.fetch(DocRef(ref="https://wiki.acme.test/p/limits"), SCOPE)).version != content.version, "a changed document is a new version"

    found = await wiki.search("hạn mức", SCOPE, limit=3)
    assert [(h.ref.ref, h.url) for h in found] == [("https://wiki.acme.test/p/limits", "https://wiki.acme.test/p/limits")], "a hit on another host is not ours"
    assert json.loads(seen[-1].content) == {"text": "hạn mức", "top": 3}
    assert (await wiki.test()).state == "degraded", "no health path: nothing was checked, and it says so"
    await wiki.close()

    keyed = build("http", changed(HTTP_CONFIG, {"auth": "header", "auth_header": "X-Api-Key", "search": ...}), {"token": TOKEN}, transport=httpx.MockTransport(handle))
    await keyed.fetch(DocRef(ref="DOC-1"), SCOPE)
    assert seen[-1].headers["x-api-key"] == TOKEN and "authorization" not in seen[-1].headers
    assert await keyed.search("hạn mức", SCOPE) == [], "no search configured: no hits, not an error"
    await keyed.close()


# ---- mcp --------------------------------------------------------------------------------------------------------------

MCP_TOKEN = "s3cret-notes-token"
FAKE_MCP_SERVER = r"""
import json, os, sys, time

PAGES = {"N-1": {"title": "Trang một", "last_edited": "2026-11-01T08:00:00Z", "content": "Hạn mức hoàn tiền là 5 lần mỗi ngày. Xem N-2."}}
with open(os.environ["FAKE_PID_FILE"], "a", encoding="utf-8") as f:
    f.write(f"{os.getpid()}\n")


def send(message):
    sys.stdout.buffer.write((json.dumps(message) + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()


def answer(mid, text, is_error=False):
    send({"jsonrpc": "2.0", "id": mid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


for raw in sys.stdin.buffer:
    if not raw.strip():
        continue
    message = json.loads(raw.decode("utf-8"))
    method, mid, params = message.get("method"), message.get("id"), message.get("params") or {}
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {"protocolVersion": params["protocolVersion"], "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-notes", "version": "1"}}})
    elif method == "tools/list":
        names = os.environ.get("FAKE_TOOLS", "fetch_page,find").split(",")
        send({"jsonrpc": "2.0", "id": mid, "result": {"tools": [{"name": name, "description": name, "inputSchema": {"type": "object"}} for name in names]}})
    elif method == "tools/call":
        name, args = params["name"], params.get("arguments") or {}
        page = args.get("id")
        if name == "find":
            hits = [{"id": key, "title": p["title"], "excerpt": f"limit={args.get('limit')!r}"} for key, p in PAGES.items() if args["query"].lower() in p["content"].lower()]
            answer(mid, json.dumps({"results": hits + [{"id": "khong-phai-ma", "title": "x"}]}))
        elif name == "lines":
            answer(mid, "- N-1: Trang một\n- (không có mã)\n- https://notes.acme.test/N-2 Trang hai")
        elif name != "fetch_page":
            send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"Unknown tool: {name}"}})
        elif page == "N-DIE":
            os._exit(3)
        elif page == "N-HANG":
            time.sleep(120)
        elif page == "N-ENV":
            seen = {"token": os.environ.get("NOTES_TOKEN"), "mode": os.environ.get("NOTES_MODE"), "office": os.environ.get("OFFICE_ONLY_SECRET")}
            answer(mid, json.dumps({"title": "env", "content": json.dumps(seen)}))
        elif page == "N-LEAK":
            answer(mid, "denied for token " + os.environ.get("NOTES_TOKEN", "") + "\nstack trace line 2", is_error=True)
        elif page == "N-PLAIN":
            answer(mid, "chỉ là chữ, không phải JSON")
        elif page in PAGES:
            answer(mid, json.dumps(PAGES[page]))
        else:
            answer(mid, f"no such page: {page}", is_error=True)
    elif mid is not None:
        send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}})
"""


@pytest.fixture
def notes(tmp_path: Path) -> dict[str, Any]:
    """Config of an `mcp` connection to a small MCP server run with this Python over stdio."""
    script = tmp_path / "fake_mcp_server.py"
    script.write_text(FAKE_MCP_SERVER, encoding="utf-8")
    return {
        "server": {"type": "stdio", "command": sys.executable, "args": [str(script)], "env": {"FAKE_PID_FILE": str(tmp_path / "pids.txt"), "NOTES_MODE": "test"}},
        "secret_env": {"NOTES_TOKEN": "token"},
        "recognize": [r"https://notes\.acme\.test/(?P<ref>N-[\w-]+)", r"(?<![\w/-])(?P<ref>N-[\w-]+)"],
        "fetch": {"tool": "fetch_page", "args": {"id": "{ref}"}, "text": "$.content", "title": "$.title", "version": "$.last_edited"},
        "search": {"tool": "find", "args": {"query": "{query}", "limit": "{limit}"}, "items": "$.results", "ref": "$.id", "title": "$.title", "snippet": "$.excerpt"},
        "timeout_s": 30,
    }


def started(config: dict[str, Any]) -> list[int]:
    """PIDs of every start of the fake server, oldest first."""
    pids = Path(config["server"]["env"]["FAKE_PID_FILE"])
    return [int(line) for line in pids.read_text(encoding="utf-8").split()] if pids.exists() else []


async def test_mcp_tool_is_a_document_source_and_the_server_is_started_once(notes: dict[str, Any], monkeypatch) -> None:
    monkeypatch.setenv("OFFICE_ONLY_SECRET", "an-api-key-of-the-office")
    adapter = build("mcp", notes, {"token": MCP_TOKEN})
    try:
        assert started(notes) == [], "nothing runs until the first use"
        assert [r.ref for r in adapter.recognize("Xem https://notes.acme.test/N-1 và N-2.")] == ["N-1", "N-2"]
        content = await adapter.fetch(DocRef(ref="https://notes.acme.test/N-1"), SCOPE)
        assert (content.title, content.version) == ("Trang một", "2026-11-01T08:00:00Z")
        assert content.text == "Hạn mức hoàn tiền là 5 lần mỗi ngày. Xem N-2." and content.links == [DocRef(ref="N-2", kind="mcp")]

        found = await adapter.search("hạn mức", SCOPE, limit=4)
        assert [(h.ref.ref, h.title, h.snippet) for h in found] == [("N-1", "Trang một", "limit=4")], "{limit} reaches the tool as a number; a hit without a reference is dropped"

        env = json.loads((await adapter.fetch(DocRef(ref="N-ENV"), SCOPE)).text)
        assert env == {"token": MCP_TOKEN, "mode": "test", "office": None}, "the server gets the connection's secret, not the office's environment"

        plain = build("mcp", changed(notes, {"fetch": {"tool": "fetch_page", "args": {"id": "{ref}"}}, "search": {"tool": "lines", "args": {"q": "{query}"}}}), {"token": MCP_TOKEN})
        try:
            content = await plain.fetch(DocRef(ref="N-PLAIN"), SCOPE)
            assert content.text == "chỉ là chữ, không phải JSON" and content.title == "N-PLAIN" and content.version.startswith("sha256:")
            assert [h.ref.ref for h in await plain.search("bất kỳ", SCOPE)] == ["N-1", "N-2"], "a plain-text result is read line by line"
        finally:
            await plain.close()

        health = await adapter.test()
        assert (health.state, health.identity) == ("ok", "fake-notes 1") and "fetch_page" in health.detail
        pids = started(notes)
        assert len(pids) == 2 and psutil.pid_exists(pids[0]), "one server per connection, kept for every call"
    finally:
        await adapter.close()
    assert await gone(pids[0]) and await gone(pids[1]), "close() ends the server process"
    await adapter.close()  # closing twice is fine


async def test_mcp_errors_missing_tool_tool_failure_and_bad_references(notes: dict[str, Any]) -> None:
    lacking = build("mcp", changed(notes, {"server.env.FAKE_TOOLS": "find,other"}), {"token": MCP_TOKEN})
    try:
        health = await lacking.test()
        assert health.state == "error" and "«fetch_page»" in health.detail and "other" in health.detail, "says which tool is missing and what there is"
    finally:
        await lacking.close()

    adapter = build("mcp", changed(notes, {"allow": ["N-1", "N-L", "N-4"]}), {"token": MCP_TOKEN})
    try:
        with pytest.raises(ConnectorError) as failed:
            await adapter.fetch(DocRef(ref="N-404"), SCOPE)
        assert failed.value.kind == "invalid" and "no such page: N-404" in str(failed.value)
        with pytest.raises(ConnectorError) as leaked:
            await adapter.fetch(DocRef(ref="N-LEAK"), SCOPE)
        assert MCP_TOKEN not in str(leaked.value) and "denied for token ***" in str(leaked.value) and "stack trace" not in str(leaked.value)
        with pytest.raises(ConnectorError) as outside:
            await adapter.fetch(DocRef(ref="N-2"), SCOPE)
        assert outside.value.kind == "forbidden" and not adapter.in_scope(DocRef(ref="N-2"), SCOPE)
        with pytest.raises(ConnectorError) as unknown:
            await adapter.fetch(DocRef(ref="N-1; drop"), SCOPE)
        assert unknown.value.kind == "invalid"
        assert len(started(notes)) == 2, "a refused reference starts nothing; a failing tool does not restart the server"
    finally:
        await adapter.close()

    bare = build("mcp", notes, {})
    health = await bare.test()
    assert health.state == "unconfigured" and "token" in health.detail
    with pytest.raises(ConnectorError) as blocked:
        await bare.fetch(DocRef(ref="N-1"), SCOPE)
    assert blocked.value.kind == "unconfigured" and len(started(notes)) == 2

    nowhere = build("mcp", changed(notes, {"server.command": "no-such-mcp-server-xyz"}), {"token": MCP_TOKEN})
    health = await nowhere.test()
    assert health.state == "error" and "no-such-mcp-server-xyz" in health.detail
    await nowhere.close()


async def test_mcp_server_that_exits_or_hangs_is_a_network_error_and_is_started_again(notes: dict[str, Any]) -> None:
    adapter = build("mcp", changed(notes, {"timeout_s": 3}), {"token": MCP_TOKEN})
    try:
        assert (await adapter.fetch(DocRef(ref="N-1"), SCOPE)).title == "Trang một"
        with pytest.raises(ConnectorError) as died:
            await adapter.fetch(DocRef(ref="N-DIE"), SCOPE)
        assert died.value.kind == "network" and "mất kết nối" in str(died.value) and "fetch_page" in str(died.value)
        assert (await adapter.fetch(DocRef(ref="N-1"), SCOPE)).title == "Trang một", "the next call starts the server again"
        first, second = started(notes)
        assert first != second and await gone(first)

        with pytest.raises(ConnectorError) as hung:
            await adapter.fetch(DocRef(ref="N-HANG"), SCOPE)
        assert hung.value.kind == "network" and "3 giây" in str(hung.value)
        assert await gone(second), "a server that stopped answering is not left running"
    finally:
        await adapter.close()


def test_mcp_bad_config_is_refused_with_the_field_named(notes: dict[str, Any]) -> None:
    for changes, named in [
        ({"server.command": ""}, ["server.command", "type: stdio"]),
        ({"server.args": "fake_mcp_server.py --port 1"}, ["server.args", "danh sách đối số"]),
        ({"server": {"type": "http", "url": "notes.acme.test/mcp"}, "secret_env": {}}, ["server.url", "https://"]),
        ({"server": {"type": "http", "url": "http://notes.acme.test/mcp"}, "secret_env": {}, "auth": "bearer"}, ["server.url", "https"]),
        ({"server": {"type": "http", "url": "https://notes.acme.test/mcp", "headers": {"Authorization": "Bearer x"}}, "secret_env": {}}, ["server.headers", "bí mật"]),
        ({"server": {"type": "http", "url": "https://notes.acme.test/mcp"}}, ["secret_env", "stdio"]),
        ({"auth": "bearer"}, ["auth", "http / sse"]),
        ({"secret_env": ["BAD NAME"]}, ["secret_env", "BAD NAME"]),
        ({"fetch.args": {"id": "N-1"}}, ["fetch", "{ref}"]),
        ({"fetch.tool": ""}, ["fetch.tool"]),
        ({"search.args": {}}, ["search", "{query}"]),
        ({"recognize": ["N-(\\d+"]}, ["recognize", "regex không hợp lệ"]),
        ({"fetch.version": "$.a[b]"}, ["fetch.version"]),
    ]:
        message = refused("mcp", changed(notes, changes))
        assert all(part in message for part in named), message
    remote = registry.validate(ConnectionSpec(id="c", use="mcp", config=changed(notes, {"server": {"type": "http", "url": "https://notes.acme.test/mcp"}, "secret_env": ..., "auth": "bearer"})))
    assert remote.server.url == "https://notes.acme.test/mcp"
    listed = registry.validate(ConnectionSpec(id="c", use="mcp", config=changed(notes, {"secret_env": ["NOTES_TOKEN"]})))
    assert listed.secret_env == {"NOTES_TOKEN": "NOTES_TOKEN"}, "a list of names: the secret field has the variable's name"


async def test_mcp_source_through_the_office(office: Office, notes: dict[str, Any]) -> None:
    await office.connections.save(ConnectionSpec(id="notes", use="mcp", config=notes), secret={"token": MCP_TOKEN})
    ticket = await office.tickets.create("Nâng hạn mức")
    fetched = await office.sourcehub.get(ticket, "N-1")
    assert (fetched.connection, fetched.use, fetched.ref.ref, fetched.content.version) == ("notes", "mcp", "N-1", "2026-11-01T08:00:00Z")
    assert [(h["connection"], h["ref"]) for h in await office.sourcehub.search(ticket, "hoàn tiền")] == [("notes", "N-1")]
    assert (await office.connections.health("notes")).state == "ok"
    (pid,) = started(notes)
    await office.connections.remove("notes")
    assert await gone(pid), "removing the connection stops its server"


# ---- command ----------------------------------------------------------------------------------------------------------

CLI_PASSWORD = "s3cret-svn-password"
FAKE_CLI = r"""
import json, os, pathlib, subprocess, sys, time

root = pathlib.Path(__file__).parent
with open(root / "calls.log", "a", encoding="utf-8") as log:
    log.write(json.dumps(sys.argv[1:]) + "\n")
action, args, out = sys.argv[1], sys.argv[2:], sys.stdout.buffer
if action == "cat":
    out.write((root / args[0]).read_bytes())
elif action == "rev":
    out.write(b"\nr42\nsecond line\n")
elif action == "grep":
    found = [
        f"{path.relative_to(root).as_posix()}:{n}:{line}"
        for path in sorted(root.glob("docs/*.md"))
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if args[0].lower() in line.lower()
    ]
    out.write("\n".join(found).encode("utf-8"))
    sys.exit(0 if found else 1)
elif action == "big":
    out.write(b"x" * 300_000)
elif action == "sleep":
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    (root / "pids.txt").write_text(f"{os.getpid()} {child.pid}", encoding="utf-8")
    time.sleep(120)
elif action == "env":
    out.write(json.dumps({name: os.environ.get(name) for name in ("SVN_PASSWORD", "SVN_MODE", "OFFICE_ONLY_SECRET")}).encode("utf-8"))
elif action == "fail":
    sys.stderr.write("\nsvn: E170001: wrong password " + os.environ.get("SVN_PASSWORD", "") + "\nat line 2\n")
    sys.exit(3)
"""


@pytest.fixture
def svn(tmp_path: Path) -> dict[str, Any]:
    """Config of a `command` connection whose "CLI" is a small script run with this Python, in a folder of documents."""
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "cli.py").write_text(FAKE_CLI, encoding="utf-8")
    (root / "docs" / "limits.md").write_text("# Hạn mức\n\nHoàn tiền tối đa 5 lần mỗi ngày. Xem docs/refunds.md.\n", encoding="utf-8")
    (root / "docs" / "refunds.md").write_text("# Hoàn tiền\n\nQuy trình hoàn tiền.\n", encoding="utf-8")
    (root / "secret.md").write_text("ngoài thư mục docs\n", encoding="utf-8")
    cli = [sys.executable, str(root / "cli.py")]
    return {
        "recognize": [r"(?<![\w./-])(?P<ref>docs/[\w./-]+\.md)"],
        "fetch": [*cli, "cat", "{ref}"],
        "version": [*cli, "rev", "{ref}"],
        "search": [*cli, "grep", "{query}"],
        "cwd": str(root),
        "timeout_s": 30,
    }


def calls(config: dict[str, Any]) -> list[list[str]]:
    """Arguments of every run of the fake CLI."""
    log = Path(config["cwd"]) / "calls.log"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []


def with_action(config: dict[str, Any], action: str, **more: Any) -> dict[str, Any]:
    return changed(config, {"fetch": [*config["fetch"][:2], action, "{ref}"], "version": ..., **more})


async def test_command_fetches_versions_and_searches(svn: dict[str, Any]) -> None:
    adapter = build("command", svn)
    assert [r.ref for r in adapter.recognize("Theo docs/limits.md, không phải /etc/docs/x.md hay docs/../secret.md")] == ["docs/limits.md"]
    content = await adapter.fetch(DocRef(ref="docs/limits.md"), SCOPE)
    assert content.text == "# Hạn mức\n\nHoàn tiền tối đa 5 lần mỗi ngày. Xem docs/refunds.md.\n" and not content.truncated
    assert (content.title, content.version, content.links) == ("docs/limits.md", "r42", [DocRef(ref="docs/refunds.md", kind="command")])
    assert calls(svn) == [["cat", "docs/limits.md"], ["rev", "docs/limits.md"]]

    found = await adapter.search("hoàn  tiền", SCOPE)
    assert [(h.ref.ref, h.snippet) for h in found] == [
        ("docs/limits.md", "docs/limits.md:3:Hoàn tiền tối đa 5 lần mỗi ngày. Xem docs/refunds.md."),
        ("docs/refunds.md", "docs/refunds.md:1:# Hoàn tiền"),
    ]
    assert calls(svn)[-1] == ["grep", "hoàn tiền"], "one argument, whitespace folded"
    assert await adapter.search("zzz-không-có", SCOPE) == [], "exit code 1 of a search is 'nothing found'"

    hashed = build("command", changed(svn, {"version": ..., "search": ...}))
    content = await hashed.fetch(DocRef(ref="docs/refunds.md"), SCOPE)
    assert content.version == content_version(content.text) and await hashed.search("hoàn tiền", SCOPE) == []
    health = await adapter.test()
    assert health.state == "ok" and Path(sys.executable).name in health.detail


@pytest.mark.parametrize("ref", ["-rf", "--help", "docs/limits.md; rm -rf /", "a; rm", "../secret.md", "docs/../secret.md", "docs/limits.md\nx", "$(touch pwned).md", "", "docs/limits.md --force"])
async def test_command_refuses_a_reference_that_is_not_one_before_anything_runs(svn: dict[str, Any], ref: str) -> None:
    for config in (svn, changed(svn, {"recognize": [r"(?P<ref>-?[\w./-]+)"]})):  # the second pattern would let options and `..` through
        adapter = build("command", config)
        with pytest.raises(ConnectorError) as err:
            await adapter.fetch(DocRef(ref=ref), SCOPE)
        assert err.value.kind == "invalid" and "không lệnh nào được chạy" in str(err.value)
        assert not adapter.in_scope(DocRef(ref=ref), SCOPE) and all(not r.ref.startswith("-") and ".." not in r.ref for r in adapter.recognize(f"xem {ref} nhé"))
    assert calls(svn) == [] and not (Path(svn["cwd"]) / "pwned").exists()


async def test_command_arguments_are_never_read_by_a_shell(svn: dict[str, Any]) -> None:
    adapter = build("command", changed(svn, {"recognize": [r"(?P<ref>docs/[^\n]+\.md)"], "version": ...}))
    odd = "docs/a b & echo hi > pwned; $(touch pwned) `touch pwned` | %PATH% .md"
    with pytest.raises(ConnectorError) as err:
        await adapter.fetch(DocRef(ref=odd), SCOPE)
    assert err.value.kind == "network" and "mã lỗi 1" in str(err.value), "the CLI got it as one file name, which does not exist"
    assert calls(svn) == [["cat", odd]] and not (Path(svn["cwd"]) / "pwned").exists()
    for query in ("-e", "--pre=cmd", "-"):
        with pytest.raises(ConnectorError, match="bắt đầu bằng '-'"):
            await build("command", svn).search(query, SCOPE)
    assert len(calls(svn)) == 1


def test_command_bad_config_is_refused_with_the_field_named(svn: dict[str, Any]) -> None:
    for changes, named in [
        ({"fetch": "svn cat {ref}"}, ["fetch", "danh sách đối số", "chuỗi lệnh shell"]),
        ({"fetch": ["sh", "-c", "svn cat {ref}"]}, ["fetch", "shell"]),
        ({"fetch": ["C:\\Windows\\System32\\cmd.exe", "/c", "type {ref}"]}, ["fetch", "shell"]),
        ({"fetch": ["powershell", "-Command", "Get-Content {ref}"]}, ["fetch", "shell"]),
        ({"fetch": ["tools\\cat.bat", "{ref}"]}, ["fetch", "batch"]),
        ({"fetch": ["svn", "cat"]}, ["fetch", "{ref}"]),
        ({"fetch": ["{ref}"]}, ["fetch", "tên chương trình"]),
        ({"fetch": []}, ["fetch", "không rỗng"]),
        ({"version": "svn info {ref}"}, ["version", "danh sách đối số"]),
        ({"search": ["rg", "-n", "--", "."]}, ["search", "{query}"]),
        ({"recognize": ["docs/[a-z+\\.md"]}, ["recognize", "regex không hợp lệ"]),
        ({"cwd": "relative/dir"}, ["cwd", "tuyệt đối"]),
        ({"timeout_s": 0}, ["timeout_s"]),
        ({"secret_env": {"1BAD": "password"}}, ["secret_env", "1BAD"]),
        ({"shell": True}, ["shell"]),
    ]:
        message = refused("command", changed(svn, changes))
        assert all(part in message for part in named), message


async def test_command_timeout_kills_the_process_and_what_it_started(svn: dict[str, Any]) -> None:
    adapter = build("command", with_action(svn, "sleep", timeout_s=4))
    with pytest.raises(ConnectorError) as err:
        await adapter.fetch(DocRef(ref="docs/limits.md"), SCOPE)
    assert err.value.kind == "network" and "quá 4 giây" in str(err.value)
    parent, child = (int(pid) for pid in (Path(svn["cwd"]) / "pids.txt").read_text(encoding="utf-8").split())
    assert await gone(parent) and await gone(child), "the command and the process it started are both gone"


async def test_command_oversized_output_is_cut_and_flagged(svn: dict[str, Any], office: Office) -> None:
    adapter = build("command", with_action(svn, "big", max_output_kb=64))
    content = await adapter.fetch(DocRef(ref="docs/limits.md"), SCOPE)
    assert len(content.text) == 64 * 1024 and content.truncated and any("cắt bớt" in note for note in content.notes)
    exact = await build("command", with_action(svn, "big", max_output_kb=300)).fetch(DocRef(ref="docs/limits.md"), SCOPE)
    assert len(exact.text) == 300_000 and not exact.truncated
    await office.connections.save(ConnectionSpec(id="svn", use="command", config=with_action(svn, "big", max_output_kb=64)))
    fetched = await office.sourcehub.get(await office.tickets.create("Nâng hạn mức"), "docs/limits.md")
    assert (fetched.use, fetched.content.truncated, len(fetched.content.text)) == ("command", True, 64 * 1024)


async def test_command_gets_the_connection_secret_not_the_office_environment_and_never_echoes_it(svn: dict[str, Any], monkeypatch) -> None:
    monkeypatch.setenv("OFFICE_ONLY_SECRET", "an-api-key-of-the-office")
    config = with_action(svn, "env", env={"SVN_MODE": "quiet"}, secret_env={"SVN_PASSWORD": "password"})
    seen = json.loads((await build("command", config, {"password": CLI_PASSWORD}).fetch(DocRef(ref="docs/limits.md"), SCOPE)).text)
    assert seen == {"SVN_PASSWORD": CLI_PASSWORD, "SVN_MODE": "quiet", "OFFICE_ONLY_SECRET": None}

    failing = build("command", with_action(svn, "fail", secret_env=["SVN_PASSWORD"]), {"SVN_PASSWORD": CLI_PASSWORD})
    with pytest.raises(ConnectorError) as err:
        await failing.fetch(DocRef(ref="docs/limits.md"), SCOPE)
    message = str(err.value)
    assert err.value.kind == "network" and "mã lỗi 3" in message and "svn: E170001: wrong password ***" in message
    assert CLI_PASSWORD not in message and "at line 2" not in message, "one redacted line of stderr, no more"

    before = len(calls(svn))
    bare = build("command", config, {})
    health = await bare.test()
    assert health.state == "unconfigured" and "password" in health.detail
    with pytest.raises(ConnectorError) as blocked:
        await bare.fetch(DocRef(ref="docs/limits.md"), SCOPE)
    assert blocked.value.kind == "unconfigured" and len(calls(svn)) == before

    for broken, part in ((changed(svn, {"fetch": ["no-such-cli-xyz", "cat", "{ref}"]}), "no-such-cli-xyz"), (changed(svn, {"cwd": str(Path(svn["cwd"]) / "missing")}), "cwd")):
        adapter = build("command", broken)
        health = await adapter.test()
        assert health.state == "error" and part in health.detail
        with pytest.raises(ConnectorError) as err:
            await adapter.fetch(DocRef(ref="docs/limits.md"), SCOPE)
        assert err.value.kind == "invalid"


async def test_command_source_through_the_office(office: Office, svn: dict[str, Any]) -> None:
    await office.connections.save(ConnectionSpec(id="svn", use="command", config=changed(svn, {"allow": ["docs/lim"]})))
    ticket = await office.tickets.create("Nâng hạn mức")
    fetched = await office.sourcehub.get(ticket, "docs/limits.md")
    assert (fetched.connection, fetched.use, fetched.content.version) == ("svn", "command", "r42") and "5 lần mỗi ngày" in fetched.content.text
    assert [h["ref"] for h in await office.sourcehub.search(ticket, "hoàn tiền")] == ["docs/limits.md"], "docs/refunds.md is outside `allow`"
    with pytest.raises(ConnectorError) as outside:
        await office.sourcehub.get(ticket, "docs/refunds.md")
    assert outside.value.kind == "forbidden"
    with pytest.raises(ConnectorError) as unknown:
        await office.sourcehub.get(ticket, "docs/../secret.md")
    assert unknown.value.kind == "not_found" and "nhận ra" in str(unknown.value)
    assert (await office.connections.health("svn")).state == "ok"
