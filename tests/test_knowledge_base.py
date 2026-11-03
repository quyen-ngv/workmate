"""Team knowledge base: typed items, verification, staleness after merges, Confluence publishing, known context."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import ProjectAccess
from actoroffice.services.knowledge import KbError

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


class FakeWiki:
    def __init__(self) -> None:
        self.pages: dict[str, str] = {}

    async def search(self, text: str, *, space: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
        return []

    async def page(self, page_id: str) -> dict[str, Any]:
        return {}

    async def upsert_page(self, space: str, title: str, html: str, *, parent_id: str | None = None) -> dict[str, Any]:
        self.pages[title] = html
        return {"id": str(len(self.pages)), "url": f"https://acme.atlassian.net/wiki/{len(self.pages)}"}


async def test_items_dedupe_search_and_verification(office: Office) -> None:
    team = await office.teams.create_team("Billing")
    with pytest.raises(KbError):
        await office.knowledge.note(kind="code_note", title="Làm tròn", content="ở RoundingUtil", by="Copilot", team_id=team.id)
    a = await office.knowledge.note(kind="fact", title="Hạn hoàn tiền", content="30 ngày", by="Copilot", team_id=team.id)
    await office.knowledge.set_status(a.id, "verified", by="Lan")
    b = await office.knowledge.note(kind="fact", title="hạn hoàn tiền", content="VIP 60 ngày", by="Copilot", team_id=team.id)
    assert b.id == a.id and b.status == "draft" and b.content == "VIP 60 ngày"  # a changed fact needs confirming again
    contract = await office.knowledge.note(
        kind="contract", title="payments-api POST /v1/refunds", content="Tạo lệnh hoàn", by="Copilot", team_id=team.id,
        data={"spec": {"where": "repo:payments/openapi.yaml"}, "auth": {"kind": "api_key", "source": "secret:payments"}, "quirks": ["idempotency-key bắt buộc"]},
    )
    other = await office.teams.create_team("Search")
    await office.knowledge.note(kind="fact", title="Hoàn tiền search", content="không liên quan", by="x", team_id=other.id)
    found = await office.knowledge.search("hoàn tiền refunds", team_id=team.id)
    assert {i.id for i in found} == {a.id, contract.id}  # never another team's items
    assert "idempotency-key" in str(contract.data)


async def test_code_notes_go_stale_after_a_merge_and_the_team_publishes_to_confluence(office: Office, runtime: FakeRuntime, tmp_path: Path) -> None:
    team = await office.teams.create_team("Billing", confluence_space="BIL")
    wiki = FakeWiki()
    office.atlassian.use(wiki=wiki)
    repo = tmp_path / "svc"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"],):
        subprocess.run(["git", "-C", str(repo), *args], check=True)
    (repo / "rounding.py").write_text("def r(x): return round(x)\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init"], check=True)
    project = await office.projects.create("svc", path=str(repo), access=ProjectAccess.WORKTREE)
    note = await office.knowledge.note(kind="code_note", title="Làm tròn tiền", content="rounding.py dùng round()", by="Copilot", team_id=team.id, anchor={"path": "rounding.py"})
    await office.knowledge.set_status(note.id, "verified", by="Minh")
    untouched = await office.knowledge.note(kind="code_note", title="Xuất hoá đơn", content="invoice.py", by="Copilot", team_id=team.id, anchor={"path": "invoice.py"})

    ticket = await office.tickets.create("Sửa làm tròn", description="x", project_id=project.id)
    await office.db.execute("UPDATE tickets SET team_id = ?, stage = 'pr', stage_template = 'code', pr_url = 'https://bitbucket.org/a/b/pull-requests/1' WHERE id = ?", (team.id, ticket.id))

    async def pr(ctx: TurnCtx) -> TurnResult:
        (ctx.spec.cwd / "rounding.py").write_text("from decimal import Decimal, ROUND_HALF_UP\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(ctx.spec.cwd), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "half up"], check=True)
        await ctx.office("stage_complete", {"summary": "PR"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    async def close(ctx: TurnCtx) -> TurnResult:
        assert "có thể đã cũ" in ctx.spec.system_prompt or "stale" in ctx.spec.system_prompt
        await ctx.office("dossier_update", {"section": "decisions", "body": "Làm tròn HALF_UP theo Lan"})
        await ctx.office("kb_note", {"kind": "code_note", "title": "Làm tròn tiền", "content": "rounding.py dùng Decimal ROUND_HALF_UP", "anchor": {"path": "rounding.py"}})
        await ctx.office("stage_complete", {"summary": "xong"})
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts += [pr, close]
    await office.tickets.assign(ticket.id, (await office.ensure_ai()).id, by="user")
    await office.scheduler.wait_idle(timeout=30)
    await office.stages.external_passed(ticket.id, "merge", by="user", note="merged")
    assert (await office.knowledge.get(note.id)).status == "stale"
    assert (await office.knowledge.get(untouched.id)).status == "draft"
    await office.scheduler.wait_idle(timeout=30)
    refreshed = await office.knowledge.get(note.id)
    assert refreshed.status == "draft" and "HALF_UP" in refreshed.content and refreshed.anchor["commit"]  # re-noted, anchored to the new commit

    gate = next(i for i in await office.interactions.pending(ticket_id=ticket.id) if i.payload.get("gate") == "knowledge")
    await office.answer(gate.id, {"approved": True}, by="user")
    for _ in range(100):
        if any(t.startswith("KB · Billing") for t in wiki.pages):
            break
        import asyncio

        await asyncio.sleep(0.02)
    assert f"{ticket.key} · Sửa làm tròn" in wiki.pages and "HALF_UP" in wiki.pages[f"{ticket.key} · Sửa làm tròn"]
    assert "HALF_UP" in wiki.pages["KB · Billing · Ghi chú code"]
    assert (await office.tickets.get(ticket.id)).confluence_url
