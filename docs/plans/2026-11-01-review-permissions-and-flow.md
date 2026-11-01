# Review of the first real tasks (AO-2..AO-7): permissions and flow

Date: 2026-11-01. Data: the office at `~/.actoroffice` (6 staged tickets on two projects, Claude and Kiro runs).

## What the data showed

| Ticket | Tool approvals | Questions | What happened |
|---|---|---|---|
| AO-2 | 3 | 1 | read-only exploration with `;` and `| head` asked every time |
| AO-3 | 16 | 3 | reads, `ruff`, `rm cap1.json`, `git commit -m "… <noreply@…>"` all asked |
| AO-4 | 9 | 8 | PR stage looped: `pr_create` asked 5 times, each failed (no code host) |
| AO-6 | 3 | 9 | same PR loop, ended when a person opened the PR by hand |
| AO-7 | 13 | 0 | Kiro run: every office tool (`@office/ticket_get`, …) asked |

### Permissions

1. Chains of read-only commands asked: `cat a; ls b | head`, `… 2>/dev/null`, `cd <workspace> && git status`.
   The policy only allowed a chain when every part was on the position's allow-list, and `cat`, `ls`, `sed -n`,
   `which` were not on it.
2. The chain check did not read quotes: `<noreply@anthropic.com>` inside a commit message counted as a redirect.
3. Kiro names our tools `@office/<tool>`; only `mcp__office__` and `office_` were recognised, so they were
   "unknown tools".
4. `pr_create`, `pr_reply`, `jira_comment` asked every time, even though they act on the ticket's own PR or issue
   after a person approved the plan, and even when no code host was connected (the call could only fail).
5. "Always allow" was stored on the actor for every project, and a chained command offered "allow Bash"
   (every command) as one click. One person clicked it.
6. Security hole: a remembered prefix rule (`Bash(git status*)`) matched a whole chain
   (`git status && curl … | sh`).
7. The `/team` page offered only "allow once" and "reject": the same command asked again in the next run.

### Flow

1. A staged ticket had no way to say "I wait for a person" without a question card: `in_review` required a
   pending `ask_human`. The prompt told the AI to end with `todo`, and `todo` means "continue by yourself", so
   20 seconds later a new run started, retried `pr_create` and asked again (AO-4: 10 runs in the PR stage).
2. A person could not waive a failing check (`pr_linked` when there is nothing to link). The waiver feature is
   on branch `t/ao-6-grant-quyen-cho-user-pas`, written by the AI and not merged.
3. GitHub repositories have no code host: `pr_create` only supports Bitbucket Cloud.

## What changed

- `security/shell.py`: quote-aware split of a command line into commands and redirects. Each command is
  classed read / workspace / network / unknown.
- Permission modes per project, like Claude Code's modes, Codex's approval presets and Kiro's tool trust:
  `read_only`, `workspace` (default) and `full`. In `workspace` mode reads, edits, read-only commands,
  workspace-only file commands, local git, the project's own test/build/lint commands and the ticket's own
  external tools run without a click. The network, other folders, recursive deletes, `git reset --hard` and
  unknown programs ask. Hard denies, protected folders and team policies apply in every mode.
- "Always allow" is stored per project, by command prefix (`Bash(uvx ruff*)`), and matched per command inside a
  chain. A whole shell is never offered on a card.
- Office tools are recognised in every runtime naming (`mcp__office__x`, `@office/x`).
- A staged ticket may end with `ticket_set_status in_review` and a note: it waits for a person and does not
  start itself again. The board shows it as "needs you".
- `/team` approval cards offer "always allow <prefix> in <project>". Settings has the mode per project and the
  remembered rules, each removable.

## Still open

- Merge the AI's branches after review: `t/ao-3` (Kiro permission payloads), `t/ao-4` (model per task/stage),
  `t/ao-6` (waive a check, force a status). AO-6 is what unblocks a stage like AO-4's.
- A GitHub code host (REST with a token, or the `gh` CLI) so `pr_create` works on GitHub repositories.
- No OS sandbox on Windows: unknown programs still ask once per prefix and project, where Codex runs them in a
  sandbox.
