# Spec: lượt chạy bền hơn khi lệnh treo hoặc mất kết nối API

Trạng thái: nháp 1 (2026-11-04 sáng). Các quyết định chốt sau bản nháp này được ghi ở nơi khác (comment trên ticket
liên quan, nhật ký vòng lặp); bản này không tự cập nhật.

## Bối cảnh

Đêm 2026-11-04, SI-32 ở bước Làm chạy `uv run pytest -q -p no:randomly 2>&1 | tail -2`. Activity ghi lệnh "xong
(73.6 phút)", watchdog báo `slow` ở 305s và `stalled` ở 4414s nhưng không làm gì. Lượt chạy kết thúc bằng
`API Error: Unable to connect to API: Self-signed certificate detected`, ticket rơi về `todo` và chờ người bấm ↻.
Cùng đêm, phiên điều phối (người duyệt) cũng chết vì cùng lỗi mạng.

Chưa rõ 73.6 phút là thời gian lệnh thật hay là lúc CLI chờ API rồi mới gửi kết quả tool. Phải điều tra trước khi
thiết kế (đọc stream event của run đó trong `runs/<run_id>/`, so mốc thời gian).

## Yêu cầu

1. **Lệnh chạy quá lâu.** Lệnh shell của AI có giới hạn cứng. Mặc định **600 giây**. Quá giới hạn thì cả cây process
   của lệnh bị dừng và AI nhận được thông báo rõ (lệnh nào, sau bao lâu, gợi ý chạy hẹp hơn).
   Mốc cảnh báo `slow` 300 giây giữ nguyên.
2. **Kẹt (stalled).** Làm đúng thiết kế gốc (`docs/plans/2026-10-31-team-copilot-design.md` §5.5), hiện mới chỉ ghi
   health.
3. **Lỗi mạng/API tạm thời** (không kết nối được, chứng chỉ, 5xx, overloaded, rate limit): tự chạy lại, không cần
   người bấm ↻. Không làm một cơ chế retry thứ hai song song với cơ chế đã có cho lỗi khởi động CLI.
4. **Phân loại lỗi.** Comment lỗi và thẻ trên web nói rõ loại: mạng/API, khởi động runtime, lệnh quá giờ, lỗi của AI.
   Người đọc biết có cần làm gì không.
5. Áp dụng cho cả Claude và Kiro.

## Ngoài phạm vi

- Đổi timeout của các check của repo (`repo:test`, `repo:lint`); chúng đã có `timeout_s` riêng trong profile.
- Cấu hình proxy/chứng chỉ cho máy người dùng.

## Câu hỏi mở

- Giới hạn lệnh có cho đổi theo dự án không?
- Retry tối đa mấy lần, cách nhau bao lâu?
