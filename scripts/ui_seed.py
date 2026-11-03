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
