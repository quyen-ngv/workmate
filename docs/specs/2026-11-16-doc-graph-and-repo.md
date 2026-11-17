# Liên kết giữa các story và kho tài liệu git

Ngày: 2026-11-16 · Trạng thái: đã làm, có test · Nối tiếp: `docs/specs/2026-11-13-doc-core.md`

## 1. Vấn đề

Doc Core đã giữ được truy vết **dọc** (REQ → AC → C) và bắt tài liệu viết lại khi tài liệu nguồn đổi. Còn thiếu:

1. **Liên kết ngang** giữa các story: T-2 cần T-1 xong trước, design của AO-13 dùng API mà AO-12 thiết kế, hai task đụng
   cùng một bảng. Trước đây chỉ là chữ trong bảng "Thứ tự và phụ thuộc", máy không đọc, AI chỉ được nhắc "hãy đọc task
   liên quan".
2. **Cây thư mục / bộ tài liệu đọc được bằng công cụ thường**: tài liệu nằm trong SQLite, muốn grep, xem cả cây, review
   bằng diff, cho Kiro / IDE đọc thì không được.
3. **Tìm khắp nơi**: `kb_search` chỉ tìm kiến thức, `history_search` tìm theo ticket; không có cách tìm "mọi tài liệu
   đã viết về hạn mức hoàn tiền" và đọc đúng mục.

## 2. Liên kết có kiểu

Cú pháp, dạng dòng như `Truy vết:` (từ khoá là dữ liệu của pack, `syntax.relations`):

| Dòng | Nghĩa | Hệ quả |
|---|---|---|
| `Phụ thuộc: T-1` · `Phụ thuộc: AO-14` · `Phụ thuộc: AO-14/C-2` | cần cái kia trước / dựa vào đúng mục đó | luật chống vòng; mục kia đổi sau khi duyệt → tài liệu này bị gọi lại (`R-BASELINE`); cái kia chưa duyệt → cảnh báo `R-DEP-READY` |
| `Liên quan: …` | đụng cùng một chỗ, nên đọc của nhau | hiện trong Context của AI, trên thẻ ticket, trong kho |
| `Thay thế: …` | cái này thay cái kia | như trên |

Viết được ở ba chỗ: ngay dưới một mục (liên kết của mục), một dòng ngoài mọi mục (của cả tài liệu), hoặc trong mục có
heading là từ khoá (`## Phụ thuộc` của spec: mọi mã trên các dòng của mục đó).

Lõi (`doccore/`):
- `Item.relations`, `ParsedDoc.relations`; `TraceGraph` có thêm các link có kiểu; `trace.link_target` phân biệt mục và
  ticket (`T-1` là mục vì `T` là loại mục của pack; `AO-14` là ticket).
- `R-LINK-VALID` (luôn bật): mục được nhắc phải tồn tại. Kiểu luật mới `acyclic` (`R-DEP-ACYCLIC` trong pack mặc định).
- `TraceGraph.affected` tính cả link có kiểu: design dựa vào `AO-12/C-2` bị gọi lại khi C-2 đổi, không khi C-1 đổi.
- Tài liệu của ticket chỉ được liên kết tới (`far`) dùng để kiểm `AO-14/C-2` và thấy nó đổi; luật không soi chúng như tài
  liệu của chính ticket, và mã trần (`C-2`) không bao giờ trỏ sang đó.

Văn phòng (`services/links.py`, bảng `ticket_links`):
- Liên kết đọc từ tài liệu (`origin = doc`) được ghi lại mỗi lần tài liệu lưu, nên không lệch với chữ. Dòng `Phụ thuộc: T-1`
  dưới `T-2` của breakdown thành liên kết giữa hai ticket con khi breakdown được duyệt (qua `doc_spawned`).
- Người thêm tay (`origin = manual`) trên trang ticket hoặc tool `ticket_links`; thêm phụ thuộc tạo vòng bị từ chối.
- `DocService`: bộ kiểm có thêm tài liệu của ticket được phụ thuộc; baseline ghi cả bản của chúng; sweep sau một lần duyệt
  chạy cả trên ticket phụ thuộc vào ticket vừa duyệt.
- Context của AI có mục "Linked tickets" (cả hai chiều) và dàn ý design/spec của ticket nó phụ thuộc.

## 3. Kho tài liệu git

Bật bằng `docs_repo: docs` (tương đối với data_dir) trong config.yaml.

```
README.md                         danh mục: mọi epic, ticket, kiến thức (văn phòng ghi)
AGENTS.md                         quy ước cây thư mục và cú pháp liên kết, cho người và cho Kiro / IDE
epics/AO-12-nang-han-muc/
  README.md                       thẻ ticket: trạng thái, tài liệu, task, liên kết (văn phòng ghi)
  analysis.md  breakdown.md  spec.T-1.md
  stories/AO-13-ly-do-tu-choi/    README.md  spec.md  design.md
tickets/AO-5-.../                 ticket có tài liệu, không thuộc epic
knowledge/
  README.md                       danh mục kiến thức
  decisions/ facts/ code-notes/ contracts/ flows/   một mục một file: 0007-ten.md
  library/                        tài liệu mọi người tự thêm: chỉ được đánh chỉ mục, không bị ghi đè
```

Mỗi file văn phòng ghi có khối đầu YAML (ticket, tài liệu, bản, trạng thái duyệt, viết dựa trên bản nào). Phần thân là
đúng tài liệu.

**Cập nhật nhanh** (yêu cầu chính):
- Văn phòng → file: nghe sự kiện của văn phòng, ghi file ngay (vài giây), nên grep thấy ngay. Commit thì gom: đợi
  `docs_commit_s` (20 s) không có thay đổi, nên một lượt AI lưu 10 lần thành 1 commit; một lần duyệt commit ngay.
- File → văn phòng: quét thư mục mỗi `docs_scan_s` (5 s), chỉ đọc nội dung file có kích thước / thời gian đổi; luôn quét
  trước khi commit. Sửa thân tài liệu → bản sửa mới (tác giả `git:<tên>`), đi qua đúng cửa lưu như mọi bản khác (baseline,
  liên kết); tài liệu đã duyệt bị sửa phải duyệt lại (ghi rõ trên ticket). File `<tên>.md` mới trong thư mục ticket →
  tài liệu mới. Sửa file kiến thức → nội dung + đã xác nhận. File mới trong `knowledge/facts/`… → mục kiến thức đã xác nhận.
- Cả hai cùng sửa một tài liệu: gộp theo dòng (`git merge-file`); trùng chỗ thì lấy file; không mất gì vì mọi bản còn
  trong DB.
- Remote: `docs_remote` (+ `docs_connection` để lấy token, qua env của tiến trình git, không ghi vào `.git/config`). Push
  sau mỗi commit; fetch mỗi `docs_pull_s` (120 s) và **ngay** khi code host báo push (webhook). Gộp: phía remote thắng
  khi trùng chỗ; rồi đọc như mọi thay đổi bên ngoài.

**Tìm**: mọi file văn bản được cắt theo heading vào FTS5 (bỏ dấu). Tool cho AI: `docs_search` (xếp hạng, ưu tiên tài liệu
hơn thẻ/danh mục sinh tự động), `docs_grep` (regex, cho mã như `AO-12/REQ-2`), `docs_read` (cả file / một mục / dải dòng),
`docs_tree`. Web: ô "Tìm trong mọi tài liệu" ở trang Kiến thức, nút Đồng bộ, trạng thái kho.

## 4. Quyết định

- **D-1. DB vẫn là nguồn sự thật cho luồng duyệt; git là bản dạng file, đồng bộ hai chiều.** Cân nhắc: git là nguồn duy
  nhất. Bỏ vì gate cần ghim đúng bản đã duyệt và kiểm trong transaction; đọc git mỗi lần kiểm chậm và dễ lệch khi có
  commit đang dở. Đổi lại: một lớp đồng bộ, có test cho từng chiều.
- **D-2. Không dùng embedding / vector.** Đơn vị chưa có hạ tầng; grep + FTS + đồ thị liên kết đủ cho vài nghìn tài liệu,
  và AI (Kiro, Claude) tìm theo kiểu agentic search tốt hơn RAG ở quy mô này.
- **D-3. Một file một tài liệu, tên ổn định theo mã ticket.** `grep -rn "AO-14"` thấy mọi chỗ nhắc ticket; đổi tiêu đề thì
  thư mục được đổi tên theo (`_move`), mã giữ nguyên.
- **D-4. Liên kết lưu một chiều (bên viết), chiều ngược suy ra** (như Doorstop): không có hai bản để lệch nhau.
- **D-5. File sinh tự động (README, AGENTS.md, khối đầu) bị ghi đè.** Sửa tay ở đó vô nghĩa; AGENTS.md nói rõ.

## 5. Chưa làm

- Xoá tài liệu bằng cách xoá file: hiện file bị ghi lại (DB là nguồn sự thật; xoá tài liệu đã duyệt cần quy trình).
- Màn hình xem diff giữa các commit của kho (git log / IDE làm được).
- Đổi tên file tài liệu trong kho thành đổi khoá tài liệu.
