"""Sample data for the temporary office that UI screenshots run against (SI-23).

The office runs it as the project's `ui.seed` command, in a fresh data folder given by AO_UI_DATA:

    uv run python scripts/ui_seed.py

It fills that folder (never ~/.actoroffice) with a team, two people and tickets at the stages the team UI shows:
understand, build (waiting at a gate card), pr (review findings, an AC [review], AC [ui] with a screenshot and one
with an environment reason), one done and one cancelled. Running it twice adds nothing.
"""

from __future__ import annotations

import asyncio
import json
import os
import struct
import sys
import zlib
from pathlib import Path

from actoroffice.app import Office
from actoroffice.config import load_settings, write_default_config
from actoroffice.domain.ids import utcnow
from actoroffice.domain.team import LOCAL_OWNER_ID
from actoroffice.domain.work import TicketStatus
from actoroffice.services.checks import CheckResult

REQUIREMENTS = """\
# Hoàn tiền cho đơn VIP

## Acceptance criteria

AC-1: Hoàn tiền trong 60 ngày
- Given một đơn VIP đặt 30 ngày trước
- When khách yêu cầu hoàn tiền
- Then đơn được hoàn đủ tiền

AC-2 [ui]: Nút hoàn tiền trên trang đơn
- Given trang chi tiết của một đơn VIP
- When nhân viên mở trang
- Then nút "Hoàn tiền" hiện ở góc phải

AC-3 [ui] [review]: Hộp xác nhận hoàn tiền
- Given trang chi tiết của một đơn VIP
- When nhân viên bấm "Hoàn tiền"
- Then hộp xác nhận hiện số tiền sẽ hoàn
- Cách kiểm: người duyệt xem ảnh hộp xác nhận
"""

HANDOFF = """\
# Handoff

| AC | Test / cách kiểm |
|---|---|
| AC-1 | test_ac1_refund_within_60_days |
| AC-2 | test_ac2_refund_button; ảnh ac-2 |
| AC-3 | không chụp được: máy build không có Chrome; người duyệt kiểm bằng tay |
"""

REVIEW = {
    "findings": [
        {"id": "F1", "severity": "medium", "file": "src/refund.py", "line": 42, "claim": "Không kiểm số tiền âm", "reply": {"action": "fixed", "note": "thêm kiểm tra"}},
    ]
}


def png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A plain one-colour PNG (no image library needed)."""
    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height)

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


async def seed(office: Office) -> None:
    if await office.db.fetchone("SELECT 1 FROM tickets WHERE stage IS NOT NULL LIMIT 1"):
        return
    teams = office.teams
    ba = await teams.create_user("Lan", roles=["ba"])
    dev = await teams.create_user("Minh", roles=["dev", "lead"])
    team = await teams.create_team("Web")
    await teams.add_member(team.id, ba.id, ["ba"])
    await teams.add_member(team.id, dev.id, ["dev", "lead"])

    async def ticket(title: str, stage: str) -> int:
        created = await office.tickets.create(title, description=title, created_by="user")
        await office.stages.start(created.id, template="code", by=LOCAL_OWNER_ID, team_id=team.id)
        await office.db.execute("UPDATE tickets SET stage = ? WHERE id = ?", (stage, created.id))
        await office.stages.ensure_run(await office.tickets.get(created.id))
        return created.id

    async def evidence(ticket_id: int) -> None:
        key = (await office.tickets.get(ticket_id)).key
        await office.tickets.put_document(ticket_id, "requirements", REQUIREMENTS, author="Copilot", base_revision=None)
        await office.tickets.put_document(ticket_id, "handoff", HANDOFF, author="Copilot", base_revision=None)
        folder = office.settings.data_dir / "artifacts" / key
        folder.mkdir(parents=True, exist_ok=True)
        shot = folder / "ac-2-nut-hoan-tien.png"
        shot.write_bytes(png(640, 400, (122, 162, 255)))
        await office.tickets.add_work_product(ticket_id, "screenshot", str(shot), {"note": "Nút Hoàn tiền trên trang đơn", "ac": "AC-2"})
        reason = "không tìm thấy Chrome hoặc Edge trên máy"
        await office.tickets.add_work_product(ticket_id, "ui_error", reason, {"ac": "AC-3", "reason": reason})

    await ticket("Thêm bộ lọc theo ngày cho danh sách đơn", "understand")

    build = await ticket("Hộp xác nhận hoàn tiền (thẻ duyệt sau build)", "build")
    await evidence(build)
    ticket_row = await office.tickets.get(build)
    stage = await office.stages.stage_def(ticket_row)
    checks = [CheckResult(id="ui_evidence", status="passed", summary="⚠️ không chụp được ảnh cho AC-3")]
    # the card a team with a gate rule on build sees (no default gate after build)
    await office.stages._open_gate(ticket_row, stage, "rules", "Thêm nút và hộp xác nhận hoàn tiền", checks, run_id=None)
    await office.db.execute("UPDATE tickets SET status = ? WHERE id = ?", (TicketStatus.IN_REVIEW.value, build))

    pr = await ticket("Hoàn tiền cho đơn VIP trong 60 ngày", "build")
    await evidence(pr)
    srun = await office.stages.ensure_run(await office.tickets.get(pr))
    data = {"id": "review_clean", "status": "passed", "summary": "1 điểm medium đã trả lời", "data": REVIEW}
    await office.db.execute(
        "UPDATE stage_runs SET finished_at = ?, outcome = 'completed', checks_json = ? WHERE id = ?", (utcnow(), json.dumps([data], ensure_ascii=False), srun.id)
    )
    await office.db.execute("UPDATE tickets SET stage = 'pr', status = ? WHERE id = ?", (TicketStatus.IN_REVIEW.value, pr))
    await office.stages.ensure_run(await office.tickets.get(pr))

    done = await ticket("Sửa lỗi chính tả trang đăng nhập", "close")
    await office.db.execute("UPDATE tickets SET status = ? WHERE id = ?", (TicketStatus.DONE.value, done))
    cancelled = await ticket("Xuất báo cáo PDF (huỷ)", "plan")
    await office.db.execute("UPDATE tickets SET status = ? WHERE id = ?", (TicketStatus.CANCELLED.value, cancelled))
    await proposals(office, team.id)
    await queue(office, team.id)
    await failed_run(office, await ticket("Gửi email xác nhận hoàn tiền (lượt lỗi mạng)", "build"))


async def failed_run(office: Office, ticket_id: int) -> None:
    """SI-38: a ticket whose last run failed on the API connection after all its automatic retries."""
    from actoroffice.domain.ids import new_id
    from actoroffice.domain.run import Run, RunKind, RunStatus
    from actoroffice.engine.failures import FailureKind, run_error

    ai = await office.ensure_ai()
    run_id = new_id("run")
    await office.runs.start(Run(id=run_id, actor_id=ai.id, kind=RunKind.TICKET, ticket_id=ticket_id))
    detail = "API Error: Unable to connect to API: Self-signed certificate detected"
    await office.runs.finish(run_id, RunStatus.FAILED, error=run_error(FailureKind.NETWORK, "Claude", 3, detail), error_kind=FailureKind.NETWORK)


async def queue(office: Office, team_id: str) -> None:
    """SI-31: a code project that runs one ticket at a time, with one ticket running and one waiting its turn."""
    from actoroffice.domain.work import Project, ProjectAccess

    # stored as is: the settings form only shows it, no git repo is needed (and none is made outside AO_UI_DATA)
    project = Project(id="prj-orders", name="orders", path=str(office.settings.data_dir.parent / "orders"), access=ProjectAccess.WORKTREE, team_id=team_id)
    await office.projects._insert(project)
    for title in ("Lọc đơn theo trạng thái", "Xuất danh sách đơn ra CSV (chờ ticket trước)"):
        created = await office.tickets.create(title, description=title, project_id=project.id, created_by="user")
        await office.stages.start(created.id, template="code", by=LOCAL_OWNER_ID, team_id=team_id)


PROPOSED = [
    {"title": "Hàng đợi tuần tự theo dự án", "why": "17 ticket viết tay chạy song song rồi xung đột (loop log)", "description": "AC-1: ticket cùng dự án chạy lần lượt", "workflow": "code"},
    {"title": "Thẻ đề xuất trên web team", "why": "team.js chưa có thẻ cho danh sách đề xuất", "description": "AC-1: thẻ liệt kê từng mục", "workflow": "quick", "depends_on": [1]},
    {"title": "Hướng dẫn dùng propose_tickets", "why": "README chưa nói cách duyệt danh sách", "description": "AC-1: README có mục mới"},
]


async def proposals(office: Office, team_id: str) -> None:
    """SI-24: a research ticket with a pending proposal card, the same card in the AI's chat, and a backlog."""
    from actoroffice.domain.chat import MessageKind

    ai = await office.ensure_ai()
    research = await office.tickets.create("Nghiên cứu: từ brainstorm tới backlog", description="Brainstorm", created_by="user")
    await office.stages.start(research.id, template="research", by=LOCAL_OWNER_ID, team_id=team_id)
    await office.db.execute("UPDATE tickets SET stage = 'produce' WHERE id = ?", (research.id,))
    await office.stages.ensure_run(await office.tickets.get(research.id))
    await office.proposals.propose(PROPOSED, actor_id=ai.id, actor_name=ai.name, run_id=None, ticket_id=research.id, conversation_id=None)

    dm = await office.chat.dm(ai.id)
    card, _ = await office.proposals.propose(PROPOSED, actor_id=ai.id, actor_name=ai.name, run_id=None, ticket_id=None, conversation_id=dm.id)
    await office.chat.post_system(dm.id, "Copilot đề xuất 3 ticket", kind=MessageKind.INTERACTION, meta={"interaction_id": card.id})

    # an approved list from an earlier chat: item 1 runs, items 2 and 3 wait in the Backlog
    earlier, _ = await office.proposals.propose(
        [{**p, "title": f"{p['title']} (đợt trước)"} for p in PROPOSED], actor_id=ai.id, actor_name=ai.name, run_id=None, ticket_id=None, conversation_id=None
    )
    await office.answer(earlier.id, {"approved": True}, by=LOCAL_OWNER_ID)


async def main() -> int:
    data = os.environ.get("AO_UI_DATA", "").strip()
    if not data:
        print("AO_UI_DATA is not set: this script fills a temporary data folder, never ~/.actoroffice", file=sys.stderr)
        return 2
    settings = load_settings(Path(data))
    write_default_config(settings)
    office = Office(settings)
    await office.open()
    try:
        await seed(office)
    finally:
        await office.close()
    print(f"seeded {settings.data_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
