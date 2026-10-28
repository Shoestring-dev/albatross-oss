-- The FOMO subscription test (albatross/deepend/fomo_verdict.py): each token's first FOMO buy gets a matched random
-- entry (same chain, same minute, liquidity within +-50%, same age band), graded by the ledger's own DEX simulation;
-- the daily report aggregates FOMO against Telegram calls and the random baseline.

CREATE TABLE IF NOT EXISTS de_fomo_baselines (
    call_id         BIGINT PRIMARY KEY,                -- trade_calls.id of the token's first FOMO buy
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    logged_at       TIMESTAMPTZ NOT NULL,
    band            TEXT,                              -- the buyer's rank band then: 1-10 | 11-50 | 51-150
    age_band        TEXT,
    liquidity_usd   DOUBLE PRECISION,
    candidates      INTEGER NOT NULL,                  -- tokens that matched; the picks are drawn from these
    picks           JSONB NOT NULL,                    -- [{token, price, liquidity_usd, status, net_24h_pct, mfe_pct}]
    net_24h_pct     DOUBLE PRECISION,                  -- the graded picks' mean
    hit_2x          DOUBLE PRECISION,                  -- the graded picks' share reaching 2x
    computed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS de_fomo_verdict (
    day             DATE PRIMARY KEY,
    computed_at     TIMESTAMPTZ NOT NULL,
    report          JSONB NOT NULL
);

-- Reddit (albatross/deepend/reddit.py): 'unverified' = the model claimed threads but none passed the evidence gate.
-- Stored for the audit, never served (only 'read' and 'none' are), retried after the back-off like a failure.
ALTER TABLE de_reddit_reads DROP CONSTRAINT IF EXISTS de_reddit_reads_status_check;
ALTER TABLE de_reddit_reads ADD CONSTRAINT de_reddit_reads_status_check
    CHECK (status IN ('read', 'none', 'blind', 'failed', 'unverified'));

-- Wallets (albatross/deepend/wallets.py): top-trader pair edges had no reader; they are no longer written.
DELETE FROM de_wallet_edges WHERE kind = 'toptrader';
