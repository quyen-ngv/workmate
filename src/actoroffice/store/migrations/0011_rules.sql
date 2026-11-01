-- Team rules (files under <data>/rules, versioned with git; this table is the index).
CREATE TABLE rules (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    kind TEXT NOT NULL,
    scope TEXT NOT NULL,
    stage TEXT NOT NULL DEFAULT 'any',
    status TEXT NOT NULL DEFAULT 'draft',
    owner TEXT,
    created_by TEXT,
    approved_by TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    spec_json TEXT NOT NULL DEFAULT '{}',
    body TEXT NOT NULL DEFAULT '',
    source TEXT,
    path TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_rules_scope ON rules(scope, status);
CREATE UNIQUE INDEX idx_rules_source ON rules(scope, source) WHERE source IS NOT NULL;

CREATE TABLE rule_versions (
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    spec_json TEXT NOT NULL,
    status TEXT NOT NULL,
    author TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (rule_id, version)
);

CREATE VIRTUAL TABLE rules_fts USING fts5(title, body, rule_id UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
