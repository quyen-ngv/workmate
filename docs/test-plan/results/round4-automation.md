# Live scenarios — round4-automation

Chạy lúc 2026-10-11 08:17.

| Kịch bản | Kết quả | Điểm | Checks | Thời gian |
|---|---|---|---|---|
| reminders — Thư ký nhắc lịch (một lần + lặp lại), không cần ticket, không tốn LLM khi nhắc | ✅ | 1.0 | 7/7 | 3.2 phút |
| collector — Thu thập dữ liệu thành automation: viết script, chạy thử, gửi duyệt, chạy theo lịch, ra file CSV | ✅ | 1.0 | 8/8 | 3.6 phút |
| weekly_report — E4 — báo cáo định kỳ: Analyst chuyển thành automation hằng tuần thay vì làm tay | ❌ | 0.4 | 4/5 | 3.3 phút |
| writer — Writer — bài đăng fanpage từ dữ kiện cho sẵn: hỏi đối tượng/giọng văn, không bịa số liệu, giao file | ❌ | 0.25 | 3/6 | 2.1 phút |

## reminders — Thư ký nhắc lịch (một lần + lặp lại), không cần ticket, không tốn LLM khi nhắc

- ✅ tạo 2 automation chỉ gồm bước gửi tin (không cần duyệt) — [('nhac-uong-nuoc-0807', 'enabled', ['notify']), ('nhac-giao-ban-thu-2', 'enabled', ['notify'])]
- ✅ nhắc họp lặp lại sáng thứ 2 (trước hoặc đúng 9:00) — ['weekly:mon@09:00']
- ✅ nhắc uống nước khoảng 2 phút sau — 130s
- ✅ không tạo ticket cho việc nhắc lịch
- ✅ lời nhắc được gửi đúng giờ trong chat — 💧 Anh ơi, đến giờ uống nước rồi! Uống một ly nước nhé.
- ✅ gửi lời nhắc không gọi LLM
- ✅ trả lời lịch: nhắc tới họp giao ban thứ 2 9h — Từ giờ đến hết thứ 2 tuần này, anh chỉ có một lịch:

- **Thứ 2, 12/10, 09:00:** họp giao ban ở phòng họp tầng 3. Em sẽ nhắc anh lúc 9h.

Nhắc uống nước lúc 08:07 hôm nay đã đến giờ nên không còn trong

## collector — Thu thập dữ liệu thành automation: viết script, chạy thử, gửi duyệt, chạy theo lịch, ra file CSV

- ✅ có automation được tạo — ['fx-rates-daily']
- ✅ lịch chạy hằng ngày buổi sáng — daily@08:00
- ✅ đã được duyệt và bật — enabled
- ✅ lần chạy thử thành công — [('succeeded', 0)]
- ✅ có file CSV kết quả — [{'path': 'fx_rates.csv', 'size': 251}]
- ✅ CSV có header + ≥1 dòng có số — ['date,fetched_at,currency,vnd_per_unit,source', '2026-10-11,2026-10-11T08:10:22+07:00,USD,25855.7214,open.er-api.com']
- ✅ chạy lại cùng ngày không nhân đôi dữ liệu
- ✅ code dùng $AO_OUTPUT_DIR

## weekly_report — E4 — báo cáo định kỳ: Analyst chuyển thành automation hằng tuần thay vì làm tay

- ✅ tạo automation thay vì làm tay mỗi tuần — ['doanh-thu-vung-thu-2']
- ✅ lịch thứ 2 lúc 08:00 — weekly:mon@08:00
- ✅ chạy thử thành công — [('succeeded', '')]
- ❌ có file CSV kết quả — []
- ✅ script đọc DB ở chế độ chỉ đọc hoặc không ghi DB

## writer — Writer — bài đăng fanpage từ dữ kiện cho sẵn: hỏi đối tượng/giọng văn, không bịa số liệu, giao file

- ✅ hỏi đối tượng/giọng văn/độ dài trước (hoặc nêu rõ giả định)
- ✅ có ticket viết bài
- ❌ có file bài viết trong workspace — []
- ❌ giữ đúng dữ kiện (giá, thời gian, phạm vi)
- ✅ không bịa thêm giá tiền — set()
- ❌ có ghi nhận kết quả bàn giao (work product)

## Nhật ký

```
[08:05:28] hired Thu as Personal Assistant (haiku)
[08:05:38] USER → Em nhắc anh uống nước sau 2 phút nữa nhé. Và mỗi sáng thứ 2 lúc 9h nhắc anh họp giao ban ở phòng họp tầng 3.
[08:06:08] Thu (posted 08:05:49) → 'Em đã đặt xong hai nhắc nhở:\n\n1. **Uống nước:** một lần lúc **08:07 hôm nay (Chủ nhật 11/10)**.\n2. **Giao ban:** mỗi sáng **thứ 2 lúc 09:00**, bắt đầu từ **thứ 2 ngày 12/10**, nhắc anh họp ở phòng họp tầng 3.'
[08:08:00] USER → Từ giờ đến hết thứ 2 tuần này anh có những lịch gì?
[08:08:30] Thu (posted 08:08:06) → 'Từ giờ đến hết thứ 2 tuần này, anh chỉ có một lịch:\n\n- **Thứ 2, 12/10, 09:00:** họp giao ban ở phòng họp tầng 3. Em sẽ nhắc anh lúc 9h.\n\nNhắc uống nước lúc 08:07 hôm nay đã đến giờ nên không còn trong danh sách lịch sắp tới. Nếu anh chưa thấy tin nhắc đó, em có thể đặt lại.'
[08:08:38] hired Khoa as Data Collector & Automation (sonnet)
[08:08:46] USER → Anh cần mỗi sáng tự lấy tỷ giá USD, EUR, JPY so với VND và lưu lại thành file CSV để theo dõi.
[08:09:16] Khoa (posted 08:08:55) → 'Chào anh, việc này em làm được bằng một script Python chạy mỗi sáng, không cần LLM. Em cần anh chốt vài điểm trước khi viết:\n\n1. **Nguồn tỷ giá**\n   - a) Vietcombank (tỷ giá niêm yết, có mua/bán/chuyển khoản). Đây là mặc định em đề xuất, vì bám sát thực tế giao dịch ở VN.\n   - b) Ngân hàng Nhà nước (tỷ giá trung tâm).\n   - c) API quốc tế miễn phí (ví dụ open.er-api.com), chỉ có một mức giá giữa.\n2'
[08:09:22] USER → Dùng nguồn công khai miễn phí không cần key, ví dụ open.er-api.com hoặc API tương tự.
[08:09:40] confirmed ticket proposal by Khoa: Tự động lấy tỷ giá USD/EUR/JPY so với VND mỗi sáng và lưu CSV → AO-1
[08:09:49] approved Khoa: Bash ls -la
[08:10:13] approved Khoa: Bash uv run python -c "import fx_rates as f; 
for d in [{'result':'error'},{'result':'success','rates':{'VND':1,'EUR':0}},{'r
[08:10:22] approved automation fx-rates-daily (hằng ngày lúc 08:00)
[08:10:49] Khoa (posted 08:09:36) → 'Em đã gửi đề xuất ticket cho anh xác nhận. Anh xác nhận thì em bắt đầu làm.\n\n- **Script:** lấy USD, EUR, JPY so với VND từ open.er-api.com, ghi nối dài vào `fx_rates.csv`. Chạy lại trong cùng ngày thì không bị trùng dòng.\n- **Lịch:** chạy 08:00 hằng ngày, kèm một tin nhắn tóm tắt.\n- **Duyệt:** em sẽ chạy thử, rồi gửi code để anh xem trước khi bật lịch.'
[08:10:51] Khoa (posted 08:10:22) → 'Tỷ giá sáng nay: USD 25,856 · EUR 28,972 · JPY 163.35 (VND, giá giữa; 3 dòng trong CSV)'
[08:10:53] Khoa (posted 08:10:22) → '✅ **AO-1 · Tự động lấy tỷ giá USD/EUR/JPY so với VND mỗi sáng và lưu CSV** — đã xong\n\nĐã xong script `fx_rates.py` (PEP 723, chạy bằng `uv run`) và báo cáo (doc `report`).\n- Chạy thật 2 lần cùng ngày: `out/fx_rates.csv` đúng cột, vẫn 3 dòng ngày 2026-10-11 (USD 25.855,72; EUR 28.972,01; JPY 163,35 VND), không trùng.\n- Validate (result != success, giá 0, thiếu mã) đều báo lỗi đúng và thoát mã 1. Ch'
[08:12:16] hired Linh as Data Analyst (sonnet)
[08:12:26] USER → Từ nay mỗi sáng thứ 2 lúc 8h em gửi anh báo cáo doanh thu tuần trước theo từng vùng nhé (file CSV).
[08:13:03] Linh (posted 08:12:38) → 'Làm được anh nhé. DB có `stores.region` nên em gộp theo vùng được. Em cần anh chốt vài điểm trước khi mở ticket viết script:\n\n1. **Tuần trước** tính từ thứ 2 đến Chủ nhật, tức tuần trước 8h sáng thứ 2 gửi. Em đề xuất giữ cách này. Hay anh muốn cách khác?\n2. **Doanh thu** là tổng `qty × unit_price` trong `order_items`. Đơn có `status` hủy hoặc chưa hoàn tất thì em loại ra. Em sẽ kiểm tra các giá tr'
[08:13:09] USER → Báo cáo doanh thu 7 ngày gần nhất có dữ liệu theo vùng, chỉ đơn paid, lưu CSV; gửi lúc 8h sáng thứ 2 hằng tuần.
[08:13:27] confirmed ticket proposal by Linh: Báo cáo doanh thu 7 ngày gần nhất theo vùng (CSV, tự động 8h thứ 2 hằng tuần) → AO-2
[08:14:19] Linh asked: 'Anh duyệt script doanh_thu_vung.py và CSV kỳ 24–30/09/2026 (tổng 67 đơn, 8.197.000 đ, đã đối chiếu khớp) chưa? Nếu duyệt, em sẽ tạo automation weekly:mon@08:00 (chạy script → notify gửi file + tóm tắt' → 'Báo cáo doanh thu 7 ngày gần nhất có dữ liệu theo vùng, chỉ đơn paid, lưu CSV; gửi lúc 8h sáng thứ 2 hằng tuần.'
[08:14:28] approved automation doanh-thu-vung-thu-2 (thứ 2 hằng tuần lúc 08:00)
[08:14:56] Linh (posted 08:13:26) → 'Em đã đề xuất ticket cho báo cáo này, anh xác nhận thì em bắt đầu. Cách em hiểu yêu cầu:\n\n- **Phạm vi:** chỉ tính đơn `paid`. Trong DB có 627 đơn paid và 44 đơn cancelled, em loại các đơn cancelled.\n- **Doanh thu:** tổng `qty × unit_price`, gộp theo `stores.region`.\n- **Kỳ báo cáo:** 7 ngày gần nhất có dữ liệu, tính đến ngày có đơn mới nhất trong DB. Dữ liệu hiện chỉ đến 30/09, nên kỳ này có thể k'
[08:14:58] Linh (posted 08:14:29) → 'Báo cáo doanh thu theo vùng 7 ngày gần nhất có dữ liệu (chỉ đơn paid):\nFile: doanh_thu_vung_2026-09-30.csv\r\nKỳ: 2026-09-24 -> 2026-09-30\r\nHà Nội: 28 đơn, 3,389,000 đ\r\nĐà Nẵng: 21 đơn, 2,751,000 đ\r\nTP.HCM: 18 đơn, 2,057,000 đ\r\nTổng: 67 đơn, 8,197,000 đ (khớp toàn chuỗi)'
[08:15:00] Linh (posted 08:14:31) → '✅ **AO-2 · Báo cáo doanh thu 7 ngày gần nhất theo vùng (CSV, tự động 8h thứ 2 hằng tuần)** — đã xong\n\nĐã tạo automation `doanh-thu-vung-thu-2` (weekly:mon@08:00): chạy doanh_thu_vung.py (ghi doanh_thu_vung_<ngày cuối kỳ>.csv, tự kiểm tổng vùng = tổng chuỗi) rồi notify gửi đường dẫn file và tóm tắt theo vùng. Automation đang chờ anh duyệt code script trong hệ thống, duyệt xong sẽ bắt đầu chạy từ th'
[08:15:33] hired Lan as Content Writer (sonnet)
[08:15:43] USER → Viết giúp chị bài đăng fanpage cho chương trình khuyến mãi này nhé: Trà đào cam sả size L giá 39.000đ; khuyến mãi mua 2 tặng 1 từ 20/10 đến 31/10/2026 tại 7 cửa
[08:16:12] Lan (posted 08:15:52) → 'Chị ơi, em hỏi nhanh vài ý để bài đúng ý chị, ý nào chị không trả lời thì em dùng mặc định nhé:\n\n1. **Tên thương hiệu và tên cửa hàng:** quán mình tên gì? Chị có danh sách 7 địa chỉ không, hay chỉ ghi "7 cửa hàng tại Hà Nội, TP.HCM, Đà Nẵng"? (Mặc định: ghi chung theo 3 thành phố, kèm câu "xem địa chỉ tại…" nếu chị có link.)\n2. **Giọng văn:** trẻ trung, vui, nhiều emoji; hay gọn gàng, chuyên nghiệ'
[08:16:19] USER → Khách là sinh viên và dân văn phòng trẻ; giọng vui, gần gũi; khoảng 120-180 chữ; đăng Facebook; có lời kêu gọi đến cửa hàng.
[08:16:30] confirmed ticket proposal by Lan: Viết bài đăng fanpage khuyến mãi Trà đào cam sả size L mua 2 tặng 1 → AO-12
[08:16:50] Lan (posted 08:16:31) → 'Em đã đề xuất ticket viết bài Facebook cho chương trình này. Chị xác nhận thì em bắt đầu viết ngay.\n\nNội dung theo đúng ý chị: khách là sinh viên và dân văn phòng trẻ, giọng vui và gần gũi, khoảng 120-180 chữ, có lời kêu gọi đến cửa hàng. Bài nêu rõ giá 39.000đ size L, mua 2 tặng 1 từ 20/10 đến 31/10/2026 tại 7 cửa hàng ở Hà Nội, TP.HCM và Đà Nẵng, chỉ áp dụng khi mua tại quầy, không áp dụng giao '
```
