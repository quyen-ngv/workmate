-- Rounds of the AI cross-check of documents (services/crosscheck.py): what one round found, keyed by the content it
-- read so that the same documents are never cross-checked twice.
CREATE TABLE doc_crosschecks (
  id INTEGER PRIMARY KEY,
  ticket_id INTEGER NOT NULL,
  stage_run_id INTEGER NOT NULL,
  input_hash TEXT NOT NULL,
  findings_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_doc_crosschecks_run ON doc_crosschecks (stage_run_id, id);
