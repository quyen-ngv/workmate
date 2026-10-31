# AutoAgent (HKUDS), AutoAgents (Link-AGI + liquidos-ai Rust), OpenClaw — technical profiles

State as of 2026-10-31. Three projects profiled on the same dimensions so the writer can line them up against Hermes Agent, herdr, OpenHands, Claude Code and Paperclip. Sources are mostly primary (GitHub READMEs, arXiv papers, docs.openclaw.ai) plus security press for OpenClaw incidents. Where I could not verify a detail it is listed under Gaps, not guessed.

Important naming note: there are at least three unrelated things called "AutoAgent(s)":
1. **HKUDS/AutoAgent** (Python, "zero-code agent OS", arXiv 2502.05957) — the most relevant to this comparison.
2. **Link-AGI/AutoAgents** (Python, IJCAI 2024, auto-generates an agent team; dormant since April 2024).
3. **liquidos-ai/AutoAgents** (Rust crate `autoagents`, actively maintained in 2026) — the one most likely meant if someone says "autoagents" as a framework in 2026.

---

## A. AutoAgent (HKUDS) — identity and maturity

### Takeaway
AutoAgent is an academic "zero-code agent operating system" (HKU Data Intelligence Lab) built on OpenAI Swarm + LiteLLM + Docker; ~9.8k stars, MIT, last code release v0.2.0 (Feb 2025), repo commits in Oct 2025 are docs-only. It is a research artifact, not a maintained product.

### Cited Findings
- Tagline "Fully-Automated and Zero-Code LLM Agent Framework"; MIT license; 9.8k stars / 1.4k forks; latest release v0.2.0 dated Feb 17, 2025; paper arXiv 2502.05957 — [GitHub README](https://github.com/HKUDS/AutoAgent)
- Paper v3 dated Oct 9, 2025; authors Jiabin Tang, Tianyu Fan, Chao Huang — [arXiv abs](https://arxiv.org/abs/2502.05957)
- Most recent commits (Oct 7–16, 2025) are "Update README.md", "Update Communication.md", "Add Feishu QR code", "Add files via upload" — i.e. documentation/community assets, not code — [GitHub commits](https://github.com/HKUDS/AutoAgent/commits/main)
- Based on OpenAI Swarm framework; three-agent design inspired by Microsoft Magentic-One; Docker required for the agent-interactive environment — [GitHub README](https://github.com/HKUDS/AutoAgent)
- Directory layout: `/autoagent` (core), `/evaluation` (GAIA, MultiHopRAG), `/loop_utils`, `/docs`, `/assets` — [GitHub README](https://github.com/HKUDS/AutoAgent)
- Limitations acknowledged in paper: GAIA Level-3 only 26.92%; anti-automation web mechanisms defeat the web agent; strict string-match evaluation — [arXiv HTML](https://arxiv.org/html/2502.05957)

### Inferences
- No code commits in the 12 months before Oct 2026 (the last are docs in Oct 2025) → effectively unmaintained; expect dependency rot (Swarm is itself deprecated by OpenAI in favour of the Agents SDK).
- Star count is driven by the paper/GAIA result rather than production adoption.

### Gaps
- Could not confirm whether any commit after Oct 16, 2025 exists (GitHub commits page fetched once; it showed Oct 2025 as latest).
- No evidence of a versioned changelog beyond v0.2.0.

---

## A. AutoAgent (HKUDS) — LLM connection

### Takeaway
LiteLLM is the only abstraction: one model string (`COMPLETION_MODEL`) plus provider API keys in `.env`; no routing, fallback, subscription auth, or cost tracking. Non-function-calling models are supported by converting tool calls into an XML text protocol.

### Cited Findings
- Default model `claude-3-5-sonnet-20241022`; switch via `COMPLETION_MODEL` using LiteLLM naming, e.g. `gpt-4o`, `mistral/mistral-large-2407`, `gemini/gemini-2.0-flash`, `huggingface/meta-llama/Llama-3.3-70B-Instruct`, `groq/deepseek-r1-distill-llama-70b`, `deepseek/deepseek-chat`, `openrouter/deepseek/deepseek-r1`, `openai/grok-2-latest` — [GitHub README](https://github.com/HKUDS/AutoAgent)
- `.env` keys: `GITHUB_AI_TOKEN` (required), `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, `HUGGINGFACE_API_KEY`, `GROQ_API_KEY`, `XAI_API_KEY`, `MISTRAL_API_KEY`, `OPENROUTER_API_KEY`; runtime flags `API_BASE_URL`, `FN_CALL` (function-calling toggle), `DEBUG` — [GitHub README](https://github.com/HKUDS/AutoAgent)
- "LLM-powered Actionable Engine" has two paradigms: Direct Tool-Use (provider-native function calling) and Transformed Tool-Use, which rewrites tool-use as "structured XML code generation" (`<function=function_name>`) for models without native tool support; LiteLLM gives access to "100+ models" — [arXiv HTML](https://arxiv.org/html/2502.05957)

### Inferences
- `FN_CALL=False` + `API_BASE_URL` is the lever for local/open models; this is the same trick Hermes-style frameworks use for weak tool-callers.
- Absence of any cost/fallback layer means a team platform would have to wrap it.

### Gaps
- No documentation found on retry/fallback, rate-limit handling, or per-run token accounting.
- Why `GITHUB_AI_TOKEN` is "required" is not explained in the README (likely for the mirror-clone `git_clone`/`test_pull_name` test flow).

---

## A. AutoAgent (HKUDS) — state management, memory, knowledge base

### Takeaway
"Memory" in AutoAgent is a vector DB the agents manage themselves (`save_raw_docs_to_vector_db`, `query_db`, `answer_query`, 4096-token chunks); conversation state is the in-context action–observation history ("system RAM"). There is no documented session persistence/resume, compaction, tiered memory, or user-editable memory files.

### Cited Findings
- Self-Managing File System: accepts text files in any format (PDF, DOC, TXT) or archives, converts to text, stores in user-defined vector DB collections; tools `save_raw_docs_to_vector_db`, `query_db`, `answer_query`; 4096-token chunking following MultiHop-RAG methodology — [arXiv HTML](https://arxiv.org/html/2502.05957)
- Action–observation pairs serve as system state, "functioning as 'system RAM'" — [arXiv HTML](https://arxiv.org/html/2502.05957)
- Local File Agent converts multi-modal data (text, video, audio, spreadsheets) into queryable formats with a paginated Markdown browser — [arXiv HTML](https://arxiv.org/html/2502.05957)
- MultiHop-RAG benchmark: 73.51% accuracy / 14.20% error vs LangChain agentic RAG 62.83% / 20.50% — [arXiv HTML](https://arxiv.org/html/2502.05957)
- Paper has "no explicit mention of cost management, context window handling, or per-user memory isolation" — [arXiv HTML](https://arxiv.org/html/2502.05957)

### Inferences
- Memory = RAG over user documents, not experiential memory; nothing is written automatically at end of a session, so staleness/verification questions do not apply.
- Multi-user / concurrent sessions are not a design concern (single-user CLI inside one Docker container).

### Gaps
- Which vector DB is used (Chroma? other) is not stated in the README/paper text I could fetch; marked unknown.
- No source on where conversation logs are written on disk or whether `auto main` can resume a prior run.

---

## A. AutoAgent (HKUDS) — task management, transparency, human-in-the-loop, tools

### Takeaway
Tasks are decomposed by an Orchestrator that hands off to Web/Coding/File worker agents (Magentic-One style); users interact through a CLI (`auto main` / `auto deep-research`) with three modes (user, agent editor, workflow editor). Sandboxing is Docker; there is no approval/permission model and no scheduler.

### Cited Findings
- Orchestrator decomposes tasks into subtasks and delegates via "handoff tools"; Web Agent has 10 tools abstracted from BrowserGym (click, web_search, visit_url…); Coding Agent has 11 tools (run_python, execute_command, create/read/write files) executed in a Docker sandbox with optional E2B — [arXiv HTML](https://arxiv.org/html/2502.05957)
- Three modes: User Mode (deep-research multi-agent), Agent Editor (create one agent by NL), Workflow Editor (design multi-agent workflow by dialogue); CLI `auto main` (all modes) and `auto deep-research` (user mode only); flags `--container_name` (default `deepresearch`), `--port` (12346) — [GitHub README](https://github.com/HKUDS/AutoAgent)
- Self-Play customization: Agent Profiling Agent → XML agent spec → Tool Editor Agent (RapidAPI/Hugging Face: 145+ APIs across 8 categories; Langchain; 9 HF categories) → Agent Editor Agent; multi-agent systems auto-create an orchestrator via `create_orchestrator_agent`; Workflow Form Agent produces event-driven (listen/trigger) workflows with XML error detection + regeneration — [arXiv HTML](https://arxiv.org/html/2502.05957)
- Seven tool categories: Coding, Web, File Management, Tool Editing, Agent Editing, Workflow Management, RAG — [arXiv HTML](https://arxiv.org/html/2502.05957)
- GAIA: 55.15% average (Level 1 71.70%, Level 2 53.49%, Level 3 26.92%), Claude 3.5 Sonnet by default; Majority-Voting workflow 75.6% on MATH-500 vs 74.2% for deepseek-v3 alone — [arXiv HTML](https://arxiv.org/html/2502.05957)
- Browser cookie import supported for web agent — [GitHub README](https://github.com/HKUDS/AutoAgent)

### Inferences
- "Workflow" here is a generated event-driven agent graph, not a ticket/plan system; there is no persistent task object, status model, or cron.
- Transparency is CLI stdout (and `DEBUG`) only; no event log model.
- No MCP support is mentioned anywhere (paper/README); tool integration is via generated Python wrappers for RapidAPI/HF/LangChain.

### Gaps
- No source on how agent definitions are stored (file format/path) after the Agent Editor creates them.
- No source on human interrupt/approval during a run.

---

## A. AutoAgent (HKUDS) — lessons

### Takeaway
Worth borrowing: the XML tool-call shim for non-function-calling models; event-driven (listen/trigger) workflow spec instead of rigid DAGs; "agents write their own tools from API catalogs" pattern. Pitfalls: unmaintained research code on a deprecated base (Swarm), no persistence, no safety model.

### Cited Findings
- Event-driven workflow chosen over graph-based for "greater flexibility"; iterative self-improvement with syntax validation when generating agents — [arXiv HTML](https://arxiv.org/html/2502.05957)
- Built on OpenAI Swarm — [GitHub README](https://github.com/HKUDS/AutoAgent)

### Inferences
- For a team-facing platform the reusable idea is the *generated agent spec in XML + validator loop* (comparable to a SKILL.md/agent-card generator), not the runtime.

### Gaps
- No independent production deployment write-ups found.

---

## B. AutoAgents (Link-AGI, IJCAI 2024) — full profile

### Takeaway
A MetaGPT-based research prototype that has an LLM Planner draft an expert-agent team (prompt/description/toolset/suggestions) and plan, with Agent/Plan Observers critiquing, then executes with self-refinement and collaborative refinement. GPT-4 only, SerpAPI search is the only tool class, last code update April 2024 — dormant.

### Cited Findings
- Description: "[IJCAI 2024] Generate different roles for GPTs to form a collaborative entity for complex tasks"; repo updated April 16, 2024; paper arXiv 2309.17288 — [SourcePulse](https://www.sourcepulse.org/projects/1845508); [Link-AGI org](https://github.com/Link-AGI)
- Paper v3 revised April 29, 2024 (first submitted Sept 29, 2023) — [arXiv abs](https://arxiv.org/abs/2309.17288)
- Drafting stage: Planner generates agents each defined by Prompt (P), Description (D), Toolset (T — limited to avoid "decision-making confusion caused by excessive tools"), Suggestions (S); Agent Observer checks role conformance/redundancy/missing roles; Plan Observer checks step coherence and assignment — [arXiv HTML](https://arxiv.org/html/2309.17288v3)
- Execution stage: knowledge-sharing memory with short-term (intermediate notions within an action), long-term (historical trajectory of actions), dynamic (task-specific extracted info); actions are self-refinement and collaborative refinement; Action Observer allocates tasks, verifies outputs, adapts plan — [arXiv HTML](https://arxiv.org/html/2309.17288v3)
- Built on MetaGPT's environment/workspace; GPT-4 API at temperature 0; ~10% improvement over baselines on Open-ended QA and Trivia Creative Writing — [arXiv HTML](https://arxiv.org/html/2309.17288v3)
- Config: `OPENAI_API_KEY` or `LLM_API_KEY`, default model `gpt-4o`, Azure variables (`OPENAI_API_TYPE`, `OPENAI_API_BASE`, `OPENAI_API_VERSION`, `DEPLOYMENT_ID`), `SERPAPI_API_KEY` (Serper / Google CSE alternatives), proxies `GLOBAL_PROXY`, `OPENAI_PROXY`; run modes: CLI, WebSocket service (`ws://127.0.0.1:9000`), Docker with web UI on port 7860; MIT; ~1.5k stars / 178 forks; HuggingFace Spaces demo — [GitHub README](https://github.com/Link-AGI/AutoAgents)

### Inferences
- Per-dimension: LLM = single OpenAI/Azure key, no fallback/cost; State = MetaGPT workspace, no resume; Memory = in-run only; Tasks = one generated plan per request, no scheduler; Knowledge = search tool only, no RAG/skills/MCP; Transparency = WebSocket stream/web UI; HITL = none; Multi-platform = none.
- The reusable idea is the *drafting-with-observers* loop (planner + critics before execution) and the 4-field agent spec (P/D/T/S), which maps naturally onto a team-platform "role template" object.

### Gaps
- Exact file layout of the repo and whether the WebSocket protocol is documented were not verified beyond the README summary.
- No activity after April 2024 found; cannot confirm if it is formally archived.

---

## B'. AutoAgents (liquidos-ai, Rust crate `autoagents`) — the "autoagents" that matters in 2026

### Takeaway
A production-oriented Rust multi-agent framework (crates `autoagents`, `autoagents-core`, `autoagents-toolkit`, provider crates; Python bindings `autoagents-py`) with ReAct executor, derive-macro tools, sliding-window memory, WASM-sandboxed tools, typed pub/sub orchestration, MCP examples and OpenTelemetry; ~615–772 stars, MIT/Apache-2.0, pushed April 2026. It is a library, not an assistant or control plane.

### Cited Findings
- "production-grade multi-agent framework in Rust" with type-safe agent model, structured tool calling, configurable memory, pluggable LLM backends — [docs.rs autoagents](https://docs.rs/crate/autoagents); [GitHub](https://github.com/liquidos-ai/AutoAgents)
- Providers: cloud OpenAI, Anthropic, Google, Azure OpenAI, DeepSeek, Groq, xAI, Phind, OpenRouter, Requesty, MiniMax; local Ollama, Mistral-rs, Llama-Cpp; experimental Burn, ONNX — [GitHub](https://github.com/liquidos-ai/AutoAgents)
- Features: ReAct and basic executors with streaming; derive macros for tools/outputs; sandboxed WASM runtime for tool execution; sliding-window memory with extensible backends; LLM guardrails; multi-agent orchestration via typed pub/sub; local TTS/STT; OpenTelemetry tracing/metrics; examples include MCP integration, coding agents, WASM — [GitHub](https://github.com/liquidos-ai/AutoAgents)
- 772 stars (README fetch) / 615 stars (gittrend, older); 345 commits; 27 open issues; dual MIT/Apache-2.0; Python bindings on PyPI with CUDA/Metal/Vulkan accel; last push April 30, 2026 (gittrend) — [GitHub](https://github.com/liquidos-ai/AutoAgents); [gittrend](https://gittrend.io/repo/liquidos-ai/AutoAgents)
- Limitation noted: some providers lack streaming or tool-call support; some backends reject multimodal inputs like PDFs with typed errors — [GitHub](https://github.com/liquidos-ai/AutoAgents)

### Inferences
- Per-dimension: LLM = many providers, no documented fallback/cost tracking; State/Memory = in-process sliding window (no persistence documented); Tasks = none (library); Knowledge = MCP via example; Transparency = OTel; HITL = WASM sandbox only; Chat gateway = none.
- Relevant to Hermes/herdr comparison only as "a Rust substrate you could build on", not as a peer product.

### Gaps
- Star count discrepancy (615 vs 772) reflects different fetch dates; exact Oct 2026 number not pinned.
- Could not verify a persistent memory backend or session store in the crate docs.

---

## C. OpenClaw — identity, maturity, history

### Takeaway
OpenClaw (ex-Warelay → Clawdbot → Moltbot → OpenClaw, Jan 2026) is by far the most deployed open-source personal agent: ~393k stars, MIT, TypeScript/pnpm monorepo, monthly CalVer releases (v2026.10.9 latest in Oct 2026), stewarded by the non-profit OpenClaw Foundation after founder Peter Steinberger joined OpenAI in Feb 2026.

### Cited Findings
- README: "The AI that really does things. Any OS. Any Platform."; Gateway is the local control plane for sessions, tools, events and channels; Control UI/CLI/TUI connect to it; 393.5k stars / 82.7k forks / 123,139 commits; MIT; TypeScript, pnpm workspace; docs at docs.openclaw.ai; stewarded by the OpenClaw Foundation, an independent 501(c)(3) with no paid tier — [GitHub README](https://github.com/openclaw/openclaw)
- Install: `curl -fsSL https://openclaw.ai/install.sh | bash`, `iwr -useb https://openclaw.ai/install.ps1 | iex`, or `npm install -g openclaw@latest --allow-scripts=openclaw` (Node 24.22.1+ / 26.12.1+); `openclaw onboard --install-daemon`, `openclaw gateway status`, `openclaw dashboard` — [GitHub README](https://github.com/openclaw/openclaw)
- Naming timeline: Warelay (Nov 24, 2025) → Clawdbot (Jan 2, 2026) → Moltbot (Jan 27, 2026, after an Anthropic trademark notice) → OpenClaw (Jan 30, 2026, voluntary: "Moltbot never quite rolled off the tongue") — [Wikipedia](https://en.wikipedia.org/wiki/OpenClaw); [HackerNoon](https://hackernoon.com/from-clawdbot-to-moltbot-to-openclaw-the-chaotic-story-of-the-trending-jarvis-ai-assistant); one aggregator claims Clawdbot launched "mid-2024" — [morphllm](https://www.morphllm.com/clawdbot) (conflicts with the Nov 2025 Warelay date; treat 2024 as unreliable)
- Founder Peter Steinberger joined OpenAI to lead next-gen personal agents (Feb 2026) — [Fortune](https://www.fortune.com/2026/02/15/openai-openclaw-ai-agent-developer-peter-steinberg-moltbot-clawdbot-moltbook); [Neowin](https://www.neowin.net/news/openclaws-founder-peter-steinberger-is-joining-openai/)
- Releases: v2026.8.1 "OpenClaw 2.0" (rebuilt web experience, stronger memory and session continuity); v2026.9.3 (update recovery, live browser automation, revocable chat links, meeting transcript search, repository-backed cloud work); v2026.9.4 (plugin/skill discovery, visible skill learning, cloud-worker controls, GPT Image 2.5); v2026.9.9 (Claude Haiku 5.5 support); v2026.10.1 (installed-skill discovery, MCP Apps, full backups); v2026.10.5 (refreshed sidebar, Skill Workshop 2.0, public conversation links); v2026.10.9 "security remediations and fixes for updates, database recovery, workspace restrictions, and sessions" — [docs releases](https://docs.openclaw.ai/releases)
- v2026.10.3 had 90 commits / 79 PRs / 99 contributors; v2026.10.4 (Oct 16, 2026) was a stability patch for host recovery after sleep/clock changes — [releasebot](https://releasebot.io/updates/openclaw); [GitHub release v2026.10.3](https://github.com/openclaw/openclaw/releases/tag/v2026.10.3)

### Inferences
- Release cadence is roughly weekly patch tags within a monthly minor; "security remediations" appear in nearly every October tag, consistent with ongoing hardening after the Jan–Mar 2026 incidents.

### Gaps
- Star/fork figures come from a single README fetch on 2026-10-31; not cross-checked.
- Foundation formation date not verified.

---

## C. OpenClaw — LLM connection

### Takeaway
Models are addressed as `provider/model` with a primary + ordered fallback chain and *auth-profile rotation before fallback*; both API keys and OAuth/subscription logins (Claude, ChatGPT/Codex) are first-class; usage/cost is pulled live from provider quota endpoints and shown per response (`/usage full`), in `/status`, CLI and Control UI.

### Cited Findings
- Config: `agents.defaults.model.primary`, `agents.defaults.model.fallbacks` (tried in order); "Auth failover: provider rotation occurs before moving to next fallback"; `agents.defaults.modelPolicy.allow` accepts `provider/model`, `provider/*`, or namespace prefixes; user session selection is strict (fails if unavailable) while configured defaults use the chain; auto-fallback enters a temporary recovery state with periodic re-probe — [docs models](https://docs.openclaw.ai/concepts/models)
- `/model <provider/model>` (session), `-a` agent default, `-g` global, `/model default -s` clears pin; specialty slots `utilityModel` (titles/recaps/summaries), `decisionModel`, `imageModel`, `pdfModel`; CLI `openclaw models status|list|set|fallbacks add|scan` (scan inspects OpenRouter free models) — [docs models](https://docs.openclaw.ai/concepts/models)
- Pluggable providers: hosted and local, "Claude, Codex, and local models" — [GitHub README](https://github.com/openclaw/openclaw)
- Usage: `/usage off|tokens|full` per-response footer (full = model, reasoning level, fast/slow, context window, estimated cost); custom footer via `messages.usageTemplate` (`openclaw.usageBar.v1` schema); provider usage pulled "directly from provider endpoints without estimated billing": Anthropic via OAuth tokens or Admin keys (`sk-ant-admin...`), OpenAI via ChatGPT/Codex OAuth or `OPENAI_ADMIN_KEY`, plus OpenRouter, DeepSeek, GitHub Copilot, MiniMax, Venice, z.ai; cached 60s; `openclaw status --usage`, `openclaw models status`, `/status` (session tokens + estimated cost), Control UI Usage tab, macOS menu bar; token counts reconstructed from transcript logs when snapshots lack them — [docs usage-tracking](https://docs.openclaw.ai/concepts/usage-tracking)

### Inferences
- The "rotate auth profiles first, then fall back to another model" ordering is a design choice worth copying: it preserves model quality under rate limits before degrading.
- Subscription-plan login (Claude Max / ChatGPT) is supported and even used for quota display — a strong signal that personal-plan auth is a mainstream path, with the attendant ToS risk for a team product.

### Gaps
- Did not find a per-company/per-user budget hard-stop (the Paperclip-style auto-pause) in OpenClaw docs.
- Exact list of all provider IDs not captured.

---

## C. OpenClaw — state management (sessions)

### Takeaway
Sessions are keyed by source (DM shared "main" session, per-group, per-room, fresh per cron run, per-webhook) and stored per agent in SQLite (`~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite`) with archived transcripts under `sessions/`; reset policies (daily/idle), compaction with memory flush, pruning (5000 rows, 30 days), and full-text session search are built in.

### Cited Findings
- Routing defaults: DMs → shared session; groups → isolated per group; rooms/channels → isolated per room; cron → fresh per run; webhooks → isolated per hook; `session.scope: "global"`; `session.dmScope` = `main` (default) | `per-peer` | `per-channel-peer` (recommended for multi-user) | `per-account-channel-peer`; `session.identityLinks` maps one person's identities across channels to one session — [docs session](https://docs.openclaw.ai/concepts/session)
- Storage: session rows + transcripts in `~/.openclaw/agents/<agentId>/agent/openclaw-agent.sqlite`; archived transcripts `~/.openclaw/agents/<agentId>/sessions/`; legacy `sessions.json` — [docs session](https://docs.openclaw.ai/concepts/session)
- Reset: `session.reset: { mode: "daily", atHour: 4 }`, `resetByType.group: { mode: "idle", idleMinutes: 120 }`; modes `none` (default) | `daily` | `idle` — [docs session](https://docs.openclaw.ai/concepts/session)
- Maintenance: `session.maintenance.maxEntries` 5000 unarchived rows, `pruneAfter` 30d, `archiveDashboardAfter` 7d, `preserveRecent`; over-capacity sessions are archived not deleted, preserving IDs; `/status` shows context usage; `openclaw sessions --json`, `openclaw sessions cleanup --dry-run`; session search gives full-text recall across transcripts — [docs session](https://docs.openclaw.ai/concepts/session)
- Each agent has its own workspace, auth profiles and "SQLite-backed session store" at `<agentDir>/openclaw-agent.sqlite`; default `~/.openclaw/agents/<agentId>/agent/` — [docs multi-agent](https://docs.openclaw.ai/concepts/multi-agent)
- Automations/cron and exec-approval state live in a *shared* SQLite: `~/.openclaw/state/openclaw.sqlite` (table `exec_approvals_config`), legacy JSON migrated via `openclaw doctor --fix` — [docs exec-approvals](https://docs.openclaw.ai/tools/exec-approvals); [docs automations](https://docs.openclaw.ai/cli/cron)
- Sub-agent session key format `agent:<agentId>:subagent:<uuid>` — [docs subagents](https://docs.openclaw.ai/tools/subagents)

### Inferences
- Two-tier storage (per-agent SQLite for transcripts; one shared SQLite for gateway state) is a sensible separation a control plane can mirror.
- Concurrency is handled per session key (one shared DM session means users on the same channel share context unless `dmScope` is tightened — a known multi-user footgun).

### Gaps
- Exact compaction algorithm (summary model, thresholds) not captured; only that compaction triggers a memory flush.
- Queue/lane behaviour for concurrent messages into one session not verified.

---

## C. OpenClaw — memory

### Takeaway
Memory is plain Markdown in `~/.openclaw/workspace` (MEMORY.md curated long-term, `memory/YYYY-MM-DD.md` daily notes, optional USER.md and DREAMS.md), indexed by a `memory-core` plugin into SQLite/Postgres with hybrid vector+keyword `memory_search`; a silent "memory flush" turn writes before compaction, and a background "dreaming" pass promotes daily notes into MEMORY.md with taint-gated scoring. Users can read/edit every file directly.

### Cited Findings
- Files: `USER.md` (optional, stable user preferences), `MEMORY.md` (curated long-term facts/decisions, loaded at session start), `memory/YYYY-MM-DD.md` (daily notes; today's and yesterday's auto-load), `DREAMS.md` (dream diary / consolidation summaries for human review); principle: "the model only remembers what gets saved to disk; there is no hidden state" — [docs memory](https://docs.openclaw.ai/concepts/memory)
- `memory-core` plugin tools: `memory_search` (hybrid semantic + keyword), `memory_get` (file or line-range read), `intent` (event-conditioned standing intents); embeddings: OpenAI (default), Gemini, Voyage, Mistral, local GGUF, Ollama; index backend SQLite (default) or PostgreSQL — [docs memory](https://docs.openclaw.ai/concepts/memory)
- Memory flush: before compaction a silent turn prompts the agent to save context; flush appends only new text to daily notes and rejects replayed content; key `agents.defaults.compaction.memoryFlush.enabled` — [docs memory](https://docs.openclaw.ai/concepts/memory)
- Dreaming: background consolidation promotes qualified short-term memories into MEMORY.md "through thresholded, taint-gated scoring", following sleep-time-compute and provenance-aware reflection; config `memory.search.provider`, dreaming toggles; CLI `openclaw memory status|search` — [docs memory](https://docs.openclaw.ai/concepts/memory)
- Multi-agent: per-agent "Memory Wiki vaults" to keep boundaries — [docs multi-agent](https://docs.openclaw.ai/concepts/multi-agent)
- Workspace bootstrap files `SOUL.md`, `AGENTS.md` per agent — [docs multi-agent](https://docs.openclaw.ai/concepts/multi-agent)

### Inferences
- Tiers map cleanly: USER.md (profile) / MEMORY.md (long-term) / daily notes (episodic) / index (retrieval). Staleness is handled by (a) only loading 2 days of notes, (b) dreaming promotion, and (c) humans editing Markdown — there is no automatic expiry/contradiction resolution documented.
- "Taint-gated" promotion implies provenance tracking of memory derived from untrusted channel input — directly relevant after the prompt-injection incidents.

### Gaps
- Exact dreaming thresholds/schedule and the taint model are not documented in the page fetched.
- Did not verify whether memory search results carry citations back to file/line in the prompt.

---

## C. OpenClaw — task management (automations/cron, heartbeat, sub-agents)

### Takeaway
There is no ticket/plan object; "tasks" are (a) automations — one-shot `--at`, interval `--every`, `--cron`, or event-based (`--on-exit`, `--stream-command`) jobs run in `main`/`isolated`/`current`/`session:<id>` with chat/webhook/owner-DM delivery, backoff and run history in SQLite — and (b) sub-agents spawned with depth/children limits that announce results back through an "announce chain" visible in a Subagents panel.

### Cited Findings
- Schedules: `--at` (ISO or `20m`, UTC unless `--tz`), `--every` (`10m`,`1h`,`1d`), `--cron` (5/6-field, `--tz`, `--exact`, `--stagger`), `--on-exit`, `--stream-command`; targets `main` (persists state), `isolated` (fresh transcript per run), `current`, `session:<id>`; delivery `--announce --channel --to`, `--webhook <url>`, `--delivery-target owner`, `--no-deliver`; CLI `openclaw automations create|list|run|edit|runs`; requires `operator.admin`; one-shots auto-delete unless `--keep-after-run`; recurring jobs back off 30s→1m→5m→15m→60m; run history 7 days / 2000 rows per job; state in shared SQLite — [docs automations](https://docs.openclaw.ai/cli/cron)
- Cron jobs use the configured model fallbacks unless overridden — [docs models](https://docs.openclaw.ai/concepts/models)
- Sub-agents: `maxSpawnDepth`, `maxChildrenPerAgent`; `agents.defaults.subagents.model` for cheaper child models; result "announces its result back to the requester for review" without blocking; thread-bound sessions default `context: "fork"`; sub-agents "do not get session or message tools by default" and cannot access cron/gateway tools; Control UI Subagents panel shows running/finished children with parent-child relations; `/subagents` lists/stops abortable runs — [docs subagents](https://docs.openclaw.ai/tools/subagents)
- Team presets with role templates (coordinator, researcher, writer, reviewer); cross-agent delegation via subagent config with visibility and tool-access control — [docs multi-agent](https://docs.openclaw.ai/concepts/multi-agent)
- `intent` tool: "event-conditioned standing intents" — [docs memory](https://docs.openclaw.ai/concepts/memory)

### Inferences
- Status model is job-level (enabled, run history, backoff) rather than issue-level; a team platform needs its own work-item layer on top.
- "Announce chain" + non-blocking spawn is essentially an async callback bus; mirrors Paperclip's parent/child issue + comment pattern.

### Gaps
- `HEARTBEAT.md`/heartbeat interval mechanism (present in earlier versions) was not confirmed on the current docs page fetched; treat as unverified for Oct 2026.
- Limits defaults (e.g., maxConcurrent values) not captured.

---

## C. OpenClaw — knowledge base, skills, tools, MCP

### Takeaway
Knowledge is skills (SKILL.md with YAML frontmatter + `metadata.openclaw` gating), loaded from a 7-level precedence chain (workspace → `.agents/skills` → `~/.agents/skills` → managed → workshop/learned → bundled → plugins), installed from ClawHub or git, compiled into an XML block in the system prompt per turn; MCP servers and "MCP Apps" are supported, with durable per-tool grants. Document RAG is only via memory search (no dedicated ingestion pipeline documented).

### Cited Findings
- SKILL.md: required `name`, `description`; `metadata.openclaw.requires.bins|env|config`, `primaryEnv` (maps to skill `apiKey`), `always`, `os` (darwin/linux/win32); `disable-model-invocation: true` hides from `$` picker; `user-invocable: true` default exposes slash command; invoke with `$skillname` — [docs skills](https://docs.openclaw.ai/tools/skills)
- Precedence (highest→lowest): `<workspace>/skills`, `<workspace>/.agents/skills`, `~/.agents/skills`, `<state-dir>/skills` (managed), workshop (agent-learned), bundled, extra dirs/plugins; discovery up to 6 levels deep; `openclaw skills install @owner/<slug>` | `git:owner/repo@ref` | `./path --as name` (`--global` → `~/.openclaw/skills`); `openclaw skills update --all`; per-agent allowlist `agents.entries.<agentId>.skills` — [docs skills](https://docs.openclaw.ai/tools/skills)
- Per turn: resolve eligible skills (gating) → inject env/API keys → compile to compact XML in system prompt → restore env; "Env injection is scoped to the host agent run, not the sandbox"; "Treat third-party skills as untrusted code" — [docs skills](https://docs.openclaw.ai/tools/skills)
- v2026.10.1 shipped "MCP Apps"; v2026.9.4 "visible skill learning"; v2026.10.5 "Skill Workshop 2.0" — [docs releases](https://docs.openclaw.ai/releases)
- "MCP tool grants persist durable permission for specific tools on configured servers when using 'Allow Always'" — [docs exec-approvals](https://docs.openclaw.ai/tools/exec-approvals)
- ClawHub marketplace + plugin SDK; companion nodes for voice, canvas, camera, screen, device-local actions — [GitHub README](https://github.com/openclaw/openclaw)
- ClawHub integrated VirusTotal scanning after the malicious-skills wave (Feb 2026) — [THN](https://thehackernews.com/2026/02/openclaw-integrates-virustotal-scanning.html)

### Inferences
- SKILL.md is the same convention as Claude Code/Agent Skills, so skills are portable between OpenClaw, Claude Code and Hermes-style agents — a strong argument for adopting the format in a team platform.
- "Workshop (agent-learned)" tier means the agent can author its own skills; combined with ClawHub this is exactly the supply-chain surface that was exploited.

### Gaps
- No documented PDF/doc ingestion pipeline beyond `pdfModel` and memory files; whether a RAG-over-documents feature exists in 2.0 is unverified.
- MCP configuration keys (server list path) not captured.

---

## C. OpenClaw — transparency and status

### Takeaway
Progress is surfaced in chat (`/status`, `/usage` footers, `/subagents`, `/model`), the Control UI (session tree, Subagents panel, Usage tab, approval prompts), TUI/CLI, and macOS menu bar; the gateway emits events such as `exec.approval.requested` to connected clients; transcripts are JSONL/SQLite per session and searchable.

### Cited Findings
- `/status` in chat shows context usage and session tokens + estimated cost; Control UI Usage tab; macOS menu bar usage — [docs usage-tracking](https://docs.openclaw.ai/concepts/usage-tracking); [docs session](https://docs.openclaw.ai/concepts/session)
- Gateway broadcasts `exec.approval.requested`; clients resolve via `exec.approval.resolve` — [docs exec-approvals](https://docs.openclaw.ai/tools/exec-approvals)
- Control UI Subagents panel; `/subagents` command — [docs subagents](https://docs.openclaw.ai/tools/subagents)
- Gateway = "local control plane for sessions, tools, events, and channel connections"; Control UI, CLI, TUI connect to it — [GitHub README](https://github.com/openclaw/openclaw)
- v2026.9.4 "terminal questions"; v2026.9.3 "revocable chat links"; v2026.10.5 "public conversation links" — [docs releases](https://docs.openclaw.ai/releases)

### Inferences
- Tool-call visibility in chat channels is configurable but I could not confirm the exact `/verbose` semantics in current docs; treat chat as "summary + optional footer" and the Control UI as the full trace.

### Gaps
- Event/log schema (names beyond `exec.approval.*`) and whether tool calls stream into chat by default were not verified.

---

## C. OpenClaw — human-in-the-loop, permissions, sandboxing

### Takeaway
Two-layer exec policy — security `deny|allowlist|full` × ask `off|on-miss|always`, with "effective policy is the stricter of `tools.exec.*` and approvals defaults" — stored in `~/.openclaw/state/openclaw.sqlite#exec_approvals_config`, per-agent allowlists with argv regex and directory-bound "Allow Always" grants; sandbox `agents.defaults.sandbox` = `off|non-main|all` (Docker); one trust boundary per gateway (explicitly not multi-tenant).

### Cited Findings
- Security modes `deny`/`allowlist`/`full`; ask modes `off`/`on-miss`/`always`; policy stored at `~/.openclaw/state/openclaw.sqlite#exec_approvals_config` with compare-and-swap hash; commands re-authorized against current policy before execution; allowlists per-agent with glob patterns and ECMAScript argv regex; "Allow Always" entries are directory-bound; `strictInlineEval: true` makes `python -c`-style inline code approval-only; standing grants with `grantExpiryDays`; macOS app hides approval panels from screenshots and refuses system input during pending approvals — [docs exec-approvals](https://docs.openclaw.ai/tools/exec-approvals)
- "One trust boundary per gateway" — single operator or mutually-trusting team; "not a hostile multi-tenant security boundary"; separate gateways for mixed trust; `openclaw security audit` checks drift; gateway binds loopback by default — [docs security](https://docs.openclaw.ai/gateway/security)
- DM policies: `pairing` (default; unknown senders get a pairing code), `allowlist`, `open`, `disabled`; groups allowlisted behind a mention gate; per-agent access profiles, read-only modes; sandbox `off/non-main/all` with Docker — [docs security](https://docs.openclaw.ai/gateway/security)
- "Treat inbound messages as untrusted input"; DM-capable channels require approval pairing by default — [GitHub README](https://github.com/openclaw/openclaw)

### Inferences
- The "stricter-of" merge between global tool policy and host approvals is a clean way to prevent a UI from loosening a config-level guardrail — worth copying.
- Directory-bound "Allow Always" and the re-authorize-at-execution step are direct responses to the 2026 argv-mutation CVEs (see next section).

### Gaps
- Exact Docker sandbox defaults (network policy, mounts) not captured.

---

## C. OpenClaw — multi-platform chat gateway and security incidents

### Takeaway
33+ channels (5 bundled, ~25 official plugins, several community) with multi-account routing, DM pairing, mention gating and identity links; but 2026 produced a cluster of incidents — CVE-2026-25253 one-click RCE via WebSocket token exfiltration (fixed 2026.1.29), Kaspersky's 512-finding audit, the ClawHavoc supply-chain campaign on ClawHub (341 → 1,184+ malicious skills), and tens of thousands of unauthenticated internet-exposed gateways.

### Cited Findings
- Channels: bundled A2A, Reef, Telegram, X/Twitter, WebChat; official plugins Buzz, ClickClack, Discord, Feishu, Google Chat, iMessage, IRC, LINE, Matrix, Mattermost, Microsoft Teams, Nextcloud Talk, Nostr, QQ Bot, Raft, Signal (signal-cli daemon), Slack, SMS, Synology Chat, Tlon, Twitch, WhatsApp (install-on-demand), Zalo, Zalo personal; external WeChat, WeCom, Yuanbao, Zalo ClawBot; voice calls via Plivo/Telnyx/Twilio; multiple accounts run simultaneously and route per chat; "DM pairing and allowlists are enforced for safety"; Telegram recommended first — [docs channels](https://docs.openclaw.ai/channels)
- Multi-agent bindings precedence: exact peer > parent peer > wildcards > guild+roles > guild > team > account > channel > fallback; `openclaw agents add`, `openclaw channels login --channel whatsapp --account work`, `openclaw agents list --bindings` — [docs multi-agent](https://docs.openclaw.ai/concepts/multi-agent)
- CVE-2026-25253 (CVSS 8.8): token exfiltration → full gateway compromise, one-click RCE via crafted link / WebSocket hijack, exploitable even on localhost-bound instances; patched in 2026.1.29; users told to rotate tokens — [THN via search](https://thehackernews.com/search/label/WebSocket%20Security); [NVD/search summary](https://nvd.nist.gov/vuln/detail/CVE-2026-53822)
- Jan 31, 2026 advisories CVE-2026-25253 (RCE), CVE-2026-25157 (command injection), CVE-2026-25158 (path traversal); Kaspersky audit late Jan 2026: 512 vulnerabilities, 8 critical — [eastondev](https://eastondev.com/blog/en/posts/ai/20260227-openclaw-security-alert/)
- Later 2026 CVEs include argv changing between approval and execution (allowlist bypass), inline-command parsing bypass, env-var assignment injection bypassing exec preflight — [search summary incl. NVD CVE-2026-53866 / 53853](https://nvd.nist.gov/vuln/detail/CVE-2026-53866)
- ClawHavoc: Koi Security found 341 malicious of 2,857 ClawHub skills audited, 335 from one operation; by Mar 1, 2026 >1,184 confirmed malicious skills among >10,700 packages; typosquats and "lost Bitcoin finder" lures; Trend Micro found 39 skills dropping Atomic Stealer on macOS — [THN](https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html); [eSecurityPlanet](https://esecurityplanet.com/threats/hundreds-of-malicious-skills-found-in-openclaws-clawhub); [eastondev](https://eastondev.com/blog/en/posts/ai/20260227-openclaw-security-alert/)
- Exposure: SecurityScorecard reported >135,000 exposed instances in 82 countries, >50,000 RCE-exploitable — [search summary](https://thehackernews.com/2026/02/researchers-find-341-malicious-clawhub.html); eastondev cites ">42,000" exposed — [eastondev](https://eastondev.com/blog/en/posts/ai/20260227-openclaw-security-alert/) (figures conflict; different scanners/dates)
- Late-2025 npm package compromises via account takeovers were also reported in the vulnerability search summary — [search summary](https://nvd.nist.gov/vuln/detail/CVE-2026-53822) (secondary; not independently verified)
- Mitigations documented: update ≥2026.1.29, sandbox with network restrictions, Docker least privilege, review skills before install, secret managers instead of plaintext credentials, rotate keys — [eastondev](https://eastondev.com/blog/en/posts/ai/20260227-openclaw-security-alert/); ClawHub VirusTotal scanning — [THN](https://thehackernews.com/2026/02/openclaw-integrates-virustotal-scanning.html)

### Inferences
- The incident set maps to three design lessons: (1) a local control plane with a browser UI needs origin-bound, short-lived tokens (CVE-25253); (2) "approve the command shape, then execute exactly that shape" — hash/CAS and re-authorization at exec time; (3) an open skill marketplace without signing/scanning becomes malware distribution within weeks.
- OpenClaw's own docs now say multi-user = multiple gateways; a team-facing platform must put the tenant boundary *above* the OpenClaw-style gateway (one gateway per user or per trust group).

### Gaps
- NVD entries CVE-2026-53822/53853/53866/42435 were surfaced by search but their bodies were not fetched; exact descriptions are from search summaries only.
- THN article body for CVE-2026-25253 returned 403; researcher/firm attribution not confirmed.

---

## C. OpenClaw — lessons for a team-facing AI engineer platform

### Takeaway
Borrow: Markdown-first memory with flush-before-compaction and provenance-gated promotion; `provider/model` + fallbacks + auth rotation; stricter-of policy merge and directory-bound grants; SKILL.md precedence chain; session-key routing with `dmScope`/`identityLinks`; non-blocking sub-agent announce chain; live provider-quota display. Avoid: single trust boundary per gateway, plaintext credentials in config, unsigned skill marketplaces, DM-shared "main" session default for multi-user.

### Cited Findings
- "the model only remembers what gets saved to disk; there is no hidden state" — [docs memory](https://docs.openclaw.ai/concepts/memory)
- "Effective policy is the stricter of `tools.exec.*` and approvals defaults" — [docs exec-approvals](https://docs.openclaw.ai/tools/exec-approvals)
- "not a hostile multi-tenant security boundary for mutually adversarial users" — [docs security](https://docs.openclaw.ai/gateway/security)
- Critique: "We're letting AI write code for us, and we're also letting AI execute code for us — this is a massive leap of trust" — [eastondev](https://eastondev.com/blog/en/posts/ai/20260227-openclaw-security-alert/)

### Inferences
- OpenClaw is the closest deployed peer to Hermes Agent on memory/skills/gateway design; the differentiator a team platform can add is the tenant/approval/audit layer OpenClaw explicitly declines to provide.

### Gaps
- No primary-source post-mortem from the OpenClaw Foundation on ClawHavoc was located in this pass.

---

## Side-by-side quick matrix (for the writer)

| Dimension | AutoAgent (HKUDS) | AutoAgents (Link-AGI) | autoagents (Rust) | OpenClaw |
|---|---|---|---|---|
| LLM | LiteLLM, `COMPLETION_MODEL`, XML tool shim; no fallback/cost | OpenAI/Azure only | 15+ providers incl. local; no fallback docs | `provider/model`, fallbacks, auth rotation, OAuth subs, live quota/cost |
| State | in-context only; Docker container | MetaGPT workspace | in-process | per-agent SQLite + archives, reset/prune/search, compaction |
| Memory | agent-managed vector DB (4096-tok chunks) | short/long/dynamic in-run | sliding window | MEMORY.md + daily md + hybrid index, flush, dreaming |
| Tasks | orchestrator handoffs, generated workflows | planner + observers | none | automations (at/every/cron/event), sub-agents w/ announce chain |
| Knowledge | RAG tools, generated API tools | search tool | MCP example | SKILL.md chain, ClawHub, MCP/MCP Apps |
| Transparency | CLI/DEBUG | WebSocket/web UI | OTel | chat footers, /status, Control UI, events |
| HITL | Docker only | none | WASM | deny/allowlist/full × ask modes, pairing, sandbox |
| Chat gateway | none | none | none | 33+ channels, multi-account, identity links |
| Maturity | 9.8k★, MIT, code frozen Feb 2025 | 1.5k★, MIT, frozen Apr 2024 | ~700★, MIT/Apache, active Apr 2026 | 393k★, MIT, weekly tags, v2026.10.9 |
