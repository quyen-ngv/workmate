-- Run events get a per-run sequence (UIs replay from it after a reconnect) and a normalized activity line.
ALTER TABLE run_events ADD COLUMN seq INTEGER;
ALTER TABLE run_events ADD COLUMN activity_json TEXT;
UPDATE run_events SET seq = id;
CREATE INDEX idx_run_events_seq ON run_events(run_id, seq);
