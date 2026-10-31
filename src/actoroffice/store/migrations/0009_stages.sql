-- Team copilot: tickets go through stages (understand → plan → build → pr → close) with human gates.
ALTER TABLE tickets ADD COLUMN team_id TEXT;
ALTER TABLE tickets ADD COLUMN stage TEXT;
ALTER TABLE tickets ADD COLUMN stage_template TEXT;
ALTER TABLE tickets ADD COLUMN owner_user_id TEXT;
ALTER TABLE tickets ADD COLUMN mode TEXT NOT NULL DEFAULT 'wait';
ALTER TABLE tickets ADD COLUMN paused INTEGER NOT NULL DEFAULT 0;
ALTER TABLE tickets ADD COLUMN jira_key TEXT;
ALTER TABLE tickets ADD COLUMN jira_url TEXT;
ALTER TABLE tickets ADD COLUMN pr_url TEXT;
ALTER TABLE tickets ADD COLUMN confluence_url TEXT;
CREATE UNIQUE INDEX idx_tickets_jira ON tickets(jira_key) WHERE jira_key IS NOT NULL;
CREATE INDEX idx_tickets_team ON tickets(team_id, stage);

CREATE TABLE stage_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    stage TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    session_id TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    summary TEXT,
    usage_json TEXT NOT NULL DEFAULT '{}',
    cost_usd REAL NOT NULL DEFAULT 0,
    rules_json TEXT NOT NULL DEFAULT '[]',
    checks_json TEXT NOT NULL DEFAULT '[]',
    steps_json TEXT NOT NULL DEFAULT '[]'
);
CREATE INDEX idx_stage_runs_ticket ON stage_runs(ticket_id, id);

ALTER TABLE documents ADD COLUMN approved_revision INTEGER;
ALTER TABLE runs ADD COLUMN stage_run_id INTEGER;
ALTER TABLE runs ADD COLUMN purpose TEXT;
