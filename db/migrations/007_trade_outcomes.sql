-- Backtester outcomes (docs/SPEC-LEDGER.md §4): one row per ledger call per exit policy. Unlike the
-- ledger these are derived numbers, recomputed by the grade_ideas job until the call resolves, and the
-- whole table can be rebuilt from trade_calls + candles at any time.
--
-- open rows: exit_reason/exit_price/exit_at are NULL and the returns are unrealised (marked to the last
-- closed candle). ungradeable rows: every number is NULL; exit_reason says why.
CREATE TABLE trade_call_outcomes (
    call_id         BIGINT NOT NULL REFERENCES trade_calls(id),
    policy          TEXT NOT NULL,                 -- stop_target | horizon
    status          TEXT NOT NULL,                 -- open | graded | ungradeable
    exit_reason     TEXT,                          -- stop | target | horizon | no_candles | no_dex_cost_model | ...
    exit_price      DOUBLE PRECISION,              -- market level of the exit, before costs
    net_return_pct  DOUBLE PRECISION,              -- signed to the call, after fees, slippage and funding
    r_multiple      DOUBLE PRECISION,              -- net_return_pct / risk_pct
    mfe_pct         DOUBLE PRECISION,              -- best excursion from the entry fill while held
    mae_pct         DOUBLE PRECISION,              -- worst excursion (negative = against the call)
    graded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    exit_at         TIMESTAMPTZ,                   -- close of the exit candle
    cost_pct        DOUBLE PRECISION,              -- round-trip taker fees + slippage charged
    funding_pct     DOUBLE PRECISION,              -- funding paid by the call (+ = cost); NULL = no data
    risk_pct        DOUBLE PRECISION,              -- entry fill -> stop distance
    long_net_pct    DOUBLE PRECISION,              -- always-long over the same window, same costs
    short_net_pct   DOUBLE PRECISION,              -- always-short over the same window, same costs
    candles         TEXT,                          -- price source walked, e.g. hyperliquid:5m
    PRIMARY KEY (call_id, policy)
);
CREATE INDEX ON trade_call_outcomes (status);
