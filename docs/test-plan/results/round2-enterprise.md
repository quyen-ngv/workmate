# Live scenarios — round2-enterprise

Chạy lúc 2026-10-11 07:47.

| Kịch bản | Kết quả | Điểm | Checks | Thời gian |
|---|---|---|---|---|
| enterprise — Luồng doanh nghiệp: BA thu thập yêu cầu → Dev Lead họp BA lên giải pháp/kế hoạch → giao dev → QA → BA nghiệm thu; có thay đổi yêu cầu giữa chừng | ✅ | 1.0 | 15/15 | 32.2 phút |

## enterprise — Luồng doanh nghiệp: BA thu thập yêu cầu → Dev Lead họp BA lên giải pháp/kế hoạch → giao dev → QA → BA nghiệm thu; có thay đổi yêu cầu giữa chừng

- ✅ BA hỏi làm rõ trước khi tạo ticket — Dạ, em nhận ạ. Để làm đúng ý anh, em cần hỏi vài điểm chính (em đã đề xuất sẵn mặc định, anh chỉ cần chọn hoặc sửa):

1. **Người dùng & phân quyền:** ai sẽ dùng?
   a) Nhân viên + quản lý trực tiếp du
- ✅ BA viết requirements chuẩn (US + AC Given/When/Then) — ['acceptance', 'change-request', 'design', 'handoff', 'meeting-notes', 'plan', 'requirements', 'test-plan', 'test-report']
- ✅ Lead có design + plan
- ✅ plan có người làm, ước lượng, hạn
- ✅ có phòng họp BA–Lead
- ✅ Lead và BA cùng trao đổi trong phòng họp — {'Long', 'Hoa'}
- ✅ việc được chia cho ≥2 dev — {'Minh', 'Tu'}
- ✅ có QA + test-report
- ✅ có ghi nhận thay đổi yêu cầu
- ✅ requirements được cập nhật sau thay đổi (rev ≥2) — [2]
- ✅ BA nghiệm thu (acceptance)
- ✅ mọi ticket xong — {'AO-1': ('feature', 'done', 'Hoa'), 'AO-12': ('change_request', 'done', 'Hoa'), 'AO-2': ('design', 'done', 'Long'), 'AO-13': ('design', 'done', 'Long'), 'AO-8': ('task', 'done', 'Quang'), 'AO-7': ('task', 'done', 'Tu'), 'AO-6': ('task', 'done', 'Tu'), 'AO-5': ('task', 'done', 'Minh'), 'AO-4': ('task', 'done', 'Minh'), 'AO-16': ('task', 'done', 'Quang'), 'AO-15': ('task', 'done', 'Tu'), 'AO-14': ('task', 'done', 'Minh')}
- ✅ test của sản phẩm pass — 194 passed in 6.31s
- ✅ code có xử lý huỷ đơn (thay đổi yêu cầu)
- ✅ BA báo cáo lại trong chat của người dùng
- ghi chú: tree: {'AO-1': ('feature', 'done', 'Hoa'), 'AO-12': ('change_request', 'done', 'Hoa'), 'AO-2': ('design', 'done', 'Long'), 'AO-13': ('design', 'done', 'Long'), 'AO-8': ('task', 'done', 'Quang'), 'AO-7': ('task', 'done', 'Tu'), 'AO-6': ('task', 'done', 'Tu'), 'AO-5': ('task', 'done', 'Minh'), 'AO-4': ('task', 'done', 'Minh'), 'AO-16': ('task', 'done', 'Quang'), 'AO-15': ('task', 'done', 'Tu'), 'AO-14': ('task', 'done', 'Minh')}

## Nhật ký

```
[07:15:16] hired Hoa as Business Analyst (sonnet)
[07:15:20] hired Long as Dev Lead (sonnet)
[07:15:24] hired Minh as Developer (sonnet)
[07:15:28] hired Tu as Developer (sonnet)
[07:15:32] hired Quang as QA Engineer (sonnet)
[07:15:49] USER → Công ty anh muốn có chức năng xin nghỉ phép cho nhân viên. Em làm giúp anh nhé.
[07:16:18] Hoa → 'Dạ, em nhận ạ. Để làm đúng ý anh, em cần hỏi vài điểm chính (em đã đề xuất sẵn mặc định, anh chỉ cần chọn hoặc sửa):\n\n1. **Người dùng & phân quyền:** ai sẽ dùng?\n   a) Nhân viên + quản lý trực tiếp duyệt *(đề xuất)*\n   b) Nhân viên + quản lý + HR duyệt thêm\n   c) Khác\n\n2. **Loại nghỉ:** cần hỗ trợ loại nào?\n   a) Phép năm, nghỉ ốm, nghỉ không lương *(đề xuất)*\n   b) Thêm thai sản, cưới hỏi, tang l'
[07:16:24] USER → Quản lý trực tiếp duyệt, nghỉ trên 3 ngày thì thêm HR duyệt. Có 3 loại: phép năm 12 ngày/năm, nghỉ ốm, nghỉ không lương. Lưu SQLite, giai đoạn 1 chỉ cần thư việ
[07:16:43] confirmed ticket proposal by Hoa: Chức năng xin nghỉ phép – Giai đoạn 1 (thư viện Python + CLI demo) → AO-1
[07:19:21] approved Minh: Bash ls -a (always Bash(ls -a*))
[07:19:38] approved Minh: Bash mkdir -p src/leave_app tests && uv --version
[07:20:58] approved Minh: Bash git commit -q -m "T-1: khung dự án, DB, models, repository, workdays, seed và test" -m "Co-Authored-By: Claude Sonnet 5.
[07:21:14] approved Tu: Bash git ls-files (always Bash(git ls-files*))
[07:21:23] approved Minh: Bash git ls-files (always Bash(git ls-files*))
[07:22:22] approved Tu: Bash rm handoff-AO-6.md (always Bash(rm handoff-AO-6.md*))
[07:22:32] approved Tu: Bash git commit -q -m "T-3: approvals.py (approve, reject, list_requests) và test" -m "Co-Authored-By: Claude Sonnet 5.5 <nor
[07:22:48] approved Minh: Bash git commit -q -m "T-2: tạo đơn, số dư, hủy đơn (requests.py) và test

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthrop
[07:23:11] approved Tu: Bash cat src/leave_app/requests.py src/leave_app/approvals.py src/leave_app/seed.py src/leave_app/db.py src/leave_app/errors. (always Bash(cat src/leave_app/requests.py*))
[07:23:20] approved Tu: Bash grep -n "^def " src/leave_app/repository.py (always Bash(grep -n*))
[07:23:30] approved Tu: Bash mkdir -p tests && cat > src/leave_app/__main__.py <<'EOF'
"""Chạy `python -m leave_app`."""

import sys

from leave_app.
[07:24:50] approved Tu: Bash export LEAVE_APP_DB=demo_tmp.db; L="uv run --quiet leave-app"; $L init --force; $L --user 4 create --type annual --from 
[07:25:21] approved Tu: Bash sed -i 's|# không phải quản lý|# đơn đã ở trạng thái cuối (không còn duyệt được)|; s|# đơn đã duyệt$|# không thể hủy đơn
[07:25:44] approved Quang: Bash git ls-files (always Bash(git ls-files*))
[07:27:04] approved Quang: Bash mkdir -p /tmp/qa && export LEAVE_APP_DB=/tmp/qa/demo.db && U="uv run --quiet leave-app" && $U init --force; for c in "--
[07:27:35] approved Quang: Bash export LEAVE_APP_DB=/tmp/qa/demo.db; U="uv run --quiet leave-app"; for c in "--user 4 create --type annual --from 2026-1
[07:27:58] approved Quang: Bash git rev-parse --short HEAD (always Bash(git rev-parse*))
[07:28:07] approved Quang: Bash grep -n "def seed" -A12 src/leave_app/seed.py (always Bash(grep -n*))
[07:28:24] approved Quang: Bash git add tests/test_acceptance.py (always Bash(git add*))
[07:28:33] approved Quang: Bash git commit -q -m "T-5: tests/test_acceptance.py (mỗi AC 1 test + ca bổ sung design §8)" -m "Co-Authored-By: Claude Sonne
[07:30:26] Hoa → 'Em đã đề xuất ticket cho giai đoạn 1. Anh xác nhận là em bắt đầu làm.\n\n**Phạm vi đã chốt:**\n- **Người dùng:** nhân viên, quản lý trực tiếp và HR.\n- **Loại nghỉ:** phép năm (12 ngày/năm), nghỉ ốm, nghỉ không lương.\n- **Luồng duyệt:** quản lý trực tiếp duyệt trước. Đơn nghỉ trên 3 ngày thì HR duyệt thêm.\n- **Hình thức:** thư viện Python + CLI, lưu bằng SQLite, làm trong 1 tuần (đến 18/10).\n- **Chưa '
[07:30:28] Minh → '✅ **AO-4 · T-1 Khung dự án, DB, models, repository, seed** — đã xong\n\nĐã xong T-1 trên branch t/ao-1-chuc-nang-xin-nghi-phep (1 commit). Gồm pyproject/README/.gitignore; src/leave_app: errors, models, db (có `transaction()` BEGIN IMMEDIATE), repository, workdays, seed (Lan/Mai HR, Bình MANAGER, An/Chi); tests conftest (fixture `conn`), test_workdays, test_seed (AC-7.1), test_repository. Kiểm tra: '
[07:30:30] Tu → '✅ **AO-6 · T-3 Duyệt/từ chối và liệt kê đơn (approvals.py)** — đã xong\n\nĐã làm `src/leave_app/approvals.py` (approve, reject, list_requests) theo design rev 2 §5 và `tests/test_approvals.py` (28 test). Test phủ AC-3.x, 4.x, 5.x. Ba ca bổ sung cũng được kiểm: đơn Bình do Lan duyệt → APPROVED; đơn Lan do Mai duyệt và Lan không tự duyệt; số dư đúng sau từ chối. Kết quả `uv run pytest -q`: 49 pass. `u'
[07:30:32] Minh → '✅ **AO-5 · T-2 Tạo đơn, số dư, hủy đơn (requests.py)** — đã xong\n\nĐã tạo `src/leave_app/requests.py` (`create_request`, `get_balance`, `cancel_request`) theo design rev 2 §5, và `tests/test_requests.py`. Các thao tác ghi chạy trong `BEGIN IMMEDIATE`, rollback khi lỗi, ghi history `CREATE`/`CANCEL`. Lỗi bằng tiếng Việt; lỗi AC-1.6 nêu rõ số dư còn lại.\n\nCác luật đã làm: Bình (không có quản lý) vào '
[07:30:34] Tu → '✅ **AO-7 · T-4 CLI, README, kịch bản demo** — đã xong\n\nĐã làm xong T-4. Commit e204f24 trên nhánh t/ao-1-chuc-nang-xin-nghi-phep, chưa push.\n- Thêm `cli.py` (7 lệnh, `--db`, `--user`) và `__main__.py`.\n- Thêm `tests/test_cli.py` (9 test) và viết lại README với kịch bản demo 8 bước.\n- Lỗi nghiệp vụ in `Lỗi: ...` ra stderr, mã thoát 1. Tham số sai mã thoát 2. Không in stack trace.\n- Kiểm tra: `uv ru'
[07:30:36] Quang → '✅ **AO-8 · T-5 Kế hoạch & thực thi kiểm thử, test-report** — đã xong\n\nKết luận GO. Cả 27 AC và KPI 1–3 đều đạt. Đã viết `test-plan` rev 1 (ma trận AC → TC) và `test-report` rev 1.\n- Đã thêm `tests/test_acceptance.py` (45 test), commit trên nhánh `t/ao-1-chuc-nang-xin-nghi-phep`. Mỗi AC có ít nhất 1 test. Có thêm các ca ở design §8: Bình không có quản lý, Lan/Mai, đúng 3 và 4 ngày, số dư sau từng t'
[07:30:38] Long → '✅ **AO-2 · Giải pháp & kế hoạch: Chức năng xin nghỉ phép GĐ1** — đã xong\n\nBáo cáo AO-2/AO-1 GĐ1: đã xong, GO để bàn giao demo.\n**Đã làm:** design rev 2, plan rev 1, meeting-notes rev 1 (họp với Hoa, 6 điểm nghiệp vụ được chốt). Dev/QA xong cả 5 ticket: AO-4 khung/DB/seed (Minh), AO-5 tạo/hủy/số dư (Minh), AO-6 duyệt/liệt kê (Tu), AO-7 CLI+README (Tu), AO-8 kiểm thử (Quang).\n**Vị trí:** nhánh `t/ao'
[07:30:40] Hoa → '✅ **AO-1 · Chức năng xin nghỉ phép – Giai đoạn 1 (thư viện Python + CLI demo)** — đã xong\n\nNghiệm thu GĐ1: 27/27 AC pass, KPI 1–3 đạt. Chi tiết trong doc `acceptance` rev 1 của AO-1.\n\n**Bản giao:** thư viện `leave_app` và CLI nằm trên nhánh `t/ao-1-chuc-nang-xin-nghi-phep`, trong `src/leave_app/`. README có kịch bản demo.\n\n**Cách kiểm tra:** tôi đối chiếu từng AC với test-report của Quang (AO-8) v'
[07:31:43] == injecting a requirement change ==
[07:31:45] USER → Anh vừa họp với HR: cần thêm cho phép nhân viên huỷ đơn khi đơn chưa được duyệt, và nghỉ ốm trên 2 ngày phải có ghi chú giấy khám bệnh.
[07:32:03] confirmed ticket proposal by Hoa: Thay đổi: huỷ đơn khi chưa duyệt & nghỉ ốm >2 ngày bắt buộc ghi chú giấy khám → AO-12
[07:33:16] approved Long: Bash git ls-files (always Bash(git ls-files*))
[07:34:22] approved Minh: Bash ls src/* tests (always Bash(ls src/**))
[07:35:15] approved Minh: Bash grep -n -B2 -A3 "\bS\b.*\"20\|def make\|, S," tests/test_acceptance.py
[07:35:24] approved Minh: Bash sed -i 's|def make(conn, actor, kind, start, end, reason="lý do"):|def make(conn, actor, kind, start, end, reason="lý do
[07:35:33] approved Minh: Bash sed -i 's|create_request(conn, AN, "sick", MON, FRI, "")|create_request(conn, AN, "sick", MON, FRI, "", "giấy khám")|; s
[07:36:32] approved Minh: Bash git commit -q -m "T-7: ghi chú giấy khám nghỉ ốm (BR-11), nhãn tiếng Việt khi hủy đơn (BR-12), migrate cột medical_note

[07:36:56] approved Tu: Bash git log --oneline -5 && git status --short && ls src/* tests
[07:37:20] approved Tu: Bash grep -rn "MEDICAL_NOTE_THRESHOLD_DAYS =" src (always Bash(grep -rn*))
[07:37:29] approved Tu: Bash python - <<'EOF'
import re
p='src/leave_app/cli.py'
s=open(p,encoding='utf-8').read()
s=s.replace('''    p.add_argument(
[07:37:45] approved Tu: Bash cat >> tests/test_cli.py <<'EOF'


def test_ac73_giay_kham(db: str, capsys: pytest.CaptureFixture[str]) -> None:
    sic
[07:37:54] approved Tu: Bash python - <<'EOF'
p='README.md'
s=open(p,encoding='utf-8').read()
i=s.index("   **Nghỉ ốm và giấy khám**")
j=s.index("4. 
[07:38:04] approved Quang: Bash python - <<'EOF'
p='tests/test_acceptance.py'
s=open(p,encoding='utf-8').read()
marker="# ---------- US-7 ----------"
ne
[07:38:06] approved Tu: Bash uv run pytest -q 2>&1 | tail -15
[07:38:15] approved Tu: Bash uv run ruff check . 2>&1 | tail -15
[07:38:24] approved Tu: Bash uv run ruff format src tests && uv run pytest -q 2>&1 | tail -15
[07:38:40] approved Tu: Bash git checkout -- tests/test_acceptance.py tests/test_repository.py tests/test_requests.py (always Bash(git checkout*))
[07:40:10] approved Quang: Bash bash .qa_demo.sh (always Bash(bash .qa_demo.sh*))
[07:41:31] approved Quang: Bash git commit -m "T-9: test nghiệm thu AC-1.8–1.11, AC-6.1–6.7, AC-7.3, BR-11 (migration), BR-12" -m "Co-Authored-By: Claud
[07:44:14] Hoa → 'Em vừa đề xuất một ticket thay đổi yêu cầu, gắn vào AO-1. Anh xác nhận là em làm tiếp.\n\n- **Huỷ đơn:** nhân viên chỉ huỷ được đơn của mình khi đơn chưa được duyệt hoặc từ chối. Huỷ phép năm thì số ngày được hoàn lại. Hàm huỷ đơn đã có từ AO-5, nên em sẽ nhờ Long rà xem nó có khớp quy tắc mới không.\n- **Nghỉ ốm:** trên 2 ngày làm việc thì bắt buộc có ghi chú giấy khám. Thiếu ghi chú thì hệ thống kh'
[07:44:16] Minh → '✅ **AO-14 · T-7: Thư viện — ghi chú giấy khám (BR-11) + rà/sửa huỷ đơn (BR-12)** — đã xong\n\nĐã làm xong T-7 trên nhánh t/ao-1-chuc-nang-xin-nghi-phep (commit "T-7: ..."). Gồm: cột medical_note kèm tự ALTER cho DB cũ, models/repository cập nhật, create_request có tham số medical_note (SICK >2 ngày làm việc phải có ghi chú, strip, kiểm sau days>0 và trước trùng ngày, lưu cho mọi loại), cancel_reques'
[07:44:18] Tu → '✅ **AO-15 · T-8: CLI --medical-note, cancel, README demo (AC-7.3)** — đã xong\n\nĐã làm xong, commit 4e215eb, "T-8: ...".\n- `cli.py`: `create --medical-note`, `cancel --note`. Đơn có ghi chú giấy khám thì in dòng "Giấy khám: ...". Bảng `list` có thêm cột "Giấy khám" khi có đơn nào có ghi chú.\n- README: cập nhật bảng lệnh, ghi chú DB cũ tự thêm cột `medical_note`, thêm bước demo hủy đơn (người khác h'
[07:44:20] Quang → '✅ **AO-16 · T-9: QA — kiểm thử chấp nhận AC mới (AC-1.8–1.11, AC-6.x, AC-7.3) + hồi quy** — đã xong\n\nKết luận: GO. Mọi AC mới (AC-1.8–1.11, AC-6.1–6.7, AC-7.3) đạt, hồi quy AC cũ đạt, không có lỗi S1–S3.\n\n**Đã làm**\n- Test-plan và test-report: bản tóm tắt nằm ở AO-16. Bản đầy đủ là rev 2 ở doc của AO-8 (nơi có rev 1). Test-plan có ma trận truy vết cho AC-1.8–1.11, AC-6.1–6.7, AC-7.3, BR-11, BR-12.'
[07:44:22] Long → '✅ **AO-13 · Rà tác động & triển khai thay đổi AO-12: huỷ đơn chưa duyệt + ghi chú giấy khám nghỉ ốm >2 ngày** — đã xong\n\nBáo cáo AO-13 (triển khai AO-12).\nĐã làm:\n- Rà tác động: `cancel_request` khớp BR-12; chỉ sửa thông báo dùng nhãn tiếng Việt và bổ sung test.\n- Design rev 3 và plan rev 2 (doc của AO-2), kèm design/plan tóm tắt trên AO-13.\n- AO-14 (Minh): cột `medical_note`, migration, BR-11, te'
[07:44:24] Hoa → '✅ **AO-12 · Thay đổi: huỷ đơn khi chưa duyệt & nghỉ ốm >2 ngày bắt buộc ghi chú giấy khám** — đã xong\n\nĐã nghiệm thu thay đổi AO-12. Tất cả AC mới đều pass: AC-1.8–1.11, AC-6.1–6.7 và AC-7.3, cùng BR-11, BR-12 và hồi quy các AC cũ. Chi tiết trong doc `acceptance` rev 2 của AO-1.\n\n**Bản giao:** code nằm trên nhánh `t/ao-1-chuc-nang-xin-nghi-phep`. Gồm cột `medical_note` kèm migration, kiểm tra BR-1'
```
