# Design — <KEY> <tên task>
Phiên bản: 1 · Cập nhật: YYYY-MM-DD · Người viết: <Dev>

## 1. Bối cảnh
Mục tiêu / không phải mục tiêu. Viết theo nghiệp vụ (spec) bản nào; đã đọc phân tích, chia task và design của task liên quan nào.

## 2. Thay đổi
Mỗi thay đổi là một C, viết đủ để người khác làm được mà không phải hỏi lại. Dưới mỗi C:
- `Truy vết:` các AC / BR mà nó đáp ứng. C không xuất phát từ AC nào thì gắn thẻ `[kỹ thuật]` sau ID và nêu lý do.
- `Nguồn:` file (kèm dòng) nó đụng tới. Thành phần chưa có thì gắn thẻ `[mới]` và trích chỗ nó được gắn vào (nơi gọi, nơi đăng ký, cấu hình).
- `Trích:` nguyên văn dòng code đang có mà thay đổi này dựa vào, chép từ kết quả tool (C sửa code đã có thì bắt buộc).
- `Thay đổi: F-n` nếu cố ý làm khác hiện trạng đã ghi trong phân tích.

### C-1: <thay đổi ở thành phần đã có>
Hiện tại làm gì, sẽ làm gì; lỗi, chạy đồng thời, gọi lặp lại thì xử lý ra sao.
Truy vết: AC-1.1, BR-1
Nguồn: src/module/file.py:10-40
Trích: `<dòng code nguyên văn>`

### C-2 [mới]: <thành phần mới>
Trách nhiệm, đặt ở đâu, ai gọi nó.
Truy vết: AC-1.2
Nguồn: src/module/caller.py:55-60

### C-3 [kỹ thuật]: <việc kỹ thuật> vì <lý do>
Nguồn: src/module/other.py:1-30

## 3. Ảnh hưởng
Lấy từ code_impact trên mọi thứ sẽ sửa: mỗi nơi gọi, endpoint, topic, bảng, service bị chạm là một dòng.

| Nơi bị chạm | Loại (caller / endpoint / topic / bảng / service) | Xử lý bởi C-n hoặc "không ảnh hưởng vì …" |
|---|---|---|
| src/module/caller.py:55 | caller | C-1 |

## 4. Mô hình dữ liệu
Bảng / trường thêm, sửa; migration. Không đổi thì ghi "không đổi".

## 5. Giao diện
API / CLI / hàm công khai: chữ ký, input/output, lỗi trả về.

## 6. Quyết định kỹ thuật
Mỗi quyết định định hình giải pháp là một D, đã hỏi dev trước khi viết phần Thay đổi: ít nhất hai phương án thật,
phương án chọn kèm lý do, và ai chọn (AI tự chọn thì ghi `Chọn bởi: giả định A-n`).

### D-1: <quyết định>
Bối cảnh: vì sao phải chọn.
- Phương án A: … (được / mất)
- Phương án B: … (được / mất)
Chọn: A — <lý do, hệ quả>
Chọn bởi: <tên dev>

## 7. Phi chức năng
Bảo mật / phân quyền, hiệu năng, log và giám sát. Mục nào không áp dụng thì ghi "không áp dụng vì …".

## 8. Kiểm thử
Mỗi AC có ít nhất một dòng; ghi thêm dữ liệu test và lệnh chạy.

| AC | Loại test (unit / integration / e2e) | Mô tả |
|---|---|---|
| AC-1.1 | unit | … |

## 9. Triển khai & rollback
Thứ tự triển khai, cờ bật/tắt, migration dữ liệu, cách quay lui khi lỗi; hoặc "không áp dụng vì …".

## 10. Rủi ro
| Rủi ro | Khả năng | Ảnh hưởng | Giảm thiểu |
|---|---|---|---|

## 11. Truy vết
| AC / BR | Thay đổi (C) |
|---|---|
| AC-1.1 | C-1 |

## 12. Các bước làm
1. <bước> (thuộc C-1)

## 13. Câu hỏi mở
Còn câu hỏi chưa có dòng "Trả lời" thì chưa gửi duyệt được; câu đã trả lời thì giữ lại. Không có câu hỏi thì ghi "không có".

### Q-1: <câu hỏi>
Trả lời: <câu trả lời> — <ai trả lời>

## 14. Giả định
- A-1: <điều đang coi là đúng mà chưa ai xác nhận>

## 15. Change log
| Phiên bản | Ngày | Thay đổi | Người |
|---|---|---|---|
| 1 | YYYY-MM-DD | Bản đầu | <Dev> |
