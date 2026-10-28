-- The call ledger (docs/SPEC-LEDGER.md): every trade idea from every source, append-only and
-- hash-chained. Rows are never updated or deleted; a revision is a new row with parent_id set.

CREATE TABLE trade_calls (
    id              BIGSERIAL PRIMARY KEY,
    logged_at       TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    source_kind     TEXT NOT NULL,                 -- agent | strategist | caller | bot | rule | manual
    source_id       TEXT NOT NULL,                 -- e.g. strategist, a caller name, a rule id
    scope           TEXT NOT NULL,                 -- asset symbol or chain:address
    market          TEXT NOT NULL,                 -- perp | spot | dex
    direction       TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    tf              TEXT NOT NULL,                 -- timeframe whose horizon the idea targets
    entry_type      TEXT NOT NULL DEFAULT 'market',
    ref_price       DOUBLE PRECISION NOT NULL,     -- Albatross's own price at logged_at
    stop            DOUBLE PRECISION,
    targets         DOUBLE PRECISION[] NOT NULL DEFAULT '{}',
    horizon_until   TIMESTAMPTZ NOT NULL,
    reward_risk     DOUBLE PRECISION,              -- computed by code from ref/stop/first target
    confidence      DOUBLE PRECISION,
    status          TEXT NOT NULL,                 -- proposed | vetoed
    thesis          TEXT,
    evidence        JSONB,
    critique        JSONB,                         -- devil's-advocate verdict and reason
    parent_id       BIGINT REFERENCES trade_calls(id),
    agent_run_id    BIGINT REFERENCES agent_runs(id),
    prev_hash       TEXT NOT NULL,
    hash            TEXT NOT NULL UNIQUE
);
CREATE INDEX ON trade_calls (logged_at DESC);
CREATE INDEX ON trade_calls (scope, logged_at DESC);

CREATE FUNCTION trade_calls_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'trade_calls is append-only: log a new row with parent_id instead';
END $$;
CREATE TRIGGER trade_calls_no_update BEFORE UPDATE OR DELETE ON trade_calls
    FOR EACH ROW EXECUTE FUNCTION trade_calls_append_only();
CREATE TRIGGER trade_calls_no_truncate BEFORE TRUNCATE ON trade_calls
    FOR EACH STATEMENT EXECUTE FUNCTION trade_calls_append_only();
