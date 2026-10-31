# Live scenarios — round4-enterprise-b

Chạy lúc 2026-10-11 08:32.

| Kịch bản | Kết quả | Điểm | Checks | Thời gian |
|---|---|---|---|---|
| enterprise — Luồng doanh nghiệp: BA thu thập yêu cầu → Dev Lead họp BA lên giải pháp/kế hoạch → giao dev → QA → BA nghiệm thu; có thay đổi yêu cầu giữa chừng | ❌ | 0.47 | 14/15 | 20.1 phút |

## enterprise — Luồng doanh nghiệp: BA thu thập yêu cầu → Dev Lead họp BA lên giải pháp/kế hoạch → giao dev → QA → BA nghiệm thu; có thay đổi yêu cầu giữa chừng

- ❌ BA hỏi làm rõ trước khi tạo ticket — Anh ơi, tin nhắn này giống hệt tin lúc nãy, nên em chưa có câu trả lời mới của anh. Em đã gửi 5 câu hỏi ở tin trước. Anh trả lời theo số và chữ cái là được, ví dụ "1a, 2a, 3b, 4a, 5a". Nếu muốn dùng c
- ✅ BA viết requirements chuẩn (US + AC Given/When/Then) — ['acceptance', 'change-request', 'design', 'handoff', 'meeting-notes', 'plan', 'requirements', 'test-plan', 'test-report']
- ✅ Lead có design + plan
- ✅ plan có người làm, ước lượng, hạn
- ✅ có phòng họp BA–Lead
- ✅ Lead và BA cùng trao đổi trong phòng họp — {'Long', 'Hoa', 'Minh', 'Quang', 'Tu'}
- ✅ việc được chia cho ≥2 dev — {'Tu', 'Minh'}
- ✅ có QA + test-report
- ✅ có ghi nhận thay đổi yêu cầu
- ✅ requirements được cập nhật sau thay đổi (rev ≥2) — [2]
- ✅ BA nghiệm thu (acceptance)
- ✅ mọi ticket xong — {'AO-3': ('feature', 'done', 'Hoa'), 'AO-11': ('change_request', 'done', 'Hoa'), 'AO-4': ('design', 'done', 'Long'), 'AO-10': ('task', 'done', 'Quang'), 'AO-9': ('task', 'done', 'Tu'), 'AO-8': ('task', 'done', 'Tu'), 'AO-7': ('task', 'done', 'Minh'), 'AO-6': ('task', 'done', 'Quang'), 'AO-5': ('task', 'done', 'Minh')}
- ✅ test của sản phẩm pass — 136 passed in 16.02s
- ✅ code có xử lý huỷ đơn (thay đổi yêu cầu)
- ✅ BA báo cáo lại trong chat của người dùng
- ghi chú: tree: {'AO-3': ('feature', 'done', 'Hoa'), 'AO-11': ('change_request', 'done', 'Hoa'), 'AO-4': ('design', 'done', 'Long'), 'AO-10': ('task', 'done', 'Quang'), 'AO-9': ('task', 'done', 'Tu'), 'AO-8': ('task', 'done', 'Tu'), 'AO-7': ('task', 'done', 'Minh'), 'AO-6': ('task', 'done', 'Quang'), 'AO-5': ('task', 'done', 'Minh')}

## Nhật ký

```
[08:13:07] USER → Công ty anh muốn có chức năng xin nghỉ phép cho nhân viên. Em làm giúp anh nhé.
[08:13:27] USER → Quản lý trực tiếp duyệt, nghỉ trên 3 ngày thì thêm HR duyệt. Có 3 loại: phép năm 12 ngày/năm, nghỉ ốm, nghỉ không lương. Lưu SQLite, giai đoạn 1 chỉ cần thư việ
[08:13:46] confirmed ticket proposal by Hoa: Chức năng xin nghỉ phép (giai đoạn 1: thư viện Python + CLI, SQLite) → AO-3
[08:14:41] approved Long: Bash ls -a (always Bash(ls -a*))
[08:15:56] == injecting a requirement change while developers work ==
[08:15:58] USER → Anh vừa họp với HR: cần thêm cho phép nhân viên huỷ đơn khi đơn chưa được duyệt, và nghỉ ốm trên 2 ngày phải có ghi chú giấy khám bệnh.
[08:16:02] approved Minh: Bash ls -la (always Bash(ls -la*))
[08:16:18] confirmed ticket proposal by Hoa: Thay đổi yêu cầu: huỷ đơn khi chưa duyệt; nghỉ ốm trên 2 ngày cần ghi chú giấy khám → AO-11
[08:16:20] approved Minh: Bash uv --version (always Bash(uv --version*))
[08:18:21] approved Minh: Bash ls tests (always Bash(ls tests*))
[08:18:37] approved Minh: Bash git commit -q -m "T-1: khung dự án, CSDL, mô hình, lỗi, lịch ngày làm việc, facade LeaveService (theo requirements rev 2
[08:19:05] approved Minh: Bash git commit -q -m "Thêm test_ac_traceability.py của Quang (AO-6), chưa chỉnh sửa" -m "Co-Authored-By: Claude Sonnet 5.5 <
[08:19:34] approved Tu: Bash git ls-files (always Bash(git ls-files*))
[08:19:36] approved Minh: Bash git ls-files (always Bash(git ls-files*))
[08:19:51] approved Quang: Bash git ls-files (always Bash(git ls-files*))
[08:20:06] approved Tu: Bash cat src/leave_app/workflow.py src/leave_app/history.py src/leave_app/requests.py src/leave_app/service.py src/leave_app/ (always Bash(cat src/leave_app/workflow.py*))
[08:20:08] approved Tu: Bash git log --oneline -3; git status --short; grep -n "AC-[235]" tests/test_ac_traceability.py | head -50
[08:20:24] approved Tu: Bash cat tests/test_ac_traceability.py; git diff --stat
[08:20:39] approved Quang: Bash git add tests/conftest.py tests/test_ac_traceability.py tests/test_fixtures.py (always Bash(git add*))
[08:20:54] approved Quang: Bash git commit -m "AO-6: fixture create_sick, test smoke fixture, traceability theo requirements rev 2

Co-Authored-By: Clau
[08:22:16] approved Minh: Bash git commit -m "T-2: create_request và get_balance (US-1, US-4, BR-10), test AC-1.1..1.13, AC-4.1..4.4" -m "Co-Authored-B
[08:22:32] approved Tu: Bash git commit -m "T-3: duyệt/từ chối/hủy đơn và lịch sử (US-2, US-3, US-5) theo requirements rev 2" -m "Co-Authored-By: Cla
[08:24:46] approved Tu: Bash rm -f demo.db; for c in "init" "seed-demo" "add-holiday 2026-11-20 Ngay-nha-giao" "create --as an --type annual --from 2
[08:25:28] approved Tu: Bash rm -f demo.db (always Bash(rm -f*))
[08:25:43] approved Tu: Bash git commit -q -m "T-4: CLI leave (argparse), seed-demo, README kịch bản demo, test AC-6.1..6.3 và e2e

Co-Authored-By: C
[08:27:04] approved Quang: Bash uv run python -c "import leave_app.service as s,inspect;print(inspect.getsource(s))"
[08:27:33] approved Quang: Bash grep -n "^from conftest\|^from tests\|^import conftest" tests/test_create.py tests/test_workflow.py
[08:28:15] approved Quang: Bash rm qa_demo.py (always Bash(rm qa_demo.py*))
[08:28:43] approved Quang: Bash git commit -m "AO-10: test QA bổ sung (ca biên/e2e theo test-plan), 136 test pass" -m "Co-Authored-By: Claude Sonnet 5.5
[08:30:31] approved Long: Bash git commit -am "Đổi tên 4 test ngưỡng HR về đúng mã AC-2.1/2.2 (D-1 của QA)" -m "Co-Authored-By: Claude Sonnet 5.5 <nore
```
