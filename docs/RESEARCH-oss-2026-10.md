# Học hỏi từ cộng đồng open source (10/2026)

Bản tổng hợp bốn nhánh research làm ngày 11/10/2026:

1. Hermes Agent và các agent cá nhân tự cải thiện.
2. Công cụ automation.
3. Framework kiểu "công ty AI".
4. Các hệ thống memory cho agent.

Phần lớn số liệu benchmark do chính nhà phát triển công bố, nên chỉ nên tham khảo. Mục nào ghi **[?]** là chưa kiểm chứng được.

## 1. Mỗi nhóm làm thế nào, tóm tắt

### Hermes Agent (Nous Research, MIT, v0.21.6 ra ngày 08/10/2026)

Bản chất là **một agent cá nhân chạy 24/7, tự học dần**. Đây là nhóm gần với actor-office nhất.

**Memory**
- Gồm 2 file có giới hạn cứng: `MEMORY.md` (khoảng 2.200 ký tự) và `USER.md` (khoảng 1.375 ký tự).
- Khi file đầy, tool trả lỗi kèm toàn bộ danh sách, và agent **phải tự gom gọn**. Không có bước compaction tự động nào.
- Memory được nạp như một **snapshot đóng băng** lúc đầu phiên, để giữ prompt cache.
- Mọi phiên được lưu vào SQLite FTS5. Agent tìm lại bằng `session_search`, kết quả là tin nhắn gốc.

**Vòng học**
- Sau mỗi lượt, một **fork chạy nền** dùng model rẻ và chỉ có quyền với memory và skill. Nó review lượt vừa làm rồi tạo hoặc vá skill (`SKILL.md`, ưu tiên sửa kiểu patch old→new).
- Có linter nhắc "bài học, không phải nhật ký".
- Có thể bật `write_approval` để skill mới phải chờ duyệt (`/skills diff|approve`).
- **Curator** chạy hằng tuần khi máy rảnh: skill không dùng 30 ngày bị đánh dấu "cũ", 90 ngày thì lưu trữ. Curator không bao giờ xóa skill.

**Cron**
- Lịch viết bằng ngôn ngữ tự nhiên, cron hoặc ISO. Mỗi lần chạy là **một phiên mới, prompt phải tự đủ thông tin**.
- Bước script chạy trước có thể in `{"wakeAgent": false}` để **bỏ qua LLM** ở lượt đó. Marker `[SILENT]` dùng khi không có gì để báo.
- Khi lỗi:
  - lỗi gom theo **chữ ký lỗi** thành một incident;
  - retry sau 5, 15, 30 phút;
  - job tạm dừng khi hết quota;
  - sau khi khởi động lại, lượt bị lỡ được chạy bù đúng 1 lần.

**Kanban**
- Là bảng việc bền vững có các trạng thái triage → todo → ready → running → blocked → review → done.
- Việc được claim nguyên tử và có heartbeat. Claim bị treo thì được thu hồi.
- Worker **bắt buộc** kết thúc bằng một trong ba lệnh: complete, block (kèm lý do) hoặc xin review. Thoát mà không gọi lệnh nào bị tính là vi phạm.

**Subagent (`delegate_task`)**
- Subagent con không được hỏi người dùng, không được ghi memory và không được gửi tin nhắn.
- Kết quả phải theo `output_schema`. Cost của con được cộng vào cha.

**An toàn**
- Có 3 lớp: blocklist cứng (vẫn áp dụng cả ở chế độ YOLO), deny glob, và một LLM nhỏ chấm rủi ro rồi đẩy lên người dùng khi không chắc.
- Prompt hỏi duyệt hết hạn sau 300 giây và **mặc định từ chối**.
- Hermes đọc lịch sử duyệt để gợi ý allowlist, nhưng không tự áp dụng.

**Use case thật**
- Nguồn: bài viblo và danh sách awesome-hermes-usecases.
- Các ví dụ:
  - morning briefing gửi qua Telegram;
  - tóm tắt hộp thư;
  - theo dõi giá nhà, cổ phiếu, hồ sơ công bố (script lọc trước, LLM chỉ chạy khi có tin);
  - học giọng văn qua khoảng 10 vòng góp ý rồi tự viết thành skill `house-style`;
  - review PR, deploy qua SSH;
  - "swarm" theo kanban: worker → verifier → synthesizer.
- Người dùng kể mất khoảng 1–3 tuần sửa trước khi agent "hiểu mình", và **thư viện skill khó debug**.

**Cảnh báo:** bài *Skill Misevolution* (arXiv 2608.12851) thử 21 hệ tự tiến hóa skill.
- **Cả 21 hệ đều lưu cả quy trình nguy hiểm thành skill.** Ở 15 hệ, hành vi đó còn mang sang các phiên sau.
- Kiểm tra skill cả lúc ghi lẫn lúc nạp làm tỉ lệ gây hại giảm từ 21,3% xuống 4,0%.

### OpenClaw, Letta, Goose
- **OpenClaw**:
  - mỗi agent có `SOUL.md` (tính cách, ranh giới), `USER.md`, `HEARTBEAT.md` và nhật ký `memory/YYYY-MM-DD.md`;
  - **heartbeat** là lần thức dậy định kỳ để chạy checklist, và im lặng (`HEARTBEAT_OK`) nếu không có gì.
- **Letta**:
  - memory block có giới hạn và có thể **chia sẻ giữa nhiều agent**;
  - **sleep-time agent** suy ngẫm bất đồng bộ;
  - từ 2026 có MemFS: memory là markdown nằm trong git.
- **Goose**: phân biệt subagent (dùng một lần) với sub-recipe (tái sử dụng được). Cách phân biệt này giống agent step với automation của mình.

### Công cụ automation (n8n, Windmill, Trigger.dev, Inngest, Kestra, Activepieces, Huginn)
- **Truyền dữ liệu theo id bước**, ví dụ `results.<id>` (Windmill) hoặc `$json` (n8n).
- **Durable step**: mỗi bước lưu kết quả, nên chạy lại chỉ tiếp từ bước còn dở (Inngest, Windmill WAC).
- **Bước chờ duyệt giữa flow**: token kèm timeout, hết hạn thì hủy hoặc chạy tiếp. Không cho người tạo tự duyệt.
- **Duyệt từng tool** trong AI agent (n8n): người duyệt thấy tên tool và tham số; nếu từ chối, model nhận được thông báo đó.
- **Idempotency key** cho mỗi lần chạy (Trigger.dev).
- **Lỗi tạm thời và bug được tách riêng**: lỗi tạm thì retry kèm backoff; lỗi kiểu `NonRetriableError` thì không retry.
- **Bỏ qua nếu lần trước còn đang chạy**. Có handler khi hỏng N lần và khi hồi phục.
- **Phiên bản bất biến** (Activepieces): sửa trên draft, publish thì khóa phiên bản.
- **Dry run** (Huginn), pinned data (n8n), và đánh giá bằng bộ dữ liệu đầu vào/kỳ vọng (n8n Evaluations).
- **Trigger** gồm schedule, webhook, flow-trigger (khi flow khác xong), polling, realtime. Kestra còn có "MCP Tool trigger" để biến flow thành tool cho agent.
- **Agent tự viết và gọi workflow** qua MCP (n8n, Windmill). Schema đầu vào của script dùng luôn làm định nghĩa tool.

### Framework "công ty AI"

**Paperclip** (khoảng 100k sao)
- Sơ đồ tổ chức gồm cả người và agent. Mọi việc đi qua issue có một assignee, checkout nguyên tử, và lỗi 409 thì không retry.
- **@mention chỉ để báo, không đánh thức ai.** Muốn nhờ việc thì tạo issue "courier" giao cho người kia.
- Câu hỏi làm rõ là một record có cấu trúc. Trong lúc chờ, issue nằm ở `in_review`; câu "tôi sẽ chờ" viết bằng văn xuôi **không** được tính là trạng thái chờ hợp lệ.
- **Execution policy**:
  - các bước review và approve được runtime ép buộc;
  - "request changes" trả việc về cho người làm trước;
  - mỗi quyết định được ghi audit.
- **Budget** theo công ty, agent và dự án: cảnh báo ở 80%, tự dừng ở 100%.
- **Task watchdog**: khi cả cây issue im lặng, đánh thức một người review để đối chiếu bằng chứng với lời khai "đã xong".

**MetaGPT**
- Quy trình (SOP) nằm sẵn trong prompt.
- Các role trao đổi bằng **tài liệu** (PRD, design, task list) chứ không chat. Tài liệu vào message pool, mỗi role subscribe theo loại tài liệu.

**ChatDev**: các bản experiential co-learning và iterative refinement giữ một kho "đường tắt" rút ra từ các lần làm trước, có cắt tỉa.

**CrewAI**
- Guardrail của task là hàm `(bool, value)` hoặc tiêu chí do LLM chấm, retry tối đa 3 lần.
- Memory có cây scope. Recall chấm điểm theo 0,5 ngữ nghĩa + 0,3 độ mới + 0,2 độ quan trọng. Khi một memory mới giống memory cũ trên 0,85, LLM quyết định giữ, sửa hay xóa.

**OpenHands**
- Có chính sách xác nhận ConfirmRisky (LLM phân tích rủi ro), **stuck detector** bật mặc định, và condenser lịch sử.
- TheAgentCompany: hệ tốt nhất hoàn thành trọn 30% việc, chi phí khoảng $4/việc.

**MAST** (thống kê 1.642 trace), các lỗi hay gặp nhất:
- lặp bước: 15,7%;
- nói một đằng làm một nẻo: 13,2%;
- không biết khi nào dừng: 12,4%;
- không theo spec: 11,8%;
- verify sai: 9,1%;
- không hỏi lại khi cần: 6,8%.

**Lưu ý cho Claude Agent SDK**: `ResultMessage.usage` **không gồm subagent**, phải dùng `model_usage`. Hiện actor của mình không gọi subagent nên số liệu vẫn đúng. Khi bật subagent thì phải sửa chỗ này.

### Memory (mem0, Zep/Graphiti, Letta, LangMem, Cognee, MIRIX, Supermemory, Honcho…)
- **mem0 đổi hướng (04/2026)**: bỏ cơ chế LLM tự UPDATE/DELETE, chuyển sang chỉ ADD vì gộp kiểu phá hủy làm mất thông tin.
- **Graphiti/Zep**: fact có thời hạn hiệu lực. Fact mâu thuẫn thì *vô hiệu hóa* fact cũ (đặt ngày kết thúc) chứ không xóa. Cần Neo4j, không chạy được trên SQLite.
- **Baseline đơn giản rất mạnh**: Letta đạt 74% LoCoMo chỉ bằng file và grep. Full-context còn thắng cả mem0 lẫn Zep trong chính paper của mem0. Bộ đáp án LoCoMo sai khoảng 6%.
- **Kiến trúc chung của các hệ tốt**:
  - lõi nhỏ luôn nạp vào prompt;
  - phần còn lại truy hồi theo nhu cầu (FTS/BM25, có thể thêm vector);
  - trích xuất memory **sau** mỗi lượt chạy, không làm giữa lượt;
  - tóm tắt theo từng tập/phiên;
  - truy hồi có ý thức về thời gian;
  - memory dạng quy trình được nâng thành skill.

## 2. Đối chiếu với actor-office

Những gì mình **đã có**, ngang hoặc hơn các dự án trên:
- **Ticket**: cây ticket, ticket `change_request`, ngắt agent đang chạy qua gate khi yêu cầu đổi (giống Paperclip).
- **Kiểm soát**: Definition of Done theo vị trí (guardrail tất định), automation duyệt theo fingerprint code.
- **Tự xử lý lỗi**: fix ticket khi automation lỗi, tự dừng sau 3 lần.
- **Phối hợp**: `ask_colleague`, phòng họp cho mỗi cây ticket.
- **Quan sát và đánh giá**: usage theo từng tin nhắn (Hermes chưa có thống kê diff), chấm điểm kịch bản kiểu TheAgentCompany.

Phần **memory** của mình thực ra đã giống Hermes ở các điểm chính:
- dùng FTS5 có tìm không dấu và xếp hạng bm25;
- nạp vào prompt theo ngân sách khoảng 2.500 ký tự: memory ghim, rồi memory khớp với việc, rồi memory gần đây;
- actor tự ghi bằng `memory_save` và tự tìm bằng `memory_search`;
- actor tự viết skill của riêng mình bằng `skill_save`;
- "Gom gọn" chỉ xóa mềm nên vẫn khôi phục được.

**Điểm yếu** so với cộng đồng:
1. **Tìm lịch sử**: actor chưa tìm được trong ticket, tài liệu và chat cũ (Hermes có `session_search`).
   **Đã làm ngày 11/10:** tool `history_search` dùng FTS5, đồng bộ bằng trigger, chỉ thấy chat mà actor có tham
   gia. Memory nạp vào prompt giờ có kèm ngày để model ưu tiên memory mới hơn.
2. **Vòng học**: skill chỉ được viết khi actor tự nhớ ra. Chưa có bước rút kinh nghiệm chạy nền sau mỗi run, và
   skill do actor viết không cần duyệt (rủi ro theo bài Skill Misevolution).
3. **Automation**: chưa có output theo từng bước, retry cho lỗi tạm thời, chặn chạy chồng, cổng "không cần LLM",
   hay dry-run trước khi duyệt.
4. **Quy trình ticket**: "done" đóng ticket luôn, không có QA/Lead review do runtime ép. Chưa có watchdog khi cả
   cây im lặng, chưa có budget.

## 3. Đề xuất, theo thứ tự ưu tiên

| # | Ý tưởng | Nguồn | Áp vào actor-office | Công sức |
|---|---|---|---|---|
| 1 | ✅ **Tìm lịch sử bằng FTS5** (đã làm 11/10) | Hermes `session_search` | Tool `history_search` tìm trong ticket, comment, tài liệu (bản mới nhất) và chat mà actor tham gia. Memory nạp vào prompt có kèm ngày. | — |
| 2 | **Gom gọn kiểu "thay thế" thay vì xóa mềm** | Graphiti, mem0 2026 | Thêm `superseded_by`. "Gom gọn" chỉ đề xuất thay thế, người dùng xem diff rồi áp dụng. Memory ghim thì không bao giờ bị thay. | S |
| 3 | **Tóm tắt mỗi ticket khi đóng** (episodic) | LongMemEval, MIRIX | Khi `done`, haiku viết 150–250 token (mục tiêu, kết quả, quyết định, lỗi, file), đánh index FTS. Ticket mới thì truy hồi tóm tắt này thay vì transcript. | S |
| 4 | **Rút kinh nghiệm sau mỗi run → đề xuất skill** | Hermes review fork, Letta sleep-time | Sau mỗi run, một bước haiku chỉ có quyền memory và skill, đề xuất memory mới (kèm `supersedes`) và **bản vá SKILL.md**. Bản vá vào Inbox dạng diff, duyệt mới áp dụng (dùng lại cơ chế fingerprint). Usage line hiện chip "đã học". | M |
| 5 | **Kiểm skill lúc ghi và lúc nạp** | Skill Misevolution | Lint "bài học, không phải nhật ký", quét lệnh nguy hiểm. Skill do actor viết phải được duyệt; hash đổi thì phải duyệt lại. | S |
| 6 | **Automation: output theo id bước, retry, chặn chạy chồng, hồi phục** | Windmill, Inngest, Trigger.dev | `{steps.<id>.output}`. Exit 75 nghĩa là lỗi tạm, retry với backoff và không mở fix ticket. Có run đang chạy thì bỏ lượt (`skipped`). Gắn `UNIQUE(automation, slot)`. Chạy thành công sau chuỗi lỗi thì đóng fix ticket và báo "đã hồi phục". | M |
| 7 | **Cổng "không cần LLM" + marker `[SILENT]`** | Hermes cron | Script in `{"continue": false}` hoặc `[SILENT]` thì dừng flow, không chạy notify hay agent. Theo dõi giá chỉ gọi LLM khi có tin. | S |
| 8 | **Dry-run trước khi duyệt + lưu phiên bản** | Huginn, Activepieces | Khi gửi duyệt thì chạy thử `AO_DRY_RUN=1`, notify chỉ ghi log. Thẻ duyệt hiện output thật. Bảng `automation_versions`, mỗi run trỏ tới version đã chạy, UI hiện diff so với bản đã duyệt. | M |
| 9 | **Trigger mới, rẻ**: `after:<automation>`, `webhook:<key>`, `poll:` | Kestra, Huginn | Webhook đưa body vào `{trigger.body}`. Poll dùng script in JSON, chỉ item mới mới kích hoạt. | M |
| 10 | **Review do runtime ép buộc trước `done`** | Paperclip execution policy, MAST | Vị trí có `review_by` (QA hoặc Lead). Dev bấm done thì ticket sang `in_review` cho người kế tiếp; "yêu cầu sửa" trả về người làm. Có bảng audit quyết định. | M |
| 11 | **Watchdog khi cả cây ticket im lặng** | Paperclip | Khi mọi lá đã done, blocked hoặc waiting mà không còn run nào, đánh thức Lead/PM đối chiếu bằng chứng với lời khai, mở lại nếu thiếu. | S |
| 12 | **Kết thúc run phải có trạng thái** | Hermes Kanban, Paperclip | Run ticket phải kết thúc bằng done, block kèm câu hỏi, hoặc xin review. Chỉ viết "em sẽ chờ" thì nhắc lại. Mình đã có `todo` continuation, chỉ cần siết thêm. | S |
| 13 | **Budget phân cấp** | Paperclip, Claude SDK `max_budget_usd` | Văn phòng → actor → ticket: cảnh báo ở 80%, dừng ở 100%. Truyền `max_budget_usd` vào từng run. | S |
| 14 | **Stuck detector** | OpenHands, MAST FM-1.3 | Gate phát hiện cùng một tool call lặp N lần, hoặc ticket bị đá qua lại giữa các actor, thì ngắt run và báo. | S |
| 15 | **SOUL.md + heartbeat theo vị trí** | OpenClaw | Giọng văn và ranh giới cho từng vị trí. Heartbeat là automation có sẵn, im lặng khi không có gì. Ví dụ: Lead xem board mỗi sáng. | S |
| 16 | **Telegram pairing** | Hermes, OpenClaw | Người lạ nhắn vào thì nhận mã 8 ký tự, hết hạn sau 1 giờ, duyệt trên web. Mặc định từ chối tất cả. | S, làm khi tới Telegram |

**Không nên làm lúc này:**
- engine durable kiểu Temporal (chỉ cần checkpoint theo bước là đủ);
- editor DAG kéo thả;
- graph DB cho memory (Neo4j);
- A2A;
- vector DB riêng (thêm `sqlite-vec` sau, khi FTS không đủ).

**Cách đo:** trước khi làm mục 1–4, viết khoảng 30–50 câu hỏi trên lịch sử ticket thật để có mốc so sánh:
- fact cũ đã bị thay;
- nhớ được thông tin xuyên ticket;
- biết nói "không biết".

Đo số token memory mỗi lượt và độ chính xác, trước và sau.

## Nguồn chính
- **Hermes**:
  - [repo](https://github.com/NousResearch/hermes-agent)
  - tài liệu: [memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory), [skills](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills), [cron](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron), [kanban](https://hermes-agent.nousresearch.com/docs/user-guide/features/kanban), [security](https://hermes-agent.nousresearch.com/docs/user-guide/security)
  - [viblo: use case trên X](https://viblo.asia/p/hermes-agent-nhung-use-case-an-tuong-nhat-tren-x-ZjJYWNZpVOE), [awesome-hermes-usecases](https://github.com/aliaihub/awesome-hermes-usecases), [Skill Misevolution](https://arxiv.org/abs/2608.12851)
- **OpenClaw**: [repo](https://github.com/openclaw/openclaw)
- **Letta**: [sleep-time](https://docs.letta.com/guides/agents/architectures/sleeptime), [MemFS](https://docs.letta.com/letta-agent/concepts/memfs), [benchmark](https://www.letta.com/blog/benchmarking-ai-agent-memory)
- **Automation**:
  - Windmill: [approval](https://www.windmill.dev/docs/flows/flow_approval), [error handling](https://windmill.dev/docs/flows/error_handling)
  - Trigger.dev: [idempotency](https://trigger.dev/docs/idempotency), [wait-for-token](https://trigger.dev/docs/wait-for-token)
  - Inngest: [durable agents](https://www.inngest.com/docs/learn/durable-agents)
  - n8n: [HITL tools](https://docs.n8n.io/advanced-ai/human-in-the-loop-tools), [evaluations](https://docs.n8n.io/advanced-ai/evaluations/light-evaluations)
  - Kestra: [triggers](https://kestra.io/docs/workflow-components/triggers)
  - Activepieces: [versioning](https://activepieces.com/docs/flows/versioning)
- **Công ty AI**:
  - Paperclip: [execution policy](https://github.com/paperclipai/paperclip/blob/master/docs/guides/execution-policy.md), [watchdog](https://github.com/paperclipai/paperclip/blob/master/doc/TASK-WATCHDOG.md)
  - [MetaGPT](https://arxiv.org/abs/2308.00352)
  - ChatDev: [ECL](https://arxiv.org/abs/2312.17025)
  - CrewAI: [tasks](https://docs.crewai.com/en/concepts/tasks), [memory](https://docs.crewai.com/en/concepts/memory)
  - [TheAgentCompany](https://arxiv.org/abs/2412.14161), [MAST](https://arxiv.org/abs/2503.13657)
  - Claude Agent SDK: [Python reference](https://code.claude.com/docs/en/agent-sdk/python)
- **Memory**:
  - mem0: [blog](https://mem0.ai/blog/mem0-the-token-efficient-memory-algorithm)
  - Graphiti: [repo](https://github.com/getzep/graphiti), [paper](https://arxiv.org/abs/2501.13956)
  - [LongMemEval](https://arxiv.org/abs/2410.10813), [LoCoMo audit](https://dev.to/penfieldlabs/we-audited-locomo-64-of-the-answer-key-is-wrong-and-the-judge-accepts-up-to-63-of-intentionally-33lg)
  - Claude: [memory tool](https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool), [context management](https://claude.com/blog/context-management)
  - [MIRIX](https://arxiv.org/pdf/2507.07957), [Honcho](https://github.com/plastic-labs/honcho)
