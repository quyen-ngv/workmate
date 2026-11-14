"""The AI cross-check with a real agent: a design that passes every machine rule but does not say what its spec says.

    uv run python scenarios/crosscheck_live.py [--model kiro] [--minutes 8]

The spec asks for a limit of 9 refunds a day and a reason when a refund is refused. The design raises the limit to 12
and says nothing about the reason, while tracing to the right AC. The machine rules cannot see that; the cross-check
must. Then a corrected design must come back clean. Uses the runtime's credits.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from doc_ui_demo import analysis, design, make_repo, spec  # noqa: E402

from actoroffice.app import Office  # noqa: E402
from actoroffice.config import Settings  # noqa: E402
from actoroffice.domain.work import ProjectAccess  # noqa: E402
from actoroffice.services.checks import CheckContext  # noqa: E402


def log(text: str = "") -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


async def put(office: Office, ticket, key: str, body: str, *, approve: bool = False) -> None:
    current = await office.tickets.get_document(ticket.id, key)
    await office.tickets.put_document(ticket.id, key, body, author="Copilot", base_revision=current.revision if current else None)
    await office.docs.on_saved(await office.tickets.get(ticket.id), key)
    if approve:
        await office.db.execute("UPDATE documents SET approved_revision = revision WHERE ticket_id = ? AND key = ?", (ticket.id, key))


async def main(model: str, minutes: float) -> int:
    root = Path(tempfile.mkdtemp(prefix="ao_xcheck_"))
    office = Office(Settings(data_dir=root / "office", default_model=model, ai_model=model, doc_sweep_s=0))
    await office.open()
    await office.start_notifier()
    failures: list[str] = []
    try:
        await office.install_default_positions(skills=False)
        team = await office.teams.create_team("Pay")
        project = await office.projects.create("Shop", path=str(make_repo(root)), access=ProjectAccess.READ_ONLY)
        await office.projects.configure(project.id, team_id=team.id)
        epic = await office.tickets.create("Nâng hạn mức hoàn tiền", project_id=project.id)
        await put(office, epic, "analysis", analysis(epic.key), approve=True)
        task = await office.tickets.create("Nâng hạn mức lên 9", project_id=project.id, parent_id=epic.id)
        await put(office, task, "spec", spec(epic.key, "T-1"), approve=True)
        await office.db.execute("UPDATE tickets SET stage_template = 'task-spec', stage = 'design', team_id = ? WHERE id = ?", (team.id, task.id))
        await office.stages._open_run(task.id, "design")
        task = await office.tickets.get(task.id)
        stage = await office.stages.stage_def(task)

        wrong = (
            design(task.key, epic.key)
            .replace("Từ 5 lên 9.", "Từ 5 lên 12 cho mọi khách.")
            .replace("Thêm trường lý do vào câu trả lời.", "Ghi log nội bộ khi từ chối; câu trả lời cho khách không đổi.")
            .replace('Sau thay đổi, nó trả `{"ok": false, "reason": "daily_limit"}`.', "Sau thay đổi, câu trả lời vẫn như cũ.")
            .replace("có thêm trường `reason` (chuỗi, ví dụ `daily_limit`)", "không đổi")
        )
        await put(office, task, "design", wrong)
        rules = [f for f in await office.docs.check(task, stage) if f.severity == "error"]
        log(f"luật máy trên design sai: {len(rules)} lỗi (mong đợi 0: máy không thấy được chỗ lệch nghĩa)")
        if rules:
            failures.append(f"luật máy báo lỗi ngoài dự kiến: {[f.rule for f in rules]}")

        async def cross() -> tuple[object, list]:
            ctx = CheckContext(office, await office.tickets.get(task.id), stage, await office.stages.current_run(task), None, project, None)
            started = time.monotonic()
            result = await asyncio.wait_for(office.crosscheck.check(ctx), timeout=minutes * 60)
            found = await office.crosscheck.findings(await office.tickets.get(task.id))
            log(f"đối chiếu AI: {result.status} sau {time.monotonic() - started:.0f}s")
            for f in found:
                log(f"  - [{f.severity}] {f.rule} · {f.item}: {f.message[:260]}")
            return result, found

        result, found = await cross()
        text = " ".join(f.message for f in found)
        if result.status != "failed":
            failures.append(f"design sai mà check là {result.status}")
        if not any(f.severity == "error" for f in found):
            failures.append("không có phát hiện nào chặn")
        if "12" not in text:
            failures.append("không bắt được hạn mức 12 thay vì 9")
        if "lý do" not in text.lower():
            failures.append("không bắt được việc bỏ lý do từ chối")

        log("sửa design cho đúng nghiệp vụ rồi đối chiếu lại")
        await put(office, task, "design", design(task.key, epic.key))
        result, found = await cross()
        # A real model may still have remarks on a sample design. What must hold: the two planted differences are gone.
        planted = [f for f in found if f.severity == "error" and ("12" in f.message or "vẫn như cũ" in f.message or "không đổi" in f.message)]
        if result.status == "skipped" or planted:
            failures.append(f"sau khi sửa vẫn còn điểm lệch đã cài: {result.status}, {[f.message[:120] for f in planted]}")
        if any(f.severity == "error" for f in found):
            log("(bản đã sửa còn phát hiện khác, xem ở trên; kịch bản chỉ đòi hai điểm lệch cố ý biến mất)")
        runs = await office.runs.for_ticket(task.id, limit=10)
        log(f"lượt chạy đối chiếu: {[(r.status.value, r.error) for r in runs]}")
    finally:
        await office.close()
        shutil.rmtree(root, ignore_errors=True)
    print("\n== KẾT QUẢ: " + ("ĐẠT" if not failures else "KHÔNG ĐẠT: " + "; ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="kiro")
    parser.add_argument("--minutes", type=float, default=8)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(args.model, args.minutes)))
