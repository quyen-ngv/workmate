# herdr vs Claude Code / Agent SDK vs OpenAI Codex CLI — technical profile (state as of 2026-10-31)

Scope note: three tools profiled on the same dimensions (connection/auth, state, memory, extension/skills, task/status/event model, remote/API, maturity) for an engineer building a team-facing AI-engineer platform whose UI follows herdr's layout (sidebar of spaces/agents with "Needs you" badges, tabs, split panes, Files/Changes panel). Versions/dates are given where sources state them; uncertainty is flagged inline.

---

## KQ1. herdr — architecture, status detection, states and rollup

### Takeaway
herdr (v0.9.5, 2026-10-14) is a single Rust binary that runs as a background **server** (owns PTYs, workspaces, tabs, panes, agent state) plus thin attachable **clients**; agent state is derived primarily from **screen-manifest regex rules over the live bottom buffer of each pane** (TOML, bundled + remotely patched + local overrides), with optional per-agent **integrations** (hooks) that report *session identity* for resume — not state — and a small set of agents that self-report. Five states (`working`/`blocked`/`done`/`idle`/`unknown`) roll up pane → tab → workspace → sidebar.

### Cited Findings
- Architecture: "By default, Herdr runs as a background server plus one or more attached clients." Server maintains panes/process state; multiple clients can attach to one server and view different workspaces/tabs independently. **Sessions** are named runtime namespaces (`herdr session list`, `herdr session attach [name]`; default session `herdr`) with "completely separate panes, sockets, and persisted runtime state." Hierarchy: Workspace ("one workspace per repo, task, or investigation"; owns tabs/panes; sidebar rolls up agent status) → Tab (layout) → Pane ("a real terminal"). Three UI input modes: terminal, prefix (`ctrl+b`), navigate. — [herdr docs: Concepts](https://herdr.dev/tui/docs/concepts/)
- Client/server transport: Unix socket locally; `--remote` enables SSH tunnelling; "the client is a thin process in the terminal window that captures input and renders output." — [Better Stack guide (updated 2026-06-08)](https://betterstack.com/community/guides/ai/herdr-ai-agent/)
- Agent states (official): `blocked` (needs input/approval), `working` (actively running), `done` (finished, unseen), `idle` (finished/seen), `unknown` (unclassified). — [herdr docs: Concepts](https://herdr.dev/tui/docs/concepts/)
- Detection — three mechanisms: (1) **Screen-manifest detection**: "Herdr reads the live bottom buffer of terminal panes to determine agent state using bundled and remote detection manifests"; (2) **integration reporting** (takes precedence over screen detection where an agent's integration reports state); (3) **native agent support** (Command Code, Crush, Kiro CLI, Muse Code, Prime Agent report their own state). — [herdr docs: Agents](https://herdr.dev/tui/docs/agents/)
- Manifest locations/priority: bundled inside the binary; remote manifests cached in herdr's state dir and checked automatically; **local overrides at `~/.config/herdr/agent-detection/<agent>.toml`** ("Local overrides always win, followed by newer compatible remote vs. bundled"). Manifests are "TOML files containing regex and screen-matching rules"; remote manifests can only patch existing rules — new agents need a binary update. — [herdr docs: Agents](https://herdr.dev/tui/docs/agents/)
- Blocked is deliberately strict: "Herdr only marks `blocked` when the live bottom-buffer snapshot matches known visible approval, question, or permission UI." — [herdr docs: Agents](https://herdr.dev/tui/docs/agents/)
- Rollup: "Blocked agent → pane, tab, and workspace appear blocked; Working agent → workspace appears active; Done agent → remains visible until viewed." — [herdr docs: Agents](https://herdr.dev/tui/docs/agents/)
- Sidebar: "split into two layers: top showing workspaces with aggregate state dots, and bottom showing agents inside the selected or active workspace"; the rollup is to "🔴 blocked, 🟡 working, 🔵 done, or 🟢 idle, so you always know who needs you." — [WebSearch summary citing herdr.dev/agent-guide and reviews](https://herdr.dev/agent-guide.md)
- Integrations (what hooks do): Claude Code integration = `herdr integration install claude`; installs `hooks/herdr-agent-state.sh` under `~/.claude` (or `CLAUDE_CONFIG_DIR`) and adds hook entries to `settings.json`; uses **`SessionStart`** to report session identity on startup/resume/clear/compact/fork; "The system derives state from screen detection rather than hook reports"; needs integration v6+ for native session restore. Codex integration = `herdr integration install codex`; installs `herdr-agent-state.sh` under `~/.codex` (or `CODEX_HOME`), updates `hooks.json`, ensures `[features] hooks = true` in `config.toml`; v5+ required; state again comes from screen manifests. Uninstall: `herdr integration uninstall <agent>`. — [herdr docs: Integrations](https://herdr.dev/tui/docs/integrations/)
- Self-reported state via API: `pane.report_agent` with semantic states `idle|working|blocked|done`; `pane.report_agent_session` stores native session refs with optional `resume_argv` for resume after restart; `pane.report_metadata` tokens (display-only, TTL). — [herdr docs: Socket API](https://herdr.dev/tui/docs/socket-api/)
- Custom labels: `herdr agent rename w1:p1 reviewer` / `--clear`; metadata tokens via `herdr pane report-metadata w1:p1 --source custom:indexer-display --token summary=indexing`; `herdr agent attach reviewer` (direct attach; detach `ctrl+b q`). Env knobs: `HERDR_AGENT=<agent>` for wrapped/VM-run agents; `HERDR_PROCESS_DETECTION=child-groups` for restricted Linux runtimes. — [herdr docs: Agents](https://herdr.dev/tui/docs/agents/)
- Supported agents (27+): Claude Code, Codex, GitHub Copilot CLI, Cursor Agent CLI, OpenCode, Pi, OMP, Droid, Devin CLI, Kimi Code CLI, Kilo Code CLI, Hermes Agent, Qoder CLI, Qwen Code, Letta Code, MastraCode, Grok CLI, Antigravity CLI, Amp, Kiro CLI, Maki, Gemini CLI, Cline (+ native: Command Code, Crush, Kiro CLI, Muse Code, Prime Agent). — [herdr docs: Agents](https://herdr.dev/tui/docs/agents/)
- Release notes (Sept–Oct 2026): "Self-reporting agents can now restore exact sessions post-restart without built-in integration"; "Agents transition to idle once panes return to shell prompts, even with background tasks running"; improved recognition for Codex/Antigravity/Grok/Kiro/Droid/OpenCode. — [GitHub releases](https://github.com/herdrdev/herdr/releases)
- Agent-facing diagnostics: `herdr agent explain <target> --json` returns structured output explaining the detector classification; `herdr agent list`; `herdr integration status`. — [herdr agent guide](https://herdr.dev/agent-guide.md)

### Inferences
- For a platform UI: herdr's model is **state = f(last screen) + identity = hook**, which is why "blocked" is conservative and why upstream TUI redesigns can break detection (confirmed as a reviewer complaint). A platform that drives agents via SDK/app-server (Claude stream-json, Codex app-server) can get authoritative state (`turn/started`, `requestApproval`, `result`) and should not need screen scraping — but herdr's five-state vocabulary and pane→tab→workspace rollup is a good UI contract to copy.
- `done` vs `idle` ("finished, unseen" vs "finished, seen") is the key UX distinction behind the "Needs you" feel; it is view-state, not agent-state.

### Gaps
- The docs pages do not define a literal UI label "Needs you"; the term appears in marketing/landing copy and reviews. I could not find an official spec of a "Needs you" sidebar section as a distinct component beyond the blocked/done rollup.
- No official documentation found for "parent/child notes" between agents; the closest documented primitives are `herdr agent prompt`, `agent wait`, and `--outlive-parent` (see KQ2).

---

## KQ2. herdr — notifications, plugins/marketplace, session state (detach/restore/replay/resume/handoff), remote machines, worktrees, socket API, config formats, license and maturity

### Takeaway
herdr persists layout (`session.json`) and optionally screen history, resumes agents via their native `--resume`/`resume <id>` using session IDs captured by hooks, offers an experimental **live handoff** of PTYs across server upgrades, exposes a newline-delimited JSON socket API with event subscriptions, and has a TOML plugin system with a GitHub-topic marketplace. It is Apache-2.0 (relicensed from AGPL in July 2026), 47k stars, pre-1.0, VC-funded (YC Aug 2026, $6M seed Sept 2026).

### Cited Findings
**Notifications / sounds**
- Config `[ui.toast]` with `delivery`, `delay_seconds` (example: `delivery = "herdr"`, `delay_seconds = 1`, `position = "bottom-right"`); delivery options are in-app toast, outer terminal, OS notifications, or disabled; popups suppressed for the active tab. `[ui.sound]` keys: `path`, `done_path`, `request_path` (distinct sounds for done vs. request/blocked). — [herdr docs: Configuration](https://herdr.dev/tui/docs/configuration/); [Flavio Copes deep dive (2026-10-30)](https://flaviocopes.com/herdr/)
- "Herdr supports system notifications and audio alerts when agents change state in background panes." — [Better Stack](https://betterstack.com/community/guides/ai/herdr-ai-agent/)
- Agent-to-agent coordination: "A coordinator can target another agent, send prompts via `herdr agent prompt`, wait for state changes, and read terminal output. Parent agents can start child agents with `--outlive-parent` flag to control lifetime." — [Flavio Copes](https://flaviocopes.com/herdr/)

**Session state**
- Detach/reattach: `ctrl+b q` detaches; server keeps all panes/agents/processes; `herdr` reattaches. — [herdr docs: Session state](https://herdr.dev/tui/docs/session-state/)
- Restore after server restart: processes are lost; "workspaces, tabs, panes, cwd, layout, and focus" restored; panes without a stronger recovery path restart as new shells in the saved cwd. — [Session state](https://herdr.dev/tui/docs/session-state/)
- Pane history replay: off by default ("contains sensitive data like tokens and secrets"); `pane_history = true` stores contents in `session-history.json`; replays only if layout exactly matches. — [Session state](https://herdr.dev/tui/docs/session-state/)
- Native agent resume: Claude Code via `claude --resume <id>`, Codex via `codex resume <id>` (requires current integration versions); config `[session] resume_agents_on_restore`, `startup_per_agent_delay_ms`. — [Session state](https://herdr.dev/tui/docs/session-state/); [Configuration](https://herdr.dev/tui/docs/configuration/)
- Live handoff (experimental, opt-in): `herdr update --handoff` or `herdr --remote workbox --handoff` transfers "pane PTYs and processes, agent identity and durable metadata" to the new server; in-flight streams may be interrupted. — [Session state](https://herdr.dev/tui/docs/session-state/)
- State files: `session.json` (primary), `session-backups/` (up to 3), `session-history.json`, `session-snapshots/` (up to 48 layout snapshots). Survival matrix: detach keeps processes/layout/screen/agent; restart keeps layout only (+history if enabled, +agent only with native restore); update with handoff keeps all best-effort. — [Session state](https://herdr.dev/tui/docs/session-state/)

**Remote machines**
- `herdr machine add workbox` (interactive; discovers remote sessions, offers to install); `--label`, `--remote-session`; `herdr machine list|rename|disable|enable|remove|reconnect`; profiles store "an opaque ID, label, SSH target, explicit remote session, and enabled state" (no credentials). Auto-retries after network interruption/sleep; `remote.manage_ssh_config=true` shares an approved OpenSSH connection; badges `! auth` / `! error`; `prefix+w` navigates across machines; `herdr --machine <id> agent list` for scripting. Each remote machine runs its own server/sessions. — [herdr docs: Connecting machines](https://herdr.dev/tui/docs/connecting-machines/)
- Thin client over SSH: `herdr --remote ssh://user@server:port` streams rendering only; native support in Moshi iOS terminal (reviewer claim). — [Bitdoze review (2026-07-10, updated 2026-09-02)](https://www.bitdoze.com/herdr-agent-multiplexer/)
- Multi-machine arrived "since version 0.9". — [Flavio Copes](https://flaviocopes.com/herdr/)

**Worktrees**
- `herdr worktree create --cwd ~/project --branch fix-header`; default storage `~/.herdr/worktrees/<repo>/<branch>` (config `[worktrees] directory`); each worktree becomes a separate workspace. Socket API tracks "worktree provenance on workspace records". Release note: "Worktree groups survive checkouts that are temporarily unavailable." — [Flavio Copes](https://flaviocopes.com/herdr/); [Configuration](https://herdr.dev/tui/docs/configuration/); [Socket API](https://herdr.dev/tui/docs/socket-api/); [Releases](https://github.com/herdrdev/herdr/releases)

**Socket API / CLI**
- Transport: newline-delimited JSON over Unix domain socket (Windows: named pipe); one request per line, responses echo `id`: `{"id":"req_1","method":"ping","params":{}}`; errors `{"id":"req_1","error":{"code":"not_found","message":"pane not found"}}`; codes `not_found`, `invalid_params`, `platform_unsupported`, `plugin_disabled`, `events_lost`. — [Socket API](https://herdr.dev/tui/docs/socket-api/)
- Socket path: default `~/.config/herdr/herdr.sock`; named session `~/.config/herdr/sessions/<name>/herdr.sock`; resolution order `--session` → `HERDR_SOCKET_PATH` → `HERDR_SESSION` → default. No auth beyond socket file permissions. — [Socket API](https://herdr.dev/tui/docs/socket-api/)
- Method families: `ping`, `server.stop`, `server.reload_config`, `server.agent_manifests`, `server.reload_agent_manifests`; workspace/tab CRUD+focus+rename+move; pane split/swap/move/zoom/resize/focus, send text/keys/input, read output, run processes, `pane.report_agent`, `pane.report_agent_session`, `pane.report_metadata`; agent list/get/read/explain/prompt/wait/start/focus/rename; layout export/apply; `events.subscribe` with subscriptions such as `{"type":"pane.agent_status_changed","pane_id":"w1:p1"}` plus workspace/tab/pane lifecycle and `worktrees: created/opened/removed`; plugin `plugin.link`, `plugin.action.invoke`, `plugin.pane.open`. — [Socket API](https://herdr.dev/tui/docs/socket-api/)
- CLI equivalents: `herdr workspace create --cwd ~/project --label api`, `herdr tab create --label logs`, `herdr pane split w1:p1 --direction right`, `herdr pane run w1:p2 "npm test"`, `herdr pane read w1:p2 --source recent --lines 50`, `herdr agent wait w1:p1 --until done`, `herdr api schema --json`, `herdr api snapshot`. Protocol stability: "JSON API clients should ignore unknown fields and treat unsupported methods as normal errors." — [Socket API](https://herdr.dev/tui/docs/socket-api/)
- Agent skill: `skills/herdr/SKILL.md` teaches agents to split panes, run commands without stealing focus, read output, wait on other agents; install `npx skills add herdrdev/herdr --skill herdr -g`. — [Agent guide](https://herdr.dev/agent-guide.md)

**Plugins / marketplace**
- Manifest `herdr-plugin.toml`: required `id`, `name`, `version`, `min_herdr_version`; optional `description`, `platforms`; sections `[[build]]`, `[[startup]]`, `[[actions]]`, `[[events]]` (e.g. `on = "worktree.created"`, `command = [...]`, receives `HERDR_PLUGIN_EVENT_JSON`), `[[panes]]` (placements `overlay`|`popup`|`split`|`tab`|`zoomed`), `[[link_handlers]]` (Rust regex on clicked URL), `[[keys.command]]`. Commands are argv arrays, no shell expansion. Env: `HERDR_SOCKET_PATH`, `HERDR_BIN_PATH`, `HERDR_PLUGIN_ID/ROOT/CONFIG_DIR/STATE_DIR/CONTEXT_JSON`, `HERDR_WORKSPACE_ID/TAB_ID/PANE_ID`. Install `herdr plugin install owner/repo[/subdir]` (GitHub only), `plugin link`, `plugin update`, `plugin uninstall/unlink`. No sandboxing ("A plugin is ordinary code that runs on your machine"). No plugin storage API in v1. — [herdr docs: Plugins](https://herdr.dev/tui/docs/plugins/)
- Marketplace: indexed from GitHub topic `herdr-plugin`, refreshed every 30 min, "not a reviewed catalog". Notable plugins: herdr-plus, herdr-file-viewer (git-aware file browsing), herdr-reviewr (PR review sidebar), ccgram (Telegram notifications), vim-herdr-navigation. — [Plugins](https://herdr.dev/tui/docs/plugins/); [Bitdoze](https://www.bitdoze.com/herdr-agent-multiplexer/); [Preuve review (2026-10-12)](https://preuve.ai/blog/herdr-review)

**Config**
- `~/.config/herdr/config.toml` (Windows `%APPDATA%\herdr\config.toml`); sections `[keys]`, `[theme]`, `[theme.custom]`, `[ui]`, `[ui.sidebar.agents]` (`rows`, `row_gap`, `rows_by_agent`), `[ui.sidebar.spaces]`, `[ui.toast]`, `[ui.sound]`, `[terminal]`, `[server]` (`headless_cols/rows`), `[session]`, `[remote]`, `[worktrees]`. `herdr --default-config > ...`; `herdr server reload-config`. — [Configuration](https://herdr.dev/tui/docs/configuration/)

**License / maturity**
- GitHub API (2026-10-31): license `Apache-2.0`, 47,049 stars, 3,759 forks, created 2026-03-27, language Rust, latest release v0.9.5 (2026-10-14). LICENSE history: commit "chore: relicense herdr under apache-2.0" (2026-07-22); earlier "docs: clarify dual licensing" (2026-05-26). — [api.github.com/repos/herdrdev/herdr](https://api.github.com/repos/herdrdev/herdr); [LICENSE commits](https://api.github.com/repos/herdrdev/herdr/commits?path=LICENSE)
- Conflict: Bitdoze (Jul/Sep 2026) states "AGPL-3.0-or-later … Commercial license available" — [Bitdoze](https://www.bitdoze.com/herdr-agent-multiplexer/); superseded by the July 22 relicense (Preuve Oct 2026 and GitHub both say Apache 2.0) — [Preuve](https://preuve.ai/blog/herdr-review).
- Funding/maturity: joined Y Combinator (2026-08-06), "runtime under Apache-2.0 while building enterprise features on top" — [ecosistemastartup.com](https://ecosistemastartup.com/herdr-entra-a-y-combinator-25k-estrellas-y-runtime-abierto-para-agentes-de-ia/); $6M seed Sept 2026 (Bessemer-led), "pre-1.0 software with 11 releases in September alone" — [Preuve](https://preuve.ai/blog/herdr-review). #1 GitHub trending 2026-06-30; solo founder Ogulcan Celik (bus factor cited) — [Bitdoze](https://www.bitdoze.com/herdr-agent-multiplexer/).
- Reviewer pitfalls: text-rendering delays with 10+ panes; Windows "preview"/WSL glitches ("rendering, scrolling, hotkeys and losing autosuggestions"); no agent sandboxing; after restart "saved layout returns but running processes do not"; screen-history off by default for secrets; manifest auto-updates from herdr.dev unless disabled; "Detection isn't perfect; agent updates can break screen manifests"; "Doesn't isolate file changes between agents"; users struggle beyond 2–3 parallel sessions. — [Bitdoze](https://www.bitdoze.com/herdr-agent-multiplexer/); [Preuve](https://preuve.ai/blog/herdr-review); [Flavio Copes](https://flaviocopes.com/herdr/)
- Possible new product line (LOW CONFIDENCE): a fetch of herdr.dev's landing page (2026-10-31) returned copy describing a desktop app where agents "communicate status through OSC 7501, an open protocol", an `hc` CLI (`hc run --bg codex '...'`, `hc send t1`), `settings.json` for "fonts, theme, sounds", and an FAQ saying "The app, herdr-core and Herdr TUI are all open source" (Apache 2.0). Flavio Copes notes "Desktop app announced October 29, 2026" and a `herdr-gpui` repo exists. This differs from the TUI docs (config.toml, socket API) and I could not verify the OSC 7501 protocol from an official doc. — [herdr.dev](https://herdr.dev/); [Flavio Copes](https://flaviocopes.com/herdr/); [herdr-gpui releases](https://github.com/penso/herdr-gpui/releases/tag/v20261007.3)

### Inferences
- Replicable primitives for the platform: (a) `events.subscribe` with `pane.agent_status_changed`-style events; (b) `agent wait --until <state>`; (c) `report_agent_session` + `resume_argv` so the orchestrator can relaunch an agent with the right native resume command after a crash; (d) per-worktree workspace; (e) distinct "request" vs "done" sounds.
- herdr intentionally avoids being the agent harness — it wraps PTYs. A platform that owns the agent processes can be stricter (true blocked signals, cost, diffs) but should keep herdr's "detach survives client death" invariant.

### Gaps
- No official doc for the exact semantics of "notes" (parent/child notes) — only `agent prompt`/`wait`/`--outlive-parent`. Treat "notes" as unverified terminology.
- The `docs/versions/*/website/src/content/docs/*.mdx` path from the brief was not confirmed (repo contents listing failed locally); official docs now live under `herdr.dev/tui/docs/*`.
- No published performance numbers or SLA; no documented authentication for the socket API (local trust only).

---

## KQ3. Claude Code / Agent SDK — LLM connection: API key vs subscription vs Bedrock/Vertex/Foundry, base-URL proxy, Agent SDK credits

### Takeaway
Claude Code picks a credential in a fixed precedence (cloud provider env → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` → `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → profiles → `/login` subscription OAuth); `ANTHROPIC_BASE_URL` routes to a gateway, and since 2026-06-15 Agent SDK/programmatic use on subscriptions is metered against a separate monthly "Agent SDK credit" ($20 Pro / $100 Max 5x / $200 Max 20x) billed at API rates.

### Cited Findings
- Precedence: 1) cloud provider creds when `CLAUDE_CODE_USE_BEDROCK` / `CLAUDE_CODE_USE_VERTEX` / `CLAUDE_CODE_USE_FOUNDRY` set; 2) `ANTHROPIC_AUTH_TOKEN` (sent as `Authorization: Bearer`, for gateways); 3) `ANTHROPIC_API_KEY` (`X-Api-Key`; interactive mode asks once to approve; in `-p` always used); 4) `apiKeyHelper` script; 5) `CLAUDE_CODE_OAUTH_TOKEN` (one-year token from `claude setup-token`; model requests only, no Remote Control/claude.ai connectors; not read in `--bare`); 6) Anthropic profile/WIF credentials; 7) subscription OAuth from `/login`. A signed-in Claude apps gateway session outranks all. — [Authentication](https://code.claude.com/docs/en/authentication)
- Credential storage: macOS Keychain (falls back to `~/.claude/.credentials.json` mode 0600); Linux `~/.claude/.credentials.json`; Windows `%USERPROFILE%\.claude\.credentials.json`; under `CLAUDE_CONFIG_DIR` the file and Keychain entry are keyed to that directory (enables multiple accounts: `alias claude-work='CLAUDE_CONFIG_DIR=~/.claude-work claude'`). — [Authentication](https://code.claude.com/docs/en/authentication)
- Admin controls: `forceLoginMethod` (`claudeai`|`console`|`gateway`), `forceLoginOrgUUID`, `allowedProviders` (e.g. `["anthropic","bedrock"]`, or `["customEndpoint"]` with a pinned `ANTHROPIC_BASE_URL` in the managed `env` block; v2.1.285+). — [Authentication](https://code.claude.com/docs/en/authentication); [LLM gateway](https://code.claude.com/docs/en/llm-gateway)
- Gateway pattern: `ANTHROPIC_BASE_URL` points at the gateway; "Setting only that variable, without a gateway credential, doesn't replace the subscription" — the claude.ai login stays active and gateways must forward the OAuth capability in `anthropic-beta`; with a gateway credential/`apiKeyHelper` active, traffic is billed per token to the credential owner. — [LLM gateway](https://code.claude.com/docs/en/llm-gateway)
- Hosting note: "the subprocess reads `ANTHROPIC_API_KEY` from its environment … or set `ANTHROPIC_BASE_URL` to route model calls through a proxy that injects the key outside the container." — [Hosting the Agent SDK](https://code.claude.com/docs/en/agent-sdk/hosting)
- Bare mode: `claude --bare -p` never reads OAuth credentials/keychain; needs `ANTHROPIC_API_KEY` or `apiKeyHelper`; Bedrock/Vertex/Foundry still read their own creds; `--bare` "will become the default for `-p` in a future release." — [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)
- Agent SDK credit policy: on 2026-04-04 Anthropic banned subscriptions from powering third-party agents (OpenClaw); from 2026-06-15 reinstated via metered monthly "Agent SDK credit": Pro $20, Max 5x $100, Max 20x $200, Team Premium $100/seat, Enterprise Premium $200/seat; no rollover; "Charged at API rates"; after exhaustion requires "extra usage" pay-as-you-go; interactive Claude Code still draws on normal subscription limits. — [VentureBeat (2026-05-13)](https://venturebeat.com/technology/anthropic-reinstates-openclaw-and-third-party-agent-usage-on-claude-subscriptions-with-a-catch)
- As of 2026-10-07 the credits "can be claimed into a Claude Console organization and used with an API key from that organization" (secondary sources). — [Totalum blog](https://www.totalum.app/blog/claude-agent-sdk-credits-2026); [claudefa.st](https://claudefa.st/blog/guide/development/agent-sdk-credit)
- Auto mode (permission classifier) availability by provider: Anthropic API, Claude Platform on AWS, Bedrock, Google Cloud Agent Platform, Microsoft Foundry, signed-in Claude apps gateway. Channels are *not* available on Bedrock/Vertex/Foundry. — [Permission modes](https://code.claude.com/docs/en/permission-modes); [Channels](https://code.claude.com/docs/en/channels)

### Inferences
- For a team platform: run agents under an org Console API key (or gateway with `ANTHROPIC_AUTH_TOKEN`) for predictable billing/attribution; per-user subscription tokens (`CLAUDE_CODE_OAUTH_TOKEN`) work but are capped by the Agent SDK credit and can't use claude.ai connectors.
- `CLAUDE_CONFIG_DIR` per tenant/user is the isolation primitive for credentials, sessions and auto-memory simultaneously.

### Gaps
- I could not fetch Anthropic's primary announcement of the June 15 credit change (only press/secondary); exact claim-to-Console mechanics are from secondary blogs.

---

## KQ4. Claude Code / Agent SDK — state: transcripts, resume/fork, SessionStore, compaction, CLAUDE_CONFIG_DIR, settingSources

### Takeaway
Sessions are JSONL transcripts at `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl` (format explicitly unstable), resumable by ID from any directory (v2.1.223+), forkable (`--fork-session`/`/branch`), mirrorable to external storage via the SDK `SessionStore` adapter (dual-write, best-effort), with manual and automatic compaction; `CLAUDE_CONFIG_DIR` relocates everything and `settingSources` controls which settings files the SDK loads.

### Cited Findings
- Storage: "`~/.claude/projects/<project>/<session-id>.jsonl`, where `<project>` is your working directory path with non-alphanumeric characters replaced by `-`"; >200 chars → truncated + hash. "Each line is a JSON object… The entry format is internal to Claude Code and changes between versions, so scripts that parse these files directly can break." Knobs: `CLAUDE_CONFIG_DIR` (move off `~/.claude`), `CLAUDE_CODE_PROJECT_DIR_NAME` (pin project dir name; v2.1.234+, only with `CLAUDE_CONFIG_DIR`), `cleanupPeriodDays` (30-day default retention), `CLAUDE_CODE_TRANSCRIPT_LOCAL_GC`, `CLAUDE_CODE_SKIP_PROMPT_HISTORY`, `--no-session-persistence`. — [Manage sessions](https://code.claude.com/docs/en/sessions)
- Resume: `claude --continue`, `claude --resume` (picker), `--resume <name|id|transcript-path>`, `--from-pr <n>`, `/resume`; ID lookup order: current project + worktrees, then every other project on the machine (resolves only if exactly one other project holds it); `-p`/SDK sessions are hidden from the picker and `--continue` but resumable by ID; `claude -p --continue` includes them. Naming: `claude -n name`, `/rename`, `Ctrl+R` in picker; AI-generated titles. Fork: `/branch [name]`, `--fork-session`; "If you resume the same session in two terminals without forking, messages from both interleave into one transcript." — [Manage sessions](https://code.claude.com/docs/en/sessions)
- What resumes restore: history (post-compaction summary + later messages), interrupted tool calls not re-run, model, `--agent`, permission mode (with a detailed table; `bypassPermissions` never restored), active goal, session-scoped scheduled tasks; NOT restored: `--mcp-config`, `--settings`, `--plugin-dir`, `--fallback-model`, `--add-dir`. `CLAUDE_CODE_RESUME_INTERRUPTED_TURN=1` continues an interrupted turn. — [Manage sessions](https://code.claude.com/docs/en/sessions); [Headless](https://code.claude.com/docs/en/headless)
- Background sessions / agent view: `claude attach <id>`, `claude stop <id>`, `claude agents --json`, `--bg`; resuming a running background session attaches instead (v2.1.285+). — [Manage sessions](https://code.claude.com/docs/en/sessions)
- SDK options: `resume`, `continue`/`continue_conversation`, `forkSession`/`fork_session`, `sessionId`, `resumeSessionAt` (message UUID), `persistSession` (TS only), `sessionStore`/`session_store`, `title`; helpers `listSessions()`, `getSessionMessages()`, `getSessionInfo()`, `renameSession()`, `tagSession()` (Python snake_case). V2 `createSession()` API removed in TS SDK 0.3.142. — [Sessions (SDK)](https://code.claude.com/docs/en/agent-sdk/sessions); [TypeScript reference](https://code.claude.com/docs/en/agent-sdk/typescript)
- SessionStore interface: required `append(key, entries)` and `load(key)`; optional `listSessions`, `listSessionSummaries`, `delete`, `listSubkeys`; `SessionKey = {projectKey, sessionId, subpath?}` (`subpath` e.g. `subagents/agent-<id>`); `foldSessionSummary` helper; `InMemorySessionStore` shipped; reference adapters for S3/Redis/Postgres; conformance suite. Dual-write: subprocess writes local disk first, SDK mirrors; resume-from-store runs in a temp `CLAUDE_CONFIG_DIR` and deletes local copy at end; mirror failures retried ≤3× then emit `{type:"system", subtype:"mirror_error"}`; dedupe by `entry.uuid`; conflicts with `persistSession:false` and file checkpointing; Python resume-from-store copies only credentials + `.claude.json` (so `apiKeyHelper` in user settings fails). `forkSession` rewrites session IDs/UUIDs rather than byte-copying. — [Session storage](https://code.claude.com/docs/en/agent-sdk/session-storage)
- Compaction: `/compact [instructions]`; hooks `PreCompact`/`PostCompact` with matcher `manual`|`auto`; `getSessionMessages` returns the post-compaction chain (e.g. 503 raw entries → 18 messages); SDK emits `SDKCompactBoundaryMessage`. — [Manage sessions](https://code.claude.com/docs/en/sessions); [Hooks](https://code.claude.com/docs/en/hooks); [Session storage](https://code.claude.com/docs/en/agent-sdk/session-storage); [TypeScript reference](https://code.claude.com/docs/en/agent-sdk/typescript)
- Multi-tenant isolation: `settingSources: []` skips user/project/local settings; auto memory loads regardless, so set `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`; per-tenant `CLAUDE_CONFIG_DIR` and `cwd`; TS `env` replaces the subprocess env (spread `process.env`). Resources ~1 GiB RAM/5 GiB disk/1 CPU per agent; one `claude` subprocess per session over stdio; no built-in session timeout (use `maxTurns`); `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` for subagent stall. — [Hosting](https://code.claude.com/docs/en/agent-sdk/hosting)

### Inferences
- A team platform should treat the JSONL as opaque and build its own event store from stream-json/SDK messages, using `SessionStore` only for resume durability.
- `CLAUDE_CODE_PROJECT_DIR_NAME` + `CLAUDE_CONFIG_DIR` gives deterministic per-session paths — useful when the platform embeds Claude Code and wants to find transcript + auto-memory directories without re-deriving the cwd encoding.

### Gaps
- Auto-compact threshold percentage and the `autoCompactEnabled` setting name were not confirmed in the pages fetched (context-window page not fetched).

---

## KQ5. Claude Code — memory: CLAUDE.md tiers, auto memory, subagent memory, loading into the system prompt

### Takeaway
Two systems: human-written CLAUDE.md/AGENTS.md (managed → user → project → local tiers plus `.claude/rules/`) and Claude-written **auto memory** under `~/.claude/projects/<project>/memory/` with a `MEMORY.md` index of which only the first 200 lines / 25 KB are injected each session; subagents can get their own memory dir via `memory: user|project|local` frontmatter.

### Cited Findings
- "Claude Code has two complementary memory systems. Both are loaded at the start of every conversation. Claude treats them as context, not enforced configuration." CLAUDE.md: written by you, scope "Project, user, or org", loaded "Every session". Auto memory: written by Claude, "Per repository, shared across worktrees", loaded "Every session (first 200 lines or 25KB)". Claude can read a repo's `AGENTS.md` in place of CLAUDE.md; `.claude/rules/*.md` scopes rules to file types; "Subagents can also maintain their own auto memory." — [How Claude remembers your project](https://code.claude.com/docs/en/memory)
- Auto memory path: "`~/.claude/projects/<project>/memory/` loads into the system prompt regardless of `settingSources`"; disable with `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`. — [Hosting](https://code.claude.com/docs/en/agent-sdk/hosting)
- With `CLAUDE_CODE_PROJECT_DIR_NAME=work` and `CLAUDE_CONFIG_DIR=/srv/tenant-a`, transcripts go to `/srv/tenant-a/projects/work/` and auto memory to `/srv/tenant-a/projects/work/memory/`. — [Manage sessions](https://code.claude.com/docs/en/sessions)
- CLAUDE.md tiers (hosting doc): "`~/.claude/CLAUDE.md` for the user tier and the session's working directory for the project tier"; `SessionStore` does not mirror memory files. — [Hosting](https://code.claude.com/docs/en/agent-sdk/hosting)
- Hooks: `InstructionsLoaded` fires when "CLAUDE.md or `.claude/rules/*.md` loaded". — [Hooks](https://code.claude.com/docs/en/hooks)
- Subagent memory: frontmatter `memory: user|project|local` → `~/.claude/agent-memory/<agent-name>/`, `.claude/agent-memory/<agent-name>/`, `.claude/agent-memory-local/<agent-name>/`; the subagent "Includes first 200 lines or 25KB of `MEMORY.md` in system prompt" and gets Read/Write/Edit for the memory dir; `omitClaudeMd: true` skips user/project CLAUDE.md at startup. — [Subagents](https://code.claude.com/docs/en/sub-agents)
- Example in this environment: `~/.claude/projects/<project>/memory/MEMORY.md` is an index of bullet links to topic files (e.g. `[User language & autonomy](user-language-and-autonomy.md) — ...`). — (observed in this session's system context; consistent with the docs)
- SDK `getContextUsage({detail:"full"})` returns categories, totalTokens, maxTokens and `memoryFiles[]`. — [TypeScript reference](https://code.claude.com/docs/en/agent-sdk/typescript)

### Inferences
- The MEMORY.md-as-index + topic files pattern (200-line/25 KB cap on the index only) is directly portable to a platform memory layer: keep a small always-injected index and lazily-read detail files.

### Gaps
- The full memory page (53 KB) was fetched but my extraction of tier precedence (managed `/etc/claude-code/CLAUDE.md`, `@import` syntax, `.claude/rules/` `paths:` frontmatter, `autoMemoryEnabled`-style settings) was truncated; those specifics should be re-verified at the source before quoting.

---

## KQ6. Claude Code — skills (SKILL.md), plugins/marketplaces, hooks, permission modes, sandboxing, channels

### Takeaway
Skills are `SKILL.md` files with YAML frontmatter discovered from enterprise/user/project/nested/plugin dirs (descriptions always in context, body on invoke); hooks cover ~35 lifecycle events with `command|http|mcp_tool|prompt|agent` handlers and JSON decision outputs; six permission modes; OS sandbox (Seatbelt/bubblewrap) for Bash only; Channels (research preview) push Telegram/Discord/iMessage events into a running session.

### Cited Findings
**Skills**
- Frontmatter keys: `name`, `description`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `context: fork`, `agent`, `model`, `effort`, `argument-hint`, `arguments`, `background`, `hooks`, `paths`, `shell`, `metadata`, `license`, `compatibility`. Locations/priority: enterprise managed → `~/.claude/skills/<name>/SKILL.md` → `.claude/skills/` → nested `<subdir>/.claude/skills/` → `--add-dir` → plugin `<plugin>/skills/` (namespaced `/<plugin>:<name>`) → claude.ai synced `~/.claude/skills/synced/`. "Skill descriptions are always loaded into context… Full skill content loads only when invoked". Substitutions `$ARGUMENTS`, `$0..$N`, `${CLAUDE_SESSION_ID}`, `${CLAUDE_SKILL_DIR}`, `${CLAUDE_PROJECT_DIR}`, `${CLAUDE_PLUGIN_ROOT}`; dynamic injection with `` !`cmd` `` (local sources only); live-reloaded; `/reload-skills`, `/reload-plugins`. — [Skills](https://code.claude.com/docs/en/skills)
- Plugin marketplaces: `/plugin marketplace add anthropics/claude-plugins-official`, `/plugin install telegram@claude-plugins-official`, `/plugin marketplace update`; settings keys `enabledPlugins`, `extraKnownMarketplaces`; plugin hooks file `hooks/hooks.json`; `${CLAUDE_PLUGIN_DATA}` persistent dir. — [Channels](https://code.claude.com/docs/en/channels); [Hooks](https://code.claude.com/docs/en/hooks); [Session storage](https://code.claude.com/docs/en/agent-sdk/session-storage)

**Hooks**
- Events: `SessionStart`, `Setup`, `SessionEnd`, `UserPromptSubmit`, `UserPromptExpansion`, `Stop`, `StopFailure`, `PreToolUse`, `PermissionRequest`, `PermissionDenied`, `PostToolUse`, `PostToolUseFailure`, `PostToolBatch`, `InstructionsLoaded`, `ConfigChange`, `CwdChanged`, `DirectoryAdded`, `FileChanged`, `PreCompact`/`PostCompact`, `PreModelSwitch`/`PostModelSwitch`, `SubagentStart`/`SubagentStop`, `TeammateIdle`, `TaskCreated`/`TaskCompleted`, `Notification`, `MessageDisplay`, `Elicitation`/`ElicitationResult`, `WorktreeCreate`/`WorktreeRemove`. Types `command`, `http`, `mcp_tool`, `prompt`, `agent`; configured in `~/.claude/settings.json`, `.claude/settings.json`, `.claude/settings.local.json`, plugin `hooks/hooks.json`, skill/subagent frontmatter. Exit 0 = success (stdout JSON parsed), exit 2 = block; JSON fields `continue`, `decision`, `reason`, `systemMessage`, `hookSpecificOutput.{permissionDecision: allow|deny|ask|defer, updatedInput, additionalContext, updatedToolOutput, worktreePath}`. Common input: `session_id`, `prompt_id`, `transcript_path`, `cwd`, `scratchpad_dir`, `permission_mode`, `hook_event_name`, `agent_id`, `agent_type`. `async`/`asyncRewake` command hooks; default timeout 600 s; `terminalSequence` output (e.g. OSC 777 notify). — [Hooks reference](https://code.claude.com/docs/en/hooks)

**Permission modes**
- `default` (Manual), `acceptEdits`, `plan`, `auto` (classifier model, default Claude Sonnet 5, server-side when available; `CLAUDE_CODE_AUTO_MODE_MODEL`), `dontAsk` (deny anything that would prompt), `bypassPermissions` (`--dangerously-skip-permissions`). Set via `--permission-mode`, `permissions.defaultMode`, Shift+Tab. Built-in starting mode (v2.1.283+): interactive/VS Code/desktop `auto`; `claude -p`/Agent SDK `default`; cloud `acceptEdits`. Never auto-approved: `permissions.ask` rules, `AskUserQuestion`, critical-path `rm`, etc. `-p` flags: `--allowedTools`, `--permission-prompt-tool`, `--permission-prompts none` (v2.1.259+; denials appear as `permission_denied` system messages and in result `permission_denials`). — [Permission modes](https://code.claude.com/docs/en/permission-modes); [Headless](https://code.claude.com/docs/en/headless)

**Sandboxing**
- macOS Seatbelt; Linux/WSL2 bubblewrap + socat (optional seccomp); native Windows unsupported. Settings `sandbox.enabled`, `autoAllowBashIfSandboxed`, `filesystem.allowWrite/denyRead/disabled`, `network.allowedDomains/deniedDomains/allowLocalBinding/allowManagedDomainsOnly`, `excludedCommands`, `allowUnsandboxedCommands`, `failIfUnavailable`, `credentials.files/envVars` (`deny`|`mask`). Sandboxed: Bash/PowerShell/Monitor + children; NOT sandboxed: Read/Edit/Write, WebFetch, MCP servers, hooks. `/sandbox` panel. — [Sandboxing](https://code.claude.com/docs/en/sandboxing)

**Channels**
- "A channel is an MCP server that pushes events into your running Claude Code session"; research preview; requires Anthropic auth (not Bedrock/Vertex/Foundry); plugins (Bun) for Telegram, Discord, iMessage, fakechat demo; `claude --channels plugin:telegram@claude-plugins-official`; token at `~/.claude/channels/telegram/.env` or `TELEGRAM_BOT_TOKEN`; pairing `/telegram:access pair <code>`, `policy allowlist`; model receives `<channel source="plugin:...">` events; permission-relay capability lets allowlisted senders approve tool use; enterprise `channelsEnabled`, `allowedChannelPlugins`; `--dangerously-load-development-channels` for custom channels. — [Channels](https://code.claude.com/docs/en/channels)

### Inferences
- Hooks (`http` type) are the cleanest way for a platform to get authoritative lifecycle signals from locally-run Claude Code sessions without parsing screens: `PreToolUse`/`PermissionRequest` → "blocked", `Stop` → "done/idle", `Notification` (matcher `permission_prompt`) → "needs you".
- The hook input `transcript_path` + `SessionEnd` gives a free archival path.

### Gaps
- Channels reference (building your own channel, `notifications/claude/channel`) not fetched.

---

## KQ7. Claude Code / Agent SDK — task/status: todo/Task tools, stream-json events, subagents, background tasks, cost

### Takeaway
For UIs, `claude -p --output-format stream-json` (with `--verbose --include-partial-messages`) emits typed NDJSON: `system` (`init`, `api_retry`, `plugin_install`, `permission_denied`, `mirror_error`, hook events), `assistant`/`user` (with `parent_tool_use_id` for subagent attribution), `stream_event` deltas, `tool_progress`, task messages, `compact_boundary`, and a final `result` carrying `total_cost_usd`, `usage`, `modelUsage`, `num_turns`, `duration_ms`, `permission_denials`, `session_id`.

### Cited Findings
- Output formats: `text`, `json`, `stream-json`; streaming example `claude -p "Explain recursion" --output-format stream-json --verbose --include-partial-messages`; last line is `result`; `--json-schema` → `structured_output`; `system/init` carries model, tools, `mcp_servers[]`, `mcp_server_errors[]`, `plugins[]`, `plugin_errors[]`, `capabilities[]` (e.g. `interrupt_receipt_v1`); `system/api_retry` fields `attempt`, `max_retries`, `retry_delay_ms`, `error_status`, `error` category; `system/plugin_install` statuses. Subagent messages appear as `assistant`/`user` with `parent_tool_use_id` (null for main; `forked-command-` prefix for prompt-started forked skills); `--forward-subagent-text` forwards their text/thinking. SIGTERM → exit 143, runs `SessionEnd`; `-p` waits for background work up to 10 min (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`). Piped stdin capped at 10 MB. — [Run Claude Code programmatically](https://code.claude.com/docs/en/headless)
- SDK message types: `SDKSystemMessage`, `SDKUserMessage`, `SDKAssistantMessage`, `SDKResultMessage` (`stop_reason`, `total_cost_usd`, `usage{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}`, `modelUsage[]`, `num_turns`, `duration_ms`, `duration_api_ms`, `permission_denials[]`, `session_id`, `structured_output`, `context_usage`), `SDKPartialAssistantMessage`, `SDKStreamEventMessage`, `SDKToolUseMessage`, `SDKToolProgressMessage`, `SDKToolUseSummaryMessage`, `SDKHookStarted/Progress/ResponseMessage`, `SDKTaskStartedMessage` (id, toolUseId, description), `SDKTaskProgressMessage`, `SDKTaskNotificationMessage`, `SDKCommandsChangedMessage`, `SDKCompactBoundaryMessage`, `SDKPermissionDeniedMessage`, `SDKMirrorErrorMessage`, `SDKInformationalMessage`. Options include `permissionMode`, `canUseTool`, `allowedTools`, `hooks`, `includeHookEvents`, `settingSources`, `agents`, `mcpServers`, `strictMcpConfig`, `systemPrompt {type:"preset", preset:"claude_code", append}`, `maxTurns`, `maxBudgetUsd`, `enableFileCheckpointing`, `sandbox`, `env`, `cwd`, `model`, `fallbackModel`, `effort`, `outputFormat`. Built-in task tools: `TaskCreate`, `TaskUpdate`, `TaskList`, `TodoWrite`, plus `Agent`, `Skill`. — [TypeScript reference](https://code.claude.com/docs/en/agent-sdk/typescript)
- Result subtypes: `success`, `error_max_turns`, `error_max_budget_usd` (single-shot `query()` raises after yielding an error result). — [Sessions (SDK)](https://code.claude.com/docs/en/agent-sdk/sessions); [Hosting](https://code.claude.com/docs/en/agent-sdk/hosting)
- Cost: `--output-format json` includes `total_cost_usd` and per-model breakdown; resumed runs report the conversation's whole total; figures are "client-side estimates". — [Headless](https://code.claude.com/docs/en/headless)
- Subagents: definitions in `.claude/agents/`, `~/.claude/agents/`, plugins, `--agents`; frontmatter `tools`, `disallowedTools`, `model`, `permissionMode`, `maxTurns`, `skills`, `memory`, `effort`, `isolation: worktree`, `background`, `hooks`, `mcpServers`; background subagents default in interactive sessions (`CLAUDE_CODE_FORK_SUBAGENT=0`, Ctrl+B to background); nesting up to 3 (`CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH`); resume via `SendMessage`; named subagents become "teammates" with agent teams. Hook events `TaskCreated`/`TaskCompleted`, `TeammateIdle`, `SubagentStart/Stop`. — [Subagents](https://code.claude.com/docs/en/sub-agents); [Hooks](https://code.claude.com/docs/en/hooks)
- Telemetry for hosted agents: `CLAUDE_CODE_ENABLE_TELEMETRY=1`, OTEL exporters; prompt text not exported by default. — [Hosting](https://code.claude.com/docs/en/agent-sdk/hosting)

### Inferences
- A herdr-style status can be computed from the stream alone: `system/init` → working; `tool_use` block with `AskUserQuestion` or a `canUseTool` callback pending → blocked ("needs you"); `result` → done; `permission_denied`/`error_max_*` → attention. `parent_tool_use_id` lets a UI render nested subagent lanes.
- Cost per session is available from `result` without any extra accounting; `maxBudgetUsd` is the hard stop.

### Gaps
- Exact JSON shape of `TaskCreate`/`TaskList` tool results and the `task_*` stream subtypes was summarized by the fetcher, not quoted verbatim; verify in the TS reference before relying on field names.

---

## KQ8. Codex CLI — providers/auth, config.toml

### Takeaway
Codex (Rust, Apache-2.0) authenticates via ChatGPT sign-in (plan-included usage) or API key (`preferred_auth_method = "apikey"`, `auth.json` under `$CODEX_HOME`), and is configured by `~/.codex/config.toml` with keys for model/provider, `approval_policy`, `sandbox_mode`, `mcp_servers`, `features`, `hooks`, `notify`, `memories`, `history.persistence`, `project_doc_max_bytes`, `shell_environment_policy`.

### Cited Findings
- Repo: "Lightweight coding agent that runs in your terminal"; Rust (`codex-rs`); Apache-2.0; auth: "ChatGPT sign-in (recommended for Plus, Pro, Business, Edu, Enterprise plans)" or API key. — [github.com/openai/codex](https://github.com/openai/codex)
- Auth mechanics: `preferred_auth_method = "chatgpt"` (default) prefers ChatGPT auth if present, else API key; set `"apikey"` to force; override `codex --config preferred_auth_method="apikey"`; credentials in `$CODEX_HOME/auth.json` (`~/.codex` on Mac/Linux, `%USERPROFILE%\.codex` on Windows); ChatGPT login "draws on plan-included limits rather than racking up per-token API charges". — [inventivehq](https://inventivehq.com/blog/codex-cli-chatgpt-login-vs-api-key); [codex docs/authentication.md mirror](https://forge.lthn.ai/core/core-agent-ide/src/commit/3baccba0ac2026d3be9de9378913ad6774facb03/docs/authentication.md)
- Config reference (official, now at learn.chatgpt.com): `model` (e.g. `gpt-6.1-sol`), `model_provider` (default `openai`), `model_providers.<id>` (`base_url`, auth, headers), `model_reasoning_effort` (`low|medium|high|xhigh|max|ultra`), `approval_policy` (`on-request`, `never`, or granular object with `sandbox_approval`, `rules`, `mcp_elicitations`, `request_permissions`, `skill_approval`), `approvals_reviewer` (`user`|`auto_review`), `sandbox_mode` (`read-only|workspace-write|danger-full-access`), `[sandbox_workspace_write]` (`exclude_slash_tmp`, `exclude_tmpdir_env_var`, `network_access`, `writable_roots`), `mcp_servers.<id>` (`command`, `args`, `cwd`, `enabled`, `startup_timeout_sec`, `tool_timeout_sec`, `default_tools_approval_mode`, per-tool `approval_mode`), `permissions.<name>` profiles + `default_permissions` (`:read-only`, `:workspace`, `:danger-full-access`), `[features]` (`multi_agent`, `network_proxy`, `unified_exec`, `web_search`, `memories`, `hooks`, `shell_snapshot`), `hooks` (`PreToolUse`, `PostToolUse`, `SessionStart`, `SessionEnd`), `notify` (array command receiving JSON payload), `history.persistence` (`save-all`|`none`), `project_doc_max_bytes`, `shell_environment_policy` (`inherit all|core|none`, `filters`, `set`), `[memories]`, `tui.vim_mode_default`, `tui.notifications`, `tui.theme`. — [Codex config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- Note: `developers.openai.com/codex/*` now 308-redirects to `learn.chatgpt.com/docs/*` (observed 2026-10-31).

### Inferences
- `notify` (JSON payload to an external command) and `[features] hooks` are the equivalents of Claude's hooks for pushing status to a platform; herdr uses exactly this (`hooks.json` + `features.hooks = true`).
- `model_providers.<id>.base_url` is Codex's analogue of `ANTHROPIC_BASE_URL` (OpenAI-compatible endpoints, e.g. local models).

### Gaps
- Official authentication page at learn.chatgpt.com returned 404 at the guessed path; auth details come from a docs mirror and a secondary blog.
- ChatGPT-plan rate-limit windows and the exact "Codex usage" quotas were not verified.

---

## KQ9. Codex CLI — session/rollout storage, resume/fork/archive, memory (AGENTS.md + built-in memories)

### Takeaway
Every session is a JSONL "rollout" at `~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl` (cold files zstd-compressed) indexed by a SQLite state DB; `codex resume [--last|--all|<id>]`, `codex fork`, `codex archive` (v0.136+). Project guidance is `AGENTS.md` (capped by `project_doc_max_bytes`); a built-in **memories** preview (2026-04-16) consolidates sessions into `~/.codex/memories/MEMORY.md` that is injected into the system prompt, with AGENTS.md winning on conflict.

### Cited Findings
- Rollouts: path `~/.codex/sessions/YYYY/MM/DD/rollout-{timestamp}-{id}.jsonl`, optionally `.jsonl.zst`; item types `SessionMeta` (session id, cwd, provider, CLI version), `TurnContext` (per-turn model, approval policy, sandbox), `ResponseItem`, `EventMsg` (UserMessage, AgentMessage, TokenCount, lifecycle), `Compacted`; persistence modes Limited (default) vs Extended; exec output truncated to 10,000 bytes; "JSONL Rollout Files — source of truth… SQLite State Database — indexes metadata (git_sha, archived_at, model name); powers session picker". — [Daniel Vaughan, session lifecycle (2026-06-08)](https://codex.danielvaughan.com/2026/06/08/codex-cli-session-lifecycle-archive-resume-fork-rollout-persistence-management/)
- Commands: `codex resume` (picker), `codex resume --last`, `codex resume --last --all`, `codex resume <UUID>`, `codex exec resume --last "prompt"`; `codex fork [--last|<UUID>]` (new ThreadId, `forked_from_id`, copies history); `codex archive <id>` / `codex unarchive` / `/archive` (v0.136, June 2026); v0.137 "cross-surface session sync via App Server v2 RPCs"; background zstd compression, rematerialized on resume. — [Daniel Vaughan (2026-06-08)](https://codex.danielvaughan.com/2026/06/08/codex-cli-session-lifecycle-archive-resume-fork-rollout-persistence-management/)
- Memories: shipped 2026-04-16 ("Codex for (almost) everything"); config `[features] memories = true`, `[memories] generate_memories`, `use_memories`, `extract_model`, `consolidation_model` (plus `max_raw_memories_for_consolidation`, `max_unused_days` per config reference); storage `~/.codex/memories/` (`MEMORY.md`, `memory_summary.md`, `raw_memories.md`, `rollout_summaries/`, `skills/`, `memories_extensions/`); two-phase pipeline (per-rollout extraction with secret redaction → serial global consolidation by an internal sub-agent with "no approvals, no network, and local write access only"); runs at session startup for non-ephemeral, non-sub-agent threads; "`MEMORY.md` … contents are included in the system prompt of future sessions when `use_memories` is enabled"; `/memories` TUI command; unavailable in EEA/UK/CH at launch. — [Daniel Vaughan memory deep dive (2026-04-18, updated 2026-10-28)](https://codex.danielvaughan.com/2026/04/18/codex-built-in-memory-system-deep-dive/)
- Precedence: "global AGENTS.md → directory-chain AGENTS.md files → injected memories → your prompt, with AGENTS.md directives taking precedence"; official guidance "Keep required team guidance in AGENTS.md or checked-in documentation. Treat memories as a helpful local recall layer." — [Daniel Vaughan](https://codex.danielvaughan.com/2026/04/18/codex-built-in-memory-system-deep-dive/)
- herdr resumes Codex via `codex resume <id>` using the session id reported by the hook integration. — [herdr Session state](https://herdr.dev/tui/docs/session-state/)

### Inferences
- Codex's rollout schema (`SessionMeta`/`TurnContext`/`ResponseItem`/`EventMsg`/`Compacted`) is more explicitly typed than Claude's "internal" JSONL and is also exposed via app-server `thread/items/list`, so a platform can reconstruct Codex history without file parsing.
- Both vendors converged on the same memory design: a consolidated `MEMORY.md` injected into the system prompt, team instructions file wins on conflict.

### Gaps
- Most Codex session/memory specifics come from one secondary author (danielvaughan.com); the official pages were not reachable at guessed URLs. Version numbers (v0.129/0.136/0.137) are as reported there.
- Exact SQLite file name/location and the state DB schema were not confirmed.

---

## KQ10. Codex CLI — approval modes, sandbox (Seatbelt/Landlock/bwrap), MCP

### Takeaway
Codex separates *what the agent may touch* (`sandbox_mode`: read-only / workspace-write / danger-full-access, enforced by Seatbelt on macOS, bubblewrap+seccomp on Linux, MXC or legacy modes on Windows) from *when it asks* (`approval_policy`: on-request / never / granular; `untrusted` deprecated), with presets Auto / Read Only / Full Access and an optional `auto_review` reviewer agent; MCP servers are first-class in `config.toml`.

### Cited Findings
- Presets: Auto = `workspace-write` + `on-request`; Read Only = `read-only` + `on-request`; Full Access = `danger-full-access` "[Elevated Risk] No sandbox or approvals". `on-failure` also documented; "The deprecated `untrusted` policy has been retired; users should migrate to `on-request` or define project-level `trust_level = "untrusted"`". — [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security); [Security](https://learn.chatgpt.com/docs/security)
- Platform mechanisms: macOS "Seatbelt policies executed via sandbox-exec"; Linux `bwrap` + `seccomp`; Windows "MXC (preferred) or legacy elevated/unelevated modes; WSL2 inherits Linux"; Codex Cloud: isolated containers, agent phase offline by default. (The older `security` page summary still cites "Landlock + seccomp" for Linux — treat Linux mechanism as bwrap/Landlock-family, version-dependent.) — [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security); [Security](https://learn.chatgpt.com/docs/security)
- Network: off by default; `[sandbox_workspace_write] network_access = true`; `[features.network_proxy] enabled = true, domains = {...}` with allow/deny, wildcards, `allow_local_binding`. Protected paths in workspace-write: `.git`, `.agents`, `.codex`, `.aws` (recursive read-only). `approvals_reviewer = "auto_review"` routes escalations through a reviewer agent (low/medium proceed, critical denied, high needs authorization). — [Agent approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security)
- MCP: `mcp_servers.<id>` table with `command/args/cwd/enabled/startup_timeout_sec/tool_timeout_sec` and per-tool `approval_mode`; granular `approval_policy.mcp_elicitations`. — [Config reference](https://learn.chatgpt.com/docs/config-file/config-reference)
- App-server turn overrides: `sandboxPolicy` values `readOnly`, `workspaceWrite`, `dangerFullAccess`, `externalSandbox`; `approvalPolicy` per turn. — [App server](https://learn.chatgpt.com/docs/app-server)

### Inferences
- Codex's "externalSandbox" sandbox policy is designed for platforms that already isolate the process (containers) — equivalent to Claude's `bypassPermissions` inside a container, but keeps approvals configurable.
- The reviewer-agent (`auto_review`) is the analogue of Claude's auto-mode classifier.

### Gaps
- Windows "MXC" is named in the doc summary but not expanded; no primary source fetched explaining it.

---

## KQ11. Codex CLI — app-server protocol for UIs, event model, TUI progress display, cloud tasks

### Takeaway
`codex app-server` is a bidirectional JSON-RPC 2.0 (header omitted) server over stdio/WebSocket/Unix socket with a Thread → Turn → Item model, `thread/*`, `turn/*` methods, `item/*` notifications and server→client approval requests; the TUI groups consecutive read/list/search commands into one "exploring" cell; cloud tasks run via `codex cloud exec/list`.

### Cited Findings
- App server: "the interface Codex uses to power rich clients (for example, the Codex VS Code extension)… JSON-RPC 2.0 messages (with the `"jsonrpc":"2.0"` header omitted on the wire)"; transports stdio (`--listen stdio://`, JSONL) and WebSocket (`--listen ws://IP:PORT`, experimental); overload error `-32001` "Server overloaded; retry later"; schemas via `codex app-server generate-ts --out ./schemas` / `generate-json-schema`; source in `codex-rs/app-server`. — [App server (developers.openai.com → learn.chatgpt.com)](https://developers.openai.com/codex/app-server)
- Handshake: `initialize` with `clientInfo{name,title,version}` then `initialized` notification; capabilities `experimentalApi`, `optOutNotificationMethods`, `requestAttestation`; auth enforced before initialize; Unix socket `--listen unix://`; remote CLI `codex --remote ws://127.0.0.1:4500`. — [App server](https://learn.chatgpt.com/docs/app-server)
- Methods: `thread/start`, `thread/resume`, `thread/fork`, `thread/list`, `thread/read`, `thread/archive|unarchive|delete`, `thread/turns/list`, `thread/items/list`, `thread/compact/start`, `thread/revert`, `thread/goal/set|get|clear`; `turn/start` (per-turn `model`, `effort`, `cwd`, `sandboxPolicy`, `approvalPolicy`, `summary`, `outputSchema`, `collaborationMode`), `turn/steer`, `turn/interrupt`; `command/exec`, `process/spawn` (experimental), `review/start`. — [App server](https://learn.chatgpt.com/docs/app-server)
- Notifications: `thread/started`, `thread/closed`, `thread/status/changed`, `thread/archived`, `thread/reverted`; `turn/started`, `turn/completed`; `item/started`, `item/completed`, `item/agentMessage/delta`; server→client requests `item/commandExecution/requestApproval`, `item/fileChange/requestApproval`, `item/permissions/requestApproval`. Item types: user input, agent messages, command execution, file changes, reasoning, MCP tool calls, web search, function call outputs. Thread status: `notLoaded`, `idle`, `systemError`, `active` (with `activeFlags`). — [App server](https://learn.chatgpt.com/docs/app-server)
- TUI grouping: in the Codex TUI exec-cell model, "An `ExecCell` can represent either a single command or an 'exploring' group of related read/list/search commands"; grouping condition: source not `UserShell`, parsed commands non-empty, all `ParsedCommand::{Read, ListFiles, Search}`; the TUI "retain[s] the last exec history cell so that it can be updated by the next tool call, enabling the ability to update or merge successive tool calls." — [lemurclaw-tui exec_cell/model.rs (codex-rs TUI derivative)](https://docs.rs/crate/lemurclaw-tui/latest/source/src/tui_internal/exec_cell/model.rs); [WebSearch on codex-rs tui](https://forge.lthn.ai/core/core-agent-ide/src/commit/0cf57e1f42b54fb1ed9e6af6fdfd18da106e4298/codex-rs/tui)
- Cloud: "Codex Cloud runs agent tasks inside isolated containers on OpenAI's infrastructure"; `codex cloud exec --env <ENV_ID> [--attempts 3]`, `codex cloud list`, interactive picker; "Delegate Refactor to Cloud" workflow; Slack/Linear triggers. — [Daniel Vaughan cloud delegation (2026-05-19)](https://codex.danielvaughan.com/2026/05/19/codex-cli-cloud-delegation-workflows-plan-locally-execute-remotely-apply-diffs/); [Codex cloud tasks docs](https://developers.openai.com/codex/ide/cloud-tasks)
- TUI commands: `/status` (model, token usage, config), `/diff` (git diff incl. untracked), `/compact`, `/review`. — [Daniel Vaughan TUI mastery (2026-04-20)](https://codex.danielvaughan.com/2026/04/20/codex-cli-tui-mastery-slash-commands-keyboard-shortcuts-session-workflows/)

### Inferences
- For a platform UI, Codex app-server is the more "UI-native" surface of the three: typed items with `requestApproval` requests map directly to a "Needs you" badge and `thread/status/changed` to the sidebar dot; `thread/list` + `thread/items/list` replace file parsing. Claude's equivalent is stream-json + `canUseTool`/hooks; herdr has neither and infers from screens.
- The "Explored / Edited" grouping the brief mentions corresponds to the exploring-cell merge of `Read|ListFiles|Search` commands (label text not verified).

### Gaps
- I could not confirm the literal on-screen labels "Explored"/"Edited"/"Ran" from a primary source; the mechanism is confirmed, the strings are not.
- Official cloud-tasks page redirected and was not fetched; command flags come from a secondary source.
- Full app-server v2 item schemas and `thread/status/changed` payload fields were not captured verbatim.

---

## KQ12. Cross-cutting lessons worth borrowing and pitfalls (for a herdr-layout team platform)

### Takeaway
Borrow herdr's UI contract (five states, done-vs-idle seen semantics, pane→tab→space rollup, distinct done/request sounds, wait-until-state API, worktree-per-space) but obtain state from the agents' own protocols (Claude hooks/stream-json, Codex app-server) rather than screen manifests; keep per-session config dirs and external session mirrors for durability; budget via `maxBudgetUsd` / plan credits.

### Cited Findings (lessons, each anchored above)
- herdr: strict `blocked` only on visible approval UI avoids false "needs you" — [herdr Agents](https://herdr.dev/tui/docs/agents/); `done` persists "until viewed" — [herdr Agents](https://herdr.dev/tui/docs/agents/); `herdr agent wait --until done` + `events.subscribe` as orchestration primitives — [Socket API](https://herdr.dev/tui/docs/socket-api/); native resume via stored `resume_argv` — [Socket API](https://herdr.dev/tui/docs/socket-api/); handoff of PTYs on upgrade — [Session state](https://herdr.dev/tui/docs/session-state/).
- herdr pitfalls: detection breaks when agent TUIs change, no file isolation between agents, restart loses processes, WSL/Windows glitches, cognitive overload past 2–3 agents — [Flavio Copes](https://flaviocopes.com/herdr/); [Preuve](https://preuve.ai/blog/herdr-review); [Bitdoze](https://www.bitdoze.com/herdr-agent-multiplexer/).
- Claude: `parent_tool_use_id` for subagent lanes; `permission_denials` and `total_cost_usd` in `result`; `--permission-prompts none` for unattended; `http` hooks for lifecycle — [Headless](https://code.claude.com/docs/en/headless); [Hooks](https://code.claude.com/docs/en/hooks). Pitfalls: JSONL format unstable; `-p` sessions hidden from picker; same session in two terminals interleaves; Python store-resume loses `apiKeyHelper`; TS `env` replaces env; Agent SDK credit caps on subscriptions — [Sessions](https://code.claude.com/docs/en/sessions); [Session storage](https://code.claude.com/docs/en/agent-sdk/session-storage); [VentureBeat](https://venturebeat.com/technology/anthropic-reinstates-openclaw-and-third-party-agent-usage-on-claude-subscriptions-with-a-catch).
- Codex: approval requests as explicit JSON-RPC server→client requests; `turn/steer` mid-turn; `thread/goal/*`; `externalSandbox` for container hosts; reviewer-agent approvals — [App server](https://learn.chatgpt.com/docs/app-server); [Approvals & security](https://learn.chatgpt.com/docs/agent-approvals-security). Pitfalls: WebSocket transport "experimental and unsupported"; memories region-locked and async; docs URLs migrating (developers.openai.com → learn.chatgpt.com) — [App server](https://developers.openai.com/codex/app-server); [Memory deep dive](https://codex.danielvaughan.com/2026/04/18/codex-built-in-memory-system-deep-dive/).

### Inferences
- Status vocabulary to standardize: `working | blocked(needs-you) | done(unseen) | idle(seen) | unknown`, with sources ranked: native protocol event > hook > screen heuristic.
- A "Files/Changes" panel can be fed from Codex `fileChange` items and Claude `Edit/Write` tool_use blocks (+ `enableFileCheckpointing` for revert) rather than polling git.

### Gaps
- No head-to-head benchmark or published latency/throughput data for any of the three was found.
