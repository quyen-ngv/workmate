# Live scenarios — round1-analyst

Chạy lúc 2026-10-11 06:59.

| Kịch bản | Kết quả | Điểm | Checks | Thời gian |
|---|---|---|---|---|
| analyst — Phân tích dữ liệu từ DB (SQLite): doanh thu theo vùng, tăng trưởng, sản phẩm bán chạy — số liệu phải đúng | ✅ | 1.0 | 7/7 | 2.8 phút |

## analyst — Phân tích dữ liệu từ DB (SQLite): doanh thu theo vùng, tăng trưởng, sản phẩm bán chạy — số liệu phải đúng

- ✅ doanh thu T9 Hà Nội đúng (17,891,000)
- ✅ doanh thu T9 TP.HCM đúng (10,434,000)
- ✅ doanh thu T9 Đà Nẵng đúng (12,428,000)
- ✅ tăng trưởng Hà Nội đúng (+9.6%)
- ✅ tăng trưởng TP.HCM đúng (-8.5%)
- ✅ tăng trưởng Đà Nẵng đúng (+19.8%)
- ✅ sản phẩm bán chạy nhất T9: Trà đào
- ghi chú: expected: {"sep": {"Hà Nội": 17891000, "TP.HCM": 10434000, "Đà Nẵng": 12428000}, "aug": {"Hà Nội": 16324000, "TP.HCM": 11403000, "Đà Nẵng": 10374000}, "top": "Trà đào"}

## Nhật ký

```
[06:57:01] hired Linh as Data Analyst (sonnet)
[06:57:07] USER → Chị cần biết doanh thu tháng 9/2026 theo từng vùng, so với tháng 8 tăng hay giảm bao nhiêu %, và sản phẩm nào bán chạy nhất tháng 9 (theo số lượng). Dữ liệu ở f
[06:57:43] Linh → 'Em đã xem qua `sales.db`. Dữ liệu có đủ các cột cần cho 3 câu hỏi của chị: đơn hàng, chi tiết đơn, cửa hàng kèm vùng, và sản phẩm. Dữ liệu chạy từ 01/08 đến 30/09/2026, nên so sánh tháng 9 với tháng 8 được. Trước khi làm, em cần chị chốt 2 điểm:\n\n1. **Đơn bị hủy:** có 44 đơn `cancelled` trên tổng 671 đơn.\n   - Em đề xuất chỉ tính đơn `paid` và loại đơn hủy khỏi cả doanh thu lẫn số lượng bán.\n   - '
[06:57:50] USER → Chỉ tính đơn đã thanh toán (status = paid), bỏ đơn huỷ.
[06:58:01] confirmed ticket proposal by Linh: Báo cáo doanh thu theo vùng tháng 9/2026 so với tháng 8 và sản phẩm bán chạy nhất → AO-1
[06:58:17] approved Linh: Bash mkdir -p /f/_ao_lab/home/ws/AO-1 && cd /f/_ao_lab/home/ws/AO-1 && cat > report.py <<'EOF'
# /// script
# requires-python
[06:59:27] Linh → 'Em đã gửi đề xuất ticket phân tích, chị xác nhận là em bắt đầu làm.\n\nPhạm vi như sau:\n- **Đơn tính:** chỉ đơn `paid`.\n- **Doanh thu:** `qty × unit_price`.\n- **Kết quả:** doanh thu từng vùng tháng 9, % tăng giảm so với tháng 8, và sản phẩm bán chạy nhất tháng 9 theo số lượng.'
[06:59:29] Linh → '✅ **AO-1 · Báo cáo doanh thu theo vùng tháng 9/2026 so với tháng 8 và sản phẩm bán chạy nhất** — đã xong\n\nĐã có tài liệu `report` (rev 1) và script `report.py` + `out.txt` trong F:\\_ao_lab\\home\\ws\\AO-1. Kết quả: T9 toàn chuỗi 40.753.000 đ, +7,0% so với T8 (38.101.000 đ). Theo vùng: Hà Nội +9,6%, TP.HCM -8,5%, Đà Nẵng +19,8%. Sản phẩm bán chạy nhất T9: Trà đào (249 ly). Kiểm tra: tổng vùng bằng tổn'
```
