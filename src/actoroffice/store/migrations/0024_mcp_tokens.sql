-- Personal tokens for the office as a read-only MCP server (web/mcp_api.py): a developer's IDE sends one as a bearer
-- token. Only the SHA-256 of a token is kept; the value is shown once, when it is created.
CREATE TABLE mcp_tokens (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name TEXT NOT NULL DEFAULT '',
    token_hash TEXT NOT NULL UNIQUE,
    team_id TEXT,                    -- the team a call is answered for when it names no ticket (NULL: the user's only team)
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX idx_mcp_tokens_user ON mcp_tokens(user_id);
