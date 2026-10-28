-- The culling system (albatross/agents/cull.py; docs/PLAN-LEARNING-LOOP.md section 7): each agent's rung on the
-- ladder (active, watch, probation, paused, retired) and every weekly review that put it there. Effects live in
-- albatross/controls.py (budget, pause, the paused agents' 10% sample) and the ensemble (weight 0 from probation on).
CREATE TABLE IF NOT EXISTS agent_status (
    agent       TEXT PRIMARY KEY,
    rung        TEXT NOT NULL DEFAULT 'active'
                CHECK (rung IN ('active', 'watch', 'probation', 'paused', 'retired')),
    streak      INT NOT NULL DEFAULT 0,             -- counted reviews in a row without skill
    since       TIMESTAMPTZ NOT NULL DEFAULT now(), -- on this rung since
    reason      TEXT,
    counted_at  TIMESTAMPTZ,                        -- the last review that counted toward the ladder
    set_by      TEXT NOT NULL DEFAULT 'review',     -- review | owner
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS agent_reviews (
    id          BIGSERIAL PRIMARY KEY,
    reviewed_at TIMESTAMPTZ NOT NULL,
    agent       TEXT NOT NULL,
    metric      TEXT NOT NULL,          -- direction | trades | critic | unreviewed | manual
    verdict     TEXT NOT NULL,          -- earns | no_skill | harmful | too_early | unreviewed | manual
    value       DOUBLE PRECISION,       -- skill (hit-rate points over the fair baseline) or mean R a day
    band        DOUBLE PRECISION,       -- half-width of its 95% interval
    n           DOUBLE PRECISION,       -- effective samples: graded windows / 6, or days
    cost_usd    DOUBLE PRECISION,       -- API spend, last 7 days
    pool_usd    DOUBLE PRECISION,       -- subscription allowance used, last 7 days
    counted     BOOLEAN NOT NULL,
    rung_before TEXT,
    rung_after  TEXT NOT NULL,
    detail      JSONB
);
CREATE INDEX IF NOT EXISTS agent_reviews_agent ON agent_reviews (agent, reviewed_at DESC);
