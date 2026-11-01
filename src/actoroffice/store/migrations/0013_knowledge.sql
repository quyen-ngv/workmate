-- Team knowledge base: typed items with sources, anchors and a verification state.
CREATE TABLE kb_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    team_id TEXT,
    project_id TEXT,
    kind TEXT NOT NULL,              -- fact | code_note | decision | contract | flow
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'draft',   -- draft | verified | stale | rejected
    sources_json TEXT NOT NULL DEFAULT '[]',
    anchor_json TEXT,                -- {"path": "...", "commit": "..."} or {"paths": [...]} / {"spec": ..., "version": ...}
    ticket_id INTEGER,
    created_by TEXT NOT NULL,
    verified_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_kb_team ON kb_items(team_id, status);
CREATE INDEX idx_kb_ticket ON kb_items(ticket_id);
CREATE VIRTUAL TABLE kb_fts USING fts5(title, content, tokenize = 'unicode61 remove_diacritics 2');
