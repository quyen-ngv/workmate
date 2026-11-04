-- Sources of a ticket (SI-39): ticket keys, Jira keys, repo files and URLs named in its description, and the people's
-- comments its first run received. "read" comes from the run activity only; "skipped" from the source_skip tool.
CREATE TABLE ticket_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    locator TEXT NOT NULL DEFAULT '',
    position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unread',
    reason TEXT,
    error TEXT,
    done_at TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(ticket_id, kind, ref, locator)
);
CREATE INDEX idx_ticket_sources_ticket ON ticket_sources(ticket_id, active, position);
-- NULL: the description was never split into sources (tickets created before this migration)
ALTER TABLE tickets ADD COLUMN sources_at TEXT;
