-- Connections to outside systems (Jira, Confluence, Bitbucket, GitHub…): which adapter, for whom, with what settings.
-- The secret is never here: only the name of its file in <data_dir>/secrets/.
CREATE TABLE connections (
  id TEXT PRIMARY KEY,
  use TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'office',
  config_json TEXT NOT NULL DEFAULT '{}',
  secret_ref TEXT,
  enabled INTEGER NOT NULL DEFAULT 1,
  can_write INTEGER NOT NULL DEFAULT 0,
  health_json TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- What was fetched from a connection, kept by version so that an unchanged document costs no request.
CREATE TABLE source_cache (
  connection_id TEXT NOT NULL,
  ref TEXT NOT NULL,
  version TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  body TEXT NOT NULL,
  meta_json TEXT NOT NULL DEFAULT '{}',
  fetched_at TEXT NOT NULL,
  PRIMARY KEY (connection_id, ref)
);
