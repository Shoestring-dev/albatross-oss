-- The output tracker (albatross/api/output.py, the System > Output page): every job run with what it produced,
-- and the controls the owner adjusts from that page.
--   job_runs        one row per run of every tracked job (ingest container and host worker): ok | failed |
--                   paused (skipped: paused on the Output page) | overlap (skipped: the last run still going)
--   agent_controls  pause a job or a model agent, or scale its budget; read by every job run and model call
CREATE TABLE IF NOT EXISTS job_runs (
    job         TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL,
    seconds     REAL,
    status      TEXT NOT NULL CHECK (status IN ('ok', 'failed', 'paused', 'overlap')),
    rows        INT,
    detail      JSONB,                         -- a job's own breakdown when it returns one: {"new": .., "changed": ..}
    error       TEXT
);
SELECT create_hypertable('job_runs', 'started_at', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs (job, started_at DESC);
SELECT add_retention_policy('job_runs', INTERVAL '30 days', if_not_exists => TRUE);

CREATE TABLE IF NOT EXISTS agent_controls (
    key           TEXT PRIMARY KEY,             -- a job name (ingest_health) or a model agent (agent_runs.agent)
    paused        BOOLEAN NOT NULL DEFAULT false,
    budget_scale  REAL NOT NULL DEFAULT 1.0 CHECK (budget_scale >= 0 AND budget_scale <= 5),
    note          TEXT,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- repeat detection groups an agent's calls by input fingerprint
CREATE INDEX IF NOT EXISTS agent_runs_agent_input ON agent_runs (agent, input_hash, started_at DESC);
