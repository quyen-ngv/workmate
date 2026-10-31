-- Team copilot: real people (BA, Dev, QE…) in teams, and the people taking part in each ticket.
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT,
    telegram_id INTEGER,
    default_roles_json TEXT NOT NULL DEFAULT '[]',
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE UNIQUE INDEX idx_users_telegram ON users(telegram_id) WHERE telegram_id IS NOT NULL;
CREATE UNIQUE INDEX idx_users_name ON users(name COLLATE NOCASE);

CREATE TABLE teams (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    jira_project_key TEXT,
    confluence_space TEXT,
    confluence_parent_id TEXT,
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE memberships (
    team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    roles_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (team_id, user_id)
);

CREATE TABLE ticket_participants (
    ticket_id INTEGER NOT NULL REFERENCES tickets(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    roles_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY (ticket_id, user_id)
);
