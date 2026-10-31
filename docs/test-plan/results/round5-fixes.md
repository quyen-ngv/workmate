# Live scenarios — round5-fixes

Chạy lúc 2026-10-11 08:42.

| Kịch bản | Kết quả | Điểm | Checks | Thời gian |
|---|---|---|---|---|
| weekly_report — E4 — báo cáo định kỳ: Analyst chuyển thành automation hằng tuần thay vì làm tay | ✅ | 1.0 | 5/5 | 3.7 phút |
| writer — Writer — bài đăng fanpage từ dữ kiện cho sẵn: hỏi đối tượng/giọng văn, không bịa số liệu, giao file | ✅ | 1.0 | 6/6 | 3.3 phút |

## weekly_report — E4 — báo cáo định kỳ: Analyst chuyển thành automation hằng tuần thay vì làm tay

- ✅ tạo automation thay vì làm tay mỗi tuần — ['bao-cao-doanh-thu-vung-tuan']
- ✅ lịch thứ 2 lúc 08:00 — weekly:mon@08:00
- ✅ chạy thử thành công — [('succeeded', '')]
- ✅ có file CSV kết quả — [{'path': 'reports/doanh-thu-vung-2026-09-30.csv', 'size': 174}]
- ✅ script đọc DB ở chế độ chỉ đọc hoặc không ghi DB

## writer — Writer — bài đăng fanpage từ dữ kiện cho sẵn: hỏi đối tượng/giọng văn, không bịa số liệu, giao file

- ✅ hỏi đối tượng/giọng văn/độ dài trước (hoặc nêu rõ giả định)
- ✅ có ticket viết bài
- ✅ có file bài viết trong workspace — ['bai-dang-fanpage-tra-dao-cam-sa.md']
- ✅ giữ đúng dữ kiện (giá, thời gian, phạm vi)
- ✅ không bịa thêm giá tiền — set()
- ✅ có ghi nhận kết quả bàn giao (work product)

## Nhật ký

```
[08:35:27] hired Linh as Data Analyst (sonnet)
[08:35:37] USER → Từ nay mỗi sáng thứ 2 lúc 8h em gửi anh báo cáo doanh thu tuần trước theo từng vùng nhé (file CSV).
[08:36:14] Linh (posted 08:35:51) → 'Được anh. Vì cần viết và test script nên em sẽ làm qua một ticket, rồi đặt lịch chạy tự động. Em đã xem dữ liệu `sales.db`: có 4 bảng (orders, order_items, products, stores) và vùng nằm ở `stores.region`. Em cần anh chốt vài điểm:\n\n1. **Doanh thu tính thế nào?** Em đề xuất: chỉ tính đơn `paid` (bỏ `cancelled`), và doanh thu = tổng `qty × unit_price`. Anh đồng ý không?\n2. **"Tuần trước" là khi nào?'
[08:36:20] USER → Báo cáo doanh thu 7 ngày gần nhất có dữ liệu theo vùng, chỉ đơn paid, lưu CSV; gửi lúc 8h sáng thứ 2 hằng tuần.
[08:36:38] confirmed ticket proposal by Linh: Script báo cáo doanh thu 7 ngày gần nhất theo vùng (CSV) + lịch thứ 2 hằng tuần 8h → AO-1
[08:37:37] approved automation bao-cao-doanh-thu-vung-tuan (thứ 2 hằng tuần lúc 08:00)
[08:38:26] Linh (posted 08:36:33) → 'Em đã tạo ticket đề xuất, anh xác nhận thì em bắt đầu làm.\n\n- **Phạm vi báo cáo:** chỉ tính đơn `paid`. Kỳ báo cáo là 7 ngày kết thúc ở ngày có đơn mới nhất trong `sales.db`, hiện là 30/09/2026. Mỗi vùng một dòng, gồm số đơn và doanh thu.\n- **Cách làm:** em viết một script Python và kiểm tra kỹ, gồm đối chiếu tổng các vùng với tổng toàn chuỗi. Script lưu file ở `reports/doanh-thu-vung-YYYY-MM-DD.c'
[08:38:28] Linh (posted 08:37:38) → 'Báo cáo doanh thu theo vùng (thứ 2 hằng tuần):\nCẢNH BÁO: dữ liệu chưa được cập nhật - đơn mới nhất 2026-09-30, cách hôm nay (2026-10-11) 11 ngày.\r\nKỳ báo cáo: 2026-09-24 đến 2026-09-30 (chỉ đơn paid)\r\n- Hà Nội: 28 đơn, 3,389,000 đ\r\n- Đà Nẵng: 21 đơn, 2,751,000 đ\r\n- TP.HCM: 18 đơn, 2,057,000 đ\r\nTổng: 67 đơn, 8,197,000 đ\r\nFile: F:\\_ao_lab\\r5\\home\\automations\\bao-cao-doanh-thu-vung-tuan\\out\\reports\\d'
[08:38:30] Linh (posted 08:37:57) → '✅ **AO-1 · Script báo cáo doanh thu 7 ngày gần nhất theo vùng (CSV) + lịch thứ 2 hằng tuần 8h** — đã xong\n\nĐã làm xong; automation chờ anh duyệt code thì mới bắt đầu chạy. Báo cáo đầy đủ ở document `report`.\n\n**Script**: `doanh_thu_vung.py` (PEP 723, chạy bằng `uv run`), kèm test `test_doanh_thu_vung.py` (5/5 pass).\n- Chỉ tính đơn paid. Doanh thu = SUM(qty × unit_price).\n- Kỳ báo cáo là 7 ngày kết'
[08:39:03] hired Lan as Content Writer (sonnet)
[08:39:13] USER → Viết giúp chị bài đăng fanpage cho chương trình khuyến mãi này nhé: Trà đào cam sả size L giá 39.000đ; khuyến mãi mua 2 tặng 1 từ 20/10 đến 31/10/2026 tại 7 cửa
[08:39:50] Lan (posted 08:39:25) → 'Chị ơi, em hỏi nhanh vài điểm để bài đăng chuẩn rồi em lên ticket viết nhé:\n\n1. **Tên thương hiệu/fanpage** là gì ạ? Có danh sách 7 cửa hàng, gồm địa chỉ hoặc link, để đưa vào bài không? (Mặc định: chỉ ghi chung "7 cửa hàng tại Hà Nội, TP.HCM, Đà Nẵng" kèm dòng "xem địa chỉ tại…".)\n2. **Mua 2 tặng 1** áp dụng thế nào ạ? Ly tặng có phải cùng loại, cùng size L không? Ly nào được tặng, ví dụ ly có gi'
[08:39:56] USER → Khách là sinh viên và dân văn phòng trẻ; giọng vui, gần gũi; khoảng 120-180 chữ; đăng Facebook; có lời kêu gọi đến cửa hàng.
[08:40:14] confirmed ticket proposal by Lan: Viết bài đăng fanpage: Trà đào cam sả size L mua 2 tặng 1 (20–31/10/2026) → AO-2
[08:41:10] Lan (posted 08:40:10) → 'Em đã lên ticket viết bài, chị xác nhận là em bắt đầu viết ngay.\n\nPhạm vi em chốt:\n- **Bài đăng:** 1 bài Facebook, khoảng 120–180 chữ. Giọng vui, gần gũi, nhắm tới sinh viên và dân văn phòng trẻ. Cuối bài có lời kêu gọi đến cửa hàng.\n- **Nội dung chương trình:** Trà đào cam sả size L giá 39.000đ, mua 2 tặng 1, từ 20/10 đến 31/10/2026. Áp dụng tại 7 cửa hàng ở Hà Nội, TP.HCM, Đà Nẵng. Chỉ áp dụng k'
[08:41:12] Lan (posted 08:40:41) → '✅ **AO-2 · Viết bài đăng fanpage: Trà đào cam sả size L mua 2 tặng 1 (20–31/10/2026)** — đã xong\n\nĐã viết bản nháp bài Facebook, lưu tại AO-2\\bai-dang-fanpage-tra-dao-cam-sa.md. Chưa đăng, chờ chị duyệt.\n- Bài có đủ dữ kiện: Trà đào cam sả size L 39.000đ/ly, mua 2 tặng 1 ly cùng loại cùng size, 20/10–31/10/2026, 7 cửa hàng ở Hà Nội, TP.HCM, Đà Nẵng.\n- Điều kiện "chỉ áp dụng khi mua tại quầy, không'
[08:41:18] USER → Khách là sinh viên và dân văn phòng trẻ; giọng vui, gần gũi; khoảng 120-180 chữ; đăng Facebook; có lời kêu gọi đến cửa hàng.
[08:41:48] Lan (posted 08:41:28) → 'Chị ơi, bài này em viết xong rồi, ở ticket AO-2. Em đã dùng đúng các yêu cầu chị vừa nhắc: khách là sinh viên và dân văn phòng trẻ, giọng vui gần gũi, khoảng 120–180 chữ, đăng Facebook, có lời kêu gọi đến cửa hàng.\n\n- **Bản nháp:** em lưu ở `AO-2\\bai-dang-fanpage-tra-dao-cam-sa.md`. Lúc này em tìm trong thư mục chat này thì chưa thấy file. Có thể file nằm trong workspace của ticket AO-2, nên chị m'
```
