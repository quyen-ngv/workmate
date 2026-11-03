# Review of the self-improvement runs (SI-1..SI-6): quality, rules and backlog

Date: 2026-11-03. Data: office `F:/_ao_self` (6 tickets on this repo, Claude Opus 5.5), the merged branches, the
reviewer's findings and the check results. Decision from the owner: one ticket at a time.

## 1. How the flow ran

| Stage | Who acts | What happened in the 6 tickets |
|---|---|---|
| understand | AI → BA gate | The size check sent SI-1 and SI-3 to the quick workflow in the first run. Questions were few, multiple-choice, with code references; one (SI-6 Q1) corrected the ticket. |
| plan / scope | AI → Dev gate | Plans gave real options and an AC → test map; quick plans stayed under 15 lines. No plan was sent back. |
| build | AI, checks | Code was small and focused, every AC had a test (369 tests at the end). The reviewer found a real issue in 4 of the 4 code-workflow tickets. |
| pr | AI → merge gate | Without a code host the committed branch is the review (one run). A person merged by hand. |
| close | AI → people | 3–5 accurate knowledge items per ticket. |

## 2. What went wrong (quality)

1. **Reviewer findings were dropped.** `review_clean` fails only on `high`; `medium` passes, the summary shows only
   the first finding and the merge card does not list them. Four correct `medium` findings reached the main branch:

   | From | Still in the code |
   |---|---|
   | SI-4 | `unbind()` deletes `chat_id` in the DB after an await: a `/start` in that window loses the new group after a restart |
   | SI-2 | an auto-approved card leaves a one-time grant that is never used; after the permission narrows, that call runs once without asking |
   | SI-5 | "do not run the tests again" also applies when the PR stage fixes review comments |
   | SI-6 | a wait loop with a nested `for … done` is not detected (`wait_loop` returns False) |

   The human review (diff reading) missed them too: nobody read `check-review-clean.txt`.
2. **Two checks never ran.** No lint tool in the repo (`repo:lint` skipped; the AI ran `uvx ruff` by itself and
   left existing errors) and no coverage report (`coverage_changed_lines` skipped).
3. **Tickets ran interleaved from a stale base.** SI-5 and SI-6 conflicted in `tests/test_prompt_pack.py`; tickets
   that started before a merge worked on old code. Hence the owner's decision: one ticket at a time.
4. **Merging is manual** for a repo without a code host, and nothing re-runs the tests on the merged result.
5. **Documents are heavy**: about 25k characters per code ticket; the handoff repeats the dossier.
6. **The suite is slow** (2.5–6.5 min) and runs at least twice per ticket.

## 3. Rules written (office `F:/_ao_self`, scope `repo:actor-office`, all active)

| Id | Kind | Rule | Why |
|---|---|---|---|
| R-1 | guideline | Vietnamese for people, English for code and commits; Conventional Commits | keep what the runs already did |
| R-2 | guideline | Every medium/high review finding: fix it or record why not, before stage_complete | §2.1 (until backlog 01 enforces it) |
| R-3 | guideline | asyncio: write state before a network await, re-check after it | SI-4 race |
| R-4 | guideline | Security and permission code never weakens a decision; deny paths tested; grants have a lifecycle | SI-2 grant |
| R-5 | guideline | Prompt changes get a test on the built prompt for every workflow | SI-3/SI-5 pattern |
| R-6 | guideline | How tests are written here (Office + FakeRuntime, no real network, AC names) | consistency |
| R-7 | guideline | Shell parser changes: test nesting, quotes, multi-line, PowerShell | SI-6 nested loop |
| R-8 | guideline | Web UI: no inline styles, CSS variables, Vietnamese strings, errors as toasts | CSP, conventions |
| R-9 | check (build) | `node --check src/actoroffice/web/static/team/team.js` | JS syntax errors are not caught by pytest |
| R-10 | policy | deny `git push` | this repo is reviewed and merged locally |
| R-11 | gate (build) | changes to security/, engine/gate.py, services/access.py, services/approvals_recheck.py need a lead | permission code |

## 4. Backlog (one ticket at a time, in this order)

Ticket files: `F:/_ao_self/backlog/NN.json` (create the next one only when the previous is merged and closed).

| # | Ticket | Why first |
|---|---|---|
| 01 | Medium/high review findings must be fixed or answered before a stage passes; the gate card lists them | closes the hole that let 4 bugs through |
| 02 | Telegram `unbind` race (SI-4 finding) | escaped bug |
| 03 | No dangling grant after an auto-approval (SI-2 finding) | escaped bug, security |
| 04 | PR stage: "do not rerun tests" not when fixing review comments (SI-5 finding) | escaped bug |
| 05 | Wait-loop detector misses nested loops (SI-6 finding) | escaped bug |
| 06 | ruff in the repo, `repo:lint` runs, existing violations fixed | a check that never ran |
| 07 | Coverage report so `coverage_changed_lines` runs | a check that never ran |
| 08 | Per-project "one ticket at a time" queue | makes the owner's decision part of the product |
| 09 | Merge button for repos without a code host (tests on the merged result) | removes the manual merge, adds a post-merge test |
| 10 | Update the ticket branch with the main branch before the PR | catches conflicts in the AI's stage, not at merge |
| 11 | Faster test suite (durations, slow tests, xdist if safe) | every check waits on it |
| 12 | Less overlap between handoff and dossier | lighter documents |

## 5. Merge checklist for the person (until 01 and 09 land)

1. Read `check-review-clean.txt` of the last build run: every medium/high finding fixed or answered.
2. Read the diff against the plan; look for state changed across an `await`, grants and permissions, regexes.
3. Merge into the main branch, run the full suite on the merged result, then press "merged".
4. Restart the office only when no run is active.
