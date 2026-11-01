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
| SI-4 Telegram: switch the bot to a new group from Settings | code | Three good multiple-choice questions with code references, requirements with 9 AC; the reviewer found a race that the AI fixed. Merged; 308 tests pass. |

## Round 2 (after restarting on rounds 1's merges)

| Ticket | Workflow | What happened |
|---|---|---|
| SI-5 PR stage: do not rerun the whole suite | code | flaky_rerun reuses the build stage's `repo:test` result for the same clean commit; the PR prompt says not to rerun. Its plan already followed SI-3 ("only the related tests"). Merged. |
| SI-6 No background command + sleep-loop waits | code | Four questions, one correcting the ticket: an ASK reason only reaches people, so the loop is denied with guidance to the AI. Quote-aware detector for sh and PowerShell. Merged; 369 tests pass. |

## Numbers

| Ticket | Workflow | Runs | Tool approvals | Questions | Gates | API-equivalent cost |
|---|---|---|---|---|---|---|
| SI-1 | quick | 5 | 1 | 0 | 1 | $1.02 |
| SI-2 | code | 8 | 0 | 1 | 3 | $6.27 |
| SI-3 | quick | 5 | 0 | 1 | 1 | $1.67 |
| SI-4 | code | 9 | 0 | 3 | 3 | $4.85 |
| SI-5 | code | 6 | 0 | 0 | 3 | $3.86 |
| SI-6 | code | 7 | 0 | 4 | 3 | $4.84 |

The only tool approval (a multi-line commit message) was fixed after SI-1. Two of the questions came from the
watchdog false positive fixed in round 1; the others were real design decisions.

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

8. In SI-4's PR stage the AI ran the suite in the background and waited with an unbounded `until … sleep` loop:
   filed as SI-6.