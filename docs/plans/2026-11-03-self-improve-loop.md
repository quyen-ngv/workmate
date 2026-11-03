# Continuous self-improvement loop

Started 2026-11-03. One ticket at a time in the office `F:/_ao_self` (Claude Opus 5.5, project `actor-office`,
rules R-1..R-11). For each ticket: create it from the backlog, answer and approve as the team, review with the
merge checklist (diff + `check-review-clean.txt`), merge into `team-copilot`, run the full suite, restart the office
when idle, write down what the run showed, and add improvement tickets to the backlog.

## Runs

| Ticket | Backlog | Workflow | Result | What the run showed |
|---|---|---|---|---|
| SI-7 | 01 | code | merged (49283f8), 391 tests | 3 good questions (one corrected the ticket: the merge gate has no card). The reviewer's 2 medium findings were never shown to the AI: a passing check only says "Checks passed", so rule R-2 could not be followed. Sent back from the merge review with a steer. |
| SI-8 | 02 | quick (by the AI) | merged, 393 tests | Plan and fix in one approval. The AI proved the test fails without the fix by saving a patch to /tmp (needed approval): rule R-12 says use `git stash`. Quick has no independent review: backlog 13. |
| SI-9 | 03 | quick (by the AI) | merged | The security gate rule R-11 fired on `engine/gate.py` and asked the lead. The AI applied R-12 (`git stash`) by itself. A security fix still had no independent review because quick has none (backlog 13). |
| SI-10 | 04 | quick (by the AI) | merged | Two-sentence prompt fix with prompt tests for both workflows (rule R-5 followed). |
| SI-11 | 13 | quick (by the AI) | merged | One-word change plus tests; the AI recorded a plan deviation (first use of plan_deviation) for an SI-7 test that asserted the old behaviour. From now on quick fixes get an independent review. |
| SI-12 | 05 | quick (by the AI) | merged, 452 tests | First ticket under SI-7 + SI-11: the review found 2 high (regressions: `done=1` and `echo then done` closed a loop early) and 3 medium; all fixed and answered, F5/F6 verified by hand. The AI's commit message had backticks inside double quotes (bash would run them): rejected, rule R-13. A steer created a wake that later reran the finished build stage and left a stale gate card: backlog 15. `$(`/backticks inside quotes still make the shell parser give up: backlog 14. |
| SI-13 | 15 | code | merged | One combined question (1A 2A 3A) instead of three cards. The reviewer cited rule R-3 for a read-then-drop race; fixed with a conditional UPDATE. Open low finding: a comment wake held while the last gate is pending stays after the ticket is done. |
| SI-14 | 06 | code | merged, 483 tests, ruff clean | Measured options in one question (39 vs 134 violations). The watchdog stopped a one-time undo of a misplaced README section: backlog 16. `ac_coverage` forced a test for a process criterion (AC-5): backlog 17. Merging a dependency change cannot `uv sync` while offices run from the same venv: merge script now runs tests with `--no-sync`. `repo:lint` now runs (`uv run ruff check src tests`). |
| SI-15 | 16 | quick (by the AI) | merged | Clean run: short plan, one approval, no review findings, no questions, no tool approvals. |
| SI-16 | 17 | code | merged | Regression found by the loop: a `step_done` after a passing `stage_complete` opened a second attempt and SI-13's `_open_run` cancelled the plan card; fixed directly (9442b0c, stage tools use the current stage run) because it blocked the flow. With the CPU at 100% from other apps, the AI wrapped tests in `timeout 120` and checked processes (`tasklist`): steered to run related tests per file. One turn failed with `Control request timeout: initialize` (Claude CLI start on a loaded machine), `retry` resumed it. The review found F1 medium (an empty `Cách kiểm:` took the next line), fixed with a test. Open low: F2 (a test checks only `data['review']`), F3 (`ac_coverage` skips the review handoff check when the workspace is not a git worktree). |

## Stopped

The loop was stopped by the user after SI-16. Backlog left: 07 coverage, 08 serial queue, 09 merge button, 10 update branch, 11 test speed, 12 handoff/dossier, 14 quote-aware NOT_SPLITTABLE, plus SI-16 F2/F3 and a turn retry on a CLI start timeout (`Control request timeout: initialize`).
