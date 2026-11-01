-- Model per task and per stage, plus one pending switch (applied at a run or stage boundary).
-- stage_models is JSON {stage: model}; pending_model is JSON {model, scope, stage, by, at, stage_run_id};
-- runs.model is the model a run actually used.
ALTER TABLE tickets ADD COLUMN model TEXT;
ALTER TABLE tickets ADD COLUMN stage_models TEXT;
ALTER TABLE tickets ADD COLUMN pending_model TEXT;
ALTER TABLE runs ADD COLUMN model TEXT;
