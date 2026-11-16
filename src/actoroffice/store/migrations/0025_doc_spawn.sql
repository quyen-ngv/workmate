-- Doc Core, gate hardening (services/docs.py, services/stages.py).

-- Which child ticket a breakdown item became, and which items the approver dropped. A child is found by this link, not
-- by its description; a dropped item stays dropped at the next approval unless the approver restores it.
CREATE TABLE doc_spawned (
  parent_id INTEGER NOT NULL,
  item_id TEXT NOT NULL,
  child_id INTEGER,
  dropped INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  PRIMARY KEY (parent_id, item_id)
);

-- What an acceptance was about: the text of the item (and, for a stale source, the content it was accepted against).
-- An acceptance whose hash no longer matches is ignored. NULL: accepted before this column existed, kept.
ALTER TABLE doc_findings ADD COLUMN item_hash TEXT;

-- Remarks an approver leaves on one item of a document when answering a gate card.
CREATE TABLE doc_comments (
  id INTEGER PRIMARY KEY,
  ticket_id INTEGER NOT NULL,
  interaction_id TEXT,
  doc_key TEXT NOT NULL,
  item_id TEXT,
  text TEXT NOT NULL,
  by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  reply TEXT
);
CREATE INDEX idx_doc_comments_ticket ON doc_comments(ticket_id, resolved_at);
