"""Documents of outside systems through connections: `source_get` / `source_search`, the "read" mark of a ticket's
sources, evidence pinned by a document, and nothing-connected as a normal state."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel

from actoroffice.adapters import registry
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Health, Scope
from actoroffice.ports.docs import DocContent, DocHit, DocRef
from actoroffice.security.policy import UNTRUSTED_OFFICE_TOOLS
from actoroffice.tools.registry import ToolCtx, ToolError
from actoroffice.tools.source_tools import SOURCE_TOOLS

from .fakes import FakeRuntime
from .seed import seed_db
from .test_docflow import analysis, put

PAGES: dict[str, dict[str, Any]] = {}


class WikiConfig(BaseModel):
    site: str = "https://wiki.acme"
    spaces: list[str] = []
    write: bool = False


class FakeWiki:
    """Pages at https://wiki.acme/<SPACE>/<id>, kept in PAGES."""

    name: ClassVar[str] = "fake_wiki"
    title: ClassVar[str] = "Wiki giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS})
    config_schema: ClassVar[type[BaseModel]] = WikiConfig
    secret_fields: ClassVar[tuple[str, ...]] = ()
    fetched: ClassVar[list[str]] = []

    def __init__(self, config: WikiConfig, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config = config

    def recognize(self, text: str) -> list[DocRef]:
        return [DocRef(ref=m.group(0), kind="fake_wiki") for m in re.finditer(r"https://wiki\.acme/[A-Z]+/\d+", text)]

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        return not self.config.spaces or ref.ref.split("/")[3] in self.config.spaces

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        FakeWiki.fetched.append(ref.ref)
        page = PAGES.get(ref.ref)
        if page is None:
            raise ConnectorError("not_found", f"không có trang {ref.ref}")
        return DocContent(title=page["title"], text=page["text"], version=str(page["version"]), url=ref.ref, notes=page.get("notes", []), links=[DocRef(ref=u) for u in page.get("links", [])])

    async def search_docs(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]:
        return [DocHit(ref=DocRef(ref=url), title=p["title"], snippet=p["text"][:40], url=url) for url, p in PAGES.items() if query.lower() in p["text"].lower()][:limit]

    async def test(self) -> Health:
        return Health(state="ok")

    async def close(self) -> None:
        return None


LIMITS = "https://wiki.acme/PAY/101"


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    registry.register("fake_wiki", FakeWiki)
    PAGES.clear()
    FakeWiki.fetched.clear()
    PAGES[LIMITS] = {"title": "Quy định hạn mức", "text": "Hạn mức hoàn tiền là 5 lần mỗi ngày.", "version": 3, "notes": ["sơ đồ limits.drawio: AI không đọc được"], "links": ["https://wiki.acme/PAY/102"]}
    PAGES["https://wiki.acme/HR/7"] = {"title": "Nghỉ phép", "text": "Không liên quan.", "version": 1}
    yield o
    registry.unregister("fake_wiki")
    await o.close()


def tool(name: str):
    return next(t for t in SOURCE_TOOLS if t.name == name).handler


async def ticket_ctx(office: Office, description: str = "", team_id: str | None = None):
    ticket = await office.tickets.create("Nâng hạn mức", description=description)
    if team_id:
        await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (team_id, ticket.id))
    ticket = await office.tickets.get(ticket.id)
    return ticket, ToolCtx(office, None, None, "run_1", ticket_id=ticket.id)  # type: ignore[arg-type]


async def connect(office: Office, scope: str = "office", spaces: list[str] | None = None, cid: str = "wiki") -> None:
    await office.connections.save(ConnectionSpec(id=cid, use="fake_wiki", scope=scope, config={"spaces": spaces or []}))


async def test_nothing_connected_says_what_is_missing(office: Office) -> None:
    ticket, ctx = await ticket_ctx(office)
    for name, args in (("source_get", {"ref": LIMITS}), ("source_search", {"query": "hạn mức"})):
        with pytest.raises(ToolError, match="Cài đặt › Kết nối"):
            await tool(name)(args, ctx)
    with pytest.raises(ToolError, match="Thiếu tham số"):
        await tool("source_get")({}, ctx)


async def test_get_reads_marks_the_source_and_keeps_a_copy(office: Office) -> None:
    await connect(office)
    ticket, ctx = await ticket_ctx(office, description=f"Theo quy định ở {LIMITS} và https://example.com/blog.")
    listed = {s["ref"]: s for s in await office.sources.list(ticket)}
    assert (listed[LIMITS]["required"], listed[LIMITS]["status"]) == (True, "unread"), "a URL a connection knows is a required source"
    assert listed["https://example.com/blog"]["status"] == "external"
    assert await office.sources.unread_required(ticket) == [LIMITS]

    out = await tool("source_get")({"ref": LIMITS}, ctx)
    assert out.startswith("# Quy định hạn mức\nNguồn: https://wiki.acme/PAY/101 · phiên bản 3 · qua kết nối wiki")
    assert "⚠ Phần AI không đọc được: sơ đồ limits.drawio" in out and "https://wiki.acme/PAY/102" in out
    assert "<document>\nHạn mức hoàn tiền là 5 lần mỗi ngày.\n</document>" in out
    assert f"Đã ghi nhận là nguồn đã đọc của ticket: {LIMITS}" in out
    assert await office.sources.unread_required(ticket) == []
    row = await office.db.fetchone("SELECT version, title, body FROM source_cache WHERE connection_id = 'wiki' AND ref = ?", (LIMITS,))
    assert (row["version"], row["title"]) == ("3", "Quy định hạn mức")
    PAGES[LIMITS]["version"] = 4
    await tool("source_get")({"ref": LIMITS}, ctx)
    assert (await office.db.fetchone("SELECT version FROM source_cache WHERE ref = ?", (LIMITS,)))["version"] == "4"


async def test_scope_of_the_connection_and_of_the_team(office: Office) -> None:
    pay, hr = await office.teams.create_team("Pay"), await office.teams.create_team("HR")
    await connect(office, scope=f"team:{pay.id}", spaces=["PAY"])
    ticket, ctx = await ticket_ctx(office, team_id=pay.id)
    with pytest.raises(ToolError, match="ngoài phạm vi"):
        await tool("source_get")({"ref": "https://wiki.acme/HR/7"}, ctx)
    with pytest.raises(ToolError, match="không có trang"):
        await tool("source_get")({"ref": "https://wiki.acme/PAY/999"}, ctx)
    with pytest.raises(ToolError, match="nhận ra"):
        await tool("source_get")({"ref": "https://other.example/x"}, ctx)
    assert "Quy định hạn mức" in await tool("source_get")({"ref": LIMITS}, ctx)
    other, other_ctx = await ticket_ctx(office, team_id=hr.id)
    with pytest.raises(ToolError, match="Cài đặt › Kết nối"):
        await tool("source_get")({"ref": LIMITS}, other_ctx)  # another team's connection is never used


async def test_search_across_connections(office: Office) -> None:
    await connect(office)
    ticket, ctx = await ticket_ctx(office)
    found = await tool("source_search")({"query": "hạn mức"}, ctx)
    assert found.startswith(f"- [wiki] {LIMITS} — Quy định hạn mức") and "Nghỉ phép" not in found
    assert "Không tìm thấy" in await tool("source_search")({"query": "zzz"}, ctx)


async def test_a_cited_outside_document_is_pinned_as_evidence(office: Office) -> None:
    await connect(office)
    epic = await office.tickets.create("Nâng hạn mức")
    await office.db.execute("UPDATE tickets SET stage_template = 'epic', stage = 'analyze' WHERE id = ?", (epic.id,))
    epic = await office.tickets.get(epic.id)
    await put(office, epic, "analysis", analysis(epic.key, source=LIMITS))
    findings = await office.docs.check(epic)
    assert [f for f in findings if f.severity == "error"] == []
    row = await office.db.fetchone("SELECT provider, category, version, excerpt FROM doc_evidence WHERE ticket_id = ?", (epic.id,))
    assert (row["provider"], row["category"], row["version"]) == ("fake_wiki", "docs", "3") and "5 lần mỗi ngày" in row["excerpt"]
    # a page that does not exist in the connected wiki is a source that cannot be opened
    await put(office, epic, "analysis", analysis(epic.key, source="https://wiki.acme/PAY/999"))
    assert [(f.rule, f.item) for f in await office.docs.check(epic) if f.severity == "error"] == [("R-EVIDENCE", "F-1")]
    # a URL no connection knows cannot be checked and is not held against the author
    await put(office, epic, "analysis", analysis(epic.key, source="https://docs.example.com/limits"))
    assert [f for f in await office.docs.check(epic) if f.severity == "error"] == []


def test_reading_an_outside_document_taints_the_session() -> None:
    from actoroffice.engine.executor import TICKET_TOOLS

    assert {"source_get", "source_search"} == UNTRUSTED_OFFICE_TOOLS <= {t.name for t in TICKET_TOOLS}
