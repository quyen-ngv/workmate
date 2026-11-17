-- Typed links between tickets (docs/specs/2026-11-16-doc-graph-and-repo.md): an epic's stories among themselves, a
-- story and a ticket of another epic. `origin = 'doc'` rows are read from the documents (`Phụ thuộc: T-1`,
-- `Liên quan: AO-14/C-2`) and rewritten whenever those are saved; `origin = 'manual'` rows are added by people.
CREATE TABLE ticket_links (
  id INTEGER PRIMARY KEY,
  from_id INTEGER NOT NULL,
  to_id INTEGER NOT NULL,
  relation TEXT NOT NULL,            -- a name of the pack's syntax.relations: depends_on | relates | supersedes | …
  origin TEXT NOT NULL DEFAULT 'manual',
  to_item TEXT NOT NULL DEFAULT '',  -- the item of the other ticket the link names (`C-2`), '' for the ticket as a whole
  detail TEXT NOT NULL DEFAULT '',   -- where it is written: "design C-3", "AO-12/breakdown T-2"
  created_by TEXT NOT NULL DEFAULT 'office',
  created_at TEXT NOT NULL
);
CREATE INDEX idx_ticket_links_from ON ticket_links(from_id);
CREATE INDEX idx_ticket_links_to ON ticket_links(to_id);
