# Chuẩn hoá tài liệu: Epic → Task → Design, mỗi bước có gate duyệt

Ngày: 2026-11-13 · Trạng thái: đã chốt yêu cầu, chưa làm

## 1. Bạn nói

Yêu cầu gốc:
> BA đưa epic vào, phân tích tài liệu, code, tài liệu liên quan; sau khi phân tích chia ra các task, chi tiết
> nghiệp vụ; sau đó dev design, chốt design với lead. Mỗi bước đều có gate cần duyệt.

Trả lời các câu hỏi:

| # | Câu hỏi | Bạn trả lời |
|---|---|---|
| 1 | Ai viết tài liệu, epic hình thành thế nào | Ban đầu BA đưa epic vào trước, sau đó phát triển dần |
| 2 | Ai duyệt design | Lead duyệt |
| 3 | Sau khi design được chốt | Code luôn, nhưng cho phép chọn code hay không |
| 4 | Epic từ đâu | BA đẩy vào |
| 5 | Mẫu tài liệu | Lấy mẫu chuẩn |

Tôi hiểu như sau (cần bạn xác nhận ở mục 4):
- **(1)** Epic không cần đầy đủ ngay từ đầu. BA đưa vào những gì đang có, sau đó bổ sung tài liệu và yêu cầu dần.
  Mỗi lần bổ sung, AI phân tích lại phần mới và đề xuất thêm task. Các task đã tạo không bị đụng tới, trừ khi
  phần thay đổi ảnh hưởng tới chúng.
- **(2)** Chỉ Lead duyệt design. Dev không phải gate.
- **(4)** BA tự tạo epic trên web (hoặc Telegram). Chưa lấy epic từ Jira.
- **(5)** Dùng bộ mẫu chuẩn của office (skill `doc-standards`), mở rộng thêm mẫu cho epic và task. Chưa có mẫu
  riêng theo công ty hay team.

## 2. Hiện trạng code (đã kiểm tra)

| Thành phần | Có sẵn | Thiếu |
|---|---|---|
| Workflow có các bước và gate (`domain/stage.py`) | `code`, `quick`, `automation`, `research`, `doc`. Team ghi đè được qua `TeamSettings.stage_templates` / `gates` | Workflow `epic` và `task` |
| Bắt AI đọc nguồn (SI-39, `sources_read`) | Lấy ticket, Jira issue, file trong repo và URL từ mô tả ticket; chặn nếu AI chưa đọc | Không |
| Đề xuất nhiều ticket (SI-24, `services/proposals.py`) | Một thẻ duyệt, bỏ hoặc sửa được từng mục, có phụ thuộc giữa các mục, có "chạy ngay" | Không đặt `parent_id`. Người duyệt cố định là owner/lead. Workflow chỉ chọn được `code`/`quick` |
| Gate (`GatePolicy`) | Duyệt theo vai trò, có `fallback` | Gate `plan` mặc định là dev **hoặc** lead, chưa có gate chỉ Lead |
| Kiểm tra tài liệu (`services/checks.py`) | `outputs` (có tồn tại), `ac_testable`, `plan_has_ac_map` | Chưa kiểm tra tài liệu có đúng mẫu |
| Mẫu (`templates/skills/doc-standards`) | `requirements`, `design`, `plan`, `handoff`… và quy ước ID `US`/`AC`/`T`/`D` | Mẫu `analysis` và `task-spec` |
| Sửa tài liệu sau khi đã duyệt | `Document.approved_revision`: có bản sửa mới thì gate mở lại | Không |

## 3. Tôi làm

### Luồng sau khi làm xong

```
EPIC  (BA tạo, workflow "epic")
  analyze    → analysis    [gate analysis: BA]
  breakdown  → breakdown   [gate breakdown: BA]   → tạo TASK con (parent = epic, mỗi task chọn code / không code)
  (BA bổ sung tài liệu → quay lại analyze cho phần mới → breakdown đề xuất thêm task; có thể lặp nhiều lần)
  close      → tổng kết, khi BA đóng epic và mọi task con đã xong   [gate knowledge]

TASK con, chọn "code luôn"   (workflow "task")
  design → design (gồm cả các bước làm)   [gate design: Lead]
  build → pr [merge] → close [knowledge]

TASK con, chọn "không code"   (workflow "task-spec")
  design → design   [gate design: Lead]   → xong
  (đổi sang "task" bất kỳ lúc nào trước khi xong để code tiếp)
```

### Các bước làm, mỗi bước có cách kiểm

| # | Việc | File chính | Kiểm bằng |
|---|---|---|---|
| 1 | **Mẫu chuẩn.** Thêm `analysis.md` (phạm vi, hiện trạng code, tài liệu liên quan, điểm thiếu hoặc mâu thuẫn, câu hỏi mở, giả định) và `task-spec.md` (`T-n`, user story, AC, quy tắc nghiệp vụ, phụ thuộc, nguồn). Sửa `design.md`: thêm mục "Các bước làm" và bảng truy vết AC → thiết kế. Cập nhật bảng trong `SKILL.md` | `templates/skills/doc-standards/` | Test đọc mẫu: mỗi mẫu có đủ heading bắt buộc |
| 2 | **Check `doc_template`.** Tài liệu có đủ heading của mẫu, dòng `Phiên bản`, bảng Change log, mục Giả định, ID đúng định dạng. Chạy trước gate, sai thì AI tự sửa | `services/checks.py` | Unit test: tài liệu đúng mẫu thì qua; thiếu heading hoặc sai ID thì báo đúng chỗ |
| 3 | **Check truy vết.** `breakdown_complete`: mỗi task có AC kiểm chứng được (dùng lại `ac_testable`). `design_covers_ac`: mọi AC của task đều có trong bảng truy vết của design | `services/checks.py` | Unit test cho từng check |
| 4 | **Workflow mới.** `epic` (analyze → breakdown → close), `task` (design → build → pr → close), `task-spec` (design). Thêm tên bước, tiêu đề tiếng Việt, cho đổi qua lại `task` ↔ `task-spec`. Gate `analysis`, `breakdown`: roles `ba`. Gate `design`: roles `lead`, không fallback sang owner (để BA không tự duyệt design) | `domain/stage.py` | Test engine với runtime giả: đi hết từng workflow, gate hỏi đúng người |
| 5 | **Breakdown tạo task con.** Khi BA duyệt gate `breakdown`, mỗi task trong `breakdown` thành ticket con có `parent_id = epic`, cùng team và project. `requirements` của task được chép từ phần của nó và đánh dấu đã duyệt. Trên thẻ duyệt có lựa chọn code / không code cho từng task (mặc định: code) | `services/proposals.py` hoặc service mới `services/epics.py` | Test: duyệt thì số task tạo đúng, có `parent_id`, có `requirements` đã duyệt, đúng workflow; bỏ một task thì task đó không được tạo |
| 6 | **Epic phát triển dần.** BA bổ sung vào epic (sửa mô tả, thêm link hoặc file) khi epic đang mở: epic quay lại `analyze` chỉ cho phần mới, rồi `breakdown` chỉ đề xuất task mới hoặc task bị ảnh hưởng. Task đã có không bị tạo lại. Task bị ảnh hưởng nhận thông báo "yêu cầu đổi" | `services/stages.py`, `services/epics.py` | Test: thêm tài liệu thì chỉ ra task mới; task cũ giữ nguyên, task bị ảnh hưởng có comment |
| 7 | **Tạo epic.** BA tạo epic trên web (nút **+**, loại "Epic") và trên Telegram `/epic`. CLI có `task --template epic` | `web/`, `channels/telegram.py`, `cli_team.py` | Tạo thử trên web (chụp màn hình), qua Telegram và CLI |
| 8 | **Màn hình epic.** Trang epic hiện `analysis`, `breakdown` và danh sách task con với bước và trạng thái từng task; task con có link về epic | `web/static/team/` | Chụp màn hình bằng `ui_screenshot` với dữ liệu seed |
| 9 | **Tài liệu dự án.** README: mục "Epic"; cập nhật `docs/plans/2026-10-31-team-copilot-design.md` | `README.md`, `docs/plans/` | Đọc lại |

Thứ tự làm: 1 → 2 → 3 → 4 → 5 → 7 → 8 → 6 → 9. Bước 6 (epic phát triển dần) là phần khó nhất nên làm sau,
khi luồng một lượt đã chạy.

### Quyết định

- **D-1. Gộp `plan` vào `design` cho task con.** Lead đã duyệt design thì không cần thêm một gate `plan`. Design có
  mục "Các bước làm" để bước build dùng (`requires_steps`).
- **D-2. Gate design chỉ Lead, không fallback.** Team không có ai giữ vai trò Lead thì báo ngay khi bắt đầu epic,
  không để task chờ âm thầm.
- **D-3. Không đổi `GatePolicy`.** Bạn đã chốt chỉ Lead duyệt design, nên không cần thêm kiểu "Dev rồi Lead"
  như tôi đề xuất trước đó.
- **D-4. Chưa làm** mẫu riêng theo team hoặc công ty, lấy epic từ Jira, xuất Confluence. Ghi lại để làm sau.

## 4. Cần bạn xác nhận

1. Cách hiểu "phát triển dần" ở mục 1: BA bổ sung vào epic nhiều lần, mỗi lần AI phân tích phần mới và đề xuất
   thêm task (BA duyệt mỗi lượt). Có đúng không?
2. Epic tự đóng khi mọi task con xong, hay BA bấm đóng? Tôi đang để **BA bấm đóng**, vì epic còn có thể nhận
   thêm yêu cầu.
3. Mặc định khi duyệt breakdown là **code luôn**. Có đúng không?
