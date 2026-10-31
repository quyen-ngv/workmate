# actor-office — design

Inspired by [Paperclip](https://github.com/paperclipai/paperclip): its ticket model, atomic checkout,
"exit and get woken" human-in-the-loop, and disposition discipline. Its company/board layer, plugins and
heavy runtime are deliberately left out.

## Product in one paragraph

You run a small office of AI employees from a chat. **Positions** are job templates (instructions,
tools, capability packs, policy, model); **actors** are employees hired into a position with their own
specialty, memory and skills. You chat with any actor directly or in a group (you are always a member),
with @mentions both ways. All real work is a **ticket**: the actor clarifies scope, creates or receives a
ticket, works in the ticket workspace, leaves durable progress (comments, documents, work products) and
ends with an explicit status. When a step is deterministic and repeatable, actors write code (toolbox
scripts, automations) instead of spending tokens on it.

## Architecture

```
channels (web UI, Telegram)  →  services (org, tickets, chat, interactions, memory, skills, toolbox, automations)
          ↑ EventBus                    ↓
       engine: Scheduler → Executor → AgentRuntime (Claude SDK; ACP/Kiro later)
                             │  OfficeGate = PolicyEngine + human approvals (PreToolUse hook)
                             └─ office tools (MCP, in-process): ticket_*, doc_*, ask_user, list_actors, …
store: SQLite (WAL, single connection)  ·  files: ~/.actoroffice/{ws, skills, toolbox, automations}
```

Dependencies only point one way: `channels → engine → services → store → domain`. `security` is
cross-cutting. Only the engine calls runtimes.

## Key decisions (see docs/spikes/P0-claude-sdk.md for evidence)

| Topic | Decision |
|---|---|
| Runtime | `claude-agent-sdk` with `setting_sources=[]` (actors never inherit the user's own Claude settings or memories), `strict_mcp_config`, `verbatim_prompts` |
| Policy | Every tool call goes through the `PreToolUse` hook, which returns allow / deny / ask / defer. `can_use_tool` is a deny-all fallback |
| Human in the loop | Short live wait (`approval_wait_s`), then `defer`: the run parks and the ticket goes to `in_review`. The answer stores a one-time grant (allow or deny) for exactly that call; on resume the CLI re-issues the deferred call and the hook consumes the grant |
| Questions | The `ask_user` tool returns immediately. The actor sets `in_review` and exits; the answer wakes it with the answer in the prompt |
| Tickets | Single assignee; atomic checkout (`409` = someone else owns it, never retry); blockers gate checkout and wake dependents when they close; documents use optimistic revisions |
| Disposition | An actor must end with `done` / `in_review` (only with a real pending question) / `blocked` (with `blocked_by`) / `todo`. One reminder turn, then the office moves the ticket to `in_review` for a human |
| Context | One CLI session per (actor, ticket). Stable system prompt (cacheable); only the delta goes into the turn prompt. Non-coding positions use a custom system prompt (about half the base tokens) |
| Concurrency | At most `max_concurrent_runs` (default 2, about 0.4 GB each), one run per actor and one per ticket. Code work happens in a git worktree and branch per ticket tree; merges are a separate approved step |
| Quota | `RateLimitEvent`: a warning drops capacity to 1; a rejection pauses the scheduler until the reset and re-queues the ticket with `not_before` |
| Recovery | On start, runs left `running` are failed, their checkouts released and their tickets re-queued |

## Security guidelines (enforced in code where possible)

1. **Access:** Telegram is bound to one group: the first forum group where the bot is admin and someone writes. Every other chat is ignored, and buttons work only inside that group. The office has a single user for now. With more users, add an allowlist of user IDs and check who clicked. The web UI binds to 127.0.0.1 with auth.
2. **Secrets:** kept in the OS keyring, and actors never see their values. Tools use them server-side. The `Redactor` masks known values and common token patterns in logs, events and audit rows.
3. **Least privilege:**
   - Each position has tools and capability packs.
   - File access is contained to the ticket workspace plus read-only dirs.
   - Protected paths (`~/.ssh`, `~/.claude`, cloud credentials, browser profiles, our own DB and config) are always denied.
   - Shell commands are allowed only through per-position allowlists; chaining or redirection falls back to ask.
   - Hard-deny patterns (`rm -rf /`, `curl | sh`, force push, registry edits…) apply regardless of config.
4. **Prompt injection:**
   - Web, file and API content is data.
   - Reading untrusted content taints the session, and a tainted session must ask before any external action.
   - No position should combine untrusted input, private data and outbound actions.
5. **Approvals by risk:** read and local writes are automatic. Exec asks unless allowlisted. External and destructive actions always ask. Financial and credential actions are not available to actors.
6. **Generated code:** toolbox scripts and automations are enabled only after the user reviews the diff. Dependencies are pinned. Scripts run with a scrubbed env, a timeout and memory limits.
7. **Data:** everything an actor sees is sent to Anthropic under your subscription terms, so do not feed it data you may not share. Raw crawl data has a retention limit, and memory must not hold secrets or personal data.
8. **Audit:** an append-only `audit_log` records tool decisions and mutations. A kill switch (`/panic`) is planned.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| P0 | SDK spike | ✅ |
| P1 | Core: tickets, scheduler, executor, Claude runtime, policy and approvals, CLI | ✅ |
| P2 | Chat: conversations (DM and group), mentions, chat turns, ticket proposals, PM actor, web UI (FastAPI + vanilla JS, WebSocket) | ✅ web; ✅ Telegram (forum group, topic per conversation, cards with buttons, usage line) |
| P2.5 | Projects (folder + own chats/groups/tickets; read_only / worktree / direct), usage per chat/ticket/actor + quota, Claude-style "always allow" rules + trust-all mode, done-reports in the requesting chat, memory (3 scopes, FTS5, 📌 undo), skills (files + git, agent-created, UI editor), positions editor, new-session | ✅ |
| P2.6 | Enterprise flow: BA → Dev Lead meeting (`ask_colleague`, meeting room per ticket tree) → devs → QA → BA acceptance; requirement changes (`change_notify`, mid-run interrupt, reopen); one workspace per ticket tree; due dates; standard document/code skills; positions lead, researcher, analyst, assistant, consultant; reminders (no LLM); automations v1 (reviewed scripts on a schedule, self-healing fix tickets); read-only SQLite tools; new projects (mkdir + git init) | ✅ |
| P3 | Merge flow (repo lock, rebase, test, approved merge), environments, memory compaction | |
| P2.7 | Learning from the community (see `docs/RESEARCH-oss-2026-10.md`): `history_search` (FTS5 over tickets, comments, latest documents and the actor's own chats, kept in sync by triggers); dated memories in prompts. Next: post-run reflection that proposes skill patches for approval, automation step outputs/retry/skip-if-running, review stages before `done` | in progress |
| P4 | Capability packs (MCP connectors and secrets) and the toolbox (scripts as tools, `uv run`, review gate) | |
| P5 | Automations v2: pipelines of script, llm, approval and notify steps; triggers other than time; state | v1 done (P2.6) |
| P7 | Kiro CLI via ACP (`runtimes/kiro_acp.py`): the office is the ACP client (fs, terminal and permissions all go through the gate), office tools are exposed through a stdio MCP shim (`runtimes/office_mcp.py`), and the runtime is picked per actor by model (`kiro`, `kiro:<id>`, see `runtimes/router.py`). Tested against a scripted ACP agent; not yet run against a real `kiro-cli`. Still open: one bot per actor on Telegram | in progress |

## Projects

A project is a folder plus its own conversations, groups and tickets; staff is shared across projects.

| Access | Tickets work in | Chat |
|---|---|---|
| `read_only` | a scratch folder per ticket tree, with read access to the project folder | reads the folder (`Read/Glob/Grep`, `list_dir`) |
| `worktree` | a git worktree + branch per ticket tree, created from the repo root | same |
| `direct` | the project folder itself; the scheduler runs one ticket at a time per project | same |

Project folders cannot be a drive root, the home folder, the office data dir or a protected location. Every
run gets `GIT_CEILING_DIRECTORIES=<workspaces>`, so a git command in a scratch folder never reaches an
enclosing repository (for example a home folder that is itself a git repo).

## Enterprise flow (P2.6)

```
user ─chat→ BA (elicits: ≤5 numbered questions with options) ─propose_ticket(feature)→ user confirms
BA ticket: `requirements` (US + Given/When/Then AC) → sub-ticket `design` for the Dev Lead → BA blocked
Lead ticket: ask_colleague(BA) in the tree's meeting room → parked (in_review) → BA answers in chat → Lead woken
            `design` + `plan` (owners, hours, dates) + `meeting-notes` → dev sub-tickets (due, blocked_by) + QA → Lead blocked
devs/QA: one shared workspace per ticket tree (git worktree on the feature branch) → handoff / test-plan / test-report
all done → Lead report → BA woken → `acceptance` per AC → report in the user's chat
```

- **Asking colleagues** (`ask_colleague`): posts the question in the tree's meeting room (a group linked to the root
  ticket; the user is a member) as an `ask_colleague` interaction. The colleague's reply in that room (or the user's)
  resolves it and wakes the asker's ticket run. Waiting actors are not woken as chat participants.
- **Requirement changes**: `TicketService.change_notice` writes a `change` comment on every affected ticket (default:
  the whole tree), wakes open ones and optionally reopens done ones. A running actor hears about it at its next tool
  call: `OfficeGate` denies that one call with the change text ("REQUIREMENTS CHANGED…"), once. Queued work sees the
  changes in its first prompt. The user can record a change from the ticket page; the BA records it with a
  `change_request` ticket under the feature (document `change-request`, new `requirements` revision, `change_notify`).
- **Definition of done** (`Position.done_docs`): a position may require documents before it can set `done`
  (dev → `handoff`, qa → `test-plan` + `test-report`, lead → `design` + `plan`, analyst / researcher → `report`,
  consultant → `memo|report`); `done` is also refused while sub-tickets are open. Evidence, not claims.
- **Merge** (`MergeService`, Git box on the feature ticket): the user merges a finished tree's branch into the
  project's main branch. Refused while someone works on the tree, when the worktree or the project folder has
  uncommitted changes, or when the project's test command fails; a conflicting merge is aborted.
- **Commits are attributed** to the employee (`GIT_AUTHOR_NAME = "Minh (Developer)"`), never the machine's identity.
- **Project conventions**: when the project folder has `AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md` or
  `docs/CONVENTIONS.md`, actors are told to read them first; they override the office defaults.
- **Shell hygiene**: writing file content through the shell (heredoc, `cat/echo/printf/tee >`, `sed -i`) is denied with
  a hint to use Write/Edit (workspace-checked, no approval click); saving a program's output still asks.
- **Standards**: shared skills shipped with the app (`doc-standards` with templates for requirements, design, plan,
  meeting notes, test plan/report, change request, acceptance, report, handoff; `python-project-standard`,
  `clarifying-questions`, `automation-playbook`, `research-method`, `data-analysis`). "Cập nhật vị trí mẫu" refreshes
  positions and skills from the templates.

## Memory compaction

"Gom gọn" on an actor profile (6+ unpinned memories) asks the meta model, with no tools, to merge duplicates and
drop outdated items. The answer must be JSON that refers only to known memory ids; otherwise nothing changes. Old
entries are soft-deleted; pinned ones are never touched.

## Automations: flows the actors design

There is no special feature per use case (no "reminder feature", no "report feature"). When work repeats, happens at
a given time, or needs watching, the actor who got the request designs an **automation**: a short flow of steps
the office runs by itself (`automation_save`, available to every employee in tickets and in chat).

| Step | What it does | Cost | Review |
|---|---|---|---|
| `notify {text}` | posts a message in the conversation (`{output}`, `{date}`, `{time}` are filled in) | none | no |
| `script {file}` | runs a Python script the actor wrote and tested; stdout becomes `{output}` | none (no LLM) | yes: the code |
| `agent {actor, prompt}` | gives a colleague a ticket with the prompt + `{output}` | LLM | yes |

Timing: `when` (first or only run) and/or `schedule` (`daily@HH:MM`, `weekdays@HH:MM`, `weekly:mon@HH:MM`, `every:N`).
Examples:
- reminder = `[notify]` + `when`;
- data drop = `[script]` daily;
- KPI = `[script, notify "{output}"]`;
- analysed report = `[script, agent Linh]`.

Safety:
- Flows with a script or agent step wait for the user's review of that exact version. A fingerprint of the steps
  and the script code is checked again before every run, and a re-save cancels the older review cards.
- Scripts run with:
  - `uv run --script` (PEP 723) or `py`;
  - a scrubbed environment;
  - `$AO_OUTPUT_DIR`;
  - a timeout that kills the process tree.
- Failures are posted in the chat and open one fix ticket for the owner. Three failures in a row pause the
  schedule.
- A timer loop (15 s) starts due flows. A one-time flow becomes `finished`; a missed repeating slot runs once.

## Usage after every message

The gate counts each allowed tool call (`core/stats.py`):
- files written or edited, with a real line diff;
- commands run;
- web reads and file reads;
- office tool calls.

The run stores this as `stats` next to tokens and cost. Every actor message in the chat shows one line:
`📄 2 file · +40 −3 dòng · ⌘ 4 lệnh · 🌐 1 lần đọc web · ⏱ 1 phút 12s · 18.2k tok · ≈$0.041`. Hovering it lists
the files and commands. Done-reports show the total for the ticket and its sub-tickets (`/api/tickets/{key}/work`):
runs, people, files, lines, commands, time and tokens.

## Databases

`db_schema` / `db_query` read SQLite files inside the project folder or workspace in `mode=ro` with
  an authorizer (SELECT/READ only, safe PRAGMAs), one statement, a row cap and a time limit.
