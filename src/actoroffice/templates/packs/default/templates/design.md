# Design — <KEY> <tên task>
Phiên bản: 1 · Cập nhật: YYYY-MM-DD · Người viết: <Dev>

## 1. Bối cảnh
Mục tiêu / không phải mục tiêu. Viết theo nghiệp vụ (spec) bản nào.

## 2. Thay đổi
Mỗi thay đổi là một C. Dưới mỗi C:
- `Truy vết:` các AC / BR mà nó đáp ứng. C không xuất phát từ AC nào thì gắn thẻ `[kỹ thuật]` sau ID và nêu lý do.
- `Nguồn:` file (kèm dòng) nó đụng tới. Thành phần chưa có thì gắn thẻ `[mới]`.
- `Thay đổi: F-n` nếu cố ý làm khác hiện trạng đã ghi trong phân tích.

### C-1: <thay đổi ở thành phần đã có>
Hiện tại làm gì, sẽ làm gì.
Truy vết: AC-1.1, BR-1
Nguồn: src/module/file.py:10-40

### C-2 [mới]: <thành phần mới>
Trách nhiệm, đặt ở đâu.
Truy vết: AC-1.2

### C-3 [kỹ thuật]: <việc kỹ thuật> vì <lý do>
Nguồn: src/module/other.py:1-30

## 3. Mô hình dữ liệu
Bảng / trường thêm, sửa; migration.

## 4. Giao diện
API / CLI / hàm công khai: chữ ký, input/output, lỗi trả về.

## 5. Quyết định kỹ thuật
### D-1: <quyết định>
- Bối cảnh · Phương án đã cân nhắc (≥2) · Quyết định · Hệ quả

## 6. Kiểm thử
Unit / integration / e2e cho từng AC; dữ liệu test; lệnh chạy.

## 7. Rủi ro
| Rủi ro | Khả năng | Ảnh hưởng | Giảm thiểu |
|---|---|---|---|

## 8. Truy vết
| AC / BR | Thay đổi (C) |
|---|---|
| AC-1.1 | C-1 |

## 9. Các bước làm
1. <bước> (thuộc C-1)

## 10. Giả định
- A-1: <điều đang coi là đúng mà chưa ai xác nhận>

## 11. Change log
| Phiên bản | Ngày | Thay đổi | Người |
|---|---|---|---|
| 1 | YYYY-MM-DD | Bản đầu | <Dev> |
