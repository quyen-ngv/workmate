# actor-office

A chat-first office of AI actors. You create positions (BA, DEV, QA, Writer, Collector, PM…) and hire actors
into them. Actors work through **tickets**, ask you when something is unclear or risky, remember what they
learn, and turn repetitive work into code.

Actors run on your local **Claude Code** login (subscription) through the Claude Agent SDK. No Docker, no
server: one Python process and one SQLite file.

> Status: web UI with the enterprise flow (P2.6). Done:
> - chat (DMs, groups, @mentions) with ticket proposals, approvals and questions;
> - projects (folder + git worktree per feature + approved merge);
> - the BA → Dev Lead meeting → devs → QA → acceptance flow, with requirement changes synced to every ticket;
> - Definition of Done per position;
> - memory (with compaction) and versioned skills, including shipped document and code standards;
> - reminders and automations that run without the LLM;
> - read-only SQLite tools.
>
> - Telegram: one bot and one forum group, with a topic per conversation (see below).
>
> See [docs/DESIGN.md](docs/DESIGN.md) and the scenario test plan
> [docs/test-plan/scenarios.md](docs/test-plan/scenarios.md).

## Requirements

- Windows 11, macOS or Linux; Python 3.12 (`py` on Windows); [uv](https://docs.astral.sh/uv/); git
- Claude Code logged in (`claude` once, interactively). The SDK ships its own CLI binary and reuses that login.

## Quick start (web UI)

```bash
uv sync
uv run actoroffice serve        # opens http://localhost:8765/login?t=… in your browser
```

The login link is printed in the terminal and stays the same across restarts (the token is kept in
`~/.actoroffice/secrets/web_token`). In the UI:

1. In **Nhân sự**, hire actors into positions. The office ships these positions: BA, Dev Lead, Developer, QA,
   PM, Researcher, Data Analyst, Business Consultant, Personal Assistant, Data Collector & Automation, Writer.
   "Cập nhật vị trí mẫu" refreshes them and the standard skills.
2. Chat with an actor in **Tin nhắn**, or create a **Nhóm** and use `@Tên` / `@all`.
3. When real work is needed, the actor proposes a ticket. Click **Tạo ticket** to start it. Progress,
   approvals and questions appear in the same chat and under **Đang chờ bạn**.

The server binds to 127.0.0.1 only. Do not expose it. Use a tunnel with authentication if you need remote
access.

## Telegram (optional)

The office can be mirrored into one private Telegram group with **Topics** turned on. Each DM, group and
ticket meeting room becomes a topic. Messages flow both ways. Decisions show up as cards with buttons; to
answer a question, reply to its card. After each run, the actor's message gets the usage line. A "done"
report also gets the ticket's totals and its deliverable files.

1. In Telegram, open @BotFather, send `/newbot` and copy the token (`123456789:AA…`).
2. Provide the token in one of these ways. They are checked in this order:
   - an environment variable (`ACTOROFFICE_TELEGRAM_TOKEN`, `TELEGRAM_BOT_TOKEN` or `TELEGRAM`);
   - `uv run actoroffice telegram <token>`, which stores it in `~/.actoroffice/secrets/telegram_token`;
   - a `TELEGRAM=…` line in a `.env` file, in the folder you start from or in `~/.actoroffice`.
3. Create a Telegram group, turn on **Topics** in its settings, and add the bot as an **admin** with
   "Manage topics" permission. Admin rights are also what let the bot read every message.
4. Start (or restart) `uv run actoroffice serve`, then send `/start` in the group. The bot binds to that group
   and creates a topic for every actor. `/sync` creates any missing topics.

Telegram can also do what the web's side panels do. Type `/` to see the commands, or `/menu`:

- **Create:** tickets (`/new_ticket`), groups (`/new_group`), projects (`/new_project`) and hires (`/hire`).
  A form asks one question at a time, and `/cancel` stops it.
- **Browse:** `/tickets` shows each ticket with its documents, and lets you comment, change a requirement,
  reassign or cancel it. `/automations` can run, pause or resume an automation and shows the last run.
  `/team` shows each actor's usage, memory, pause/resume and a link to their chat.
- **Other:** `/skills` to read a skill, `/inbox` for what is waiting for you, `/usage` for today and the last
  7 days, and `/projects` for the chat topics of each project.

Editing positions, skills and permissions is still done on the web. The bot ignores every other chat. To turn
Telegram off, run `uv run actoroffice telegram --off`.

## Kiro CLI as a runtime (optional)

An actor can run on [Kiro CLI](https://kiro.dev/docs/cli/) instead of Claude. Set its **Model** to `kiro`, which
uses Kiro's default model, or to `kiro:<model id>`. The office then drives `kiro-cli acp` over the Agent Client
Protocol. It reads and writes files and runs commands for Kiro, so the same rules apply as with Claude:
workspace limits, approvals, "always allow" rules, requirement-change interrupts and the per-run stats. Office
tools such as tickets, chat and memory reach Kiro through a local MCP server.

1. Install Kiro CLI. On Windows 11, run this in PowerShell:
   `irm 'https://cli.kiro.dev/install.ps1' | iex`
2. Sign in once with `kiro-cli login`.
3. If `kiro-cli` is not on PATH, set `kiro_cli_path` in `~/.actoroffice/config.yaml`. Set `kiro_engine: v2`
   only for an old CLI.

Kiro reports credits rather than tokens, so the usage line shows steps, files, lines and time but not tokens.

## Quick start (terminal only)

```bash
uv run actoroffice init                                   # ~/.actoroffice: config + default positions
uv run actoroffice hire writer Lan --specialty "Tech content"
uv run actoroffice ticket "Viết giới thiệu dự án" -d "3 câu, lưu vào intro.md" -a Lan
uv run actoroffice run                                    # run the office in this terminal
```

While `run` is active, approvals and questions appear in the terminal:

```
⚠ [ix_074e…] Minh · AO-2 wants Bash: npm install left-pad  (shell command needs approval)
y ix_074e…        # approve      n ix_074e…   # reject      a <id> <answer>   # answer a question
```

If nobody answers within `approval_wait_s` (90 s by default), the run **parks**: the ticket goes to
`in_review` and the actor stops. You can answer later with `uv run actoroffice answer <id> y`, and the actor
resumes exactly where it stopped.

Other commands: `positions`, `actors`, `tickets [--all]`, `show AO-1`, `comment AO-1 "…"`,
`assign AO-1 Minh`, `inbox`, `run --once -v`.

Data lives in `~/.actoroffice` (override with `--home` or `ACTOROFFICE_HOME`). The ticket workspaces are in
`~/.actoroffice/ws/<ticket>`.

Typical enterprise use:

1. Create a project for the codebase. Choose "git worktree" mode; an empty folder becomes a new repo.
2. Message the BA: "Công ty muốn chức năng X". The BA asks a few questions with options, then proposes the
   feature ticket.
3. The BA writes `requirements` and hands over to the Dev Lead.
4. The Dev Lead meets the BA in the feature's **Họp …** group, writes `design` and `plan` (owners, estimates,
   dates), and assigns tasks to developers and QA.
5. Team reports arrive in that group. Your chat with the BA receives only the final acceptance report.
6. To change a requirement mid-way, tell the BA. Alternatively, use "Thay đổi yêu cầu" on the ticket page. The
   change reaches running work at its next step, queued work at its start, and done work when reopened.
7. When the feature is accepted, merge the branch from the **Git** box on the feature ticket.

## Development

```bash
uv run py -m pytest -q              # unit + engine + enterprise-flow tests (fake runtime, no Claude calls)
uv run py spikes/p0_claude_sdk.py   # live SDK behaviour checks (uses a little quota, model haiku)
```

Live scenarios exercise real actors against a separate lab office, so your own office is never touched:

```bash
uv run actoroffice --home F:/_ao_lab/home init          # then set web_port: 8766 in its config.yaml
ACTOROFFICE_HOME=F:/_ao_lab/home uv run actoroffice serve --no-browser
uv run py scenarios/run.py --home F:/_ao_lab/home --only analyst,enterprise
```

Results, with objective checks per scenario, are written to `docs/test-plan/results/`.
