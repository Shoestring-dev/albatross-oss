-- The strategy lab (2026-10-09).
-- Every strategy ever tried, memecoin call filters and factor-rank variants alike, with where it came from (seed,
-- random, a mutation or crossover of earlier ones, the strategy researcher), its results on older data (discovery)
-- and on recent weeks it was never fitted to (holdout), and once it trades on paper, its forward record.
-- status: queued | rejected | survivor | paper | champion | retired. A config is tested once (config_hash).
CREATE TABLE IF NOT EXISTS lab_strategies (
    id           BIGSERIAL PRIMARY KEY,
    family       TEXT NOT NULL,                 -- meme | factor
    config       JSONB NOT NULL,
    config_hash  TEXT NOT NULL UNIQUE,
    name         TEXT NOT NULL,
    origin       TEXT NOT NULL,                 -- seed | random | mutation | crossover | researcher
    parent_ids   BIGINT[] NOT NULL DEFAULT '{}',
    idea         TEXT,                          -- the researcher's reason for proposing it
    status       TEXT NOT NULL DEFAULT 'queued',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    tested_at    TIMESTAMPTZ,
    discovery    JSONB,                         -- {n, mean, se, t, days}
    holdout      JSONB,
    forward      JSONB,
    t_needed     DOUBLE PRECISION,              -- the bar when it was tested: it rises with the number tried
    score        DOUBLE PRECISION,              -- discovery t: ranks parents and candidates for paper
    paper_at     TIMESTAMPTZ,
    decided_at   TIMESTAMPTZ,
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS lab_strategies_family_status ON lab_strategies (family, status);
CREATE INDEX IF NOT EXISTS lab_strategies_tested ON lab_strategies (tested_at DESC);
