-- Failure alerts (albatross/alerts). One row per incident: a condition that opens, stays open while
-- the monitor keeps seeing it (last_seen), and resolves when it clears. The same condition recurring
-- later is a new row, so the history reads as incidents.
CREATE TABLE alerts (
    id              BIGSERIAL PRIMARY KEY,
    key             TEXT NOT NULL,                 -- condition identity, e.g. job_failing:grader
    kind            TEXT NOT NULL,                 -- job_failing | job_stale | openrouter_402 | ...
    severity        TEXT NOT NULL,                 -- critical | warning
    title           TEXT NOT NULL,
    detail          TEXT,
    first_seen      TIMESTAMPTZ NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL,
    resolved_at     TIMESTAMPTZ
);
CREATE UNIQUE INDEX alerts_one_open_per_key ON alerts (key) WHERE resolved_at IS NULL;
CREATE INDEX ON alerts (resolved_at DESC);

-- What the notifier last told the outside world about each condition, for "at most one message per
-- alert per hour" across flapping incidents.
CREATE TABLE alert_notices (
    key             TEXT PRIMARY KEY,
    state           TEXT NOT NULL,                 -- open | resolved
    sent_at         TIMESTAMPTZ NOT NULL
);
