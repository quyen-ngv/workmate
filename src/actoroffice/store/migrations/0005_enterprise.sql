-- Enterprise flow: due dates, automations (flows of steps on a schedule), per-run work stats
ALTER TABLE tickets ADD COLUMN due_at TEXT;
ALTER TABLE runs ADD COLUMN stats_json TEXT;

CREATE TABLE automations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    owner_actor_id TEXT REFERENCES actors(id),
    project_id TEXT,
    conversation_id TEXT,               -- where it posts
    ticket_id INTEGER,                  -- the ticket that created it
    steps_json TEXT NOT NULL,           -- [{"type": "notify" | "script" | "agent", ...}]
    schedule TEXT,                      -- repeat rule; NULL = once (next_at) or manual
    next_at TEXT,
    status TEXT NOT NULL,               -- pending_review | enabled | disabled | finished
    failures INTEGER NOT NULL DEFAULT 0, -- consecutive failures
    approved_sha TEXT,                  -- fingerprint (steps + script code) that may run
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_automations_due ON automations(status, next_at);

CREATE TABLE automation_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
    trigger TEXT NOT NULL,              -- schedule | manual | actor
    status TEXT NOT NULL,               -- running | succeeded | failed
    exit_code INTEGER,
    output TEXT,
    error TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER
);
CREATE INDEX idx_automation_runs ON automation_runs(automation_id, id);
