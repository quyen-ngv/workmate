-- Projects own conversations (tickets already carry project_id)
ALTER TABLE conversations ADD COLUMN project_id TEXT REFERENCES projects(id);
CREATE INDEX idx_conversations_project ON conversations(project_id);
CREATE INDEX idx_tickets_project ON tickets(project_id);
