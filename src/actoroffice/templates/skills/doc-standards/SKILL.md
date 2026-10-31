---
name: doc-standards
description: Office templates for ticket documents (requirements, design, plan, meeting-notes, test-plan, test-report, change-request, report, acceptance). Read before writing any of them.
---

# Document standards

Every ticket document uses the template of its key, in the office language, in Markdown. Keep the section
headings even when a section is short ("Không có" is a valid answer). Read the matching template file:

| Document key | Who writes it | Template file |
|---|---|---|
| `requirements` | BA | templates/requirements.md |
| `design` | Dev Lead | templates/design.md |
| `plan` | Dev Lead | templates/plan.md |
| `meeting-notes` | whoever ran the meeting | templates/meeting-notes.md |
| `test-plan` / `test-report` | QA | templates/test.md |
| `change-request` | BA (or whoever records the change) | templates/change-request.md |
| `acceptance` | BA | templates/acceptance.md |
| `report`, `memo`, `status-report` | researcher, analyst, consultant, PM | templates/report.md |
| `handoff` | Developer | templates/handoff.md |

Rules for every document:
- First line: `# <Document> — <ticket key> <title>` then `Phiên bản: N · Cập nhật: YYYY-MM-DD · Người viết: <name>`.
- Requirements and plans end with a **Change log** table (version, date, change, by). Never delete history:
  mark superseded items with ~~strike~~ and say why.
- IDs: user stories `US-1`, acceptance criteria `AC-1.1`, tasks `T-1`, test cases `TC-1`, risks `R-1`,
  decisions `D-1`. Reference IDs across documents (traceability).
- Dates are absolute (YYYY-MM-DD), estimates in hours, owners by name.
- Facts you were not given are marked as **Assumption** and listed in the assumptions section.
