-- Jev trade scout (albatross/agents/jev_scout.py): every candidate trade Jev scored, one row per coin,
-- timeframe, side and candle (the first score in that candle), graded later with the backtester's simulator
-- so Jev's probabilities can be checked against what happened (calibration), triggered or not.
CREATE TABLE jev_scores (
    id              BIGSERIAL PRIMARY KEY,
    scored_at       TIMESTAMPTZ NOT NULL,
    bar_ts          TIMESTAMPTZ NOT NULL,          -- the candle it was scored in
    symbol          TEXT NOT NULL,
    tf              TEXT NOT NULL,
    direction       TEXT NOT NULL CHECK (direction IN ('long', 'short')),
    entry           DOUBLE PRECISION NOT NULL,
    stop            DOUBLE PRECISION NOT NULL,
    target          DOUBLE PRECISION NOT NULL,
    horizon_until   TIMESTAMPTZ NOT NULL,
    p_win           DOUBLE PRECISION NOT NULL,     -- Jev: P(target before stop within the horizon)
    ev_r            DOUBLE PRECISION NOT NULL,     -- p x reward:risk - (1 - p) - round-trip cost, in R
    our_view        JSONB,                         -- what our own work said about this coin and timeframe
    triggered       BOOLEAN NOT NULL DEFAULT false,
    call_id         BIGINT REFERENCES trade_calls(id),   -- the paper trade, when it was triggered
    run_id          BIGINT,
    outcome         TEXT,                          -- graded | ungradeable (NULL until the horizon has passed)
    exit_reason     TEXT,                          -- target | stop | horizon | ...
    r_multiple      DOUBLE PRECISION,
    net_return_pct  DOUBLE PRECISION,
    graded_at       TIMESTAMPTZ,
    UNIQUE (symbol, tf, direction, bar_ts)
);
CREATE INDEX ON jev_scores (scored_at);
CREATE INDEX ON jev_scores (horizon_until) WHERE outcome IS NULL;
