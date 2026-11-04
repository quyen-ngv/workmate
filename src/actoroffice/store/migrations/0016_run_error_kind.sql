-- What kind of failure ended a run (SI-38): network | runtime_start | command_timeout | ai; NULL when it did not fail.
ALTER TABLE runs ADD COLUMN error_kind TEXT;
