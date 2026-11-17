# Nghiệp vụ — <KEY> <tên task>
Phiên bản: 1 · Cập nhật: YYYY-MM-DD · Người viết: <BA>

## 1. Mục tiêu
- Task này đáp ứng yêu cầu nào của epic, cho ai, đo thành công bằng gì.

## 2. Thuật ngữ
Dùng đúng thuật ngữ của bản phân tích; chỉ ghi từ mới của task này, hoặc "không có từ mới".

## 3. User stories
Mỗi AC là một dòng danh sách riêng, một kết quả quan sát được, viết Given / When / Then, kèm dòng Truy vết tới REQ của
phân tích (ticket khác thì kèm mã: `Truy vết: EP-4/REQ-2`). Mỗi story có cả trường hợp lỗi, không quyền, rỗng, chạm giới hạn.

### US-1: <tên>
Là <vai trò>, tôi muốn <khả năng>, để <lợi ích>. Ưu tiên: Must/Should/Could.

- AC-1.1 (happy path) — Given … When … Then …
  Truy vết: REQ-1
- AC-1.2 (lỗi / ngoại lệ) — Given … When … Then …
  Truy vết: REQ-1

## 4. Quy tắc nghiệp vụ
Quy tắc kiểm được bằng một ví dụ cụ thể. Quy tắc trái với hiện trạng (một F của phân tích) thì thêm dòng `Thay đổi: F-n`.

### BR-1: <quy tắc>
Điều kiện, giá trị, ngoại lệ.
Truy vết: REQ-1

## 5. Thay đổi so với hiện trạng
| Quy tắc / AC | Hiện trạng (F) | Sau khi làm |
|---|---|---|
| BR-1 | F-1 | … |

## 6. Ngoài phạm vi
- Điều người đọc dễ tưởng là có nhưng task này không làm, kèm lý do; hoặc "không có".

## 7. Phụ thuộc
Mỗi dòng một thứ phải có trước, kèm lý do. Ticket hoặc task khác thì ghi mã của nó (mã ticket, ID task của breakdown,
hoặc `<mã ticket>/<ID mục>` khi phụ thuộc đúng một mục): máy đọc các mã trong mục này thành liên kết "phụ thuộc".
Hệ thống hoặc dữ liệu bên ngoài thì ghi bằng lời. Không có thì ghi "không có".

- <mã ticket hoặc ID> — <cần gì ở đó, vì sao>

## 8. Câu hỏi mở
Còn câu hỏi chưa có dòng "Trả lời" thì chưa duyệt được; câu đã trả lời thì giữ lại. Không có câu hỏi thì ghi "không có".

### Q-1: <câu hỏi>
Trả lời: <câu trả lời> — <ai trả lời>

## 9. Giả định
- A-1: <điều đang coi là đúng mà chưa ai xác nhận>

## 10. Change log
| Phiên bản | Ngày | Thay đổi | Người |
|---|---|---|---|
| 1 | YYYY-MM-DD | Bản đầu | <BA> |
