# Self-improvement loop: Copilot works on its own code

Date: 2026-11-01. A separate office (`F:/_ao_self`, port 8770, ticket prefix `SI`) runs the AI engineer on
Claude Opus 5.5 (`ai_model: claude-opus-5-5`) against this repository (`actor-office`, branch `team-copilot`,
permission mode `folder`). A person (here: Claude Code acting for the owner) files tickets, answers questions,
passes the gates, reviews each branch, merges it into `team-copilot` and presses "merged". When the AI's way of
working goes wrong, the process itself is fixed, the office restarts, and the next ticket shows whether the fix
holds.

## Round 1

| Ticket | Workflow | What happened |
|---|---|---|
| SI-1 Telegram: only /start binds a group | code → quick (by the AI) | Size check worked: the AI switched to quick in its first run. Short plan with file:line and 3 AC, one approval, 11 lines of code + 2 tests, 271 tests pass. Merged. |
| SI-2 Auto-approve pending cards when permissions widen | code | Requirements with 12 AC and real safety rules (only re-check calls whose exact arguments are known), plan A/B, a new service and 15 tests, 5 accurate KB items. Merged; 293 tests pass. |
| SI-3 Do not run the whole test suite twice in build | code → quick (by the AI) | Filed from SI-1's build. The build prompt now asks for the related tests only when the office runs the suite; tests guard the wording in both workflows. Merged. |
| SI-5 PR stage: do not rerun the whole suite several times | code | Filed from SI-2: the suite ran 4–5 times per ticket (build, repo:test, the AI again in PR, flaky_rerun twice). |
| SI-4 Telegram: switch the bot to a new group from Settings | code | Three good multiple-choice questions with code references, then requirements with 9 AC. |

### Process fixes found by watching

1. After a workflow switch the next session searched and read the same code again: the switch reason now
   reaches the next prompt, and `stage_switch` asks for the cause with file:line.
2. Without a code host the PR stage looped asking for a PR URL (AO-4): the committed branch is now the review;
   `pr_linked` passes on a clean branch with commits, and `pr_create` says so. SI-1's PR stage took one run.
3. A multi-line commit message (newline inside quotes) asked for approval: quoted newlines are text now.
4. The watchdog stopped SI-2 and SI-3 at their third edit of one file while writing a planned change: a loop is
   now the same edit twice, an edit undone, or 10 edits without a command; reads count per part of a file.
5. SI-1's build ran the whole suite itself and then again in the `repo:test` check: filed as SI-3 for the AI.
6. SI-1's reply points people to "Settings" to switch groups, which did not exist: filed as SI-4.
7. The independent reviewer (review_clean) found a real race in SI-4's `unbind()`; the AI fixed it before the
   gate. The reviewer and the suite then ran again: a second `stage_complete` repeats every check.
