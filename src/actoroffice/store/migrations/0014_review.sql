-- Review findings of a stage run with their ids and the AI's replies (SI-7): {"next": n, "findings": [...]}.
ALTER TABLE stage_runs ADD COLUMN review_json TEXT NOT NULL DEFAULT '{}';
