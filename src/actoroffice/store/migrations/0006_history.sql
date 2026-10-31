-- Searchable work history (tickets, comments, documents, chat) for the history_search tool.
-- Kept in sync by triggers; trigger bodies stay on one line because migrations are split on ';' at line ends.
CREATE VIRTUAL TABLE history_fts USING fts5(body, kind UNINDEXED, ref UNINDEXED, conv UNINDEXED, at UNINDEXED, tokenize = 'unicode61 remove_diacritics 2');
CREATE TRIGGER history_msg AFTER INSERT ON messages BEGIN INSERT INTO history_fts(body, kind, ref, conv, at) VALUES (NEW.body, 'msg', NEW.id, NEW.conversation_id, NEW.created_at); END;
CREATE TRIGGER history_comment AFTER INSERT ON comments BEGIN INSERT INTO history_fts(body, kind, ref, conv, at) VALUES (NEW.body, 'comment', NEW.ticket_id, NULL, NEW.created_at); END;
CREATE TRIGGER history_ticket AFTER INSERT ON tickets BEGIN INSERT INTO history_fts(body, kind, ref, conv, at) VALUES (NEW.title || char(10) || NEW.description, 'ticket', NEW.id, NULL, NEW.created_at); END;
CREATE TRIGGER history_ticket_edit AFTER UPDATE OF title, description ON tickets BEGIN DELETE FROM history_fts WHERE kind = 'ticket' AND ref = OLD.id; INSERT INTO history_fts(body, kind, ref, conv, at) VALUES (NEW.title || char(10) || NEW.description, 'ticket', NEW.id, NULL, NEW.updated_at); END;
CREATE TRIGGER history_doc AFTER INSERT ON documents BEGIN INSERT INTO history_fts(body, kind, ref, conv, at) VALUES (NEW.body, 'doc', NEW.ticket_id || ':' || NEW.key, NULL, NEW.updated_at); END;
CREATE TRIGGER history_doc_edit AFTER UPDATE OF body ON documents BEGIN DELETE FROM history_fts WHERE kind = 'doc' AND ref = OLD.ticket_id || ':' || OLD.key; INSERT INTO history_fts(body, kind, ref, conv, at) VALUES (NEW.body, 'doc', NEW.ticket_id || ':' || NEW.key, NULL, NEW.updated_at); END;
INSERT INTO history_fts(body, kind, ref, conv, at) SELECT body, 'msg', id, conversation_id, created_at FROM messages;
INSERT INTO history_fts(body, kind, ref, conv, at) SELECT body, 'comment', ticket_id, NULL, created_at FROM comments;
INSERT INTO history_fts(body, kind, ref, conv, at) SELECT title || char(10) || description, 'ticket', id, NULL, created_at FROM tickets;
INSERT INTO history_fts(body, kind, ref, conv, at) SELECT body, 'doc', ticket_id || ':' || key, NULL, updated_at FROM documents;
