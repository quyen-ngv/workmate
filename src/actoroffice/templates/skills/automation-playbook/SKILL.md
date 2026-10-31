---
name: automation-playbook
description: How to design an automation (a flow of notify / script / agent steps on a schedule) for work that repeats, happens at a set time, or needs watching. Use whenever a request mentions time or repetition.
---

# Automation playbook

## 1. Decide whether it is an automation
Signals: "mỗi ngày / thứ 2 hằng tuần / cuối tháng", "nhắc tôi", "lúc 9h", "khi … thì báo", "theo dõi", a task the
user asks for a second time. One-off work stays a ticket.

## 2. Design the flow with the cheapest steps
| Need | Step | Example |
|---|---|---|
| Tell someone something at a time | `notify` | reminder, deadline nudge, daily agenda |
| Deterministic data work | `script` | fetch prices, pull rows from a DB, compute a KPI, check a URL |
| Judgment, writing, analysis | `agent` | "Linh: comment on this week's numbers", "Lan: draft the newsletter" |

Combine steps; a step's printed output is `{output}` for the next one:
- reminder: `[notify "Anh ơi, 9:00 họp giao ban ở phòng 3"]`, schedule `weekly:mon@08:45`
- data drop: `[script fetch.py]`, schedule `daily@07:30` (the office posts the summary)
- report: `[script kpi.py, notify "KPI tuần: {output}"]`
- analysed report: `[script kpi.py, agent Linh "Nhận xét số liệu tuần, nêu 3 điểm cần chú ý"]`
- watch: `[script check_price.py]` every:60 that prints only when something changed, `[notify "{output}"]`

Avoid an `agent` step when a script or a message is enough: it spends an employee's time on every run.

## 3. Scripts (for `script` steps)
- Write `<name>.py` in your working directory, with a PEP 723 header for dependencies:
  ```
  # /// script
  # requires-python = ">=3.11"
  # dependencies = ["httpx"]
  # ///
  ```
- Write files to `Path(os.environ.get("AO_OUTPUT_DIR", "out"))` (UTF-8). The script runs with that folder as its working directory; read files that sit next to the script via `Path(__file__).parent`. Re-running must not duplicate data:
  dedupe by key, or write one dated file per run.
- Be polite to sources: set timeouts and a User-Agent, keep delays small, and respect robots.txt and API limits.
- Print a 1-5 line summary, which becomes `{output}`. Exit 0 on success. On failure, exit non-zero with a clear
  message on stderr.
- No secrets in the code, and no personal data unless the ticket requires it.
- Test it with `uv run <name>.py`, twice, and check the output files.

## 4. Save and confirm
`automation_save(name, description, steps, when?, schedule?)`.
- Flows with only `notify` steps start right away.
- Flows with `script` or `agent` steps wait until the user reviews them; the review card shows the code.
- Failures are posted in the chat, and a fix ticket is opened for you.

Tell the user in one or two sentences what the flow does and when it runs. To cancel a flow, use
`automation_stop`. To answer "what is scheduled", use `automation_list`.
