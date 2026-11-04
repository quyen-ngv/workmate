-- Why a ticket waits for another (SI-31): 'dep' = a real dependency set by a person or the AI,
-- 'queue' = the project runs one ticket at a time and this ticket waits its turn.
ALTER TABLE ticket_blockers ADD COLUMN kind TEXT NOT NULL DEFAULT 'dep';
