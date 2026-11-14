-- What the office already reacted to (services/docs.py `react`): one row per cause, so that a document that stays
-- "lệch" or "cũ" for the same reason is announced once, not on every sweep.
CREATE TABLE doc_notices (
  ticket_id INTEGER NOT NULL,
  doc_key TEXT NOT NULL,
  kind TEXT NOT NULL,
  cause TEXT NOT NULL,
  action TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (ticket_id, doc_key, kind, cause)
);
