# Live scenarios — round4-enterprise

Chạy lúc 2026-10-11 08:12.

| Kịch bản | Kết quả | Điểm | Checks | Thời gian |
|---|---|---|---|---|
| enterprise — Luồng doanh nghiệp: BA thu thập yêu cầu → Dev Lead họp BA lên giải pháp/kế hoạch → giao dev → QA → BA nghiệm thu; có thay đổi yêu cầu giữa chừng | ❌ | 0.25 | 1/2 | 6.1 phút |
| pm_status — PM — hỏi tiến độ: trả lời từ dữ liệu ticket (trạng thái, ai đang làm, rủi ro), không bịa | ✅ | 1.0 | 3/3 | 0.9 phút |

## enterprise — Luồng doanh nghiệp: BA thu thập yêu cầu → Dev Lead họp BA lên giải pháp/kế hoạch → giao dev → QA → BA nghiệm thu; có thay đổi yêu cầu giữa chừng

- ✅ BA hỏi làm rõ trước khi tạo ticket — Dạ, dự án đang trống nên em xin hỏi anh vài câu để chốt phạm vi:

1. **Quy trình duyệt**: đơn nghỉ được duyệt thế nào?
   a) Một cấp: quản lý trực tiếp duyệt (em đề xuất)
   b) Hai cấp: quản lý trực t
- ❌ có ticket tính năng

## pm_status — PM — hỏi tiến độ: trả lời từ dữ liệu ticket (trạng thái, ai đang làm, rủi ro), không bịa

- ✅ trích dẫn mã ticket thật — []
- ✅ không bịa ticket — set()
- ✅ nói đúng là đã xong hết / hoặc nêu việc còn mở — 0 việc mở

## Nhật ký

```
[08:05:18] hired Hoa as Business Analyst (sonnet)
[08:05:22] hired Long as Dev Lead (sonnet)
[08:05:26] hired Minh as Developer (sonnet)
[08:05:30] hired Tu as Developer (sonnet)
[08:05:34] hired Quang as QA Engineer (sonnet)
[08:05:51] USER → Công ty anh muốn có chức năng xin nghỉ phép cho nhân viên. Em làm giúp anh nhé.
[08:06:27] Hoa (posted 08:06:02) → 'Dạ, dự án đang trống nên em xin hỏi anh vài câu để chốt phạm vi:\n\n1. **Quy trình duyệt**: đơn nghỉ được duyệt thế nào?\n   a) Một cấp: quản lý trực tiếp duyệt (em đề xuất)\n   b) Hai cấp: quản lý trực tiếp rồi HR\n   c) Tự động duyệt nếu còn đủ phép\n\n2. **Loại nghỉ**: cần những loại nào?\n   a) Phép năm, nghỉ ốm, nghỉ không lương (em đề xuất)\n   b) Thêm thai sản, cưới hỏi, tang lễ\n   c) Chỉ phép năm\n\n'
[08:11:18] hired Phuong as Project Manager (haiku)
[08:11:30] USER → Tình hình các việc trong dự án này thế nào rồi em? Có gì đang trễ hoặc bị chặn không?
[08:11:59] Phuong (posted 08:11:39) → 'Hiện **không có việc nào của dự án Nghỉ phép** trong hệ thống. Thư mục `leave-app` chỉ có kho git (`.git`), chưa có tài liệu hay mã nguồn ứng dụng nghỉ phép, và không có ticket nào gắn với dự án này.\n\nTicket duy nhất trong văn phòng là **AO-1** (tỷ giá USD/EUR/JPY so với VND, do Khoa làm). Ticket này đã xong, không trễ, nhưng còn một việc đang chờ bạn: script và lịch chạy 08:00 hằng ngày chưa bật,'
```
