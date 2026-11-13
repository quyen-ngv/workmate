"""Doc Core with a real agent on a real repository (this one by default): an epic goes through `analyze` until the BA
gate opens. The agent reads the code through the code index, writes the `analysis` document and has it checked
(template, sources that exist, open questions). Uses the runtime's credits.

    uv run python scenarios/doc_flow_live.py --home F:/_ao_doc [--model kiro] [--minutes 20] [--repo F:/path]
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import time
from pathlib import Path

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.work import InteractionKind, ProjectAccess

TITLE = "Cho phép mỗi team chọn giai đoạn nào bắt buộc đọc nguồn"
DESCRIPTION = (
    "Hiện nay việc bắt AI đọc hết nguồn của ticket (check `sources_read`) chỉ chạy ở vài giai đoạn cố định trong code. "
    "Yêu cầu: (1) mỗi team cấu hình được danh sách giai đoạn bắt buộc đọc nguồn; (2) team không cấu hình gì thì giữ đúng "
    "hành vi hiện nay. BA: Lan."
)


def log(text: str = "") -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


async def main(home: Path, repo: Path, model: str, minutes: float) -> int:
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    office = Office(Settings(data_dir=home / "office", default_model=model, ai_model=model, approval_wait_s=5, max_concurrent_runs=1))
    await office.start()
    failures: list[str] = []
    try:
        team = await office.teams.create_team("Office")
        lan = await office.teams.create_user("Lan")
        await office.teams.add_member(team.id, lan.id, ["ba"])
        project = await office.projects.create("actor-office", path=str(repo), access=ProjectAccess.READ_ONLY)
        await office.projects.configure(project.id, team_id=team.id)
        epic = await office.tickets.create(TITLE, description=DESCRIPTION, project_id=project.id)
        epic = await office.stages.start(epic.id, template="epic", by="user", team_id=team.id, owner_user_id=lan.id)
        log(f"bắt đầu {epic.key} ở giai đoạn {epic.stage} với model {model}")
        deadline = time.monotonic() + minutes * 60
        last = None
        card = None
        while time.monotonic() < deadline and card is None:
            await asyncio.sleep(5)
            epic = await office.tickets.get(epic.id)
            status = await office.stages.status(epic)
            if (epic.stage, status["status"]) != last:
                last = (epic.stage, status["status"])
                log(f"giai đoạn {epic.stage}: {status['status']}")
            for item in await office.interactions.pending(ticket_id=epic.id):
                p = item.payload
                if item.kind == InteractionKind.QUESTION:
                    answer = p.get("recommended") or (p.get("options") or ["Đồng ý với đề xuất của bạn"])[0]
                    await office.answer(item.id, {"answer": answer}, by=lan.id)
                    log(f"Lan trả lời '{str(p.get('question'))[:90]}' → {answer[:70]}")
                    office.scheduler.kick()
                elif item.kind == InteractionKind.APPROVE_TOOL:
                    await office.answer(item.id, {"approved": False}, by="user")
                    log(f"từ chối tool {p.get('tool')} (bước này chỉ đọc)")
                elif item.kind == InteractionKind.APPROVE_STAGE:
                    card = item
            if status["status"] == "failed":
                log("lượt chạy lỗi; thử lại giai đoạn một lần")
                await office.stages.retry(epic.id, by="user")

        print("\n================ KẾT QUẢ ================")
        runs = await office.runs.for_ticket(epic.id, limit=20)
        tools: dict[str, int] = {}
        for run in runs:
            for a in await office.runs.activities(run.id):
                name = str((a.get("fields") or {}).get("tool") or "")
                if name and a.get("status") != "running":
                    tools[name] = tools.get(name, 0) + 1
        print("tool đã gọi:", dict(sorted(tools.items(), key=lambda x: -x[1])))
        for r in await office.stages.runs(epic.id):
            failed = [c["id"] for c in r.checks if c.get("status") == "failed"]
            print(f"- {r.stage} lần {r.attempt}: {r.outcome or 'đang mở'} · check đỏ lần cuối: {failed}")
        doc = await office.tickets.get_document(epic.id, "analysis")
        if doc is None:
            failures.append("không có tài liệu analysis")
        else:
            print(f"\n--- analysis (bản {doc.revision}) ---\n{doc.body}")
            findings = await office.docs.check(await office.tickets.get(epic.id))
            errors = [f for f in findings if f.status == "open" and f.severity == "error"]
            print(f"\nphát hiện còn mở: {len(errors)} lỗi, {len([f for f in findings if f.status == 'open']) - len(errors)} cảnh báo")
            for f in errors:
                print(f"  - {f.rule} {f.item}: {f.message}")
            rows = await office.db.fetchall("SELECT item_id, source, ref, version, error FROM doc_evidence WHERE ticket_id = ? AND revision = ?", (epic.id, doc.revision))
            print(f"bằng chứng đã ghim: {len(rows)}")
            for r in rows:
                print(f"  - {r['item_id']}: {r['source']} → {r['ref']} @ {str(r['version'])[:10]}" + (f"  LỖI: {r['error']}" if r["error"] else ""))
            if errors:
                failures.append(f"analysis còn {len(errors)} lỗi")
            if not rows:
                failures.append("không ghim được bằng chứng nào")
        if card is None:
            failures.append("gate analysis chưa mở trong thời gian cho phép")
        else:
            p = card.payload
            print(f"\nthẻ duyệt: gate {p.get('gate')} → {p.get('approver_names')} · check: {[(c['id'], c['status']) for c in p.get('checks', [])]}")
            print("trạng thái khớp:", p.get("doc_core", {}).get("states"))
            if p.get("gate") != "analysis" or p.get("approver_names") != ["Lan"]:
                failures.append(f"thẻ duyệt sai: {p.get('gate')} {p.get('approver_names')}")
        if not any("code_" in name for name in tools):
            failures.append("AI không dùng tool code_* nào")
        print("lượt chạy:", [(r.status.value, r.error) for r in runs])
    finally:
        await office.close()
    print("\n== KẾT QUẢ: " + ("ĐẠT" if not failures else "KHÔNG ĐẠT: " + "; ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--model", default="kiro")
    parser.add_argument("--minutes", type=float, default=20)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(Path(args.home), Path(args.repo), args.model, args.minutes)))
