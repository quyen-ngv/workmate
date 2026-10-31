---
name: data-analysis
description: Steps and checks for answering business questions with data from a database or files (db_schema, db_query, reproducible scripts). Use for any analysis task.
---

# Data analysis

1. Pin down the question: metric definitions (e.g. "doanh thu" = sum of paid orders minus refunds?), period,
   time zone, segments, filters. Ask when the definition changes the answer.
2. Explore: `db_schema` to see tables and row counts; sample a few rows with `db_query ... LIMIT 5`; check
   keys and nulls before joining.
3. Compute in SQL with aggregates (GROUP BY, window functions); keep each query small and named in your notes.
   For multi-step work write a script (PEP 723, `uv run`) that writes CSV results — reproducible.
4. Validate: totals reconcile with a simple independent query; no fan-out from joins (compare COUNT(*) vs
   COUNT(DISTINCT id)); outliers explained; percentages add up.
5. Report (doc-standards report template): answer first, a small table of key numbers, the exact queries,
   limitations (data quality, missing periods), and what the numbers do NOT prove (correlation ≠ cause).
6. Recurring: make the script an automation (automation-playbook).
