"""Seed a demo office for the team UI (no LLM calls): a team, people, rules, knowledge and tickets at different
stages with activity, a gate card and a question.

    uv run py scenarios/team_demo.py --home F:/_ao_demo
    ACTOROFFICE_HOME=F:/_ao_demo uv run actoroffice serve --no-browser   # then open /team
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from actoroffice.app import Office
from actoroffice.config import load_settings, write_default_config
from actoroffice.domain.ids import new_id
from actoroffice.domain.run import Run, RunKind, RunStatus, Usage
from actoroffice.domain.work import InteractionKind


async def main(home: Path) -> None:
    settings = load_settings(home)
    write_default_config(settings)
    office = Office(settings)
    await office.open()
    try:
        ai = await office.ensure_ai()
        team = await office.teams.create_team("Billing", jira_project_key="BIL", confluence_space="BIL")
        lan = await office.teams.create_user("Lan", telegram_id=111, email="lan@acme.vn")
        minh = await office.teams.create_user("Minh", telegram_id=222)
        hoa = await office.teams.create_user("Hoa", telegram_id=333)
        await office.teams.add_member(team.id, lan.id, ["ba"])
        await office.teams.add_member(team.id, minh.id, ["dev", "lead"])
        await office.teams.add_member(team.id, hoa.id, ["qe"])
        await office.rules.propose(title="Test Java", body="Dùng JUnit 5 + AssertJ; tên test chứa AC id.", kind="guideline", scope=f"team:{team.id}", stage="build", by="user", activate=True)
        await office.rules.propose(title="Không force push", body="Không ghi đè lịch sử", kind="policy", scope="global", spec={"deny_patterns": ["git push .*--force", "--no-verify"]}, by="user", activate=True)
        await office.knowledge.note(kind="fact", title="Hạn hoàn tiền", content="Đơn thường 30 ngày, VIP 60 ngày", by="Copilot", team_id=team.id, sources=["Lan, AO-1"])

        # 1) a ticket at the build stage, working, with steps and activity
        t1 = await office.tickets.create("Hoàn tiền cho đơn VIP trong 60 ngày", description="Khách VIP được hoàn tiền trong 60 ngày.")
        t1 = await office.stages.start(t1.id, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
        await office.db.execute("UPDATE tickets SET stage = 'build', jira_key = 'BIL-7', jira_url = 'https://acme.atlassian.net/browse/BIL-7' WHERE id = ?", (t1.id,))
        await office.wakes.drop((await office.wakes.pending_for(ai.id, t1.id)).id, "demo")
        t1 = await office.tickets.get(t1.id)
        srun = await office.stages.ensure_run(t1)
        await office.stages.set_steps(srun.id, ["Viết test AC-1, AC-2", "Thêm RefundPolicy", "Chạy mvn verify", "Viết handoff"])
        await office.stages.step_done(srun.id, 1)
        await office.stages.step_done(srun.id, 2)
        run_id = new_id("run")
        await office.runs.start(Run(id=run_id, actor_id=ai.id, kind=RunKind.TICKET, ticket_id=t1.id))
        events = [
            ("text", {"text": "Đọc RefundService để biết luật hiện tại trước khi thêm VIP."}),
            ("tool_use", {"tool": "Read", "input": {"file_path": "src/main/java/acme/refund/RefundService.java"}, "id": "a"}),
            ("tool_result", {"id": "a", "error": False, "text": "..."}),
            ("tool_use", {"tool": "Grep", "input": {"pattern": "refundWindow"}, "id": "b"}),
            ("tool_result", {"id": "b", "error": False, "text": "Found 4 files"}),
            ("tool_use", {"tool": "Read", "input": {"file_path": "src/main/java/acme/refund/RefundPolicy.java"}, "id": "c"}),
            ("tool_result", {"id": "c", "error": False, "text": "..."}),
            ("tool_use", {"tool": "Edit", "input": {"file_path": "src/main/java/acme/refund/RefundPolicy.java", "old_string": "30", "new_string": "customer.isVip() ? 60 : 30\n"}, "id": "d"}),
            ("tool_result", {"id": "d", "error": False, "text": "ok"}),
            ("tool_use", {"tool": "Bash", "input": {"command": "mvn -q test -pl billing"}, "id": "e"}),
            ("tool_result", {"id": "e", "error": True, "text": "Tests run: 12, Failures: 1\nRefundPolicyTest.ac2_vip_after_60_days"}),
            ("text", {"text": "AC-2 lỗi: tính ngày theo ngày đặt hàng thay vì ngày giao. Sửa lại theo dossier."}),
            ("tool_use", {"tool": "Bash", "input": {"command": "mvn -q test -pl billing"}, "id": "f"}),
        ]
        for kind, payload in events:
            await office.runs.event(run_id, kind, payload)
        await office.db.execute("UPDATE tickets SET status = 'in_progress', checkout_run_id = ? WHERE id = ?", (run_id, t1.id))
        await office.stages.add_usage(srun.id, Usage(input_tokens=48_000, output_tokens=6_200), 0.21, None)

        # 2) a ticket waiting for the BA at the requirement gate
        t2 = await office.tickets.create("Xuất báo cáo đối soát theo ngày", description="Kế toán cần file đối soát hằng ngày.")
        t2 = await office.stages.start(t2.id, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
        await office.wakes.drop((await office.wakes.pending_for(ai.id, t2.id)).id, "demo")
        await office.tickets.put_document(t2.id, "requirements", "## AC\n- AC-1: Given ngày D, When kế toán tải báo cáo, Then có đủ giao dịch D.", author="Copilot", base_revision=None)
        await office.tickets.put_document(t2.id, "dossier", "# Dossier\n## Bối cảnh\nKế toán đối soát thủ công.", author="Copilot", base_revision=None)
        stage = await office.stages.stage_def(await office.tickets.get(t2.id))
        from actoroffice.services.checks import CheckResult

        await office.stages._open_gate(
            await office.tickets.get(t2.id), stage, "requirement", "1 AC, phạm vi: giao dịch đã chốt trong ngày.",
            [CheckResult(id="outputs", status="passed", summary="đủ tài liệu"), CheckResult(id="ac_testable", status="passed", summary="1 AC kiểm thử được")], run_id=None,
        )
        await office.db.execute("UPDATE tickets SET status = 'in_review' WHERE id = ?", (t2.id,))

        # 3) a ticket with a multiple-choice question for the BA
        t3 = await office.tickets.create("Giới hạn số lần đổi trả mỗi tháng", description="Chặn lạm dụng đổi trả.")
        t3 = await office.stages.start(t3.id, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
        await office.wakes.drop((await office.wakes.pending_for(ai.id, t3.id)).id, "demo")
        await office.interactions.create(
            InteractionKind.QUESTION,
            {"question": "Giới hạn tính theo tài khoản hay theo số điện thoại?", "options": ["Tài khoản", "Số điện thoại", "Cả hai"], "recommended": "Tài khoản",
             "roles": ["ba"], "roles_label": "BA", "approver_names": ["Lan"], "approver_ids": [lan.id], "asked_by": "Copilot", "ticket": t3.key,
             "context": "ReturnService.java:88 đang đếm theo customer_id"},
            actor_id=ai.id, ticket_id=t3.id,
        )
        await office.db.execute("UPDATE tickets SET status = 'in_review' WHERE id = ?", (t3.id,))
        await office.runs.finish(run_id, RunStatus.RUNNING)  # keep it "running" for the demo board
        print(f"Demo office ready at {home}. Run: ACTOROFFICE_HOME={home} uv run actoroffice serve --no-browser, then open /team")
    finally:
        await office.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    asyncio.run(main(Path(parser.parse_args().home)))
