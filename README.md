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

## Team copilot (one AI engineer with a team of people)

One AI engineer ("Copilot") works with people who hold roles (BA, Dev, QE, Lead, Admin). A ticket goes through
stages with gates people pass: **understand → [BA approves] → plan → [Dev approves] → build → pr → [merge] →
close → [people confirm the knowledge]**. Design: [docs/plans/2026-10-31-team-copilot-design.md](docs/plans/2026-10-31-team-copilot-design.md).

**Run it:** double-click `run.bat` (Windows) or run `./run.sh` (macOS, Linux, WSL). It installs what is
needed and opens the web page. Everything else is done on the web, under **Cài đặt** and **Người**:

1. Add people and teams, with their roles and Telegram ids.
2. Add a repo: its folder on this machine, its team, and its test/build/lint commands. The repo's
   `CLAUDE.md`, `AGENTS.md` and `.kiro/steering` are imported as rules.
3. Optionally connect Telegram, Jira + Confluence and Bitbucket. Each one is verified before it is saved.
4. Press **+** to give the AI a task.

You need Python 3.12, [uv](https://docs.astral.sh/uv/), git and a logged-in Claude Code. The same steps can be
scripted from the terminal:

```sh
uv run actoroffice init                                   # creates the AI engineer, checks this machine (doctor)
uv run actoroffice user add Lan --telegram 111 && uv run actoroffice user add Minh --telegram 222
uv run actoroffice team add Billing --jira BIL            # --jira: issues labelled "ai" become tickets
uv run actoroffice team member Billing Lan ba && uv run actoroffice team member Billing Minh dev lead
uv run actoroffice project add billing --path F:/code/billing      # the repo the AI works in (git worktree per ticket)
uv run actoroffice project set billing --team Billing --repo-url acme/billing --profile '{"test": ["mvn","-q","test"]}'
uv run actoroffice task "Hoàn tiền VIP 60 ngày" --team Billing --owner Lan --project billing
uv run actoroffice serve                                  # web: /team (herdr-like) · Telegram: /task, topic per ticket
```

- Connections (credentials stay in `<data>/secrets`, never shown to the AI): `jira login <url> <email> <token>`
  (Jira + Confluence), `bitbucket <email> <token> <workspace>`, `resource add` for shared environments.
- Rules: `rules add … --scope team:<id>` / web Rules page / `/rule` on Telegram; repo `CLAUDE.md`, `AGENTS.md`,
  `.kiro/steering/*.md` are imported with `rules import <project>`.
- Small, clear changes (a layout fix, a typo, a config value) take the **quick** workflow: one short plan, one
  approval, the change, its PR. The AI switches to it by itself from the understand stage (or back to the full
  workflow when the change turns out bigger); people can switch it with the ⇄ button on the ticket.
- Permissions per repo (Settings): **read-only** (every change asks), **workspace** (default: edits, read-only
  commands, local git and the repo's test/build commands run; the network, other folders and unknown programs
  ask), **folder** (anything whose paths stay in the code folder runs, any program included) or **full**. "Always allow" on an approval card is remembered for that repo, by command prefix.
- Runtime per team: Claude (default) or Kiro CLI (`ai_model: kiro` in config.yaml or the team settings).
- Demo without the LLM: `uv run py scenarios/team_demo.py --home F:/_ao_demo`. Live run with Claude:
  `uv run py scenarios/live_e2e.py --home F:/_ao_live` (uses tokens).

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
uv run py -m pytest -q              # unit + engine + enterprise-flow tests (fake runtime, no Claude calls), in parallel (-n auto)
uv run py -m pytest -q -n0          # the same in one process (easier to debug; tests/seed.py holds the shared DB/git templates)
uv run py spikes/p0_claude_sdk.py   # live SDK behaviour checks (uses a little quota, model haiku)
```

Live scenarios exercise real actors against a separate lab office, so your own office is never touched:

```bash
uv run actoroffice --home F:/_ao_lab/home init          # then set web_port: 8766 in its config.yaml
ACTOROFFICE_HOME=F:/_ao_lab/home uv run actoroffice serve --no-browser
uv run py scenarios/run.py --home F:/_ao_lab/home --only analyst,enterprise
```

Results, with objective checks per scenario, are written to `docs/test-plan/results/`.

### Lint

```bash
uv run ruff check src tests         # rules and line length are set in pyproject.toml [tool.ruff]
```

The project profile's lint command (web Cài đặt → "Lệnh lint") should be `uv run ruff check src tests`, so the
`repo:lint` check runs it at the build stage. There is no auto-formatter (`ruff format` is not used).

## UI evidence (screenshots taken by the AI)

Acceptance criteria tagged `[ui]` (e.g. `AC-4 [ui]: …`, also `AC-4 [ui] [review]`) need a screenshot at the build
stage when the change touches the project's UI files. The AI takes it with the office tool `ui_screenshot`: the
office starts the project's app on this machine, opens the page in headless Chrome/Edge (only localhost; requests
to other hosts are blocked), runs the clicks/fills/waits and keeps the PNG with the AC. People see the images in
the pr pane, on the gate card and in the PR description. The check `ui_evidence` fails when an AC `[ui]` has no
image and no reason ("không chụp được: …" in the handoff, or an environment error the tool recorded).

The browser is driven by playwright with the installed Chrome or Edge (nothing is downloaded):
`uv sync --extra ui` (it is also in the dev group). Without it the tool reports an environment error.

Set it up in web Cài đặt → the project's "Giao diện" fields. The commands get `AO_UI_PORT`, `AO_UI_DATA` (a fresh
temporary folder) and `AO_UI_TOKEN` (random each start) in their environment; `{port}` and `{data}` are replaced in
their arguments, `{token}` only in the URL. The app must listen on 127.0.0.1 only. For actor-office itself:

| Field | Value |
|---|---|
| Lệnh seed | `uv run python scripts/ui_seed.py` |
| Lệnh start | `uv run actoroffice --home {data} serve --host 127.0.0.1 --port {port} --view-only --no-browser --token-env AO_UI_TOKEN` |
| URL | `http://127.0.0.1:{port}/login?t={token}&next=/team` |
| File UI | `src/**/static/**` |

`scripts/ui_seed.py` fills the temporary folder with a team, people and tickets at understand, build (gate card),
pr (review, AC `[review]`, AC `[ui]` with an image and one with a reason), done and cancelled. `serve --token-env`
takes the sign-in token from that variable and never writes it to `secrets/web_token`. The app is stopped and its
folder deleted when the AI's turn ends. Wait time: `profile.timeouts.ui` (default 60 s).
