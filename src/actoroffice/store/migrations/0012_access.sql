-- Shared environments the AI's runs may reach (services, databases, brokers, log stores), and the time-limited
-- grants people give a ticket. Secrets are referenced by name; their values live in <data>/secrets/resources.json.
CREATE TABLE resources (
    id TEXT PRIMARY KEY,
    team_id TEXT,
    project_id TEXT,
    kind TEXT NOT NULL,
    name TEXT NOT NULL,
    env TEXT NOT NULL DEFAULT 'dev',
    config_json TEXT NOT NULL DEFAULT '{}',
    max_level TEXT NOT NULL DEFAULT 'read',
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_resources_name ON resources(COALESCE(team_id, ''), name);

CREATE TABLE grants (
    id TEXT PRIMARY KEY,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    resource_id TEXT NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    granted_by TEXT,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    envelope_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_grants_ticket ON grants(ticket_id, revoked_at);
