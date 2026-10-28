-- A job with nothing to run with yet (no key or account set up) is recorded as 'skipped', never 'failed'
-- (albatross/ingest/health.py), so a fresh install with no keys raises no alerts.
ALTER TABLE job_runs DROP CONSTRAINT IF EXISTS job_runs_status_check;
ALTER TABLE job_runs ADD CONSTRAINT job_runs_status_check CHECK (status IN ('ok', 'failed', 'paused', 'overlap', 'skipped'));
