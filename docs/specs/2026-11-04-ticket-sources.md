# Spec: nguồn tài liệu của ticket

Trạng thái: nháp 1 (2026-11-04).

## Vấn đề

Yêu cầu thật của một ticket thường nằm rải rác: mô tả ticket, file spec trong repo, thiết kế gốc, comment chốt quyết
định trên ticket khác, nhật ký vòng lặp, KB. Hiện nay:

- Đường dẫn file, mã ticket và URL trong mô tả chỉ là chữ; không có gì nhận ra chúng.
- Bước Tìm hiểu không phải chứng minh đã đọc những nguồn đó. Check `ac_testable` chỉ kiểm cấu trúc AC.
- KB, memory và ticket tương tự được tìm bằng `title + description[:400]`; nguồn nằm sau ký tự 400 không góp vào việc
  tìm.

## Yêu cầu

1. **Nhận ra tham chiếu.** Khi tạo ticket (web, API, Jira, `ticket_create` của AI) và khi mô tả đổi, office tách các
   tham chiếu trong mô tả: mã ticket (`SI-12`, `AO-7`, mã Jira), đường dẫn file trong repo của dự án
   (`docs/x.md`, `src/a/b.py`, có thể kèm `§5.5` hoặc `:120`), URL `http(s)://`. Lưu thành danh sách có cấu trúc.
2. **Bắt buộc đọc.** Ở bước Tìm hiểu (`code`) và Chốt cách sửa (`quick`), mọi tham chiếu phải được AI đọc
   (`ticket_get`/`doc_get` cho ticket, đọc file cho đường dẫn, `wiki_page`/`repo_fetch`… cho URL) **hoặc** được ghi là
   không liên quan kèm lý do. Một check mới kiểm điều này từ activity của run, không tin lời AI.
3. **Dossier ghi nguồn.** Mục `links` của dossier liệt kê từng nguồn: đã đọc / không liên quan (lý do) / không đọc
   được (lỗi). Yêu cầu nào lấy từ nguồn nào thì ghi `_Nguồn: …_` như `dossier_update` đã hỗ trợ.
4. **Mâu thuẫn.** Khi hai nguồn nói khác nhau, requirements ghi rõ chọn nguồn nào và vì sao (mặc định nguồn mới hơn
   thắng); nếu không chắc thì hỏi người.
5. **Hiện trên web.** Pane ticket có mục "Nguồn" cho thấy danh sách và trạng thái đọc của từng nguồn. Theo `DESIGN.md`.
6. Tìm KB/memory/ticket tương tự dùng cả tiêu đề của các nguồn đã nhận ra, không chỉ 400 ký tự đầu mô tả.

## Ghi chú

- Cần migration cho danh sách tham chiếu. Tham chiếu tới ticket của công ty/đội khác thì không được đọc lộ dữ liệu.
- Comment người viết trên ticket **trước** lượt chạy đầu tiên cũng là một nguồn (xem nhật ký vòng lặp, vòng 3).
