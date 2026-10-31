# Team Copilot — thiết kế chi tiết (bản đủ để code)

Ngày: 2026-10-31 · Trạng thái: **draft chờ duyệt** · Thay thế bản trước cùng tên · Tổng quan: `2026-10-31-team-copilot-overview.md`
Nền code: actor-office (pivot). Ngôn ngữ: Python 3.12, FastAPI, SQLite+FTS5, React+Vite. Runtime: Claude Agent SDK, Kiro CLI (ACP).

---

## 0. Phạm vi và quyết định nền

**Sản phẩm**: một AI engineer là thành viên của team người (BA, Dev, QE). Nhận request → tìm hiểu → hỏi đúng người → chốt requirement → plan cho Dev duyệt → code + test trong worktree → PR → tài liệu hóa kiến thức. Ngoài code còn nhận việc automation, research, doc.

**Khác tool "ticket vào → agent làm" ở**: cộng tác hai chiều, gate do người giữ, minh bạch từng bước, học qua ticket. Token đo theo ticket/stage để so sánh, **không** đặt ngân sách cứng.

| Quyết định | Chốt |
|---|---|
| Gate | 3 gate mềm theo *approver policy*; một người nhiều vai duyệt được cả ba |
| Runtime | Claude SDK và Kiro ACP ngang hàng; mọi tính năng qua tool + gate + prompt pack |
| Rules | file markdown + git, DB index |
| KB | bảng `memories` mở rộng; 5 kind: fact, code_note, decision, contract, flow; publish Confluence một chiều |
| Truy cập env chung | access envelope (proxy local, grant TTL); prod không tồn tại |
| Chất lượng | AI không tự chấm pass; check do engine chạy; reviewer session độc lập |
| UI | FastAPI giữ; frontend React+Vite+Tailwind theo layout herdr |
| Chat POC | Telegram forum group; Teams/Slack sau qua `ChatChannel` |
| Hạ tầng POC | local (worktree trên máy); `WorkspaceProvider` mỏng để sau lên Docker/K8s |

---

## 1. Thuật ngữ

| Từ | Nghĩa |
|---|---|
| Ticket | đơn vị việc, mirror một Jira issue; có `type` và đi qua các `stage` |
| Stage / StageRun | một bước trong vòng đời; mỗi lần chạy stage là một `stage_run` với session riêng |
| Gate | điểm dừng chờ người duyệt; định nghĩa bằi approver policy |
| Dossier | tài liệu sống của ticket, AI cập nhật, là bản bàn giao giữa các stage |
| Rule | luật cho AI: guideline / check / policy / gate; có scope và version |
| KB item | mục kiến thức có kind, scope, nguồn, anchor, trạng thái verified |
| Flow | luồng nghiệp vụ nhiều bước (xuyên service) có bảng trạng thái trước mỗi bước |
| Contract | tri thức về một API / topic / queue: spec, auth kiểu gì, owner, mẫu |
| Envelope | bộ proxy local + grant TTL để app trong worktree chạm env chung mà AI không cầm credential |
| Activity | sự kiện đã chuẩn hóa từ tool call, hiển thị cho người |

---

## 2. Kiến trúc và bố cục code

```
channels/   telegram (multi-user, vai, card v2) · [teams, slack]            ← ChatChannel
web/        FastAPI API + WebSocket · ui/ (React)                            
services/   users teams tickets stages rules knowledge jira confluence access checks review scenarios logs infra
engine/     scheduler → executor(stage-aware) → runtime router → claude_sdk | kiro_acp
            gate (policy + rules + grants + steer) · prompt_pack · activity classifier · watchdog
tools/      office tools (MCP in-process; Kiro qua office_mcp shim)
connectors/ atlassian(rovo mcp cfg) · logstore/{local,elastic,loki} · infra/{db,redis,kafka,mq} · proxy/{egress,db}
store/      SQLite WAL + FTS5 · migrations 0008..0013
files/      ~/.actoroffice/{ws, rules, kb, skills, runs/<run_id>, scenarios, secrets}
```
Chiều phụ thuộc giữ nguyên: `channels → engine → services → store → domain`; `security`, `connectors` cross-cutting; chỉ engine gọi runtime.

**Module mới / đổi** (đường dẫn `src/actoroffice/`):

| Module | Nội dung |
|---|---|
| `domain/team.py` | `User`, `Team`, `Membership`, `Role` |
| `domain/stage.py` | `StageTemplate`, `StageDef`, `StageRun`, `GatePolicy`, `StageStatus` |
| `domain/rules.py` | `Rule`, `RuleKind`, `RuleScope`, `CompiledRules` |
| `domain/knowledge.py` | thêm `KbKind` (fact, code_note, decision, contract, flow), `KbStatus` |
| `domain/access.py` | `Resource`, `AccessLevel`, `Grant` |
| `domain/activity.py` | `Activity` (kind, title, fields, refs) |
| `services/users.py`, `services/teams.py` | CRUD, map telegram_id → user, vai |
| `services/stages.py` | state machine, transitions, gate mở/đóng, suy ra `stage_status` |
| `services/rules.py` | load file ↔ index, compile, versions, import CLAUDE/AGENTS/steering |
| `services/knowledge.py` | mở rộng `memory.py`: kind mới, anchor, stale job, distill, "known" |
| `services/jira.py`, `services/confluence.py` | mirror + publish |
| `services/access.py` | grants, envelope lifecycle |
| `services/checks.py` | check runner (builtin + cmd), parser kết quả |
| `services/review.py` | reviewer session + vòng sửa |
| `services/scenarios.py` | scenario_record/load |
| `services/logs.py`, `services/infra.py` | facade trên connectors |
| `engine/prompt_pack.py` | thay `context.py` cho ticket run |
| `engine/activity.py` | classifier tool_use/tool_result → Activity (thay `core/activity.py`) |
| `engine/watchdog.py` | slow / stalled / looping |
| `engine/gate.py` | + policy rules compiled, grants, steer |
| `engine/workspace.py` | `WorkspaceProvider` (LocalWorktree) + cấp cổng + compose project name |
| `tools/*` | stage_tools, human_tools, kb_tools, rule_tools, infra_tools, log_tools, access_tools, scenario_tools, jira_tools, pr_tools |

Đóng băng (giữ code, ẩn UI): nhiều actor/positions (giữ một actor `ai`), PM actor, chat actor↔actor. Giữ nguyên: automations v1, merges, history_fts, skills.

---

## 3. Mô hình dữ liệu

### 3.1 Migration `0008_teams.sql`
```sql
CREATE TABLE users (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, email TEXT, telegram_id INTEGER UNIQUE,
  default_roles_json TEXT NOT NULL DEFAULT '[]',   -- ["ba","dev","qe","lead","admin"]
  active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE TABLE teams (
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, jira_project_key TEXT, confluence_space TEXT,
  confluence_parent_id TEXT, settings_json TEXT NOT NULL DEFAULT '{}',  -- gates, stage_templates, runtime, env policy
  created_at TEXT NOT NULL);
CREATE TABLE memberships (team_id TEXT NOT NULL, user_id TEXT NOT NULL, roles_json TEXT NOT NULL,
  PRIMARY KEY (team_id, user_id));
ALTER TABLE projects ADD COLUMN team_id TEXT;            -- FK teams
ALTER TABLE projects ADD COLUMN kind TEXT NOT NULL DEFAULT 'code';   -- code | reference | automation
ALTER TABLE projects ADD COLUMN repo_url TEXT;
ALTER TABLE projects ADD COLUMN profile_json TEXT NOT NULL DEFAULT '{}';  -- mục 9.2
CREATE TABLE ticket_participants (ticket_id INTEGER NOT NULL, user_id TEXT NOT NULL, roles_json TEXT NOT NULL,
  PRIMARY KEY (ticket_id, user_id));
```

### 3.2 Migration `0009_stages.sql`
```sql
ALTER TABLE tickets ADD COLUMN team_id TEXT;
ALTER TABLE tickets ADD COLUMN type TEXT NOT NULL DEFAULT 'code';     -- code|automation|research|doc
ALTER TABLE tickets ADD COLUMN stage TEXT;                              -- null = chưa bắt đầu
ALTER TABLE tickets ADD COLUMN stage_template TEXT;                     -- tên template đã snapshot
ALTER TABLE tickets ADD COLUMN owner_user_id TEXT;
ALTER TABLE tickets ADD COLUMN mode TEXT NOT NULL DEFAULT 'wait';       -- wait | assume
ALTER TABLE tickets ADD COLUMN jira_key TEXT UNIQUE; ALTER TABLE tickets ADD COLUMN jira_url TEXT;
ALTER TABLE tickets ADD COLUMN pr_url TEXT; ALTER TABLE tickets ADD COLUMN confluence_url TEXT;
ALTER TABLE tickets ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
CREATE TABLE stage_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, stage TEXT NOT NULL, attempt INTEGER NOT NULL,
  run_id TEXT, session_id TEXT, started_at TEXT NOT NULL, finished_at TEXT,
  outcome TEXT,                       -- completed | reopened | stopped | failed
  usage_json TEXT NOT NULL DEFAULT '{}', rules_json TEXT NOT NULL DEFAULT '[]',   -- ["R-012@3", ...]
  checks_json TEXT NOT NULL DEFAULT '[]', steps_json TEXT NOT NULL DEFAULT '[]');
CREATE INDEX idx_stage_runs_ticket ON stage_runs(ticket_id, id);
ALTER TABLE documents ADD COLUMN approved_revision INTEGER;   -- revision đã qua gate
ALTER TABLE runs ADD COLUMN stage_run_id INTEGER;
ALTER TABLE runs ADD COLUMN kind_detail TEXT;                  -- builder | reviewer | distill
```

### 3.3 Migration `0010_events.sql`
```sql
ALTER TABLE run_events ADD COLUMN seq INTEGER;                 -- per run, tăng dần; backfill = id
ALTER TABLE run_events ADD COLUMN activity_json TEXT;          -- Activity đã chuẩn hóa (null nếu không phải)
CREATE INDEX idx_run_events_seq ON run_events(run_id, seq);
```

### 3.4 Migration `0011_rules.sql`
```sql
CREATE TABLE rules (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, kind TEXT NOT NULL,          -- guideline|check|policy|gate
  scope TEXT NOT NULL,            -- global | team:<id> | project:<id> | repo:<id>
  stage TEXT NOT NULL DEFAULT 'any', status TEXT NOT NULL DEFAULT 'draft',  -- draft|active|disabled
  owner TEXT, created_by TEXT, approved_by TEXT, version INTEGER NOT NULL DEFAULT 1,
  path TEXT NOT NULL, spec_json TEXT NOT NULL DEFAULT '{}',    -- check/policy/gate spec từ frontmatter
  body TEXT NOT NULL DEFAULT '', source TEXT,                   -- "file:CLAUDE.md" khi import
  updated_at TEXT NOT NULL);
CREATE TABLE rule_versions (rule_id TEXT, version INTEGER, body TEXT, spec_json TEXT, by TEXT, at TEXT,
  PRIMARY KEY (rule_id, version));
CREATE VIRTUAL TABLE rules_fts USING fts5(id UNINDEXED, title, body);
```

### 3.5 Migration `0012_knowledge.sql`
```sql
ALTER TABLE memories ADD COLUMN team_id TEXT; ALTER TABLE memories ADD COLUMN project_id TEXT;
ALTER TABLE memories ADD COLUMN status TEXT NOT NULL DEFAULT 'draft';   -- draft|verified|stale|superseded
ALTER TABLE memories ADD COLUMN verified_by TEXT;
ALTER TABLE memories ADD COLUMN source_json TEXT NOT NULL DEFAULT '[]';  -- [{"kind":"msg","id":..},{"kind":"file","path":..,"commit":..}]
ALTER TABLE memories ADD COLUMN anchor_json TEXT;    -- code_note: {"path","commit"}; contract: {"spec","version"}; flow: {"repos":{repo:commit}}
ALTER TABLE memories ADD COLUMN data_json TEXT;      -- contract/flow: cấu trúc (mục 8.3)
ALTER TABLE memories ADD COLUMN title TEXT;
-- kind mở rộng: lesson|fact|preference|decision|code_note|contract|flow
CREATE TABLE work_products_files (id INTEGER PRIMARY KEY, ticket_id INTEGER, kind TEXT, path TEXT, caption TEXT, created_at TEXT);
```

### 3.6 Migration `0013_access.sql`
```sql
CREATE TABLE resources (id TEXT PRIMARY KEY, team_id TEXT NOT NULL, kind TEXT NOT NULL,   -- db|http|kafka|mq|redis|logstore
  name TEXT NOT NULL, env TEXT NOT NULL,            -- local|dev|staging
  config_json TEXT NOT NULL,                        -- host, port, dbname, allow_hosts, topics... (không có secret)
  secret_ref TEXT,                                  -- tên secret trong keyring/secrets dir
  max_level TEXT NOT NULL DEFAULT 'read');          -- trần theo policy
CREATE TABLE grants (id TEXT PRIMARY KEY, ticket_id INTEGER NOT NULL, resource_id TEXT NOT NULL,
  level TEXT NOT NULL,                              -- read|write-sandboxed|write-shared
  granted_by TEXT, reason TEXT, expires_at TEXT NOT NULL, revoked_at TEXT,
  envelope_json TEXT NOT NULL DEFAULT '{}');        -- cổng proxy, role tạm, prefix đã cấp
```

---

## 4. Vòng đời ticket

### 4.1 Stage template (trong `teams.settings_json.stage_templates`, có mặc định toàn cục)
```yaml
code:
  stages:
    - id: understand
      outputs: [requirements, dossier]
      checks: [ac_testable]
      gate: requirement
    - id: plan
      outputs: [plan, test-plan]
      checks: [plan_has_ac_map, plan_has_access_needs, plan_has_options]
      gate: plan            # duyệt plan + duyệt gói grant (mục 9.5)
    - id: build
      outputs: [handoff]
      requires_steps: true
      checks: [tests_changed, ac_coverage, repo:build, repo:test, repo:lint, coverage_changed_lines, no_secrets, log_lint, flow_covered]
    - id: pr
      checks: [flaky_rerun, review_clean]
      gate: merge           # external (Bitbucket); engine chỉ chờ sự kiện merged
    - id: close
      outputs: [kb-draft]
automation:
  stages:
    - {id: understand, outputs: [requirements, dossier], gate: requirement}
    - {id: design,     outputs: [plan], gate: plan}
    - {id: build,      outputs: [automation], checks: [script_runs, no_secrets]}
    - {id: review,     gate: script}        # card duyệt đúng fingerprint (automations v1)
    - {id: enabled}
research: { stages: [ {id: understand, gate: requirement}, {id: produce, outputs: [report]}, {id: review, gate: accept} ] }
doc:      { stages: [ {id: understand, gate: requirement}, {id: produce, outputs: [document]}, {id: review, gate: accept} ] }
```

### 4.2 Approver policy (trong `teams.settings_json.gates`)
```yaml
gates:
  requirement: { roles: [ba],        min: 1, fallback: owner }
  plan:        { roles: [dev, lead], min: 1, fallback: owner }
  script:      { roles: [dev, lead], min: 1, fallback: owner }
  accept:      { roles: [owner],     min: 1 }
```
Người được hỏi = `ticket_participants` có vai; rỗng → memberships của team có vai; rỗng → `fallback`. `admin`/`lead` luôn có "Duyệt thay" (ghi audit). Một user có nhiều vai → một người qua được nhiều gate.

### 4.3 State machine (`services/stages.py`)
```
start(ticket)            → stage = template.stages[0]; stage_run(attempt=1); wake(ai)
stage_complete(summary)  → run checks(stage) ; fail → run tiếp với kết quả check (max 3 vòng rồi needs_you dev)
                           pass → nếu gate: tạo Interaction approve_doc(doc, revision, policy) → chờ
                                  không gate: advance()
approve(interaction, by) → kiểm vai; documents.approved_revision = revision; advance()
reject(interaction, by, note) → comment; stage_run mới cùng stage (attempt+1) với note trong delta
advance()                → stage = next; stage_run mới; wake(ai)
stage_reopen(target, reason) [AI hoặc người] → stage_run hiện tại outcome=reopened; stage=target; attempt+1;
                           document của các stage sau đánh dấu needs_review
pause/resume/stop        → paused flag; cancel turn; resume mở lại session cũ
on requirements revision mới sau approved → gate requirement mở lại; plan.needs_review = true
on PR merged (Rovo poll / webhook) → advance() từ pr sang close
```
`stage_status` **suy ra** (không lưu):
```
paused                 nếu ticket.paused
needs_you(who)         nếu có interaction pending kind ∈ {approve_doc, question, approve_tool, env_access} nhắm người
waiting_external       nếu stage=pr và không có run và PR chưa merged; hoặc chờ Jira/Confluence
working                nếu có run status running
queued                 nếu có wake pending
blocked                nếu ticket_blockers mở
failed                 nếu run cuối failed và không có wake
done                   nếu stage=close và stage_run.outcome=completed
```

### 4.4 Session per stage và prompt pack (`engine/prompt_pack.py`)
```
[base]     vai AI, nguyên tắc: hỏi khi không chắc; không tự qua gate; ghi dossier; khai steps; không sửa test để pass
[rules]    guideline compiled cho stage (≤ 2k token; còn lại: rule_search)
[known]    "Mình đã biết": KB verified/draft liên quan + dossier ticket cũ tương tự (≤ 2k token, có nguồn)
[dossier]  bản hiện tại (toàn bộ; nếu > 6k token thì các section ngoài stage hiện tại được rút gọn)
[stage]    hướng dẫn stage: output bắt buộc, tool nên dùng, điều kiện stage_complete
[delta]    comment mới · câu trả lời · steer · kết quả check lần trước · findings reviewer
```
Claude: `[base..stage]` = system prompt (cache); `[delta]` = prompt turn. Kiro: toàn bộ là tin nhắn đầu của session; turn sau chỉ `[delta]`. Resume chỉ trong cùng `stage_run`.

---

## 5. Trạng thái, sự kiện, điều khiển

### 5.1 Nguyên tắc
Engine là nguồn sự thật (không suy đoán); một stream `run_events` cho mọi kênh; ba mức zoom (ticket / run / bằng chứng); mọi hành động trả lời được "vì sao"; payload qua `Redactor`.

### 5.2 Bốn lớp
```
TICKET   stage + stage_status (4.3)                                   ← đang ở đâu
RUN      status (giữ) + health ∈ {ok, slow, stalled, looping}        ← có ổn không
ACTIVITY dòng "đang làm gì" = activity mới nhất của run              ← đang làm gì
STEPS    checklist từ plan_steps/step_done (stage_runs.steps_json)   ← làm đến đâu
```

### 5.3 Activity (`engine/activity.py`)
`classify(kind, payload, runtime) -> Activity | None`, gọi khi executor nhận sự kiện từ sink; kết quả ghi `run_events.activity_json` và đẩy lên bus.

```python
class Activity(BaseModel):
    kind: Literal["narration","read","search","source","edit","command","question","approval",
                  "step","rule","check","deviation","stage","usage","health","error"]
    title: str                      # một dòng cho người
    fields: dict[str, Any] = {}     # path, lines, query, hits, cmd, exit, duration_ms, added, removed, ...
    refs: dict[str, str] = {}       # diff_ref, output_ref, interaction_id, rule_id, step_index
    at: str
```
Bảng phân loại (áp cho cả hai runtime; Kiro ACP: `fs/read_text_file`→read, `fs/write_text_file`→edit, `terminal/*`→command):

| tool / dấu hiệu | kind | title |
|---|---|---|
| Read, Glob, `cat/head/sed -n` | read | "Đọc `{path}` ({lines} dòng)" |
| Grep, `rg/grep`, history_search, kb_search, rule_search | search | "Tìm `{query}` → {hits}" |
| mcp rovo get page/issue, WebFetch, logs_*, db_query, kafka_peek | source | "{kind}: {title}" |
| Write, Edit, MultiEdit | edit | "Sửa `{path}` +{added} −{removed}" (diff tính bằng git diff của file sau tool_result) |
| Bash/terminal (không phải read-like) | command | "`{cmd≤80}` → exit {code} ({duration})" |
| ask_human | question | "Hỏi {roles}: {question≤80}" |
| approve_doc / approve_tool / env_access | approval | "Chờ {roles} duyệt {what}" |
| plan_steps / step_done | step | "{i}/{n} {step}" |
| gate áp policy / rule nạp | rule | "{rule_id} {effect}" |
| checks runner | check | "{check}: pass/fail" |
| plan_deviation | deviation | "Lệch plan: {what}" |
| stage transition | stage | "{from} → {to}" |
| cuối turn | usage | "Worked for {s} · {tokens} tok · ${cost}" |
| watchdog | health | "{state}: {reason}" |
| text ≤ 300 ký tự giữa các tool | narration | nguyên văn |

Output lớn: diff và stdout ghi `~/.actoroffice/runs/<run_id>/<seq>.{diff,txt}`, Activity chỉ giữ `*_ref`; API `/api/runs/{id}/refs/{name}` trả nội dung (redacted).

### 5.4 Steps
Tool `plan_steps(steps: list[str])` (bắt buộc với stage có `requires_steps`), `step_done(index, note="")`. Lưu `stage_runs.steps_json = [{text, done, note}]`. Nếu stage có `requires_steps` mà AI gọi `stage_complete` khi chưa `plan_steps` → deny kèm nhắc.

### 5.5 Watchdog (`engine/watchdog.py`)
| state | điều kiện | hành động |
|---|---|---|
| slow | một command > 300 s | activity health, không can thiệp |
| stalled | không có sự kiện > N s (understand 600, build 900) | ping runtime; nếu tiến trình chết → run failed, re-queue |
| looping | ≥3 command giống nhau (chuẩn hóa) cùng exit≠0, hoặc cùng file read ≥4 lần, hoặc ≥3 edit cùng file không có command giữa chừng | gate deny tool kế tiếp với thông điệp "dừng, gọi ask_human(dev) kèm đã thử gì" |

### 5.6 Điều khiển (API + nút)
| lệnh | hành vi |
|---|---|
| pause | `runtime.cancel(run)`; `tickets.paused=1`; stage_run giữ; resume → wake với resume_session_id |
| stop | cancel; stage_run.outcome=stopped; comment lý do; ticket ở stage hiện tại, không wake |
| steer(text) | nếu đang chạy: gate deny tool kế tiếp với `STEER: {text}` (một lần); nếu không: ghi vào delta của wake kế |
| retry_stage | stage_run mới attempt+1, session mới |
| explain | run kind=system model rẻ: tóm tắt 10 activity cuối, đăng vào topic |

### 5.7 Telegram (`channels/tg_progress.py` v2)
Một progress card per stage_run, ghim đầu topic, edit throttle 10 s, nội dung: header (key · stage · elapsed · turns · tokens) + steps + 5 activity cuối + nút `⏸ ⏹ 💬 🔍`. Card riêng cho question/gate/env_access (inline keyboard; trắc nghiệm có đáp án đề xuất in đậm; "Khác…" → reply tự do). Nút kiểm vai server-side; sai vai → toast "cần vai X". Kết thúc stage → card chốt: tóm tắt, check, usage.

### 5.8 WebSocket (`web/server.py`)
```
client → {type:"subscribe", ticket_id, since_seq?}    server → {type:"event", run_id, seq, kind, activity}
client → {type:"control", ticket_id, cmd, arg?}       server → {type:"ticket", ticket_id, stage, stage_status, who[]}
```
Reconnect gửi `since_seq` → server replay từ DB.

---

## 6. Office tools (MCP in-process; chung Claude/Kiro)

| tool | args → trả về | risk / gate |
|---|---|---|
| `ask_human` | `(roles[], question, options[]?, recommended?, blocking=True)` → `{interaction_id}` | tạo Interaction; blocking → park |
| `plan_steps` / `step_done` | xem 5.4 | read |
| `plan_deviation` | `(what, why, scope: minor\|major)` → major → Interaction `approve_doc(plan)` | write |
| `stage_complete` | `(summary)` → kết quả checks hoặc `{gate: pending}` | write |
| `stage_reopen` | `(target_stage, reason)` | write |
| `dossier_update` | `(section, body_md, sources[])` → revision | write |
| `doc_get/doc_put` (giữ) | `requirements`, `plan`, `test-plan`, `handoff`, `kb-draft`, `report` | write |
| `kb_search` | `(query, kinds[]?, scope?)` → items (≤20, có status, nguồn) | read |
| `kb_note` | `(kind, title, body, sources[], anchor?, data?)` → draft | write |
| `topic_info` | `(topic)` → producer/consumers/schema/key từ quét code + registry + KB | read |
| `rule_search` | `(query)` → rules active liên quan | read |
| `repo_fetch` | `(repo_url, ref?)` → clone/update read-only vào `reference/`, thêm `add_dirs` | external, ask lần đầu per repo |
| `jira_get` / `jira_update` | qua Rovo MCP; update chỉ sau G1 | external / ask |
| `pr_create` / `pr_comments` / `pr_reply` | qua Rovo; `pr_merge` **không tồn tại** | external, ask |
| `db_schema` / `db_query` / `db_write` | mục 9.3 | read / read / ask |
| `redis_scan` / `redis_get` · `kafka_topics` / `kafka_peek` / `kafka_produce` · `mq_queues` / `mq_peek` | mục 9.3 | read / ask |
| `logs_search` / `logs_cluster` / `logs_errors` / `logs_trace` / `logs_diff` / `app_logs` | mục 9.4 | read |
| `env_access` | `(resource, level, reason, ttl_h=4)` → Interaction | ask |
| `scenario_record` / `scenario_load` | mục 10.4 | write (local) |
| `artifact_add` | `(path, kind, caption)` → work_products_files; ảnh/video gửi lên topic | write |
| `history_search`, `memory_*`, `skill_*`, `automation_save` (giữ) | | |

Mọi tool ghi đều qua `OfficeGate.before_tool` → policy + rules compiled + grants + steer + watchdog.

---

## 7. Rules

### 7.1 File `~/.actoroffice/rules/<scope>/<id>.md` (git commit mỗi lần lưu)
```yaml
---
id: R-012
title: Không force-push
kind: policy            # guideline | check | policy | gate
scope: team:billing     # global | team:<id> | project:<id> | repo:<id>
stage: any
status: active
owner: lead@billing
version: 3
spec:                   # theo kind
  # check:  { builtin: ac_coverage }  hoặc { cmd: ["mvn","-q","verify"], cwd: ".", timeout_s: 900, parser: junit|lcov|jacoco|exit, os: {windows: [...]} }
  # policy: { deny_tools: [...], deny_patterns: ["git push --force", "--no-verify"], deny_hosts: ["*.prod.*"], mask_columns: {"users": ["email","phone"]} }
  # gate:   { when: {paths: ["db/migrations/**"]}, roles: [lead], min: 1 }
---
Nội dung markdown (guideline: đưa thẳng vào prompt)
```

### 7.2 Compile (`services/rules.py`)
```
compile(team, project, repo, stage) -> CompiledRules:
  cand = active rules where scope ∈ {global, team:T, project:P, repo:R} and stage ∈ {any, S}
  order by scope specificity (repo > project > team > global), then updated_at desc
  guidelines: dedupe by title similarity; cắt ở 2k token; phần còn lại chỉ liệt kê title + id
  checks:     union; cùng id → scope hẹp thắng
  policies:   union (không ghi đè, chỉ cộng dồn deny)
  gates:      union
  → ghi ids@version vào stage_runs.rules_json; emit activity rule cho policy/guideline đã nạp
```

### 7.3 Quản lý
Đề xuất (web form, `/rule` Telegram, AI ở close) → `draft`; duyệt: owner scope (lead cho team, admin cho global và mọi policy); cảnh báo trùng bằng `rules_fts`; import `CLAUDE.md`, `AGENTS.md`, `.kiro/steering/*.md` → guideline scope repo (`source: file:<path>`, cập nhật khi file đổi); UI: trang Rules, tab "Rules in effect" trên ticket, link từ activity `rule`/`check`.

### 7.4 Check runner (`services/checks.py`)
```python
class CheckResult(BaseModel): id: str; passed: bool; summary: str; output_ref: str | None; data: dict
run_checks(stage_run, ids) -> list[CheckResult]   # tuần tự, env sạch (scrub + envelope), cwd=worktree, timeout
```
Builtin (Python): `ac_testable` (mỗi AC có Given/When/Then), `plan_has_ac_map`, `plan_has_access_needs`, `plan_has_options`, `tests_changed` (git diff có file khớp pattern test của profile), `ac_coverage` (mỗi `AC-n` trong requirements xuất hiện trong tên/annotation test của diff), `coverage_changed_lines` (parse lcov/jacoco/coverage.xml ∩ git diff ≥ ngưỡng), `no_secrets` (gitleaks nếu có, không thì regex), `log_lint` (semgrep nếu có, không thì regex: `System.out|print(`, nối chuỗi trong log, biến `password|token|secret`), `flaky_rerun` (chạy `repo:test` 2 lần, so kết quả), `review_clean` (reviewer findings severity ≥ high = 0), `flow_covered` (flow bị chạm có scenario test, theo KB flow ↔ file map), `script_runs`, `message_schema_ok`, `contract_ok`, `migrations_ok`. `repo:*` lấy argv từ `profile_json`.

---

## 8. Kiến thức

### 8.1 Dossier (document `dossier`, sections cố định)
`context` · `requirements` (link AC) · `qa` (hỏi → trả lời → ai, msg id) · `decisions` (gì, vì sao, ai, nguồn) · `technical` (module/file, flow map, contract dùng) · `access` (nhu cầu truy cập) · `tests` (AC → test) · `deviations` · `links`. `dossier_update(section, body, sources)` thay cả section; revision tăng.

### 8.2 KB item (`memories` mở rộng)
`kind ∈ {fact, code_note, decision, contract, flow}` (+ lesson/preference cũ), `scope` (team/project/repo), `status` (draft/verified/stale/superseded), `source_json`, `anchor_json`, `data_json`, FTS5 trên title+content.

### 8.3 Cấu trúc `data_json`
```yaml
contract:   # một API / topic / queue
  kind: http|kafka|mq|grpc
  name: "payments-api POST /v1/payments" | "orders.events"
  spec: { where: "repo:payments-api/openapi.yaml" | "registry:orders-value:7" | "confluence:123", version: "..." }
  auth: { kind: api_key|oauth2|mtls|none, source: "secret:payments_api_key" }     # tên, không giá trị
  owner: { team: "payments", people: ["..."] }
  samples: { request: {...}, response: {...}, message: {...} }                     # đã che PII
  quirks: ["timeout 2s", "idempotency-key bắt buộc"]
flow:       # một luồng nghiệp vụ nhiều bước (tổng quát: order, payment, onboarding, approval, batch...)
  name: "..."
  steps:
    - id: s1, service: "...", trigger: http|event|cron|manual, input: "...", output: "..."
      writes: [{table: "...", fields: [...]}], emits: ["topic.x"], consumes: ["topic.y"]
      state_before: [{table: "...", condition: "status = 'A'"}, {event: "topic.y", exists: true}]
      invariants: ["..."]
  repos: {repo: commit}
```

### 8.4 Vòng đời
- **"Mình đã biết"** (đầu understand): `kb_search` theo từ khóa Jira + module → `[known]`; đăng tóm tắt lên topic với nhãn verified/draft để người sửa sớm.
- **Draft trong lúc làm**: AI gọi `kb_note` khi học được (contract, flow, code_note); Dev duyệt plan → các KB được trích trong plan chuyển `verified`.
- **Distill ở close**: run `kind_detail=distill` (model rẻ, không tool) đọc dossier → ≤10 KB draft + ≤3 rule candidate → một card tick ✅/❌ (owner/BA) → verified/xóa.
- **Stale job**: sau PR merged: code_note/flow có anchor trong file đã đổi → `stale`; contract có spec version mới → `stale`.
- **Confluence** (`services/confluence.py`, API v2, markdown→HTML storage): trang Dossier per ticket (parent = trang project), trang KB per kind (regenerate theo title, idempotent). Lỗi publish không chặn close.

---

## 9. Tích hợp

### 9.1 Atlassian
- Rovo MCP remote (Jira, Confluence, Bitbucket Cloud) cấu hình per team (OAuth tài khoản bot); fallback `mcp-atlassian` cộng đồng cùng tên tool.
- Jira mirror (`services/jira.py`): poll 60 s theo `jira_project_key` + JQL label `ai`; tạo ticket local + topic; cập nhật title/description/status; webhook sau.
- PR merged: poll PR state mỗi 120 s khi stage=pr.

### 9.2 Repo profile (`projects.profile_json`, viết tay cho pilot)
```json
{ "setup": [["mvn","-q","dependency:go-offline"]], "build": [["mvn","-q","-DskipTests","package"]],
  "test": [["mvn","-q","test"]], "lint": [["mvn","-q","spotless:check"]], "run": ["mvn","spring-boot:run"],
  "services": "docker-compose.dev.yml", "test_glob": ["**/src/test/**"], "coverage": {"kind":"jacoco","path":"target/site/jacoco/jacoco.xml"},
  "ports": ["APP_PORT"], "env": {"DB_URL": "envelope:db:orders-dev"}, "os": {"windows": {"run": ["mvnw.cmd","spring-boot:run"]}} }
```
Engine cấp cổng per worktree, `COMPOSE_PROJECT_NAME=ao-<ticket>`, dọn container/process khi stage kết thúc.

### 9.3 Hạ tầng dữ liệu (`connectors/infra/*`, facade `services/infra.py`)
Local (compose/Testcontainers): AI dùng CLI thật qua Bash. Env dùng chung: chỉ qua tool, credential server-side.

| tool | ràng buộc |
|---|---|
| `db_schema(resource, table?)` | information_schema; cache → KB code_note "schema:<db>" anchor = migration version |
| `db_query(resource, sql)` | role read-only; parser chặn DDL/DML; ép `LIMIT ≤ 200`; `statement_timeout 10s`; `EXPLAIN` nếu không WHERE; mask cột theo policy |
| `db_write(resource, sql)` | chỉ dev/staging; luôn ask; transaction ROLLBACK trừ khi người duyệt commit |
| `redis_scan/get/ttl` | SCAN COUNT ≤ 1000; cấm KEYS/FLUSH; giá trị cắt 4 KB |
| `kafka_topics/kafka_peek(resource, topic, n≤50, from)` | group `ao-<ticket>-peek`, không commit; decode theo registry |
| `kafka_produce` | local/dev; ask; validate schema |
| `mq_queues/mq_peek` | management API `get` ackmode `ack_requeue_true` |
MCP bên thứ ba chỉ cho local (capability pack).

### 9.4 Logs (`connectors/logstore/*`, interface `LogStore`)
```python
class LogStore(Protocol):
    async def search(self, service, env, query, since, until, level=None, limit=200) -> list[LogLine]
    async def errors(self, service, env, since, until) -> list[ErrorGroup]
    async def trace(self, trace_id) -> list[LogLine]
```
Adapters POC: `LocalFileLogStore` (ring buffer per worktree, `app_logs`), `ElasticLogStore`, `LokiLogStore`. `logs_cluster` và `logs_diff` tính ở facade bằng `drain3` (template mining) trên kết quả search. Mọi tool: trần 8 KB, "còn N dòng, hãy thu hẹp"; AI gắn `X-Request-ID` khi tự gọi.

### 9.5 Access envelope (`services/access.py`, `connectors/proxy/*`)
| resource.kind | envelope POC | sau |
|---|---|---|
| http | egress proxy local (HTTP CONNECT/forward, allowlist host từ grant, inject header auth, log per ticket); app nhận `HTTP(S)_PROXY=localhost:<port>` | — |
| db (Postgres) | tạo role tạm `ao_<ticket>` `VALID UNTIL now()+ttl`, quyền theo level (read: SELECT; write-sandboxed: schema riêng `ao_<ticket>`); app nhận DSN tới pgbouncer/TCP proxy local | MySQL/Oracle |
| kafka | POC: chỉ qua tool (`kafka_peek`), app local dùng compose | principal + ACL + group prefix |
| mq, redis | POC: chỉ qua tool | — |
Grant: `env_access` → Interaction; **plan section `access`** liệt kê nhu cầu → duyệt gói cùng Gate 2 (một click); TTL mặc định 4 h, tối đa theo policy; thu hồi tự động (xóa role, route). Policy gate: deny `env|printenv|set` in toàn bộ env, deny đọc `.env` ngoài worktree. Ticket page / card: "Access in effect".

---

## 10. Chất lượng

### 10.1 Năm lớp
| lớp | stage | cơ chế | xác nhận |
|---|---|---|---|
| AC kiểm thử được | understand | Given/When/Then; `ac_testable` | BA (G1) |
| Test plan ↔ AC | plan | bảng AC → test; `plan_has_ac_map` | Dev (G2) |
| Test trước | build | `plan_steps` có "viết test fail → code → xanh"; `tests_changed`, `ac_coverage` | engine |
| Check repo | build/pr | build, test, lint, typecheck, coverage dòng đổi, static, secrets, deps; env sạch | engine |
| Review độc lập | pr | run `kind_detail=reviewer`, context mới, prompt đối nghịch; findings JSON `{severity, file, line, claim, evidence}`; builder sửa ≤2 vòng rồi hỏi Dev; findings vào PR | engine + Dev (G3) |
Thêm: `flaky_rerun`; PR body = AC map + check summary + links; policy deny sửa test để pass / `--no-verify`.

### 10.2 UI
Unit/component (Vitest+Testing Library), tsc/eslint/axe, E2E Playwright (AI chạy app bằng `profile.run`, cổng cấp phát), **screenshot + video/trace mỗi AC** → `artifact_add` → topic Telegram + PR; AI xem lại ảnh để tự đối chiếu; QE nhận preview (worktree app hoặc video) và phản hồi trong topic; visual regression nếu repo có baseline.

### 10.3 Service khác (chọn per service ở plan, Dev duyệt)
mock → contract (`contract_ok` theo OpenAPI/Pact) → stub (WireMock/Prism từ OpenAPI trong compose, fixture trong repo) → staging (qua envelope, VCR fixture) → prod: không.

### 10.4 Kiểm thử theo luồng (tổng quát cho mọi flow nhiều bước)
1. **Flow map** (KB `flow`, 8.3) dựng ở understand/plan bằng lần theo code từ entry point → DB write → event; Dev sửa và xác nhận tại G2.
2. Precondition của bước bị sửa = `state_before` của bước đó trong flow map.
3. Tạo precondition, theo thứ tự ưu tiên: **replay từ đầu luồng** trong env local (compose + stub có state) → **scenario fixture** (`scenario_record(flow, step)` chụp rows + messages đã che PII từ một lần chạy có `X-Request-ID`; `scenario_load(name)`; version theo schema) → **builder** trong code.
4. Test bắt buộc: scenario test đi từ đầu (`flow_covered`), test bước (unit), contract/schema (`contract_ok`, `message_schema_ok`), idempotency/ordering cho consumer.

---

## 11. Runtime: Claude và Kiro

| khả năng | Claude | Kiro | xử lý |
|---|---|---|---|
| system prompt | có | không | prompt pack: Kiro ghép vào tin nhắn đầu |
| park tool chờ người | defer | từ chối + cancel + wake | gate sẵn có |
| resume | có | có | theo stage_run |
| subagent / todo | có | không | không dùng; `plan_steps` qua MCP |
| hook | PreToolUse | fs/terminal/permission qua office | gate chung |
| sự kiện | messages | session/update | map → kind thô → classifier |
Nguyên tắc: không tính năng nào chỉ chạy trên Claude. Test: `fake_acp_agent` toàn luồng; smoke `kiro-cli` thật ở P0 nếu có máy.

---

## 12. Web UI

Routes: `/` (sidebar + tabs + panes), `/t/:key` (ticket đầy đủ), `/inbox`, `/rules`, `/kb`, `/team`, `/login`.
Layout theo herdr: sidebar (Spaces = team→project, Agents = ticket đang mở, badge *Needs you* cá nhân hóa, Inbox), tabs ticket (dot cam/xanh), pane ticket (header stage/status/steps/usage; timeline nhóm `Explored (n)`, `Edited file +a −b` mở diff; ô nhập Steer/trả lời), panel phải (Files, Changes live, Commands, Rules in effect, Dossier, Access).
API chính: `GET /api/tickets?team=` · `GET /api/tickets/{key}` (stage, status, who, steps, docs, stage_runs, grants) · `POST /api/tickets/{key}/control {cmd}` · `GET /api/runs/{id}/events?since_seq=` · `GET /api/runs/{id}/refs/{name}` · `GET/POST /api/interactions` · `GET/POST/PUT /api/rules` · `GET/PUT /api/kb` · `GET/PUT /api/teams/{id}` · WS `/ws`.
Token hóa màu/khoảng cách từ đầu; dark mặc định.

---

## 13. Đa nền tảng

Hiện trạng: chỉ kiểm chứng trên Windows 11; code cơ bản portable. Quy tắc: lệnh dạng argv có override theo OS (`profile.os`, `rule.spec.os`); `pathlib`; kill process tree qua psutil; CRLF→LF trước khi diff; `init` phát hiện OS/WSL/git-bash/docker/kiro/claude và ghi `config.yaml`; CI matrix ubuntu/macos/windows chạy pytest với fake runtime từ P0.

| | Linux | macOS | Windows native | WSL2 |
|---|---|---|---|---|
| Claude SDK | bundle | bundle | wheel x64 bundle `claude.exe`, không dùng shim `.cmd` | như Linux |
| Kiro CLI | installer/.deb | installer | PowerShell installer (CLI 2.0) | như Linux |
| Bash tool | sh | zsh | Git for Windows; allowlist hiểu cmd/powershell | bash |
| Docker | native | Desktop/OrbStack/colima | Desktop (WSL2 backend) | Desktop integration |
| Git/FS | — | case-insensitive | longpaths, autocrlf, case-insensitive | repo và data dir trong FS Linux |
| Autostart | systemd user | launchd | Task Scheduler | systemd/wsl.conf |
Khuyến nghị máy POC chung: Linux hoặc WSL2.

---

## 14. Bảo mật

Telegram allowlist user + vai kiểm tra server-side; secrets ở keyring/secrets dir, AI không thấy; envelope cho env chung; policy hard-deny sẵn có (+ prod hosts, `--no-verify`, in env); taint từ nội dung Jira/Confluence/PR/log (data, không phải lệnh) → tool ngoài phải ask; audit mọi tool ghi, grant, gate; Redactor trên events, refs, Confluence publish; worktree chứa trong `ws/`, `GIT_CEILING_DIRECTORIES`.

---

## 15. Lộ trình và việc cụ thể

| Phase | Việc (code) | Kiểm chứng |
|---|---|---|
| **P0 nền** | commit baseline · migrations 0008–0010 · `domain/team,stage,activity` · `services/users,teams,stages` · executor stage-aware + `prompt_pack` · `engine/activity` classifier + `run_events.seq` · `tg_progress` v2 + card có vai · Telegram multi-user · `services/jira` poll · `init` phát hiện môi trường · CI 3 OS · Kiro smoke | Jira issue → topic + card; 2 user khác vai bấm đúng quyền; activity giống nhau trên Claude và fake-ACP |
| **P1 understand** | Rovo MCP cfg · reference repos + `repo_fetch` · `ask_human` trắc nghiệm · `kb_search`/`kb_note` + "known" · `dossier_update` · `approve_doc` + G1 → `jira_update` · mode wait/assume · `ac_testable` | ticket thật ra AC được BA chốt; card hiện đúng ai cần trả lời |
| **P2 rules + plan** | migrations 0011 · `services/rules` (file/git/index/compile/import) · `services/checks` builtin · trang Rules · plan có phương án + AC map + access needs · G2 (duyệt plan + grants) · `topic_info` · flow map trong dossier | rule team A không chạm B; rules_json ghi id@version; check fail chặn stage |
| **P3 build + pr** | `plan_steps/step_done/deviation` · watchdog · `WorkspaceProvider` + cổng + compose name · checks repo + coverage dòng đổi + flaky · `services/review` · Playwright + `artifact_add` · stub services · `pr_*` qua Rovo + vòng comment · Files/Changes live · migrations 0013 + envelope http/db + `env_access` | PR có test pass, AC↔test, ảnh UI; looping bị chặn; app worktree gọi staging qua proxy mà env không có credential |
| **P4 close + học + infra** | migrations 0012 · distill + card tick · stale job · `services/confluence` · AI đề xuất rule · `infra_tools` (db/redis/kafka/mq) · `logstore` local+elastic/loki + drain3 · `scenario_record/load` · `flow_covered` · Kiro parity toàn luồng | ticket 2 cùng loại: ít câu hỏi, research nhanh, token/stage giảm; dossier+KB lên Confluence |
| **P5 UI + loại việc khác** | React UI đầy đủ · template automation/research/doc · Teams adapter · envelope kafka | giao automation qua chat → script duyệt → chạy theo lịch |

Mỗi phase kết thúc bằng kịch bản chạy thật ghi vào `docs/test-plan/results/`.

---

## 16. Cần từ bạn trước P1 (không chặn P0)
DB loại gì (Postgres/MySQL/Oracle) · Kafka có schema registry không · auth service-to-service (API key/OAuth/mTLS) · có env dev dùng chung không · nền tảng log (Kibana/Loki/Datadog…) và log đã JSON có traceId chưa · repo pilot và stack · máy có `kiro-cli` để smoke.
