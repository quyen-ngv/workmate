-- A person may waive a failing check for one stage run: [{check, by, by_name, reason, at, via}].
-- Living on the run, a waiver ends with it (retry, reject or reopen start a new run).
ALTER TABLE stage_runs ADD COLUMN waivers_json TEXT NOT NULL DEFAULT '[]';
