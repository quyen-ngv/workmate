"""The approval experience: who a web call acts as, what a gate card says on Telegram and in chat, asking without
rejecting, the difference between two revisions of a document, the documents panel reused between live events."""

from __future__ import annotations

import asyncio

from actoroffice.channels import tg_format as fmt
from actoroffice.channels.telegram import TelegramBridge
from actoroffice.domain.work import InteractionKind
from actoroffice.services.notices import ChatNotifier, _summary, reminder_text

from .test_docflow import analysis, put
from .test_docweb import web  # noqa: F401  (fixture)
from .test_telegram import GROUP, FakeBot
from .test_telegram_team import press
from .test_web import HEADERS

GATE = {
    "ticket": "AO-4", "title": "Lý do từ chối", "stage": "design", "stage_title": "Thiết kế", "gate": "design", "roles": ["lead"], "roles_label": "Lead",
    "min": 1, "approver_ids": ["u2"], "approver_names": ["Hùng"], "summary": "Design phủ mọi AC", "docs": [{"key": "design", "revision": 3}], "checks": [],
    "approvals": [], "review": [], "review_acs": [],
    "doc_core": {
        "states": {"design": {"state": "loi", "label": "lỗi"}}, "tasks": [], "warnings": [{"id": "w1"}],
        "open": [{"id": "x1f0a9c2d7e4b", "rule": "X-DESIGN-SPEC", "doc": "AO-4/design", "item": "C-2", "message": "C-2 không nói lý do lấy từ đâu"}],
        "crosscheck": {"ran": False, "reason": "phiên đối chiếu bị lỗi mạng"},
    },
}  # fmt: skip


def as_user(user_id: str) -> dict[str, str]:
    return {**HEADERS, "X-Office-User": user_id}


# ---- Telegram and chat text (no office needed) ---------------------------------------------------------------------


def test_gate_card_on_telegram_shows_documents_differences_and_points_at_the_web() -> None:
    text, rows = fmt.card({"id": "i1", "kind": "approve_stage", "payload": GATE}, web="http://localhost:8765/team#AO-4")
    assert "design bản 3 (lỗi)" in text and "Điểm lệch chưa xử lý:</b> 1" in text and "Cảnh báo:</b> 1" in text
    assert "CHƯA đối chiếu được bằng AI</b>: phiên đối chiếu bị lỗi mạng" in text
    assert "<code>x1f0a9</code> C-2" in text  # the short id /accept takes
    assert "Duyệt thẻ này trên web</b> <code>http://localhost:8765/team#AO-4</code>" in text
    assert [b["text"] for b in rows[0]] == ["👍 Duyệt (mở trên web)", "👎 Trả lại"]
    assert "không trả lại" in text and "Reply tin này để trả lại" not in text

    clean = {**GATE, "doc_core": {**GATE["doc_core"], "open": [], "crosscheck": {"ran": True, "reason": ""}}}
    text, rows = fmt.card({"id": "i1", "kind": "approve_stage", "payload": clean})
    assert "CHƯA đối chiếu" not in text and "trên web" not in text and [b["text"] for b in rows[0]] == ["👍 Duyệt", "👎 Trả lại"]


def test_submit_phase_reads_as_sending_not_approving() -> None:
    submit = {**GATE, "phase": "submit", "approver_names": ["Minh"], "roles_label": "Dev", "final_roles_label": "Lead"}
    text, rows = fmt.card({"id": "i1", "kind": "approve_stage", "payload": submit})
    assert "Minh xem rồi gửi Lead duyệt" in text and "cần Minh duyệt" not in text
    assert [b["text"] for b in rows[0]] == ["📨 Gửi Lead duyệt", "↩️ Làm lại"]
    assert "trên web" not in text  # the author never has to give reasons: the approver decides
    done = {"id": "i1", "kind": "approve_stage", "status": "answered", "payload": submit, "response": {"approved": True, "by_name": "Minh"}}
    assert fmt.verdict(done) == "📨 Minh đã gửi duyệt"


def test_business_gate_cannot_be_approved_with_defaults_from_telegram() -> None:
    business = {**GATE, "gate": "business", "spawn": {"flows": ["task"], "assign": ["dev"], "people": []}}
    business["doc_core"] = {"states": {}, "open": [], "warnings": [], "tasks": [{"id": "T-1"}, {"id": "T-2"}]}
    assert fmt.web_only_reason(business) == "cần chọn giữ hay bỏ 2 task, luồng và người nhận từng task"
    assert fmt.web_only_reason(GATE, open_count=0) == "" and "1 điểm lệch" in fmt.web_only_reason(GATE)
    assert fmt.REJECT_RE.match("trả lại: thiếu AC").group(1) == "thiếu AC" and fmt.REJECT_RE.match("thiếu AC, trả lại đi") is None
    assert fmt.plain_error("Còn 1 điểm lệch chưa xử lý ([x1f0a9c2] X-DESIGN-SPEC C-2).") == "Còn 1 điểm lệch chưa xử lý (X-DESIGN-SPEC C-2)."


def test_chat_text_of_a_gate_card_is_a_sentence() -> None:
    assert _summary({"kind": "approve_stage", "payload": GATE}) == "AO-4 · Thiết kế đã xong: chờ Hùng duyệt"
    submit = {**GATE, "phase": "submit", "approver_names": ["Minh"], "final_roles_label": "Lead"}
    assert _summary({"kind": "approve_stage", "payload": submit}) == "AO-4 · Thiết kế đã xong: Minh xem rồi gửi Lead duyệt"
    assert "approve_stage" not in _summary({"kind": "approve_stage", "payload": {}})
    assert reminder_text({"payload": GATE}, 4) == "⏰ AO-4 · Thiết kế đã chờ Hùng duyệt hơn 4 giờ."


# ---- the web API ---------------------------------------------------------------------------------------------------


def gate_for(office, c, team, people, project, roles_gate: str = "plan"):
    """A ticket of the `code` flow with a gate card open; returns (key, card id, roles of the card)."""
    task = c.post("/api/tasks", json={"title": "Hoàn tiền", "template": "code", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()

    async def open_gate():
        ticket = await office.tickets.get(task["key"])
        await office.tickets.put_document(ticket.id, "requirements", "AC-1: Given a When b Then c", author="Copilot", base_revision=None)
        await office.tickets.put_document(ticket.id, "dossier", "# Dossier", author="Copilot", base_revision=None)
        item = await office.stages._open_gate(ticket, await office.stages.stage_def(ticket), "requirement", "xong", [], run_id=None)
        return item.id, item.payload["roles"], item.payload["approver_ids"]

    card_id, roles, approvers = c.portal.call(open_gate)
    return task["key"], card_id, roles, approvers


def test_web_calls_act_as_the_person_picked_in_the_sidebar(web) -> None:  # noqa: F811
    office, c, team, people, project = web
    key, card_id, roles, approvers = gate_for(office, c, team, people, project)
    assert roles and approvers
    allowed = next(u for u in people.values() if u.id in approvers)
    other = next(u for u in people.values() if u.id not in approvers)

    rights = c.get(f"/api/tickets/{key}/stage", headers=as_user(other.id)).json()
    assert rights["viewer"] == other.id and rights["cards"][card_id] == {"may_answer": False, "approved": False}
    assert rights["status"]["needs_viewer"] is False
    assert c.get(f"/api/tickets/{key}/stage", headers=as_user(allowed.id)).json()["cards"][card_id]["may_answer"] is True
    # an unknown id is the local owner (who may answer anything), like no header at all
    assert c.get(f"/api/tickets/{key}/stage", headers=as_user("nobody")).json()["viewer"] == "user"
    assert c.get(f"/api/tickets/{key}/stage").json()["cards"][card_id]["may_answer"] is True

    refused = c.post(f"/api/interactions/{card_id}/answer", json={"approved": True}, headers=as_user(other.id))
    assert refused.status_code == 403 and "Cần vai" in refused.json()["error"]
    assert c.post(f"/api/interactions/{card_id}/answer", json={"approved": True}, headers=as_user(allowed.id)).status_code == 200
    comments = [x["body"] for x in c.get(f"/api/tickets/{key}").json()["comments"]]
    assert any(body.startswith(f"👍 {allowed.name} duyệt") for body in comments)  # the audit names the person, not "Owner"


def test_asking_does_not_answer_the_card(web) -> None:  # noqa: F811
    office, c, team, people, project = web
    key, card_id, _roles, _approvers = gate_for(office, c, team, people, project)
    lan = people["Lan"]
    asked = c.post(f"/api/tickets/{key}/control", json={"cmd": "ask", "text": "AC-1 tính cả đơn huỷ không?"}, headers=as_user(lan.id))
    assert asked.status_code == 200 and asked.json()["held"] in (True, False)
    detail = c.get(f"/api/tickets/{key}").json()
    assert [(x["author_kind"], x["author_id"], x["body"]) for x in detail["comments"]][-1] == ("user", "Lan", "❓ AC-1 tính cả đơn huỷ không?")
    assert [p["id"] for p in detail["pending"]] == [card_id]
    assert c.post(f"/api/tickets/{key}/control", json={"cmd": "ask", "text": " "}, headers=HEADERS).status_code == 422
    steered = c.post(f"/api/tickets/{key}/control", json={"cmd": "steer", "text": "ưu tiên phí thấp"}, headers=as_user(lan.id))
    assert steered.status_code == 200 and "held" in steered.json()
    assert "Chỉ dẫn từ Lan" in c.get(f"/api/tickets/{key}").json()["comments"][-1]["body"]


def test_document_diff_tells_items_added_changed_and_removed(web) -> None:  # noqa: F811
    office, c, team, people, project = web
    epic = c.post("/api/tasks", json={"title": "Nâng hạn mức", "template": "epic", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()
    key = epic["key"]

    async def two_revisions():
        ticket = await office.tickets.get(key)
        first = analysis(ticket.key)
        await put(office, ticket, "analysis", first)
        await office.db.execute("UPDATE documents SET approved_revision = 1 WHERE ticket_id = ?", (ticket.id,))
        second = first.replace("Hạn mức hoàn tiền mỗi ngày là 9", "Hạn mức hoàn tiền mỗi ngày là 12")
        second = second.replace("## 3. Hiện trạng", "### REQ-9: Ghi nhật ký mỗi lần từ chối\nMới thêm.\n\n## 3. Hiện trạng")
        assert second != first
        await office.tickets.put_document(ticket.id, "analysis", second, author="Copilot", base_revision=1)

    c.portal.call(two_revisions)
    assert c.get(f"/api/tickets/{key}/documents/analysis/diff?from=1&to=1").status_code == 404
    diff = c.get(f"/api/tickets/{key}/documents/analysis/diff").json()
    assert (diff["from"], diff["to"], diff["basis"], diff["same"]) == (1, 2, "bản đã duyệt", False)
    by_id = {i["id"]: i for i in diff["items"]}
    assert by_id["REQ-9"]["change"] == "added" and by_id["REQ-9"]["old"] == "" and "Mới thêm." in by_id["REQ-9"]["new"]
    changed = next(i for i in diff["items"] if i["change"] == "changed")
    assert "là 9" in changed["old"] and "là 12" in changed["new"]
    assert not any("là 12" in line for line in diff["rest"])  # item text is not repeated in the line diff of the rest
    back = c.get(f"/api/tickets/{key}/documents/analysis/diff?from=2&to=2")
    assert back.status_code == 404
    assert c.get(f"/api/tickets/{key}/documents/nope/diff").status_code == 404


def test_documents_panel_is_reused_between_live_events(web, monkeypatch) -> None:  # noqa: F811
    office, c, team, people, project = web
    epic = c.post("/api/tasks", json={"title": "Nâng hạn mức", "template": "epic", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()
    key = epic["key"]
    calls = []
    real = office.docs.panel

    async def counted(ticket):
        calls.append(ticket.key)
        return await real(ticket)

    monkeypatch.setattr(office.docs, "panel", counted)
    first = c.get(f"/api/tickets/{key}/stage").json()["docs"]
    assert c.get(f"/api/tickets/{key}/stage").json()["docs"] == first and len(calls) == 1

    async def new_document():
        ticket = await office.tickets.get(key)
        await office.tickets.put_document(ticket.id, "analysis", analysis(ticket.key), author="Copilot", base_revision=None)

    c.portal.call(new_document)  # a document changed: not served from the cache
    assert [d["key"] for d in c.get(f"/api/tickets/{key}/stage").json()["docs"]["docs"]] == ["analysis"] and len(calls) == 2
    c.post(f"/api/tickets/{key}/control", json={"cmd": "steer", "text": "x"}, headers=HEADERS)  # anything done on the web: fresh
    c.get(f"/api/tickets/{key}/stage")
    assert len(calls) == 3


# ---- Telegram with an office ---------------------------------------------------------------------------------------


def test_telegram_points_at_the_web_and_reports_document_state(web) -> None:  # noqa: F811
    office, c, team, people, project = web
    epic = c.post("/api/tasks", json={"title": "Nâng hạn mức", "template": "epic", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()
    bot = FakeBot()

    async def scenario():
        tg = TelegramBridge(office, bot)
        tg.chat_id = GROUP
        ticket = await office.tickets.get(epic["key"])
        await office.db.execute("UPDATE users SET telegram_id = 1 WHERE id = ?", (people["Hùng"].id,))
        item = await office.interactions.create(
            InteractionKind.APPROVE_STAGE,
            {**GATE, "ticket": ticket.key, "stage": ticket.stage, "gate": "business", "roles": [], "spawn": {"flows": ["task"], "assign": ["dev"], "people": []},
             "doc_core": {"states": {}, "open": [], "warnings": [], "tasks": [{"id": "T-1", "title": "x", "has_spec": True}]}},
            ticket_id=ticket.id,
        )  # fmt: skip
        await tg.handle_update(press(f"i:{item.id}:y", 1, 77))
        pending = (await office.interactions.get(item.id)).status.value
        conv = await office.chat.create_group("Phòng " + ticket.key, [], created_by="user")
        await office.db.execute("UPDATE tickets SET origin_conversation_id = ? WHERE id = ?", (conv.id, ticket.id))
        await tg.handle_event("doc.state", {"ticket_id": ticket.id, "key": "analysis", "state": "lech", "action": "notify_author"})
        reminded = await ChatNotifier(office).remind(after_s=0)
        again = await ChatNotifier(office).remind(after_s=3600)
        await asyncio.sleep(0)
        return ticket.key, pending, item.id, reminded, again

    key, pending, item_id, reminded, again = c.portal.call(scenario)
    assert pending == "pending"  # 👍 did not approve with defaults
    texts = [p["text"] for p in bot.sent()]
    assert any("chưa duyệt được từ Telegram" in t and f"/team#{key}" in t and "1 task" in t for t in texts)
    assert any("Thẻ này cần chọn trên web" in p.get("text", "") for p in bot.sent("answerCallbackQuery"))
    assert any(f"<b>{key}</b>: tài liệu <code>analysis</code> đã duyệt hiện <b>lệch</b>" in t for t in texts)
    assert reminded == [item_id] and again == []
    comments = [x["body"] for x in c.get(f"/api/tickets/{key}").json()["comments"]]
    assert any(body.startswith(f"⏰ {key}") and "Hùng" in body for body in comments)
