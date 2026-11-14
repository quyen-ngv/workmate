-- Events pushed by code hosts to POST /hooks/<connection id> (services/webhooks.py). Only what the office acts on is
-- kept (kind, repository, branch, commit, pull request), never the whole payload.
CREATE TABLE webhook_events (
  id INTEGER PRIMARY KEY,
  connection_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  received_at TEXT NOT NULL,
  handled_at TEXT,
  result TEXT
);
CREATE INDEX idx_webhook_events_received ON webhook_events (received_at);
