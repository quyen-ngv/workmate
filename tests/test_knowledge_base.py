"""Team knowledge base: typed items, verification, staleness after merges, Confluence publishing, known context."""

from __future__ import annotations

import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import ProjectAccess
from actoroffice.services.knowledge import KbError

from .fake_adapters import FakeWiki, connect, install, uninstall
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
    install()
    yield o
    uninstall()
    await o.close()


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
    await connect(office, "fake_wiki", write=True)
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
        if any(p["title"].startswith("KB · Billing") for p in FakeWiki.published):
            break
        import asyncio

        await asyncio.sleep(0.02)
    pages = {p["title"]: p for p in FakeWiki.published}
    assert f"{ticket.key} · Sửa làm tròn" in pages and "HALF_UP" in pages[f"{ticket.key} · Sửa làm tròn"]["body"]
    assert "HALF_UP" in pages["KB · Billing · Ghi chú code"]["body"] and pages["KB · Billing · Ghi chú code"]["space"] == "BIL"
    assert pages["KB · Billing · Ghi chú code"]["body"].startswith("# Ghi chú code"), "markdown goes to the adapter, which converts it"
    assert (await office.tickets.get(ticket.id)).confluence_url == "https://wiki.test/BIL/1"


async def test_publish_goes_through_a_wiki_connection_that_may_write(office: Office, caplog) -> None:
    team = await office.teams.create_team("Billing", confluence_space="BIL")
    other = await office.teams.create_team("Ledger")
    ticket = await office.tickets.create("Hoàn tiền", description="x")
    await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (team.id, ticket.id))
    await office.tickets.put_document(ticket.id, "dossier", "# Dossier\n\nVIP 60 ngày", author="Copilot", base_revision=None)
    caplog.set_level("INFO", logger="actoroffice.services.knowledge")

    assert await office.knowledge.publish(ticket.id) == [] and "chưa có kết nối wiki" in caplog.text, "nothing connected: skipped, with the reason"
    await connect(office, "fake_wiki")
    assert await office.knowledge.publish(ticket.id) == [] and "chỉ đọc" in caplog.text, "read-only: skipped, with the reason"
    await connect(office, "fake_wiki", scope=f"team:{other.id}", write=True, connection_id="wiki-ledger")
    assert await office.knowledge.publish(ticket.id) == [] and FakeWiki.published == [], "another team's wiki is not this ticket's"

    await connect(office, "fake_wiki", write=True)
    assert await office.knowledge.publish(ticket.id) == ["https://wiki.test/BIL/1"]
    assert FakeWiki.published == [{"space": "BIL", "title": f"{ticket.key} · Hoàn tiền", "body": "# Dossier\n\nVIP 60 ngày\n\n", "parent_id": None}]
    assert (await office.tickets.get(ticket.id)).confluence_url == "https://wiki.test/BIL/1"

    loose = await office.tickets.create("Không team", description="x")
    await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (other.id, loose.id))  # a team with no space
    assert await office.knowledge.publish(loose.id) == [] and "chưa khai space" in caplog.text
