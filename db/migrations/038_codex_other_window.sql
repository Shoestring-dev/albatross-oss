-- 037 gained other_percent after it was first written; a database that ran the first version gets it here.
ALTER TABLE codex_limits ADD COLUMN IF NOT EXISTS other_percent DOUBLE PRECISION;
