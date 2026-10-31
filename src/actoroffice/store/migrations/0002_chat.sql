-- Chat ↔ tickets, typed messages
ALTER TABLE tickets ADD COLUMN origin_conversation_id TEXT REFERENCES conversations(id);
ALTER TABLE messages ADD COLUMN kind TEXT NOT NULL DEFAULT 'text';
ALTER TABLE messages ADD COLUMN meta_json TEXT NOT NULL DEFAULT '{}';
ALTER TABLE conversations ADD COLUMN updated_at TEXT;
CREATE INDEX idx_participants_member ON participants(member_kind, member_id);
CREATE INDEX idx_tickets_origin ON tickets(origin_conversation_id);
