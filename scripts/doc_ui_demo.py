"""A throwaway office that shows the Doc Core pages with sample data, on its own port and its own data folder (never
~/.actoroffice). No AI runs: the scheduler is not started.

    uv run python scripts/doc_ui_demo.py [--port 8766]

Open http://localhost:8766/login?t=doc-ui-demo-token-0123456789abcdef . It holds an epic waiting at the business gate
(tasks to keep, drop, hand to a developer), a task whose design waits for its developer to send it, a task at the
lead's gate with a difference the AI cross-check found, and a small indexed repository for the service map.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import uvicorn

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.ids import utcnow
from actoroffice.domain.work import ProjectAccess
from actoroffice.web.server import create_app

TOKEN = "doc-ui-demo-token-0123456789abcdef"
LOG = "\n## {n}. Change log\n| Phiên bản | Ngày | Thay đổi | Người |\n|---|---|---|---|\n| 1 | 2026-11-13 | Bản đầu | Lan |\n"

FILES = {
    "src/shop/__init__.py": "",
    "src/shop/limits.py": '"""Daily limits."""\n\nMAX_PER_DAY = 5\n\n\nclass LimitService:\n    def check(self, customer_id: int) -> bool:\n        return self._count(customer_id) < MAX_PER_DAY\n\n    def _count(self, customer_id: int) -> int:\n        return 0\n',  # noqa: E501
    "src/shop/refund.py": 'from fastapi import APIRouter\nimport httpx\n\nfrom shop.limits import LimitService\n\nrouter = APIRouter()\nlimit_service = LimitService()\n\n\n@router.post("/v1/refunds/{refund_id}")\nasync def create_refund(refund_id: int, customer_id: int) -> dict:\n    if not limit_service.check(customer_id):\n        return {"ok": False}\n    httpx.post("https://audit.internal/v1/events", json={"ref": refund_id})\n    return {"ok": True}\n',  # noqa: E501
    "pyproject.toml": '[project]\nname = "shop"\ndependencies = [\n    "fastapi",\n    "httpx",\n]\n',
}


def analysis(key: str) -> str:
    return f"""# Phân tích — {key} Hạn mức hoàn tiền
Phiên bản: 1 · Cập nhật: 2026-11-13 · Người viết: Lan

## 1. Phạm vi
- Nâng hạn mức hoàn tiền mỗi ngày.

## 2. Yêu cầu
### REQ-1: Hạn mức hoàn tiền mỗi ngày là 9
Lấy từ mô tả epic.

### REQ-2: Từ chối phải kèm lý do
Lấy từ mô tả epic.

## 3. Hiện trạng
### F-1: Hạn mức hiện là 5 lần mỗi ngày
Hằng số trong code.
Nguồn: src/shop/limits.py:3

## 4. Mâu thuẫn và thiếu
Không có.

## 5. Câu hỏi mở
### Q-1: Khách VIP có hạn mức riêng không?
Trả lời: Không, dùng chung.

## 6. Giả định
- A-1: Cách đếm số lần không đổi.
{LOG.format(n=7)}"""


def breakdown(key: str) -> str:
    return f"""# Chia task — {key} Hạn mức hoàn tiền
Phiên bản: 1 · Cập nhật: 2026-11-13 · Người viết: Lan

## 1. Mục tiêu
Theo bản phân tích 1.

## 2. Danh sách task
### T-1: Nâng hạn mức lên 9
Khách hoàn được tới 9 lần mỗi ngày.
Truy vết: REQ-1

### T-2: Lý do từ chối
Khách thấy lý do khi bị từ chối.
Truy vết: REQ-2

### T-3: Dọn hằng số cũ
Việc phụ.
Truy vết: REQ-1

## 3. Thứ tự và phụ thuộc
Không có.
{LOG.format(n=4)}"""


def spec(epic: str, task: str) -> str:
    return f"""# Nghiệp vụ — {epic} {task}
Phiên bản: 1 · Cập nhật: 2026-11-13 · Người viết: Lan

## 1. Mục tiêu
- Đáp ứng yêu cầu của epic {epic}.

## 2. User stories
### US-1: Hoàn tiền trong hạn mức
Là khách hàng, tôi muốn hoàn tiền nhiều lần hơn, để không phải chờ sang ngày.

- AC-1.1 (happy path) — Given khách đã hoàn 8 lần hôm nay When yêu cầu hoàn Then yêu cầu được chấp nhận
  Truy vết: {epic}/REQ-1
- AC-1.2 (từ chối) — Given khách đã hoàn 9 lần hôm nay When yêu cầu hoàn Then bị từ chối kèm lý do
  Truy vết: {epic}/REQ-2

## 3. Quy tắc nghiệp vụ
### BR-1: Hạn mức là 9 lần mỗi ngày
Áp cho mọi khách.
Truy vết: {epic}/REQ-1
Thay đổi: {epic}/F-1

## 4. Thay đổi so với hiện trạng
| Quy tắc | Hiện trạng | Sau khi làm |
|---|---|---|
| BR-1 | 5 lần | 9 lần |

## 5. Phụ thuộc
Không có.

## 6. Giả định
- A-1: Không đổi cách đếm.
{LOG.format(n=7)}"""


def design(key: str, epic: str) -> str:
    return f"""# Design — {key} Nâng hạn mức
Phiên bản: 1 · Cập nhật: 2026-11-13 · Người viết: Minh

## 1. Bối cảnh
Viết theo nghiệp vụ bản 1.

## 2. Thay đổi
### C-1: Đổi hằng số hạn mức
Từ 5 lên 9.
Truy vết: AC-1.1, BR-1
Nguồn: src/shop/limits.py:3
Thay đổi: {epic}/F-1

### C-2: Thông báo lý do từ chối
Thêm trường lý do vào câu trả lời.
Hiện tại `create_refund` trả `{{"ok": false}}` khi `LimitService.check` từ chối. Sau thay đổi, nó trả `{{"ok": false, "reason": "daily_limit"}}`.
Nguồn: src/shop/refund.py:10-15
Truy vết: AC-1.2

## 3. Mô hình dữ liệu
Không đổi.

## 4. Giao diện
`POST /v1/refunds/{{refund_id}}`: khi từ chối, câu trả lời có thêm trường `reason` (chuỗi, ví dụ `daily_limit`). Khi chấp nhận không đổi.

## 5. Quyết định kỹ thuật
### D-1: Giữ hạn mức là hằng số trong code
- Phương án: đưa vào cấu hình, hoặc giữ hằng số. Chọn giữ hằng số.

## 6. Kiểm thử
Unit cho AC-1.1 và AC-1.2.

## 7. Rủi ro
Không có.

## 8. Truy vết
| AC / BR | Thay đổi |
|---|---|
| AC-1.1 | C-1 |
| BR-1 | C-1 |
| AC-1.2 | C-2 |

## 9. Các bước làm
1. Sửa hằng số (C-1)
2. Thêm trường `reason` vào câu trả lời từ chối của `create_refund` (C-2)

## 10. Giả định
- A-1: Không đổi cách đếm sau khi nâng hạn mức.
{LOG.format(n=11)}"""


def make_repo(root: Path) -> Path:
    repo = root / "shop"
    for name, text in FILES.items():
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text(text, encoding="utf-8")
    for args in (["init", "-q", "-b", "main"], ["add", "-A"], ["-c", "user.name=demo", "-c", "user.email=demo@local", "commit", "-q", "-m", "seed"]):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    return repo


async def put(office: Office, ticket, key: str, body: str, *, approve: bool = False) -> None:
    await office.tickets.put_document(ticket.id, key, body, author="Copilot", base_revision=None)
    await office.docs.on_saved(await office.tickets.get(ticket.id), key)
    if approve:
        await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ? AND key = ?", (ticket.id, key))


async def seed(office: Office, root: Path) -> None:
    await office.install_default_positions(skills=False)
    team = await office.teams.create_team("Pay")
    people = {}
    for name, role in (("Lan", "ba"), ("Minh", "dev"), ("Hùng", "lead")):
        people[name] = await office.teams.create_user(name)
        await office.teams.add_member(team.id, people[name].id, [role])
    project = await office.projects.create("Shop", path=str(make_repo(root)), access=ProjectAccess.READ_ONLY)
    await office.projects.configure(project.id, team_id=team.id)

    async def task(epic, title: str, assigned: bool):
        child = await office.tickets.create(title, description=f"_Task của epic {epic.key}._", project_id=project.id, parent_id=epic.id)
        await put(office, child, "spec", spec(epic.key, "T-1"), approve=True)
        if assigned:
            await office.teams.set_participant(child.id, people["Minh"].id, ["dev"])
        child = await office.stages.start(child.id, template="task-spec", by="user", team_id=team.id, owner_user_id=people["Lan"].id)
        await put(office, child, "design", design(child.key, epic.key))
        return await office.tickets.get(child.id)

    # 1. an epic at the business gate: tasks to keep, drop and hand over
    epic = await office.tickets.create("Nâng hạn mức hoàn tiền", description="Hạn mức mỗi ngày lên 9; từ chối phải có lý do.", project_id=project.id)
    epic = await office.stages.start(epic.id, template="epic", by="user", team_id=team.id, owner_user_id=people["Lan"].id)
    await put(office, epic, "analysis", analysis(epic.key), approve=True)
    await office.db.execute("UPDATE tickets SET stage = 'breakdown' WHERE id = ?", (epic.id,))
    await office.stages._open_run(epic.id, "breakdown")
    epic = await office.tickets.get(epic.id)
    await put(office, epic, "breakdown", breakdown(epic.key))
    for t in ("T-1", "T-2", "T-3"):
        await put(office, epic, f"spec.{t}", spec(epic.key, t))
    await office.stages._open_gate(epic, await office.stages.stage_def(epic), "business", "3 task, mỗi task có nghiệp vụ riêng", [], run_id=None)
    await office.db.execute("UPDATE tickets SET status = 'in_review' WHERE id = ?", (epic.id,))

    # 2. an older epic with two tasks under design
    done = await office.tickets.create("Hạn mức theo loại khách", description="Epic mẫu đã chia task.", project_id=project.id)
    done = await office.stages.start(done.id, template="epic", by="user", team_id=team.id, owner_user_id=people["Lan"].id)
    await put(office, done, "analysis", analysis(done.key), approve=True)
    await office.db.execute("UPDATE tickets SET stage = 'close', status = 'blocked' WHERE id = ?", (done.id,))
    done = await office.tickets.get(done.id)

    # 2a. a design waiting for its developer to send it
    first = await task(done, "Nâng hạn mức lên 9", assigned=True)
    stage = await office.stages.stage_def(first)
    policy = await office.teams.gate_policy(first, "design")
    await office.stages._open_gate(first, stage, "design", "Design phủ mọi AC của spec", [], run_id=None, submit=await office.stages._submitters(first, policy))
    await office.db.execute("UPDATE tickets SET status = 'in_review' WHERE id = ?", (first.id,))

    # 2b. a design at the lead's gate with a difference the AI cross-check still sees
    second = await task(done, "Lý do từ chối", assigned=False)
    srun = await office.stages.current_run(second)
    finding = {
        "id": "x1f0a9c2d7e4b", "rule": "X-DESIGN-SPEC", "severity": "error", "doc": f"{second.key}/design", "item": "C-2",
        "message": "AC-1.2 yêu cầu trả lý do khi từ chối; C-2 chỉ thêm trường mà không nói lý do lấy từ đâu và hiện ở đâu cho khách.",
        "status": "open", "accepted_by": None, "reason": None,
    }  # fmt: skip
    await office.db.execute(
        "INSERT INTO doc_crosschecks(ticket_id, stage_run_id, input_hash, findings_json, created_at) VALUES (?, ?, 'demo', ?, ?)",
        (second.id, srun.id, json.dumps([finding], ensure_ascii=False), utcnow()),
    )
    await office.stages._open_gate(second, await office.stages.stage_def(second), "design", "Design phủ mọi AC; còn 1 điểm đối chiếu chưa xử lý", [], run_id=None)
    await office.db.execute("UPDATE tickets SET status = 'in_review' WHERE id = ?", (second.id,))
    for child in (first, second):
        await office.tickets.add_blocker(done.id, child.id)
    await office.codeintel.ensure(await office.codeintel.repos(team_id=team.id))


async def main(port: int) -> None:
    root = Path(tempfile.mkdtemp(prefix="ao_doc_ui_"))
    office = Office(Settings(data_dir=root / "office", ai_review=False, doc_sweep_s=0))
    await office.open()
    await office.start_notifier()
    try:
        await seed(office, root)
        print(f"Doc UI demo: http://localhost:{port}/login?t={TOKEN}", flush=True)
        server = uvicorn.Server(uvicorn.Config(create_app(office, TOKEN), host="127.0.0.1", port=port, log_level="warning"))
        await server.serve()
    finally:
        await office.close()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    asyncio.run(main(parser.parse_args().port))
