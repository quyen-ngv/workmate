# Chia task — <KEY> <tên epic>
Phiên bản: 1 · Cập nhật: YYYY-MM-DD · Người viết: <BA>

## 1. Mục tiêu
Epic này giao được gì khi mọi task xong. Dựa trên bản phân tích nào.

## 2. Danh sách task
Mỗi task là một T, làm được độc lập và có nghiệp vụ (spec) riêng. Dòng Truy vết ghi các REQ mà task đáp ứng:
mọi REQ của phân tích phải có ít nhất một task hoặc nằm trong "Ngoài phạm vi".
Task phải có task khác xong trước thì thêm ngay dưới nó một dòng bắt đầu bằng "Phụ thuộc:" rồi ID task kia; hai task
đụng cùng màn hình / bảng / module mà không chặn nhau thì dùng dòng "Liên quan:". Ticket ngoài epic thì ghi mã ticket.
Các dòng này thành liên kết giữa các ticket khi breakdown được duyệt.

### T-1: <tên task>
Kết quả người dùng thấy được khi task xong.
Truy vết: REQ-1
- Có code: có / không
- Ước lượng: S / M / L

## 3. Thứ tự và phụ thuộc
Bảng tóm tắt thứ tự làm và lý do; máy đọc các dòng "Phụ thuộc:" dưới từng task ở mục 2, không đọc bảng này.

| Task | Cần xong trước | Lý do |
|---|---|---|
| T-1 | … | … |

## 4. Change log
| Phiên bản | Ngày | Thay đổi | Người |
|---|---|---|---|
| 1 | YYYY-MM-DD | Bản đầu | <BA> |
