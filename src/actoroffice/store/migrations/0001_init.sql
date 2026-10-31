-- Organisation
CREATE TABLE positions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE actors (
    id TEXT PRIMARY KEY,
    position_id TEXT NOT NULL REFERENCES positions(id),
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    config_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL
);

CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    config_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- Work
CREATE TABLE tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    project_id TEXT REFERENCES projects(id),
    type TEXT NOT NULL DEFAULT 'task',
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'medium',
    assignee_actor_id TEXT REFERENCES actors(id),
    assignee_user INTEGER NOT NULL DEFAULT 0,
    parent_id INTEGER REFERENCES tickets(id),
    created_by TEXT NOT NULL,
    checkout_run_id TEXT,
    data_class TEXT NOT NULL DEFAULT 'internal',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (NOT (assignee_actor_id IS NOT NULL AND assignee_user = 1))
);
CREATE INDEX idx_tickets_assignee ON tickets(assignee_actor_id, status);
CREATE INDEX idx_tickets_parent ON tickets(parent_id);

CREATE TABLE ticket_blockers (
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    blocker_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    PRIMARY KEY (ticket_id, blocker_id),
    CHECK (ticket_id <> blocker_id)
);

CREATE TABLE comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    author_kind TEXT NOT NULL,
    author_id TEXT,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_comments_ticket ON comments(ticket_id, id);

CREATE TABLE documents (
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    body TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (ticket_id, key)
);

CREATE TABLE document_revisions (
    ticket_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    revision INTEGER NOT NULL,
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (ticket_id, key, revision)
);

CREATE TABLE work_products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    ref TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

-- Chat
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    ticket_id INTEGER REFERENCES tickets(id),
    created_by TEXT NOT NULL,
    archived INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE participants (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    member_kind TEXT NOT NULL,
    member_id TEXT NOT NULL,
    PRIMARY KEY (conversation_id, member_kind, member_id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    author_kind TEXT NOT NULL,
    author_id TEXT,
    body TEXT NOT NULL,
    mentions_json TEXT NOT NULL DEFAULT '[]',
    provenance TEXT NOT NULL DEFAULT 'trusted',
    created_at TEXT NOT NULL
);
CREATE INDEX idx_messages_conv ON messages(conversation_id, id);

-- Execution
CREATE TABLE wake_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL REFERENCES actors(id),
    ticket_id INTEGER REFERENCES tickets(id),
    conversation_id TEXT REFERENCES conversations(id),
    source TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'pending',
    coalesced INTEGER NOT NULL DEFAULT 0,
    not_before TEXT,
    run_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_wake_pending ON wake_requests(status, id);

CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(id),
    kind TEXT NOT NULL,
    ticket_id INTEGER REFERENCES tickets(id),
    conversation_id TEXT REFERENCES conversations(id),
    status TEXT NOT NULL,
    session_before TEXT,
    session_after TEXT,
    pid INTEGER,
    usage_json TEXT NOT NULL DEFAULT '{}',
    cost_usd REAL NOT NULL DEFAULT 0,
    tainted INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX idx_runs_status ON runs(status);

CREATE TABLE sessions (
    key TEXT PRIMARY KEY,
    actor_id TEXT NOT NULL REFERENCES actors(id),
    session_id TEXT NOT NULL,
    cwd TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_run_events_run ON run_events(run_id, id);

-- Human in the loop
CREATE TABLE interactions (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    actor_id TEXT REFERENCES actors(id),
    ticket_id INTEGER REFERENCES tickets(id),
    run_id TEXT REFERENCES runs(id),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT,
    resolved_by TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX idx_interactions_pending ON interactions(status, created_at);

CREATE TABLE approval_grants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    ticket_id INTEGER,
    tool TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    decision TEXT NOT NULL,
    interaction_id TEXT NOT NULL REFERENCES interactions(id),
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_grants_lookup ON approval_grants(actor_id, tool, input_hash, used);

CREATE TABLE locks (
    name TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

-- Safety
CREATE TABLE audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor_id TEXT,
    run_id TEXT,
    ticket_id INTEGER,
    action TEXT NOT NULL,
    target TEXT,
    detail_json TEXT NOT NULL DEFAULT '{}',
    outcome TEXT NOT NULL
);
CREATE INDEX idx_audit_ts ON audit_log(ts);
