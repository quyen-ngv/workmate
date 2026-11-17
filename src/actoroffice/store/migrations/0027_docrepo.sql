-- The documents repository (services/docrepo.py): what the office last wrote to, or read from, each file of the git
-- folder that mirrors its documents. A file whose content no longer matches `sha` was changed outside the office.
CREATE TABLE docrepo_files (
  path TEXT PRIMARY KEY,             -- relative to the repository root, forward slashes
  kind TEXT NOT NULL,                -- doc | ticket | kb | index | library
  ticket_id INTEGER,                 -- doc, ticket
  doc_key TEXT,                      -- doc
  revision INTEGER,                  -- doc: the revision the file holds
  kb_id INTEGER,                     -- kb
  sha TEXT NOT NULL,                 -- sha256 of the file as the office last wrote or read it
  size INTEGER NOT NULL DEFAULT 0,
  mtime_ns INTEGER NOT NULL DEFAULT 0,
  synced_at TEXT NOT NULL
);
CREATE INDEX idx_docrepo_ticket ON docrepo_files(ticket_id);
CREATE INDEX idx_docrepo_kb ON docrepo_files(kb_id);

-- Full-text index of every text file of the repository, one row per heading: what `docs_search` reads. Rebuilt from the
-- files; nothing here is a source of truth.
CREATE VIRTUAL TABLE docrepo_fts USING fts5(path UNINDEXED, heading, text, line UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
