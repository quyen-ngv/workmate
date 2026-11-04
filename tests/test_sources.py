"""SI-39: sources of a ticket — found in its description, read (from the activity) or skipped, shown on the web."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.ids import new_id
from actoroffice.domain.org import Actor
from actoroffice.domain.run import Run, RunKind, TurnResult, TurnStatus, Usage, WakeRequest, WakeSource
from actoroffice.domain.sources import parse_refs
from actoroffice.domain.stage import StageRun, default_templates
from actoroffice.domain.work import Ticket
from actoroffice.engine import prompt_pack
from actoroffice.services.jira import JiraClient, JiraMirror
from actoroffice.tools.office_tools import ticket_create
from actoroffice.tools.registry import ToolCtx, ToolError
from actoroffice.tools.stage_tools import source_skip
from actoroffice.web.server import create_app

from .fakes import FakeRuntime, TurnCtx
from .seed import seed_db
from .test_jira import FakeJira, adf
from .test_web import HEADERS, TOKEN, login


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, key_prefix="SI"), runtime=runtime)
    seed_db(o.settings.db_path)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


async def sources(office: Office, ticket: Ticket) -> list[dict[str, Any]]:
    return await office.sources.list(await office.tickets.get(ticket.id))


async def ctx_for(office: Office, ticket_id: int) -> ToolCtx:
    ai = await office.ensure_ai()
    position = await office.org.get_position(ai.position_id)
    run = await office.runs.start(Run(id=new_id("run"), actor_id=ai.id, kind=RunKind.TICKET, ticket_id=ticket_id))
    return ToolCtx(office, ai, position, run_id=run.id, ticket_id=ticket_id)


async def tool_call(office: Office, ticket_id: int, tool: str, args: dict[str, Any], *, error: str | None = None) -> None:
    """One tool call of a run of the ticket, recorded the way the runtime reports it (start, then result)."""
    ai = await office.ensure_ai()
    run = await office.runs.start(Run(id=new_id("run"), actor_id=ai.id, kind=RunKind.TICKET, ticket_id=ticket_id))
    await office.runs.event(run.id, "tool_use", {"tool": tool, "input": args, "id": "tu1"})
    await office.runs.event(run.id, "tool_result", {"id": "tu1", "text": error or "ok", "error": bool(error)})


async def staged(office: Office, title: str, description: str, *, template: str = "code", team_id: str | None = None) -> Ticket:
    """A ticket on a workflow (no run: the scheduler is never ticked here)."""
    ticket = await office.tickets.create(title, description=description)
    return await office.stages.start(ticket.id, template=template, by="user", team_id=team_id)


async def run_check(office: Office, ticket: Ticket) -> Any:
    ticket = await office.tickets.get(ticket.id)
    stage = await office.stages.stage_def(ticket)
    srun = await office.stages.ensure_run(ticket)
    [result] = await office.stages.checks.run(ticket, stage, srun, workspace=None, run_id=None, only=["sources_read"])
    return result


def stage_prompt(ticket: Ticket, srcs: list[dict[str, Any]], template: str = "code", stage_id: str = "understand") -> str:
    tpl = default_templates()[template]
    stage = tpl.stage(stage_id)
    assert stage is not None
    return prompt_pack.build(
        actor=Actor(id="copilot", position_id="ai-engineer", name="Copilot"),
        ticket=ticket,
        template=tpl,
        stage=stage,
        stage_run=StageRun(id=1, ticket_id=ticket.id, stage=stage_id, attempt=1, started_at=""),
        language="Vietnamese",
        team_name=None,
        people=[],
        project_line="",
        dossier="",
        guidelines=[],
        memories=[],
        skills=[],
        wake=WakeRequest(id=1, actor_id="copilot", source=WakeSource.ASSIGNMENT),
        resumed=False,
        answered=[],
        new_comments=[],
        changes=[],
        sources=srcs,
    ).system


# ---- AC-1, AC-2, AC-3, AC-13: found and kept ------------------------------------------------------------------


async def test_ac1_parse_and_store_sources(office: Office) -> None:
    await office.teams.create_team("Payments", jira_project_key="PAY")
    other = await office.tickets.create("Đơn hàng", description="")
    text = (
        f"Theo {other.key}, PAY-7, docs/x.md §5.5, src/a/b.py:120, src/c.py#L10-L20, https://example.com/a. "
        "Không phải nguồn: AC-1, UTF-8, R-12, ISO-8601, SI-999."
    )
    ticket = await office.tickets.create("Nguồn", description=text)
    got = [(s["kind"], s["ref"], s["locator"]) for s in await sources(office, ticket)]
    assert got == [
        ("ticket", other.key, ""),
        ("jira", "PAY-7", ""),
        ("file", "docs/x.md", "§5.5"),
        ("file", "src/a/b.py", ":120"),
        ("file", "src/c.py", "#L10-L20"),
        ("url", "https://example.com/a", ""),
    ]


async def test_ac1_ignores_codes_without_ticket_or_jira_project(office: Office) -> None:
    """No fixed list of strings to skip: a key is a source only when the ticket exists or its prefix is a Jira project."""
    await office.tickets.create("Ticket có thật", description="")  # SI-1
    ticket = await office.tickets.create("t", description="R-12 ISO-8601 F1-2 SHA-256 SI-999 AC-1 và SI-1")
    refs = [s["ref"] for s in await sources(office, ticket)]
    assert refs == ["SI-1"]
    assert all(r.kind == "code" for r in parse_refs("R-12 ISO-8601"))  # candidates only: the service decides
    await office.teams.create_team("Rules", jira_project_key="R")  # once R is a Jira project, R-12 is a Jira key
    await office.sources.sync(ticket.id)
    assert [s["ref"] for s in await sources(office, ticket)] == ["R-12", "SI-1"]


async def test_ac2_web_api_create_has_sources(tmp_path) -> None:
    office = Office(Settings(data_dir=tmp_path / "home"), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        seed_db(office.settings.db_path)
        await office.open()
        yield
        await office.db.close()

    app.router.lifespan_context = lifespan
    with TestClient(app, base_url="http://localhost:8765") as c:
        login(c)
        task = c.post("/api/tasks", json={"title": "Web", "description": "Đọc docs/x.md", "template": "code"}, headers=HEADERS).json()
        listed = c.get(f"/api/tickets/{task['key']}/sources").json()
    assert [(s["kind"], s["ref"], s["status"]) for s in listed] == [("file", "docs/x.md", "unread")]


async def test_ac2_ticket_create_tool_has_sources(office: Office) -> None:
    parent = await office.tickets.create("Cha", description="")
    out = await ticket_create({"title": "Con", "description": "Đọc docs/x.md"}, await ctx_for(office, parent.id))
    key = out.split()[1]
    assert [s["ref"] for s in await sources(office, await office.tickets.get(key))] == ["docs/x.md"]


async def test_ac2_jira_import_has_sources(office: Office) -> None:
    fake = FakeJira()
    office.jira = JiraMirror(office, JiraClient("https://acme.atlassian.net", "bot@acme.vn", "tok", transport=httpx.MockTransport(fake.handler)))
    await office.teams.create_team("Billing", jira_project_key="BIL")
    fake.add("BIL-7", "Hoàn tiền", adf("Đọc docs/x.md"), ["ai"])
    [key] = await office.jira.sync()
    assert [s["ref"] for s in await sources(office, await office.tickets.get(key))] == ["docs/x.md"]


async def test_ac3_description_change_keeps_status(office: Office) -> None:
    fake = FakeJira()
    office.jira = JiraMirror(office, JiraClient("https://acme.atlassian.net", "bot@acme.vn", "tok", transport=httpx.MockTransport(fake.handler)))
    await office.teams.create_team("Billing", jira_project_key="BIL")
    fake.add("BIL-7", "Hoàn tiền", adf("Đọc docs/a.md"), ["ai"])
    [key] = await office.jira.sync()
    ticket = await office.tickets.get(key)
    await tool_call(office, ticket.id, "Read", {"file_path": "C:\\ws\\SI-1\\docs\\a.md"})
    assert [s["status"] for s in await sources(office, ticket)] == ["read"]

    fake.add("BIL-7", "Hoàn tiền", adf("Đọc docs/a.md và docs/b.md"), ["ai"])
    await office.jira.sync()
    got = {s["ref"]: s["status"] for s in await sources(office, ticket)}
    assert got == {"docs/a.md": "read", "docs/b.md": "unread"}

    fake.add("BIL-7", "Hoàn tiền", adf("Chỉ còn docs/b.md"), ["ai"])
    await office.jira.sync()
    assert [s["ref"] for s in await sources(office, ticket)] == ["docs/b.md"]  # a source gone is hidden


async def test_ac13_old_ticket_sources_on_next_run(office: Office, runtime: FakeRuntime) -> None:
    ticket = await staged(office, "Cũ", "Theo docs/old.md")
    await office.db.execute("DELETE FROM ticket_sources WHERE ticket_id = ?", (ticket.id,))
    await office.db.execute("UPDATE tickets SET sources_at = NULL WHERE id = ?", (ticket.id,))  # as before the migration
    assert await office.db.fetchone("SELECT 1 FROM ticket_sources WHERE ticket_id = ?", (ticket.id,)) is None

    await office.scheduler.wait_idle(timeout=20)
    rows = await office.db.fetchall("SELECT ref FROM ticket_sources WHERE ticket_id = ?", (ticket.id,))
    assert [r["ref"] for r in rows] == ["docs/old.md"]
    assert "docs/old.md — chưa đọc" in runtime.specs[0].system_prompt


# ---- AC-4 … AC-8: the check -------------------------------------------------------------------------------------


async def test_ac4_sources_read_fails_on_unread_ticket(office: Office) -> None:
    other = await office.tickets.create("Luật hoàn tiền", description="")
    ticket = await staged(office, "Hoàn tiền", f"Theo {other.key} và docs/x.md")
    await tool_call(office, ticket.id, "Read", {"file_path": "/ws/repo/docs/x.md"})
    result = await run_check(office, ticket)
    assert result.status == "failed"
    assert other.key in result.summary and "docs/x.md" not in result.summary


async def test_ac5_sources_read_passes_from_activity(office: Office) -> None:
    other = await office.tickets.create("Luật hoàn tiền", description="")
    ticket = await staged(office, "Hoàn tiền", f"Theo {other.key} và docs/x.md")
    await tool_call(office, ticket.id, "ticket_get", {"ticket": other.key})
    await tool_call(office, ticket.id, "Bash", {"command": "sed -n '1,40p' docs/x.md"})
    result = await run_check(office, ticket)
    assert result.status == "passed", result.summary
    assert {s["status"] for s in await sources(office, ticket)} == {"read"}


@pytest.mark.parametrize(
    "command",
    [
        "ls docs/x.md", "git add docs/x.md", "rm docs/x.md", "wc -l docs/x.md", "sed -i s/a/b/ docs/x.md", "echo docs/x.md",
        # a read command chained with another one: never taken apart (review F1, round 2)
        "cat README.md; git add docs/x.md", "cat a.txt && rm docs/x.md", "cat docs/x.md > /dev/null", "cat a.txt | tee docs/x.md",
        "cat a.txt\ngit add docs/x.md", "cat $(echo docs/x.md)", "cat `echo docs/x.md`", "cat a.txt || rm docs/x.md",
    ],
)
async def test_ac5_commands_that_do_not_read_the_file_do_not_count(office: Office, command: str) -> None:
    ticket = await staged(office, "Hoàn tiền", "Theo docs/x.md")
    await tool_call(office, ticket.id, "Bash", {"command": command})
    assert [s["status"] for s in await sources(office, ticket)] == ["unread"]
    assert (await run_check(office, ticket)).status == "failed"


@pytest.mark.parametrize("command", ["cat docs/x.md", "head -n 50 ./docs/x.md", "tail docs/x.md", "Get-Content docs\\x.md"])
async def test_ac5_commands_that_print_the_file_count(office: Office, command: str) -> None:
    ticket = await staged(office, "Hoàn tiền", "Theo docs/x.md")
    await tool_call(office, ticket.id, "Bash", {"command": command})
    assert [s["status"] for s in await sources(office, ticket)] == ["read"]


async def test_ac5_source_skip_needs_reason(office: Office) -> None:
    other = await office.tickets.create("Luật hoàn tiền", description="")
    ticket = await staged(office, "Hoàn tiền", f"Theo {other.key}")
    ctx = await ctx_for(office, ticket.id)
    with pytest.raises(ToolError, match="reason"):
        await source_skip({"ref": other.key, "reason": "   "}, ctx)
    assert [s["status"] for s in await sources(office, ticket)] == ["unread"]
    with pytest.raises(ToolError, match="not a source"):
        await source_skip({"ref": "docs/none.md", "reason": "x"}, ctx)

    out = await source_skip({"ref": other.key, "reason": "ticket cũ, đã thay bằng spec"}, ctx)
    assert other.key in out
    [src] = await sources(office, ticket)
    assert (src["status"], src["label"], src["reason"]) == ("skipped", "không liên quan", "ticket cũ, đã thay bằng spec")
    assert (await run_check(office, ticket)).status == "passed"
    # reading it later does not turn a skipped source into read, and skip never writes "read"
    await tool_call(office, ticket.id, "ticket_get", {"ticket": other.key})
    assert [s["status"] for s in await sources(office, ticket)] == ["skipped"]


async def test_ac6_quick_scope_runs_sources_read(office: Office) -> None:
    assert "sources_read" in default_templates()["quick"].stage("scope").checks
    ticket = await staged(office, "Sửa nhỏ", "Lỗi ở src/a.py:12", template="quick")
    await office.tickets.put_document(ticket.id, "plan", "AC-1: Given x When y Then z", author="Copilot", base_revision=None)
    ctx = await ctx_for(office, ticket.id)
    result = await office.stages.complete(ticket.id, run_id=ctx.run_id, summary="xong", workspace=None)
    checks = {c["id"]: c for c in (await office.stages.runs(ticket.id))[-1].checks}
    assert checks["sources_read"]["status"] == "failed" and "src/a.py:12" in checks["sources_read"]["summary"]
    assert result


async def test_ac7_external_and_other_team_not_required_nor_leaked(office: Office, tmp_path) -> None:
    web = await office.teams.create_team("Web")
    pay = await office.teams.create_team("Payments")
    secret = await office.tickets.create("Bí mật đội Payments", description="Mô tả nội bộ của Payments")
    await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (pay.id, secret.id))
    ticket = await staged(office, "Đơn", f"Xem https://example.com/a và {secret.key}", team_id=web.id)

    result = await run_check(office, ticket)
    assert result.status == "passed", result.summary
    listed = await sources(office, ticket)
    assert [(s["status"], s["label"], s["required"]) for s in listed] == [
        ("external", "không đọc (URL ngoài)", False),
        ("out_of_scope", "ngoài phạm vi", False),
    ]
    assert listed[1]["title"] is None and listed[1]["link"] is None
    prompt = stage_prompt(await office.tickets.get(ticket.id), listed)
    assert secret.key in prompt and "Bí mật" not in prompt and "Mô tả nội bộ" not in prompt
    assert "Payments" not in str(listed)


async def test_ac8_read_error_recorded(office: Office) -> None:
    ticket = await staged(office, "Thiếu file", "Theo docs/missing.md")
    await tool_call(office, ticket.id, "Read", {"file_path": "/ws/docs/missing.md"}, error="File does not exist.")
    [src] = await sources(office, ticket)
    assert (src["status"], src["label"], src["error"]) == ("error", "không đọc được (lỗi)", "File does not exist.")
    assert (await run_check(office, ticket)).status == "passed"
    await tool_call(office, ticket.id, "Read", {"file_path": "/ws/docs/missing.md"})  # read later: read wins
    assert [s["status"] for s in await sources(office, ticket)] == ["read"]


async def test_ac8_failed_read_of_a_file_that_exists_stays_unread(office: Office) -> None:
    """Review F4: Read on the wrong root fails, but the file is in the workspace: not "could not be read"."""
    ticket = await staged(office, "Sai gốc", "Theo docs/x.md")
    (office.settings.workspaces_dir / ticket.key / "docs").mkdir(parents=True)
    (office.settings.workspaces_dir / ticket.key / "docs" / "x.md").write_text("# X\n", encoding="utf-8")
    await tool_call(office, ticket.id, "Read", {"file_path": "/tmp/x/docs/x.md"}, error="File does not exist.")
    assert [s["status"] for s in await sources(office, ticket)] == ["unread"]
    assert (await run_check(office, ticket)).status == "failed"
    # an error recorded while the file was missing goes back to unread once the file is there
    other = await staged(office, "Thiếu rồi có", "Theo docs/y.md")
    await tool_call(office, other.id, "Read", {"file_path": "/ws/docs/y.md"}, error="File does not exist.")
    assert [s["status"] for s in await sources(office, other)] == ["error"]
    (office.settings.workspaces_dir / other.key / "docs").mkdir(parents=True)
    (office.settings.workspaces_dir / other.key / "docs" / "y.md").write_text("# Y\n", encoding="utf-8")
    assert [s["status"] for s in await sources(office, other)] == ["unread"]


async def test_ac8_jira_and_confluence_reads(office: Office) -> None:
    team = await office.teams.create_team("Billing", jira_project_key="BIL")
    office.jira = JiraMirror(office, JiraClient("https://acme.atlassian.net", "bot", "tok", transport=httpx.MockTransport(FakeJira().handler)))
    ticket = await staged(office, "Jira", "BIL-3 và https://acme.atlassian.net/wiki/spaces/X/pages/12345/Spec", team_id=team.id)
    listed = await sources(office, ticket)
    assert [(s["kind"], s["required"]) for s in listed] == [("jira", True), ("url", True)]  # the site's own URL is internal
    await tool_call(office, ticket.id, "jira_issue", {"key": "BIL-3"})
    await tool_call(office, ticket.id, "wiki_page", {"id": "12345"}, error="Confluence is not connected.")
    got = {s["kind"]: (s["status"], s["error"]) for s in await sources(office, ticket)}
    assert got == {"jira": ("read", None), "url": ("error", "Confluence is not connected.")}


# ---- AC-9, AC-10: the prompt ------------------------------------------------------------------------------------


async def test_ac9_first_run_gets_human_comments_only(office: Office, runtime: FakeRuntime) -> None:
    ticket = await office.tickets.create("Bổ sung", description="Làm theo spec")
    await office.tickets.add_comment(ticket.id, "Giữ nguyên phần vị trí §5.5", author_kind="user", author_id="owner")
    await office.tickets.add_comment(ticket.id, "Ghi chú duyệt cổng của hệ thống", author_kind="system", author_id=None)
    seen: list[str] = []

    async def turn(ctx: TurnCtx) -> TurnResult:
        seen.append(ctx.spec.prompt)
        return TurnResult(status=TurnStatus.COMPLETED, session_id="s1", usage=Usage(input_tokens=1, output_tokens=1))

    runtime.scripts.append(turn)
    await office.stages.start(ticket.id, template="code", by="user")
    await office.scheduler.wait_idle(timeout=20)
    assert "Giữ nguyên phần vị trí §5.5" in seen[0]
    assert "Ghi chú duyệt cổng" not in seen[0] and "Bắt đầu quy trình" not in seen[0]
    comments = [s for s in await sources(office, ticket) if s["kind"] == "comment"]
    assert [(s["status"], s["label"]) for s in comments] == [("read", "đã đọc")]
    assert "Giữ nguyên phần vị trí" in comments[0]["title"]


async def test_ac10_understand_prompt_lists_sources_and_conflict_rule(office: Office) -> None:
    ticket = await office.tickets.create("Nguồn", description="Xem docs/x.md:120")
    listed = await sources(office, ticket)
    system = stage_prompt(ticket, listed)
    assert "# Sources of this ticket" in system and "- [file] docs/x.md:120 — chưa đọc" in system
    flat = " ".join(system.split())
    assert "Write every source in dossier section links" in flat
    assert "says which one you follow and why: by default the newer one; when unsure, ask people" in flat
    quick = " ".join(stage_prompt(ticket, listed, "quick", "scope").split())
    assert "source_skip(ref, reason)" in quick and "docs/x.md:120" in quick
    build = stage_prompt(ticket, listed, "code", "build")
    assert "Sources of this ticket" not in build and "source_skip" not in build


# ---- AC-11: the web tab -----------------------------------------------------------------------------------------


@pytest.fixture
def office_client(tmp_path):
    office = Office(Settings(data_dir=tmp_path / "home", key_prefix="SI"), runtime=FakeRuntime(scripts=[]))
    app = create_app(office, TOKEN)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        seed_db(office.settings.db_path)
        await office.open()
        yield
        await office.db.close()

    app.router.lifespan_context = lifespan
    with TestClient(app, base_url="http://localhost:8765") as c:
        yield office, c


def test_ac11_sources_api_and_file(office_client) -> None:
    office, c = office_client
    login(c)
    other = c.post("/api/tasks", json={"title": "Luật", "description": "", "template": "code"}, headers=HEADERS).json()
    task = c.post(
        "/api/tasks", json={"title": "Nguồn", "description": f"src/a/b.py:120, {other['key']}, https://example.com/a", "template": "code"}, headers=HEADERS
    ).json()
    folder = office.settings.workspaces_dir / task["key"]
    (folder / "src" / "a").mkdir(parents=True)
    (folder / "src" / "a" / "b.py").write_text("x = 1\n" * 130, encoding="utf-8")
    (folder / "secret.txt").write_text("no", encoding="utf-8")

    async def read_file() -> None:
        ticket = await office.tickets.get(task["key"])
        await tool_call(office, ticket.id, "Read", {"file_path": str(folder / "src" / "a" / "b.py"), "offset": 100})

    c.portal.call(read_file)
    listed = c.get(f"/api/tickets/{task['key']}/sources").json()
    assert [(s["kind"], s["ref"], s["locator"], s["display"], s["label"]) for s in listed] == [
        ("file", "src/a/b.py", ":120", "src/a/b.py:120", "đã đọc"),
        ("ticket", other["key"], "", other["key"], "chưa đọc"),
        ("url", "https://example.com/a", "", "https://example.com/a", "không đọc (URL ngoài)"),
    ]
    assert listed[1]["title"] == "Luật" and listed[1]["link"] == other["key"]

    got = c.get(f"/api/tickets/{task['key']}/sources/file", params={"path": "src/a/b.py"})
    assert got.status_code == 200 and got.json()["text"].count("\n") == 130
    assert c.get(f"/api/tickets/{task['key']}/sources/file", params={"path": "secret.txt"}).status_code == 403  # not a source
    assert c.get(f"/api/tickets/{task['key']}/sources/file", params={"path": "../x/src/a/b.py"}).status_code == 403


async def test_ac11_file_outside_root_is_refused(office: Office, tmp_path) -> None:
    ticket = await office.tickets.create("Trốn", description="Xem ../../etc/passwd.txt và a/../../../out.txt")
    refs = [s["ref"] for s in await sources(office, ticket)]
    root = office.settings.workspaces_dir / ticket.key
    root.mkdir(parents=True)
    for ref in refs:
        with pytest.raises(Exception, match="ngoài thư mục|không phải nguồn"):
            await office.sources.file_text(ticket, ref)


# ---- AC-12: similar work ----------------------------------------------------------------------------------------


async def test_ac12_similar_search_uses_source_titles(office: Office) -> None:
    other = await office.tickets.create("Đối soát chargeback Napas", description="")
    item = await office.knowledge.note(kind="fact", title="Quy tắc chargeback Napas", content="Napas chargeback trong 45 ngày", team_id=None, by="Lan")
    filler = "Mô tả dài về giao diện danh sách đơn hàng và bộ lọc. " * 10
    assert len(filler) > 400
    ticket = await office.tickets.create("Sửa màn hình", description=f"{filler} Xem thêm {other.key}.")
    assert item.id not in [i.id for i in await office.knowledge.search(f"{ticket.title} {ticket.description[:400]}", team_id=None)]
    found = await office.knowledge.for_ticket(await office.tickets.get(ticket.id))
    assert item.id in [i.id for i in found]
