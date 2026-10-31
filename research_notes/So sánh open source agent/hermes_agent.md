# Hermes Agent (Nous Research) — Technical Profile (as of 2026-10-31)

Scope note: Hermes Agent is an open-source (MIT) Python agent framework by Nous Research. Facts below come from the official docs (hermes-agent.nousresearch.com/docs), the GitHub repo/API, and a few third-party deep-dives (marked). Repo snapshot 2026-10-31: 259,060 stars, 56,958 forks, 52,201 open issues, created 2025-07-22, default branch `main`, latest release v0.22.5 (2026-10-23). The repo's star count is extraordinary; it is confirmed by the GitHub API, not just aggregators.

Key files/dirs to anchor on (from the official Architecture page): `run_agent.py` (`AIAgent`), `agent/conversation_loop.py`, `agent/turn_*.py`, `agent/prompt_builder.py`, `agent/context_compressor.py`, `agent/prompt_caching.py`, `model_tools.py` (tool dispatch), `tools/registry.py`, `tools/terminal_tool.py`, `tools/delegate_tool.py`, `hermes_state.py` (SQLite SessionDB), `gateway/run.py`, `gateway/session.py`, `gateway/delivery.py`, `gateway/pairing.py`, `gateway/hooks.py`, `plugins/platforms/*`, `cron/scheduler.py`, `hermes_cli/runtime_provider.py`, `hermes_cli/config.py`, `hermes_cli/models.py`, `hermes_constants.py`. User data lives under `~/.hermes/` (`config.yaml`, `.env`, `auth.json`, `state.db`, `memories/`, `skills/`, `cron/`, `logs/`, `plugins/`).

---

## 1. LLM connection: providers, routing/fallback, auth, subscription CLIs as backends, cost tracking

### Takeaway
Hermes is provider-agnostic with a shared resolver (`hermes_cli/runtime_provider.py`) mapping `(provider, model)` to `(api_mode, api_key, base_url)` across three wire protocols (`chat_completions`, `anthropic_messages`, `codex_responses`) and 40+ providers; subscription-based CLIs are supported first-class for OpenAI Codex (ChatGPT OAuth) and via reuse of Claude Code credentials for Anthropic, with an ordered `fallback_providers` chain; cost is shown per session (`/usage`, status bar) and in the dashboard's Analytics, but there is no documented pricing-table internals.

### Cited Findings
- Three API modes in the agent loop: `chat_completions`, `codex_responses`, `anthropic_messages`; the loop (`agent/conversation_loop.py`, `agent/turn_*.py`) handles "Provider selection and credential resolution", prompt construction via `agent/prompt_builder.py`, tool dispatch through `model_tools.py` — [Architecture docs](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- "Shared runtime resolver in `hermes_cli/runtime_provider.py` maps `(provider, model)` to `(api_mode, api_key, base_url)`. Handles 18+ providers, OAuth flows, credential pools, and alias resolution. Credential files managed by `tools/credential_files.py`." Model catalog in `hermes_cli/models.py`. — [Architecture docs](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- Provider runtime: Anthropic `api_mode = anthropic_messages`; OpenAI Codex `api_mode = codex_responses`; all others `chat_completions`. Providers listed include AI Gateway, OpenRouter, Nous Portal, Google/Gemini, Alibaba/DashScope, DeepSeek, AWS Bedrock, Azure Foundry, NVIDIA NIM, xAI (Grok), Ollama Cloud, LM Studio, "approximately 20+ additional". Auth resolution precedence: "explicit CLI/runtime request" → "config.yaml model/provider config" → "environment variables" → "provider-specific defaults or auto resolution". `get_provider_profile()` returns "base_url, env_vars priority list, api_mode, and fallback_models". — [Provider runtime docs](https://hermes-agent.nousresearch.com/docs/developer-guide/provider-runtime)
- For Anthropic the system "prefers refreshable Claude Code credentials over copied env tokens when both are present" (i.e., reuses the local Claude Code login). — [Provider runtime docs](https://hermes-agent.nousresearch.com/docs/developer-guide/provider-runtime)
- "OpenAI Codex (`openai-codex`) requires `hermes model` → **ChatGPT or Codex Subscription**" via OAuth. Provider families: OAuth/Portal (Nous Portal, OpenAI Codex, xAI Grok OAuth, Qwen Portal, MiniMax OAuth); API key (OpenRouter, Anthropic, DeepSeek, NVIDIA NIM, Google AI Studio, AWS Bedrock); Local (LM Studio, llama.cpp managed, Ollama Cloud); Enterprise (Azure Foundry, Hugging Face, Arcee AI). — [Fallback providers docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)
- Claude Pro/Max subscription as backend: an official-org plugin `NousResearch/hermes-plugin-claude-subscription-directsdk` "routes requests through the claude CLI and streams JSON responses, driving the unmodified official Claude Code executable as a request-scoped model client for Claude Pro/Max subscriptions"; install via `hermes plugins install NousResearch/hermes-plugin-claude-subscription-directsdk`, then `claude auth login`, then pick "Claude Subscription DirectSDK (Experimental)" in `hermes model`. A feature request (#48320) for a `claude -p` shell-out provider also exists. — [Plugin wiki](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk/wiki); [Issue #48320](https://github.com/NousResearch/hermes-agent/issues/48320)
- Fallback config (current): `fallback_providers:` list of `{provider, model}` tried in order; legacy `fallback_model: openrouter/anthropic/claude-sonnet-4` still honored; optional `fallback: min_switch_reset_seconds: 120`; CLI `hermes fallback add|list|remove|clear`. Triggers: 429 and 500/502/503 "after exhausting retry attempts"; 401/403 and 404 "immediately"; repeated malformed/empty responses. — [Fallback providers docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)
- Fallback cost caveat: "Prompt caches are keyed to the model... When fallback fires, the new provider:model has no cached prefix for your conversation, so the next request re-reads the entire history at full input-token price." Also, "Fallback support excludes subagent delegation and auxiliary tasks from inheriting parent fallback chains." — [Fallback docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers); [Provider runtime](https://hermes-agent.nousresearch.com/docs/developer-guide/provider-runtime)
- Nous Portal: "unified subscription gateway and the recommended way to run Hermes Agent"; browser OAuth; "refresh token is stored at `~/.hermes/auth.json`"; Hermes "automatically mints short-lived JWTs from this token per request"; "300+ frontier models" (Claude Opus 4.7/Sonnet 4.6, GPT-5.5/5.4, Gemini 3, DeepSeek V4, Qwen, Hermes-4). — [Nous Portal docs](https://hermes-agent.nousresearch.com/docs/integrations/nous-portal)
- Secrets policy: "Secrets (API keys, bot tokens, passwords) go in `.env`. Everything else goes in `config.yaml`." `config.yaml` supports `${VAR_NAME}` substitution. Model key example `model: anthropic/claude-opus-4`; `providers` section supports `request_timeout_seconds` and per-model `timeout_seconds`. — [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
- Auxiliary model routing: `auxiliary.compression.model: ""` (empty = main model) and `auxiliary.compression.provider: "auto"`; cron jobs have their own `cron.model` / `cron.model_provider`; subagents have `delegation.model` / `delegation.provider`. — [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration); [Cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron); [Delegation docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- Cost visibility: `/usage` = "Detailed breakdown including per-category costs"; `/context [all]` visual context usage; status bar shows "Estimated session cost (or `n/a` for unknown/zero-priced models)". Dashboard has an "Analytics" page with "Token usage and cost metrics over 7, 30, or 90-day periods". — [CLI docs](https://hermes-agent.nousresearch.com/docs/user-guide/cli); [Web dashboard docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard)
- v0.22.0 (2026-10-13, "Threshold Release"): onboarding chat on "a free trial model, requiring no API key initially"; one-click local llama.cpp; signed desktop packages; 600+ plugin catalog. — [GitHub releases](https://github.com/NousResearch/hermes-agent/releases)
- Prompt caching: `agent/prompt_caching.py` applies "Anthropic cache breakpoints for prefix caching"; design principle "System prompt unchanged mid-conversation; no cache-breaking mutations except `/model`". — [Architecture docs](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)

### Inferences
- Routing is static (primary + ordered fallback by error class), not cost/capability-based routing; per-role model choice is achieved by separate config sections (main / auxiliary / cron / delegation) rather than a policy engine.
- Subscription CLIs are supported in two styles: credential reuse (Claude Code OAuth tokens → native Anthropic API mode; Codex OAuth → `codex_responses`) and process-wrapping (DirectSDK plugin drives the `claude` binary). The former keeps the agent loop native; the latter is "experimental".

### Gaps
- No documentation found of a per-model pricing table file or how `/usage` computes cost (likely from provider usage metadata; unverified).
- Exact behaviour of "credential pools" (multi-key rotation) not documented in fetched pages.

---

## 2. State management: session model, persistence, resume, compaction, concurrency

### Takeaway
All CLI and gateway sessions are rows in one SQLite DB (`~/.hermes/state.db`, FTS5 indexed) with lineage tracking across compressions; resume restores full history; compaction is threshold-based summarization of middle turns (default at 50% of context) preceded by a memory flush; the gateway keys sessions per chat/thread and processes different chats concurrently but the same chat sequentially.

### Cited Findings
- "Database: SQLite with FTS5 full-text search via `hermes_state.py` and `gateway/session.py`. Sessions track lineage (parent/child across compressions), support per-platform isolation, and use 'atomic writes with contention handling.'" CLI flow ends with "save to `SessionDB`". — [Architecture docs](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- "All CLI and messaging sessions are stored in SQLite (`~/.hermes/state.db`) with FTS5 full-text search." — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- Resume: `hermes --continue` / `hermes -c` / `hermes -c "my project"` / `hermes --resume 20260225_143052_a1b2c3` / `hermes --resume latest`; "Resuming restores the full conversation history from SQLite. The agent sees all previous messages, tool calls, and responses — just as if you never left." `hermes sessions list`, `hermes sessions rename <id> <title>`, `/title`. — [CLI docs](https://hermes-agent.nousresearch.com/docs/user-guide/cli)
- Compression config: `compression.enabled: true`, `threshold: 0.50` ("Compress at 50% of context limit"), `target_ratio: 0.20`, `tail_mode: lean`, `protect_last_n: 20`, `in_place: true` ("Maintain session ID during compaction"). `agent/context_compressor.py` "summarizes middle turns when thresholds exceed". Manual: `/compress [here [N] | <focus>] [--preview]`. — [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration); [Architecture docs](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture); [CLI docs](https://hermes-agent.nousresearch.com/docs/user-guide/cli)
- Memory flush before compression: "a separate model call where only the `memory` tool is available, giving the agent a chance to save important facts before compression collapses the history." (third-party, Mar 31 2026; consistent with Turing Post description of a "memory flush" step) — [Vectorize article](https://vectorize.io/articles/hermes-agent-memory-explained); [Turing Post](https://www.turingpost.com/p/hermes)
- Gateway session continuity: "Gateway conversations do not reset after inactivity or at a daily boundary." Legacy reset policies deprecated; explicit `/new` or `/reset`. `/model` override per session "survives gateway restarts". `sessions.retention_days: 90`. — [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/); [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
- Telegram session keys: "Each Telegram chat (DM or group) maintains its own conversation history and session key. Group forum topics get isolated sessions automatically." DM topics map to `agent:main:telegram:dm:{chat_id}:{thread_id}`. `/topic` creates "unlimited user-driven topics, each an independent session", restorable via `/topic <session-id>`. — [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- Concurrency: Telegram `platforms.telegram.extra.max_concurrent_updates: 32` ("Cross-chat parallelism, same-chat sequential"; `1` = sequential); dedup cache of last 4096 `update_id`s. `display.busy_input_mode: interrupt | queue | steer` governs messages arriving while the agent is busy. — [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram); [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
- Durable delivery: "Final responses use a durable delivery ledger in `state.db`... If the gateway crashes or restarts between producing a response and the platform confirming receipt, the next boot redelivers the stored response." At-least-once, flood-control retries, 24-hour freshness. — [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
- Profiles: "Each profile (`hermes -p <name>`) has separate HERMES_HOME, config, memory, sessions, and gateway PID" (v0.6.0, 2026-03-30 introduced Profiles). — [Architecture docs](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture); [heyuan110 guide](https://www.heyuan110.com/posts/ai/2026-04-14-hermes-agent-guide)
- Context-window errors message: "The conversation has grown too long for the model's context window, or Hermes detected the wrong context length for your model." Users can set explicit context lengths in config. — [FAQ](https://hermes-agent.nousresearch.com/docs/reference/faq)
- v0.15.0 (2026-05-28): core agent module reduced "16,083→3,821 lines"; `session_search` speed "4,500×" faster (third-party summary). — [Turing Post](https://www.turingpost.com/p/hermes)

### Inferences
- `in_place: true` + lineage tracking implies the DB keeps compressed-ancestor rows linked to the live session, so session search can still hit pre-compression messages.
- Single-process gateway with a per-chat sequential lock is the concurrency model; no documented distributed/multi-worker mode (except cron workers under systemd).

### Gaps
- Exact SQLite table names/columns in `state.db` (sessions, messages, FTS virtual table, delivery ledger) were not in fetched docs; would require reading `hermes_state.py`.
- Default `agent.max_turns` value not captured.

---

## 3. Memory: learning loop, tiers, storage, write policy, retrieval, cache-awareness, user verification, file formats

### Takeaway
Memory is deliberately small and curated: two markdown files (`MEMORY.md` ≤2,200 chars, `USER.md` ≤1,375 chars) injected as a frozen snapshot in the system prompt (cache-friendly), an unbounded FTS5 session archive searched on demand, optional external providers (Honcho etc.) via plugins, plus skills as procedural memory; writes are agent-judged, prompted by periodic nudges and a pre-compression flush, and can be gated behind human approval (`memory.write_approval`).

### Cited Findings
- Files: `MEMORY.md` "Agent's personal notes — environment facts, conventions, things learned" (2,200 chars ≈800 tokens); `USER.md` "User profile — your preferences, communication style, expectations" (1,375 chars ≈500 tokens); both "stored in `~/.hermes/memories/`" and "injected into the system prompt as a frozen snapshot at session start." — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- Memory tool actions: `add`, `patch` ("Replace one span inside an existing entry and keep the rest"), `remove`; `target` = `"memory"` or `"user"`; "There is no `read` action — memory content is automatically injected into the system prompt at session start." — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- Frozen snapshot / cache: "The system prompt injection is captured once at session start and never changes mid-session." Changes "are persisted to disk immediately but won't appear in the system prompt until the next session starts." This "preserves the LLM's prefix cache for performance." — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- Write policy — save proactively: "user preferences, environment facts, corrections, conventions, completed work, explicit requests"; skip: "trivial info, easily re-discovered facts, raw data dumps, session-specific ephemera, and information already in context files." Over limit: "the tool returns an error and the agent must consolidate or remove entries before retrying." — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory); [Vectorize article](https://vectorize.io/articles/hermes-agent-memory-explained)
- Nudges: config keys `memory.nudge_interval` and `skills.creation_nudge_interval` exist (docs don't state defaults). Third-party: prompt memory "prompted periodically by a configurable nudge_interval and flushed proactively in gateway mode before idle timeout". — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory); [Vectorize](https://vectorize.io/articles/hermes-agent-memory-explained)
- Human verification: `memory.write_approval: true` → `/memory pending`, `/memory approve <id>` / `approve all`, `/memory reject <id>`; "every save — especially the unprompted background ones — waits for your yes/no before it ever enters your profile." Also `display.memory_notifications`. — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- Session search: `session_search` tool with "three calling shapes: discovery, scroll, and browse"; results "return actual messages from the DB — no LLM summarization, no truncation"; FTS5 "~20ms latency". (Note: Turing Post states search uses "LLM-based summarization before re-injection" — the official doc contradicts this for the current version; treat Turing Post as describing an older behaviour.) — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory); contradicted by [Turing Post](https://www.turingpost.com/p/hermes)
- Four-layer model (third-party synthesis): (1) core memory ~1.3k tokens, (2) session history SQLite FTS5, (3) optional Honcho user modeling, (4) procedural memory = skills; "keeping the prompt stable for caching" by externalizing everything else into retrieval. — [Turing Post](https://www.turingpost.com/p/hermes)
- External providers via plugins (`plugins/memory/`, single-select, `memory.provider`): "RetainDB and ByteRover — and more, such as Honcho, Hindsight, Supermemory, Mem0, OpenViking and Holographic, are available from the plugin catalog." `plugins/context_engine/` is a separate single-select plugin type. — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory); [Architecture docs](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- Config keys: `memory.memory_enabled`, `memory.user_profile_enabled`, `memory.memory_char_limit: 2200`, `memory.user_char_limit: 1375`, `memory.write_approval: false`, `memory.provider`. — [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration); [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- Persona/identity: `SOUL.md` is "instance-level (global to the agent)". Context files (project instructions) are scanned for prompt injection: "BLOCKED: [filename] contained potential prompt injection." — [Turing Post](https://www.turingpost.com/p/hermes); [Security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- Known limitation: FTS5 "struggles with rephrasing and relational questions"; "Neither prompt memory nor the session archive has any concept that 'Alice' and 'my coworker Alice from engineering' are the same person". — [Vectorize](https://vectorize.io/articles/hermes-agent-memory-explained)
- Claimed benefit (unverified, aggregator): "Nous Research's internal benchmarks show 15-30% task success rate improvement after 5 self-improvement cycles." — [Emelia](https://emelia.io/hub/hermes-agent-self-improving-framework)

### Inferences
- Token growth is bounded by hard char limits on injected memory + progressive-disclosure skills + on-demand session search, rather than by vector RAG; the trade-off is weak semantic recall.
- The "learning loop" is essentially: nudge prompts → agent decides to `memory add/patch` or `skill_manage create/patch` → optional human approval → next session injects updated snapshot.

### Gaps
- Default values of `memory.nudge_interval` / `skills.creation_nudge_interval` and the literal nudge text are not documented in fetched pages.
- Honcho "dialectic" mechanism specifics (API calls, what's stored) not covered in the official memory page fetched.

---

## 4. Skills: format, auto-creation, refinement, hub, selection

### Takeaway
Skills are agentskills.io-standard folders (`SKILL.md` + `references/ scripts/ templates/`) under `~/.hermes/skills/`, loaded by three-level progressive disclosure; the agent itself creates/patches them with `skill_manage` after non-trivial workflows, errors it solved, or user corrections, optionally gated by `skills.write_approval`; a multi-source hub with a security scanner handles sharing.

### Cited Findings
- SKILL.md frontmatter: required `name`, `description`; optional `version`, `platforms: [macos, linux]`, `metadata.hermes.{tags, category, fallback_for_toolsets, requires_toolsets, config[]}`. Folder: `SKILL.md`, `references/`, `templates/`, `scripts/`, `examples/`, `assets/`. — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Locations/precedence: "project → local (~/.hermes/skills/) → skills.create_dir → external_dirs"; project skills in `.hermes/skills/` or `.agents/skills/` need `hermes skills trust`. Config: `skills.external_dirs`, `skills.create_dir`, `skills.write_approval`, `skills.tier1_advisory` (NVIDIA SkillEvaluator), `skills.project_discovery`, `skills.trusted_project_dirs`. — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Progressive disclosure: Level 0 `skills_list()` names/descriptions (~3k tokens); Level 1 `skill_view(name)` full content; Level 2 `skill_view(name, path)` a reference file. "The agent only loads the full skill content when it actually needs it." — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Selection: agent picks from the list; users invoke `/skill-name args`, stack `/a /b task`; "Parsing stops at the first token that isn't an installed skill". Conditional activation: `fallback_for_toolsets`, `requires_toolsets`, `fallback_for_tools`, `requires_tools` (e.g., `duckduckgo-search` hidden when `FIRECRAWL_API_KEY` enables `web_search`). — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Auto-creation triggers: "Figured out non-trivial, repeatable workflow", "Hit errors and found working path", "User corrected its approach". `skill_manage` actions: `create`, `patch` (`old_string`/`new_string`, "preferred"), `patch`+`content`, `delete`, `write_file`, `remove_file`. — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Approval: with `skills.write_approval: true` "every `skill_manage` write... is staged instead of committed"; `/skills pending|diff <id>|approve <id>|reject <id>`. — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- `/learn <path|url|description>` builds skills from reference material; for large sources it "authors an expansive knowledge-base skill: a lean `SKILL.md`... plus an index, with one distilled file per chapter or topic under `references/`". Linter warns at ">60 reference files" or "`SKILL.md` >~24k chars". — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Hub: `hermes skills browse|search|install official/security/1password|check|update|audit|tap add myorg/skills-repo`; sources `official`, `skills-sh`, `well-known` (`/.well-known/skills/index.json`), `url`, `github`, `clawhub`, `lobehub`, `browse-sh`; "All hub-installed skills go through a security scanner that checks for data exfiltration, prompt injection, destructive commands, supply-chain signals". Bundled skills track origin hashes; user-modified ones skipped by sync. — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Skill bundles: `~/.hermes/skill-bundles/<name>.yaml` with `skills:` list and `instruction:`; `hermes bundles create|list|delete`. — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Skills can be bound to chat topics: Telegram `group_topics[].skill` / `dm_topics[].skill`; cron jobs attach skills (`--skill name`). — [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram); [Cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)

### Inferences
- "Self-improvement during use" is concretely `skill_manage patch` on an existing skill when the agent notices a better path; there is no automatic eval/scoring loop in core (the NVIDIA SkillEvaluator is advisory and opt-in).

### Gaps
- No official data on how often auto-created skills are actually produced, or quality metrics.

---

## 5. Task management: cron, background jobs, subagents, todo/plan

### Takeaway
Hermes has a JSON-backed cron scheduler (`~/.hermes/cron/jobs.json`, 60s ticks, fresh isolated `AIAgent` per run, delivery to any platform, incident tracking), a `delegate_task` subagent tool (fresh context, denied tools, depth ≤3, batch parallelism), background terminal processes, and a `todo` planning tool.

### Cited Findings
- Cron storage: `~/.hermes/cron/jobs.json`; outputs `~/.hermes/cron/output/{job_id}/{timestamp}.md`; ledger `~/.hermes/cron/executions.db`. Scheduler: "`cron/scheduler.py` ticks → load jobs from `jobs.json` → create fresh `AIAgent` → inject attached skills → run prompt → deliver to platform." 60-second ticks (Turing Post). — [Cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron); [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture); [Turing Post](https://www.turingpost.com/p/hermes)
- `cronjob_manage` actions `create|list|update/edit|pause|resume|run|remove|cancel`; schedules: `"in 30m"`, `"every 2h"`, `"every monday 9am"`, cron `"0 9 * * 1-5"`, ISO timestamps. Delivery targets `origin, local, telegram, discord, slack, email, sms, matrix, homeassistant, bot-chat, all`. — [Cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- Config: `cron.model`, `cron.model_provider`, `preflight: true`, `script_timeout_seconds: 3600`, `max_parallel_jobs`, `catch_up_missed: true`, `wrap_response: true`, `delivery.notify`, `failure_nudge_threshold: 3`, `failure_repeat_alert_hours: 6`, `allow_agent_scheduling: false` ("Cron-run sessions cannot recursively create more cron jobs" unless enabled), `mirror_delivery`. `no_agent=True` runs a script and delivers stdout; `context_from` chains outputs; `continuity=true` injects previous output; under systemd "each due job handed to external worker process... so restarting gateway mid-job does not kill it". CLI `hermes cron create|list|edit|pause|resume|run|cancel|remove|runs|incidents|doctor|status|tick`. — [Cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
- Subagents: `delegate_task(goal, context, images≤8, output_schema, role="leaf"|"orchestrator", tasks[], group, background)`; child "starts with completely fresh conversation". Config `delegation.max_iterations: 250`, `max_concurrent_children: 10`, `max_spawn_depth: 1` (max supported 3), `orchestrator_enabled: true`, `model`, `provider`, `worktree_isolation: false` ("git worktree per child"), `gateway_mode: auto`. Denied for subagents: `delegate_task` (leaf), `clarify`, `memory`, `send_message`, `cronjob`. Returns `status, summary, exit_reason, schema_valid/schema_errors, worktree, timestamps`. CLI shows "a tree-view... in real-time"; gateway "progress is batched and relayed to the parent's progress callback"; `Ctrl+T`/`F6` roster, `/agents`. — [Delegation docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation); [tools/delegate_tool.py mirror](https://download.hermesedi.duckdns.org/.hermes/hermes-agent/tools/delegate_tool.py)
- Subagent sudo: "subagents cannot open prompts and require `SUDO_PASSWORD` in `.env`." — [Tools docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)
- Background processes: terminal `background=true`, managed via `process` tool (list/poll/wait/log/kill). `todo` tool "for task planning". — [Tools docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)
- v0.20.0 "Herald" (Aug 2026) added "agent-to-agent communication, signed webhooks, and a research mode that ties claims to sources" (third-party). — [heyuan110 guide](https://www.heyuan110.com/posts/ai/2026-04-14-hermes-agent-guide)

### Inferences
- Cron = recurring "tickets" with incident dedup by error signature; delegation = parallel work with structured outputs (`output_schema`) — both are immediately reusable patterns for a BA/Dev/QE platform.

### Gaps
- No native ticket/issue tracker in core; Linear/Notion come via "connectors" (v0.22.0) — details not fetched.

---

## 6. Knowledge base / RAG: ingestion, search, browsing

### Takeaway
Hermes has no vector RAG in core; "knowledge" is handled via `/learn`-generated knowledge-base skills (markdown index + per-topic reference files), FTS5 session search, `web_search`/`web_extract`, a CDP browser toolset, file read/patch, and MCP; external memory/RAG plugins fill the semantic-search gap.

### Cited Findings
- `/learn` supports local code, URLs, "how I just deployed", and PDFs (e.g., `/learn ~/books/designing-data-intensive-applications.pdf`), producing a lean `SKILL.md` + `references/` per topic. — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
- Web: `web_search`, `web_extract` (Firecrawl via `FIRECRAWL_API_KEY`; DuckDuckGo skill as fallback), `x_search`; browser `browser_exec` (Chromium via Python over CDP, screenshots), `browser_cdp`, `browser_dialog`, `browser_vault`; files `read_file`, `patch`; MCP server tools via `tools/mcp_tool.py`; v0.5.0 (2026-03-28) added "MCP server mode". — [Tools docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools); [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture); [heyuan110](https://www.heyuan110.com/posts/ai/2026-04-14-hermes-agent-guide)
- SSRF protection on fetches: blocks RFC1918, loopback, link-local, CGNAT, cloud metadata; "Redirect chains are re-validated at each hop"; `allow_private_urls: true` exempts private ranges but metadata endpoints "stay blocked". — [Security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- Context files (project instruction files) are injected in the prompt's "context files" tier and scanned for injection. — [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture); [Security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security)

### Inferences
- For a team KB (rules, tickets, specs), Hermes' approach would be: curate into skills/context files (deterministic, cache-stable) rather than embed; semantic retrieval requires a memory/context-engine plugin.

### Gaps
- No official doc fetched on document-ingestion tooling beyond `/learn`; context-engine plugin list not captured.

---

## 7. Transparency/status: progress & tool calls on chat platforms, dashboard, logs

### Takeaway
Progress is surfaced by editing a single status message in place (Telegram `editMessageText`/`sendMessageDraft`), configurable verbosity (`display.tool_progress: off|new|all|verbose|log`), reactions, typing indicators, and inline-button prompts; a localhost dashboard (`hermes dashboard`, port 9119) exposes sessions, logs, analytics, cron, pairing, config; logs live under `~/.hermes/logs/`.

### Cited Findings
- Streaming: "responses stream progressively via `editMessageText`"; Bot API 9.5 `sendMessageDraft` opt-in: `gateway.streaming.enabled: true`, `transport: auto|draft|edit|off`. "Tool-progress updates edit in-place rather than appending new messages." Reactions `TELEGRAM_REACTIONS=true` (👀 processing, 👍 success, 👎 error). Rich messages via `sendRichMessage` (Bot API 10.1) with `rich_messages: true`. — [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- `display.tool_progress` modes `off, new, all, verbose, log`; `log` writes "each tool call... as a line to `~/.hermes/logs/tool_calls.log`". `display.platforms.telegram.notifications: important|all` ("important" silences tool progress). — [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/); [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- Design principle: "Observable execution: All tool calls visible via callbacks; CLI spinner/gateway chat updates". — [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- Dashboard: `hermes dashboard --port 9119 --host 127.0.0.1` (requires `pip install hermes-agent[web]`, FastAPI/Uvicorn); pages: Status, Chat (embedded TUI), Config (150+ fields), API Keys, Sessions (filter/search/export), Logs (live tail), Analytics (cost 7/30/90d), Cron, Profiles, Skills, MCP, Webhooks, Pairing, Channels, System. Auth when non-loopback: Nous Portal OAuth, username/password, self-hosted OIDC; "Fail-closed semantics". — [Web dashboard docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/web-dashboard); [fast.io guide](https://fast.io/resources/hermes-agent-web-ui-dashboard.md)
- Logs: `~/.hermes/logs/gateway.log`; dashboard logs screen "pulls the last 500 lines from ~/.hermes/logs/". — [FAQ](https://hermes-agent.nousresearch.com/docs/reference/faq); [fast.io guide](https://fast.io/resources/hermes-agent-web-ui-dashboard.md)
- Status/usage commands in chat: `/status`, `/usage`, `/compress`, `/whoami`, `/new`, `/model`; online/offline bot description indicator (`status_indicator: true`). Intentional silence tokens `[SILENT]`, `NO_REPLY`. — [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/); [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- Telemetry: "Hermes Agent does not collect telemetry, usage data, or analytics." — [FAQ](https://hermes-agent.nousresearch.com/docs/reference/faq)

### Inferences
- The "one editable status message + final rich message" pattern is the standard fix for chat spam; the `log` mode gives an audit trail without user-facing noise.

### Gaps
- Discord/Slack-specific progress rendering (threads, embeds) not fetched; the platform comparison table exists but details weren't captured.

---

## 8. Human-in-the-loop: approvals, clarify, permissions, sandboxing

### Takeaway
Risky shell commands go through `approvals.mode: smart|manual|off` (LLM risk triage by default, 300s timeout, deny-by-default when unattended), a non-overridable hardline blocklist, user `approvals.deny` globs, protected write paths, and container backends (Docker with capability drops) as the real security boundary; the `clarify` tool renders options as inline buttons; memory and skill writes can be staged for approval.

### Cited Findings
- `approvals.mode`: `smart` (default; "Use an auxiliary LLM to assess risk. Low-risk commands are auto-approved"), `manual`, `off`; `timeout` default 300s; `cron_mode`, `single_query_mode`, `unattended_mode` default `deny`. CLI options once/session/always/deny; messaging waits for yes/no, "approval cards timeout... then deny by default". YOLO toggle `--yolo`, `/yolo`, `HERMES_YOLO_MODE=1`; hardline blocklist (`rm -rf /`, fork bombs, `mkfs.*`) "cannot be overridden"; `approvals.deny` fnmatch globs. — [Security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- Telegram prompt text: "⚠️ This command is potentially dangerous (recursive delete). Reply "yes" to approve." — [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- `clarify` tool: "❓ Which framework should I use? [1. Next.js] [2. Remix] [3. Astro] [✏️ Other (type answer)]"; timeout `agent.clarify_timeout` (default 3600s); subagents cannot call `clarify`. — [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram); [Delegation docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
- File write safety: always-blocked `~/.ssh/`, `~/.aws/`, `~/.kube/`, `.env`, `vault/`, `pairing/`; `HERMES_WRITE_SAFE_ROOT` restricts write prefixes. — [Security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security)
- Sandboxing: `terminal.backend: local|docker|ssh|modal|daytona|vercel_sandbox` (+ `singularity` plugin); Docker drops all caps except `DAC_OVERRIDE, CHOWN, FOWNER`, `no-new-privileges`, `--pids-limit 256`, tmpfs limits; `container_cpu`, `container_memory: 5120`, `container_disk: 51200`, `container_persistent: true`, `docker_image: "nousresearch/hermes-sandbox:desktop"`; "Dangerous command checks are skipped in container backends since the container itself is the security boundary." — [Security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security); [Tools docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools); [Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration)
- Credential hygiene: MCP subprocesses get only safe env vars (`PATH, HOME, USER, LANG, TERM, SHELL`...); skills declare `required_environment_variables`; `terminal.env_passthrough`. Five-layer model: user authorization, dangerous-command approval, container isolation, MCP credential filtering, context-file scanning. — [Security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security); [Turing Post](https://www.turingpost.com/p/hermes)
- Staged writes with approval for memory (`memory.write_approval`) and skills (`skills.write_approval`) — see §3/§4. — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory); [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)

### Inferences
- There is no per-tool RBAC per user beyond admin/user command tiers; "who may trigger shell" is answered by allowlists + approvals, not by fine-grained capability grants per human role.

### Gaps
- Whether approval decisions in group chats are restricted to admins (vs any allowed user) was not stated in fetched pages.

---

## 9. Gateway: multi-platform design, pairing, multi-user

### Takeaway
One gateway process (`gateway/run.py`) hosts 25–30+ platform adapters (`plugins/platforms/`), routes by session key, enforces a 6-step authorization hierarchy with DM pairing codes, splits users into admin/user tiers per scope, supports per-channel model/persona overrides, and guarantees at-least-once delivery via a ledger in `state.db`.

### Cited Findings
- Platforms: "Telegram, Discord, Slack, WhatsApp, Signal, SMS, Email, Home Assistant, Mattermost, Matrix, DingTalk, Feishu/Lark, WeCom, Weixin, BlueBubbles (iMessage), QQ, Yuanbao, Microsoft Teams, LINE, ntfy, or your browser." — [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
- Flow: "Platform event → adapter → `GatewayRunner._handle_message()` → authorize → resolve session → `AIAgent.run_conversation()` → deliver response." Components: `gateway/session.py`, `gateway/delivery.py`, `gateway/pairing.py` (allowlists + DM pairing), `gateway/hooks.py`. — [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)
- Authorization hierarchy: per-platform allow-all → DM pairing approved list → platform allowlists → global allowlist → global allow-all → deny. Pairing: 8-char codes, 1h TTL, 1 request/user/10 min, 5-attempt lockout; `hermes pairing list|approve telegram XKGH5N7P|revoke`; `GATEWAY_ALLOW_ALL_USERS=true` discouraged. — [Security docs](https://hermes-agent.nousresearch.com/docs/user-guide/security); [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
- Tiers: "Every allowed user falls into one of two tiers per scope (DM vs group/channel): Admin — full access"; Telegram `allow_admin_from`, `user_allowed_commands`. Per-channel `channel_overrides` (model, provider, system prompt) and `channel_prompts`. Group controls: `TELEGRAM_GROUP_ALLOWED_USERS`, `TELEGRAM_GROUP_ALLOWED_CHATS`, `require_mention`, `observe_unmentioned_group_messages`, `guest_mode`, bot loop guard (20 msgs/5 min, 10-min cooldown). — [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/); [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- Ops: `hermes gateway run|setup|start|stop|status|install`; Telegram polling or webhook (`TELEGRAM_WEBHOOK_URL/SECRET/PORT`); `/sethome` or `TELEGRAM_HOME_CHANNEL` for cron delivery; voice STT `local` (faster-whisper) / `groq` / `openai`; `MEDIA:/path` tags send attachments (host paths when Docker). — [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/); [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- Multi-user: "Multiple users interact with the same Hermes Agent instance via Telegram, Discord, Slack, WhatsApp, or Home Assistant. Access is controlled through allowlists and DM pairing." Limitation: WhatsApp profiles "each require their own WhatsApp number/session". — [FAQ](https://hermes-agent.nousresearch.com/docs/reference/faq)
- Other entry points share the same `AIAgent`: ACP adapter (`acp_adapter/`, VS Code/Zed/JetBrains), batch, API server, MCP server mode. — [Architecture](https://hermes-agent.nousresearch.com/docs/developer-guide/architecture)

### Inferences
- Multi-user is "many humans → one agent identity/memory" (shared `MEMORY.md`/`USER.md` per profile), not per-human user models; per-person isolation requires separate profiles (and separate gateways).

### Gaps
- Microsoft Teams adapter specifics (auth, streaming support) not fetched.

---

## 10. Maturity: license, stars, cadence, limitations

### Takeaway
MIT-licensed, released publicly Feb 2026, ~259k stars by Oct 31 2026 with near-daily patch releases (v0.22.0 on Oct 13 → v0.22.5 on Oct 23, hundreds of PRs per patch), backed by a $90M Series B (Oct 7 2026); limitations center on constrained memory, weak semantic recall, Windows friction, and risk of shell tools without isolation.

### Cited Findings
- GitHub API 2026-10-31: 259,060 stars, 56,958 forks, 52,201 open issues, MIT, created 2025-07-22, description "The agent that grows with you". — [GitHub API](https://api.github.com/repos/NousResearch/hermes-agent)
- Releases: v0.22.5 (2026-10-23, ~81 PRs), v0.22.4/v0.22.3 (10-21, ~620 PRs), v0.22.2 (10-15), v0.22.1 (10-14), v0.22.0 (10-13 "Threshold"), v0.21.7 (10-11, stable channels), v0.21.6 (10-08, first stable pipeline). — [GitHub releases](https://github.com/NousResearch/hermes-agent/releases)
- Timeline (third-party): public release Feb 2026, "95.6K GitHub stars in seven weeks"; v0.5.0 (03-28) MCP server mode + Docker; v0.6.0 (03-30) Profiles; v0.15.0 (05-28); v0.16.0 (06-05) desktop app; MoA 2.0 June; v0.20.0 "Herald" Aug; Series B $90M at $1.5B on 2026-10-07. — [heyuan110](https://www.heyuan110.com/posts/ai/2026-04-14-hermes-agent-guide); [Turing Post](https://www.turingpost.com/p/hermes); [ecosistemastartup](https://ecosistemastartup.com/?p=115723)
- Usage signal: "June 2026: 224B daily tokens vs OpenClaw's 186B on OpenRouter" (third-party). — [Turing Post](https://www.turingpost.com/p/hermes)
- Limitations (third-party + docs): "Native Windows unsupported (WSL2 required)" (Turing Post; note README now documents `%LOCALAPPDATA%\hermes` native install and v0.22.1 "Windows Store fixes", so this is outdated), "Memory intentionally constrained", "Self-improvement effectiveness depends on model and tool quality"; FAQ: WSL gateway should run foreground not systemd; Telegram slash commands capped (60 default / 100 hard). — [Turing Post](https://www.turingpost.com/p/hermes); [FAQ](https://hermes-agent.nousresearch.com/docs/reference/faq); [README](https://github.com/NousResearch/hermes-agent); [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram)
- Install: `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`; Windows `iex (irm https://hermes-agent.nousresearch.com/install.ps1)`; Termux APT stable/canary. — [README](https://github.com/NousResearch/hermes-agent)

### Inferences
- 52k open issues against 60k commits suggests triage lag; expect churn in config keys (e.g., `fallback_model` → `fallback_providers`, deprecated session reset policies).

### Gaps
- Could not verify the "fastest-growing" and OpenRouter token-share claims against primary sources.

---

## 11. Lessons: design ideas worth borrowing, and pitfalls

### Takeaway
The most transferable ideas are the cache-stable prompt architecture (frozen small memory + on-demand retrieval), staged-approval writes for memory/skills, the in-place status message + inline-button clarify UX, isolated cron/subagent sessions with structured outputs, and a durable delivery ledger.

### Cited Findings (ideas, each anchored to a documented mechanism)
1. Frozen, size-capped memory injected at session start (2,200/1,375 chars) to keep the prefix cache intact; edits apply next session. — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
2. Pre-compression "memory flush" call with only the memory tool, plus periodic nudges (`memory.nudge_interval`, `skills.creation_nudge_interval`). — [Vectorize](https://vectorize.io/articles/hermes-agent-memory-explained); [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
3. Staged writes with human review (`/memory pending|approve|reject`, `/skills pending|diff|approve|reject`) — a natural fit for team "rules" that a BA/QE must sign off. — [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory); [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
4. Progressive-disclosure skills (~3k-token index, load on demand) + conditional activation by available toolsets; project-level skills with explicit trust. — [Skills docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/skills)
5. Chat UX: single in-place status message, `notifications: important`, `tool_progress: log` for audit, `clarify` with inline buttons + "Other", approval cards that deny on timeout. — [Telegram docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/telegram); [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/)
6. Isolated job sessions: cron jobs and subagents run as fresh `AIAgent`s with per-role model config, denied tool sets (`clarify`, `memory`, `send_message`, `cronjob`), `output_schema` validation, incident dedup by error signature, `failure_nudge_threshold`. — [Cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron); [Delegation docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/delegation)
7. Durable delivery ledger in SQLite for at-least-once chat delivery across restarts; cron workers survive gateway restarts under systemd. — [Messaging overview](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/); [Cron docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/cron)
8. Credential-reuse providers for subscription CLIs (Claude Code creds preferred; Codex OAuth; DirectSDK plugin wrapping the `claude` binary). — [Provider runtime](https://hermes-agent.nousresearch.com/docs/developer-guide/provider-runtime); [DirectSDK wiki](https://github.com/NousResearch/hermes-plugin-claude-subscription-directsdk/wiki)

Pitfalls (documented):
- FTS5 keyword recall fails on paraphrase/relations; no entity resolution → add semantic index for a team KB. — [Vectorize](https://vectorize.io/articles/hermes-agent-memory-explained)
- Facts not flushed before compression are lost. — [Vectorize](https://vectorize.io/articles/hermes-agent-memory-explained)
- Fallback switches lose prompt cache → cost spikes; subagents/aux tasks don't inherit fallback chains. — [Fallback docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)
- Single shared memory per profile; per-human modelling across a team needs separate profiles or an external user-model provider (Honcho). — [FAQ](https://hermes-agent.nousresearch.com/docs/reference/faq); [Memory docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)
- Terminal runs non-interactively; `.bashrc` must guard for non-interactive shells; subagents can't prompt for sudo. — [Tools docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools)
- Rapid release cadence with key renames (e.g., `fallback_model`→`fallback_providers`) — pin versions. — [Fallback docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/fallback-providers)

### Inferences
- For a BA/Dev/QE platform: model "rules" as approval-gated skills/context files, "tickets" as cron/delegated jobs with `output_schema`, and keep human-facing chat to one status line + final rich message.

### Gaps
- No primary-source evidence on how well the self-improvement loop performs in multi-user team settings.
