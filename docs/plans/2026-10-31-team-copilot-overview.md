# Team Copilot — thiết kế tổng quan (bản để review)

Ngày: 2026-10-31 · Trạng thái: **draft, chờ duyệt** · Nền code: actor-office (pivot)

## 1. Mục tiêu

Một AI engineer làm thành viên của team người thật (BA, Dev, QE). AI nhận request, tự tìm hiểu, hỏi đúng người,
chốt requirement, lên plan cho Dev duyệt, code + test trong worktree, tạo PR, và **tài liệu hóa toàn bộ kiến thức
của ticket** vào knowledge base (Confluence).

Khác với tool "ticket vào → agent làm" của tập đoàn ở bốn điểm, và đây là bốn tiêu chí thành công của POC:

| # | Tiêu chí | Đo bằng |
|---|---|---|
| 1 | **Cộng tác**: AI hỏi, người trả lời, cả hai chiều @mention | Ticket không cần đầy đủ từ đầu vẫn ra PR được |
| 2 | **Confirm**: 3 gate do người giữ, AI không tự quyết | BA chốt requirement, Dev chốt plan, người merge |
| 3 | **Minh bạch**: luôn biết AI đang ở bước nào, đang làm gì, vì sao | Người không phải hỏi "bot đang làm gì?" |
| 4 | **Học**: ticket sau giống ticket trước thì hỏi ít hơn, research nhanh hơn | Số câu hỏi, thời gian research, token theo bước |

Không ngân sách token cứng. Token được **đo** theo ticket và theo bước để so sánh, không dùng để chặn.

## 2. Vai và luồng

```
BA: tạo Jira issue (hoặc nhắn bot)                       ──┐
                                                            ▼
 ①  UNDERSTAND   AI đọc Jira/Confluence/code/KB → "Mình đã biết" → câu hỏi trắc nghiệm cho BA/Dev/QE
                 → Dossier (requirement + AC)            ══ GATE 1: BA chốt ══  → ghi lên Jira
 ②  PLAN         2–3 phương án có trade-off, file sẽ sửa, rủi ro, test plan
                 Dev sửa trực tiếp rồi duyệt              ══ GATE 2: Dev chốt ══
 ③  BUILD        worktree riêng · code + test · check script của repo · lệch plan → comment "deviation"
 ④  PR           tạo PR Bitbucket · đọc comment review · sửa · trả lời      ══ GATE 3: người merge ══
 ⑤  CLOSE        Dossier → Confluence · rút 5–10 mục KB · đề xuất rule · người tick ✅/❌ một lượt
```

- Mỗi bước là **một session mới**; Dossier là bản bàn giao giữa các bước (vừa là tài liệu, vừa là context nén).
- Requirement đổi giữa chừng: AI đang chạy bị ngắt ở tool call kế tiếp và đọc lại (đã có `change_notify`).
- Người không trả lời: mọi câu hỏi có đáp án đề xuất; BA chọn chế độ *Chờ* hoặc *Làm theo giả định* (giả định ghi
  rõ trong Dossier và PR).
- Bế tắc (thay cho budget): cùng một test fail 3 lần cùng cách sửa, hoặc đọc lại file đã đọc → dừng, hỏi Dev kèm
  tóm tắt đã thử gì.

## 3. Kiến trúc

```
Kênh:      Telegram (POC) · Web UI · [Teams, Slack sau]            ← ChatChannel
             │ topic per ticket, card có nút, progress card sửa tại chỗ
Dịch vụ:   users/roles · teams · tickets (mirror Jira) · stages · dossier · rules · knowledge · interactions
Engine:    Scheduler → Executor → RuntimeRouter → Claude SDK | Kiro ACP
                                  └ OfficeGate: policy + rules(check/policy) + approvals (PreToolUse)
Tools:     office tools (MCP in-process) · Atlassian Rovo MCP (Jira, Confluence, Bitbucket PR) · reference repos (read-only clone)
Store:     SQLite + FTS5 (POC) · files: workspaces, rules/, skills/, kb/ (git)
```

Giữ nguyên tắc một chiều của actor-office: `channels → engine → services → store → domain`.

### Tái sử dụng từ actor-office

| Giữ | Đóng băng (không xóa) | Thêm mới |
|---|---|---|
| tickets, documents có revision, `done_docs` | nhiều actor / positions (giữ 1 actor "AI") | users + roles (Telegram id → BA/Dev/QE), teams |
| interactions + card Telegram | PM actor, chat actor↔actor | Jira mirror (Jira là nguồn sự thật) |
| OfficeGate, policy, `extra_read_dirs` | automations v1 | stage state machine, session per stage |
| worktree per ticket, merge có test | — | Dossier, approve theo vai (`APPROVE_DOC`) |
| Telegram forum topics, `tg_progress` | — | Rules service (mục 6) |
| usage per ticket, `history_fts`, memories FTS5 | — | distill + KB có nguồn + Confluence publish |
| `ask_colleague` → đổi thành `ask_human(role)` | — | stall detection, activity timeline chuẩn hóa |
| Kiro ACP runtime (đã có, chưa test thật) | — | test thật với `kiro-cli`, parity sự kiện |

## 4. Runtime: Claude và Kiro

Công ty dùng Kiro, nên **cả hai runtime là bắt buộc**, chọn theo team hoặc theo ticket (`model: "kiro"` đã có
trong `RuntimeRouter`). Mọi thứ phía trên runtime (gate, rules, dossier, tools, sự kiện) phải giống hệt nhau.

| Khả năng | Claude Agent SDK | Kiro CLI (ACP) | Hệ quả thiết kế |
|---|---|---|---|
| System prompt | có | không → mở đầu tin nhắn đầu tiên | Rule/dossier đưa vào "prompt pack" độc lập runtime |
| Park tool call chờ người | có (`defer`) | không → từ chối, hủy turn, wake lại khi duyệt | Gate đã xử lý cả hai, giữ nguyên |
| Resume session | có | có (ACP session) | Session per stage áp dụng cho cả hai |
| Subagent | có | không chắc | Research không được phụ thuộc subagent; làm bằng tool |
| Hook PreToolUse | có | fs/terminal đi qua office, `request_permission` | Check/policy rule phải nằm ở gate, không ở hook riêng Claude |
| Sự kiện stream | messages | `session/update` | Chuẩn hóa thành run_events chung (mục 5) |
| Token/usage | có | có hạn chế | Đo cái có, ghi rõ runtime nào |

Nguyên tắc: **không có tính năng nào chỉ chạy trên Claude**. Tính năng mới phải chạy được qua tool + gate + prompt.

## 5. Minh bạch: biết AI đang ở đâu, làm gì, vì sao

Đây là yêu cầu hàng đầu, thiết kế ở ba tầng, cùng một nguồn dữ liệu là `run_events`.

### Tầng 1 — Tiến độ ticket (nhìn 2 giây)
- Stage hiện tại + trạng thái: `Researching` / `Needs you: BA` / `Waiting approval: Dev` / `Building` / `Tests failing` / `PR open`.
- Telegram: **một progress card trên đầu topic, sửa tại chỗ** (không spam), có nút Pause / Stop / "Xem chi tiết".
- Web: sidebar kiểu herdr, badge "Needs you" theo người đang đăng nhập; inbox "Cần tôi" gom mọi ticket.

### Tầng 2 — Hoạt động live của run (nhìn 30 giây)
Sự kiện chuẩn hóa, **giống nhau cho Claude và Kiro**:

| kind | ví dụ hiển thị |
|---|---|
| `narration` | "Đang tìm chỗ xử lý refund trong `billing/`" |
| `read` / `search` | "Đọc `RefundService.java` (120 dòng)", "Grep `roundingMode` → 4 file" |
| `source` | "Đọc Confluence: Refund policy v2 (cập nhật 2025-03)" |
| `edit` | "Sửa `RefundService.java` +18 −4" (bấm xem diff) |
| `command` | "`mvn -q test -pl billing` → 2 fail" (bấm xem output) |
| `question` / `approval` | "Hỏi BA: … (chờ 2h)" |
| `rule.applied` / `check` | "Check `coverage>=80`: pass", "Rule R-12 chặn `git push --force`" |
| `deviation` | "Lệch plan: phải sửa thêm `InvoiceMapper`" |

Web ticket page: timeline bên trái, **Files / Changes** bên phải (diff live so với base branch), tab Commands.

### Tầng 3 — Bằng chứng và lý do (nhìn 5 phút)
- Dossier: mỗi quyết định có link tới tin nhắn hoặc nguồn đã sinh ra nó.
- Plan đã duyệt và danh sách deviation.
- Kết quả check script thật (máy chạy), không phải AI tự báo.
- Rules có hiệu lực trong run này (phiên bản cụ thể).
- Token và thời gian theo bước.

### Điều khiển
Pause (giữ worktree), Stop (kết thúc stage, ghi lý do), Steer (chen chỉ dẫn vào turn kế), Retry stage, "Nói lại cho
dễ hiểu" (AI tóm tắt 10 sự kiện gần nhất bằng tiếng thường).

## 6. Rules: AI có luật, nhiều team, nhiều người thêm

### Mô hình
```
Rule
  id, title, body (markdown)
  kind:    guideline | check | policy | gate
  scope:   global | team:<id> | project:<id> | repo:<id>
  stage:   any | understand | plan | build | pr | close
  owner, created_by, approved_by, status: draft | active | disabled
  version (mỗi lần sửa tăng), history
```

| kind | là gì | thực thi bởi |
|---|---|---|
| **guideline** | "Test dùng JUnit5 + AssertJ", "không `${}` trong MyBatis" | đưa vào prompt pack đúng stage |
| **check** | script/lệnh phải pass: `mvn verify`, coverage, lint, "không sửa migration cũ" | engine chạy sau stage, fail → AI sửa, không qua được |
| **policy** | cấm/ép tool: không push main, không `--force`, không đọc ngoài repo | OfficeGate (PreToolUse), chỉ admin đặt |
| **gate** | cần ai duyệt cái gì: "đổi schema → Dev lead", "AC → BA và QE" | stage machine chọn người được hỏi |

### Quản lý
- **Lưu dạng file**: `rules/<scope>/<id>.md` có frontmatter, trong git (giống skills hiện có) → có lịch sử, diff,
  review như code; DB chỉ index. Người sửa được bằng web editor hoặc sửa file.
- **Ai thêm**: mọi thành viên đề xuất (draft); **owner của scope** duyệt (team lead cho team, admin cho global).
  Policy chỉ admin. AI cũng đề xuất rule từ bài học (stage Close) → draft chờ duyệt, không bao giờ tự active.
- **Xung đột**: scope hẹp thắng scope rộng; cùng scope thì rule mới hơn được cảnh báo trùng khi tạo; policy
  không bị guideline ghi đè.
- **Áp dụng**: đầu mỗi stage, engine "biên dịch" tập rule có hiệu lực → một section trong prompt + danh sách check
  + policy nạp vào gate. Run ghi lại **id@version** của mọi rule đã dùng (audit, tái hiện được).
- **Hiển thị**: ticket page có tab "Rules in effect"; mỗi sự kiện `rule.applied`/`check` trỏ về rule.
- **Nhập có sẵn**: `CLAUDE.md`, `AGENTS.md`, `.kiro/steering/*.md` trong repo được nhập thành guideline scope repo
  (một chiều, đánh dấu nguồn) để team không phải viết lại.

## 7. Kiến thức

- **Dossier** (document key `dossier`): bối cảnh, requirement/AC + lịch sử, Q&A (ai trả lời), quyết định có link,
  kỹ thuật (file/module, bẫy), test, deviation, link Jira/PR/Confluence.
- **KB** 3 loại: `fact` (rule nghiệp vụ, thuật ngữ), `code_note` (gắn `file@commit`, stale khi file đổi), `decision`.
  Mỗi mục có scope (team/project/repo), nguồn (message/ticket/file), trạng thái (draft/verified/stale).
  "Ai biết gì" suy ra từ metadata Q&A, không cần LLM.
- **Distill** một lần ở Close bằng model rẻ; người tick ✅/❌ một lượt trên Telegram/web.
- **"Mình đã biết"** ở đầu Understand: `history_fts` + KB theo module → hiện cho người kèm nguồn.
- **Confluence** (một chiều): trang Dossier per ticket dưới trang project; trang KB per loại; API v2, markdown → storage.

## 8. Kết nối

| | POC | Sau |
|---|---|---|
| Jira / Confluence / Bitbucket PR | **Atlassian Rovo MCP** (chính thức, hỗ trợ Bitbucket Cloud) | DC adapters nếu cần |
| Đọc code (repo chính và repo khác) | clone local; `worktree` cho repo đang sửa, `read_only` cho repo tham chiếu; tool `repo_fetch` | — |
| Chat | Telegram forum group (topic per ticket, bot tắt privacy mode) | Teams (M365 Agents SDK), Slack |
| Model | Claude (API key org) · Kiro CLI | — |

Tool ghi (PR, comment, cập nhật Jira) luôn qua gate; merge không cấp cho AI.

## 9. Đa team ngay từ POC (mô hình dữ liệu, chưa cần hạ tầng)

`Team → members (role) → projects/repos → rules scope team → KB scope team → Jira project / Confluence space`.
Mặc định team A không thấy dữ liệu team B. Hạ tầng (Docker → K8s, sandbox per ticket) để sau; chỉ giữ hai interface
mỏng: `WorkspaceProvider` (worktree local) và `ChatChannel`.

## 10. Lộ trình

| Phase | Nội dung | Kiểm chứng |
|---|---|---|
| P0 | Commit baseline actor-office · users/roles/teams · Jira mirror · stage machine · topic per ticket · run_events chuẩn hóa + progress card | Tạo Jira issue → topic xuất hiện, card tiến độ cập nhật |
| P1 | Understand: Rovo MCP, reference repos, "Mình đã biết", câu hỏi trắc nghiệm theo vai, Dossier, Gate 1 → ghi Jira | Một ticket thật ra AC được BA chốt |
| P2 | Rules service (file + editor + compile + audit) · Plan có phương án, Dev sửa và duyệt (Gate 2) | Rule team A không ảnh hưởng team B; run log ghi rule@version |
| P3 | Build + check + stall detection · PR Bitbucket · vòng comment · Files/Changes live | PR có test pass, AC map test |
| P4 | Close: distill, KB, Confluence publish, AI đề xuất rule · **Kiro parity test** toàn luồng | Ticket thứ 2 cùng loại: ít câu hỏi hơn, research nhanh hơn |
| P5 | Teams adapter, web UI kiểu herdr hoàn chỉnh, sandbox/Docker | — |

## 11. Rủi ro và câu hỏi mở

1. **Kiro**: chưa chạy thật `kiro-cli acp`; cần máy có Kiro để test ngay ở P0 (ít nhất smoke test), tránh đến P4 mới
   phát hiện khác biệt lớn (vd. không có system prompt, không park được tool call).
2. **Billing**: Agent SDK trên gói subscription tính vào "Agent SDK credit" riêng từ 6/2026 → dùng API key org.
3. **Rovo MCP** phải được admin Atlassian bật; cần xin sớm, cùng lúc với xin bot Teams.
4. **Rule phình**: 20 team × nhiều người thêm → cần owner rõ và cảnh báo trùng; giới hạn số guideline nạp vào prompt
   mỗi stage (ưu tiên scope hẹp), còn lại tra bằng tool `rule_search`.
5. **KB sai lan truyền**: mục chưa verify được gắn nhãn khi AI trích; người tick một lượt ở Close.
6. **Telegram** chỉ cho POC với dữ liệu ngoài công ty.

## 12. Cần bạn quyết

- [ ] Đồng ý 3 gate (BA, Dev, merge), QE không có gate riêng?
- [ ] Rule: lưu file + git như đề xuất, hay chỉ trong DB?
- [ ] Kiro: có máy để test `kiro-cli` ngay P0 không?
- [ ] Bắt đầu P0 sau khi duyệt doc này?
