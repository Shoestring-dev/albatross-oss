-- The Deep End market lane (owner B: albatross/deepend/market.py, docs/SPEC-DEEP-END.md): the latest market read per
-- token and this lane's bookkeeping of its Birdeye candles (its units: de_api_usage, provider 'birdeye:market').
-- Missing values are NULL, never 0.

CREATE TABLE IF NOT EXISTS de_market (
    chain                   TEXT NOT NULL,
    address                 TEXT NOT NULL,
    -- Trade stats: Jupiter tokens v2 on Solana (stored by chain.py), else Birdeye trade-data.
    stats_src               TEXT,                   -- jupiter | birdeye
    stats_at                TIMESTAMPTZ,            -- when we read them
    change_m5               DOUBLE PRECISION,       -- price change, %
    change_h1               DOUBLE PRECISION,
    change_h6               DOUBLE PRECISION,
    change_h24              DOUBLE PRECISION,
    volume_m5               DOUBLE PRECISION,       -- USD, buys + sells
    volume_h1               DOUBLE PRECISION,
    volume_h6               DOUBLE PRECISION,
    volume_h24              DOUBLE PRECISION,
    buys_h1                 INTEGER,
    sells_h1                INTEGER,
    buys_h24                INTEGER,
    sells_h24               INTEGER,
    traders_h1              INTEGER,
    traders_h24             INTEGER,
    net_buyers_h1           INTEGER,                -- Jupiter only
    organic_score           DOUBLE PRECISION,       -- Jupiter only, 0-100
    organic_label           TEXT,
    holders                 INTEGER,
    holders_change_h1_pct   DOUBLE PRECISION,       -- Jupiter's, or from holders_hist
    holders_hist            JSONB NOT NULL DEFAULT '[]',   -- [[unix s, holders], ...] of the last 2 h (Birdeye)
    -- DexScreener: paid orders and boosts.
    dex_paid                BOOLEAN,                -- an approved tokenProfile or communityTakeover order
    dex_paid_at             TIMESTAMPTZ,            -- the first such payment
    dex_orders              JSONB,                  -- {orders, boosts} as DexScreener returned them
    boosts                  INTEGER,                -- active boosts on the token's pairs
    logo                    TEXT,
    dex_at                  TIMESTAMPTZ,            -- last DexScreener check
    -- Birdeye 1m candles (micro_state.candles_at stays the coverage every reader uses).
    candles_fetched_at      TIMESTAMPTZ,            -- last request
    candles_until           TIMESTAMPTZ,            -- Birdeye's own candles are complete up to here
    candles_seen            BOOLEAN NOT NULL DEFAULT false,  -- Birdeye has returned candles for this token
    candles_error           TEXT,
    candles_retry_at        TIMESTAMPTZ,
    PRIMARY KEY (chain, address)
);

-- micro_state.candles_at meant "last GeckoTerminal fetch", with the candles complete 60 s (its refresh lag) earlier.
-- From here on it is the coverage itself, set by each source net of its own lag. Shift the stored stamps once.
UPDATE micro_state SET candles_at = candles_at - interval '60 seconds' WHERE candles_at IS NOT NULL;
COMMENT ON COLUMN micro_state.candles_at IS
    '1m candles are complete up to here (Birdeye or GeckoTerminal); ignored while candles_error is set';
