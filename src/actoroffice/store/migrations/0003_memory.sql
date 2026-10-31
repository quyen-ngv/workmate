-- Long-term memory per actor (scope: actor | position | shared)
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT REFERENCES actors(id),
    position_id TEXT REFERENCES positions(id),
    scope TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_by TEXT NOT NULL,
    source_ticket_id INTEGER REFERENCES tickets(id),
    use_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    deleted_at TEXT
);
CREATE INDEX idx_memories_actor ON memories(actor_id, deleted_at);
CREATE INDEX idx_memories_position ON memories(position_id, deleted_at);
CREATE VIRTUAL TABLE memories_fts USING fts5(content, tokenize = 'unicode61 remove_diacritics 2');
