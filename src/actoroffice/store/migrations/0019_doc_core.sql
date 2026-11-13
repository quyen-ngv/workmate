-- Doc Core (docs/specs/2026-11-13-doc-core.md): what a document revision was written from, the sources it pinned and
-- the findings people accepted. Items and trace links are parsed from the document when needed, never stored.

-- The revisions of the documents this one was written from, captured when the revision is saved.
ALTER TABLE documents ADD COLUMN baseline_json TEXT;
-- The baseline of the revision a gate approved: "approved design r4, written from spec r2".
ALTER TABLE documents ADD COLUMN approved_baseline_json TEXT;

-- One row per `Nguồn:` of an item, pinned the first time the revision is checked: the content it pointed to then.
CREATE TABLE doc_evidence (
  id INTEGER PRIMARY KEY,
  ticket_id INTEGER NOT NULL,
  doc_key TEXT NOT NULL,
  revision INTEGER NOT NULL,
  item_id TEXT NOT NULL,
  source TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  ref TEXT NOT NULL DEFAULT '',
  locator TEXT NOT NULL DEFAULT '',
  version TEXT NOT NULL DEFAULT '',
  content_hash TEXT NOT NULL DEFAULT '',
  excerpt TEXT NOT NULL DEFAULT '',
  error TEXT,
  fetched_at TEXT NOT NULL,
  UNIQUE (ticket_id, doc_key, revision, item_id, source)
);

-- A finding somebody with the right role accepted instead of having it fixed. Finding ids are stable across runs.
CREATE TABLE doc_findings (
  ticket_id INTEGER NOT NULL,
  finding_id TEXT NOT NULL,
  rule TEXT NOT NULL,
  doc_key TEXT NOT NULL,
  item_id TEXT,
  message TEXT NOT NULL,
  accepted_by TEXT NOT NULL,
  reason TEXT NOT NULL,
  accepted_at TEXT NOT NULL,
  PRIMARY KEY (ticket_id, finding_id)
);
