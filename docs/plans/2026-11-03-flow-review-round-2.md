# Flow review, round 2 (after SI-16)

Data: the self-improve office `F:/_ao_self` (tickets SI-7 to SI-16), stage timings from `stage_runs`, interactions,
failed runs, and screenshots in `F:/_ao_self/evidence/`.

## Where the time goes

| Ticket | understand | plan/scope | build | pr | close |
|---|---|---|---|---|---|
| SI-13 (code) | 4.0m | 2.4m | 20.1m | 4.2m | 1.7m |
| SI-14 (code) | 3.3m | 1.9m | 32.3m | 4.5m | 0.9m |
| SI-15 (quick) | 0.4m | 0.8m | 7.3m | 0.7m | — |
| SI-16 (code) | 4.3m | 20.8m | 233.8m | 4.5m | 1.3m |

- Build dominates. The full test suite (3–6 min, 505 tests) runs in build (`repo:test`), again in pr (`flaky_rerun`)
  and again at merge.
- SI-16 build: two turns failed at Claude start (`Control request timeout: initialize`, machine at 100% CPU) and
  waited for a person to press retry.
- People: 1–4 stage approvals, 0–4 questions, 0–1 tool approvals per ticket; one steer per ticket on average.

## Outstanding defects

| # | Defect | Evidence | Backlog |
|---|---|---|---|
| 1 | A run that fails before any token (CLI start timeout) is not retried | SI-16, 2 failed runs | 18 |
| 2 | Done tickets cannot be viewed on the web (sidebar shows 0, an open tab is empty) | `evidence/00-si16-done.png` | 19 |
| 3 | Review lows left open: SI-16 F2 (weak test), F3 (`ac_coverage` skipped outside git), SI-13 wake after done | review lists | 22 |
| 4 | The shell parser gives up on `$(`/backticks inside single quotes | SI-12 | 14 |

## Flow improvements, brainstorm to commit

| Step | Today | Improvement | Backlog |
|---|---|---|---|
| Brainstorm → tickets | People write each ticket (17 written by a script) | AI proposes a ticket list, one approval creates them in order | 21 |
| Design / UI check | "take a screenshot if you can"; no tool, no check | Screenshot tool on a temporary office, `ui_evidence` check, images on the gate card | 20 |
| Test | Full suite 3–6 min, run 3 times per ticket | Faster suite (durations, xdist) | 11 |
| Commit / branch | Ticket branch can be behind the main branch | Update the branch before PR | 10 |
| Merge | A person runs `git merge` + tests by hand | Merge button with tests on the merge result (AI never merges) | 09 |
| Queue | One ticket at a time is enforced by hand | Per-project serial queue | 08 |

## Order

18 → 11 → 09 → 10 → 19 → 20 → 21 → 22 → 08 → 14 → 12 → 07, one ticket at a time. The reviewer/approver is Claude
(in place of the user); merge stays with `merge.sh` (full suite on the merge result). Each case gets screenshots in
`F:/_ao_self/evidence/`.
