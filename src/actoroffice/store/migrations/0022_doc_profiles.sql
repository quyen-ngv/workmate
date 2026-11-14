-- What each layer changes of the document standard (services/docflow.py `DocProfile`): another pack, other rule
-- severities. Layers stack office → team → project → ticket (an epic and everything under it).
CREATE TABLE doc_profiles (
  scope TEXT PRIMARY KEY,          -- office | team:<id> | project:<id> | ticket:<id>
  profile_json TEXT NOT NULL,
  updated_by TEXT,
  updated_at TEXT NOT NULL
);
