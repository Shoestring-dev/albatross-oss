-- Micro-cap lane M1, "prices you can trust" (docs/SPEC-MICROCAPS.md §5, §6; code in albatross/micro/).
-- `chain` below is always the chain the token trades on. A call that named no chain lands in micro_tokens
-- as chain = 'evm'; the pool job finds it on DexScreener and records micro_tokens.resolved_chain.
-- EVM addresses (tokens and pools) are lowercase; Solana addresses as-is. Missing values are NULL, never 0.

ALTER TABLE micro_tokens ADD COLUMN resolved_chain TEXT;

-- Every pool seen for a watched token, from that token's point of view. A token is priced only from
-- pools where it is the BASE side (is_base); quote-side pools are kept to show what was refused.
CREATE TABLE micro_pools (
    chain           TEXT NOT NULL,
    pool            TEXT NOT NULL,
    token           TEXT NOT NULL,
    quote_token     TEXT,
    quote_symbol    TEXT,
    dex             TEXT,
    labels          TEXT[] NOT NULL DEFAULT '{}',   -- e.g. CPMM, DLMM, v4
    is_base         BOOLEAN NOT NULL,
    liquidity_usd   DOUBLE PRECISION,
    created_at      TIMESTAMPTZ,                    -- pool creation per the indexer
    first_seen      TIMESTAMPTZ NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL,
    migrated_from   TEXT,                           -- the bonding-curve pool this one replaced
    PRIMARY KEY (chain, pool, token)
);
CREATE INDEX ON micro_pools (chain, token);

-- Every change of a token's pricing pool (the deepest base-side pool wins, with hysteresis).
CREATE TABLE micro_pool_switches (
    chain           TEXT NOT NULL,
    token           TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    from_pool       TEXT,
    to_pool         TEXT NOT NULL,
    from_liquidity  DOUBLE PRECISION,
    to_liquidity    DOUBLE PRECISION,
    reason          TEXT NOT NULL,
    PRIMARY KEY (chain, token, ts)
);

-- One row per priced token: pricing pool, current consensus price and the scheduler's bookkeeping.
CREATE TABLE micro_state (
    chain           TEXT NOT NULL,
    token           TEXT NOT NULL,
    symbol          TEXT,
    name            TEXT,
    launchpad       TEXT,
    created_at      TIMESTAMPTZ,                    -- earliest pool (or mint) creation seen by any source
    pool            TEXT,                           -- pricing pool
    dex             TEXT,
    -- Consensus (§5): median of the fresh sources that agree within 5%.
    price_usd       DOUBLE PRECISION,               -- last accepted price; judge it by price_ts, it is not carried forward
    price_ts        TIMESTAMPTZ,                    -- source time of the newest quote in the agreeing set
    n_sources       SMALLINT,
    sources         TEXT[] NOT NULL DEFAULT '{}',
    spread_pct      DOUBLE PRECISION,               -- widest fresh source vs the anchor, %
    flags           TEXT[] NOT NULL DEFAULT '{}',   -- single_source, disagree, stale, rejected_print, ...
    pending_price   DOUBLE PRECISION,               -- a >10x / <0.1x print waiting for confirmation
    pending_since   TIMESTAMPTZ,
    liquidity_usd   DOUBLE PRECISION,
    liquidity_src   TEXT,                           -- dexscreener = pricing pool; jupiter = all pools of the token
    liquidity_ts    TIMESTAMPTZ,
    mcap_usd        DOUBLE PRECISION,
    fdv_usd         DOUBLE PRECISION,
    volume_h24_usd  DOUBLE PRECISION,
    txns_h1         INTEGER,
    pools_at        TIMESTAMPTZ,                    -- last pool discovery
    snapshot_at     TIMESTAMPTZ,                    -- last price poll
    candles_at      TIMESTAMPTZ,                    -- last GeckoTerminal OHLCV fetch
    candles_error   TEXT,
    PRIMARY KEY (chain, token)
);

-- Quotes per source and poll, at most one poll per token and minute (unchanged ones every 10 minutes), plus a
-- 'consensus' row whenever the verdict needs explaining (disagree, outlier, stale, rejected print, ...).
CREATE TABLE micro_snapshots (
    chain           TEXT NOT NULL,
    token           TEXT NOT NULL,
    source          TEXT NOT NULL,                  -- jupiter | dexscreener | consensus
    ingest_ts       TIMESTAMPTZ NOT NULL,           -- when we polled
    source_ts       TIMESTAMPTZ,                    -- when the source says the value held (receipt minus CDN age)
    pool            TEXT,
    price_usd       DOUBLE PRECISION,
    liquidity_usd   DOUBLE PRECISION,
    mcap_usd        DOUBLE PRECISION,
    fdv_usd         DOUBLE PRECISION,
    volume_h24_usd  DOUBLE PRECISION,
    n_sources       SMALLINT,
    flags           TEXT[] NOT NULL DEFAULT '{}',
    PRIMARY KEY (chain, token, source, ingest_ts)
);
SELECT create_hypertable('micro_snapshots', 'ingest_ts', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE micro_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'chain, token, source');
SELECT add_compression_policy('micro_snapshots', INTERVAL '3 days');

-- 1m candles of the token (not the pool: GeckoTerminal is asked for the token's side), from its pricing pool.
-- n_sources = 1 + other sources whose quotes in that minute fall inside the candle's range (±5%).
-- buys/sells stay NULL until the on-chain swap feed (M2).
CREATE TABLE micro_candles_1m (
    chain           TEXT NOT NULL,
    token           TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    pool            TEXT,
    open            DOUBLE PRECISION NOT NULL,
    high            DOUBLE PRECISION NOT NULL,
    low             DOUBLE PRECISION NOT NULL,
    close           DOUBLE PRECISION NOT NULL,
    volume_usd      DOUBLE PRECISION,
    buys            INTEGER,
    sells           INTEGER,
    source          TEXT NOT NULL,
    n_sources       SMALLINT NOT NULL DEFAULT 1,
    flags           TEXT[] NOT NULL DEFAULT '{}',   -- single_source, rejected_print, jump_confirmed
    ingest_ts       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain, token, ts)
);
SELECT create_hypertable('micro_candles_1m', 'ts', chunk_time_interval => INTERVAL '1 day');
ALTER TABLE micro_candles_1m SET (timescaledb.compress, timescaledb.compress_segmentby = 'chain, token');
SELECT add_compression_policy('micro_candles_1m', INTERVAL '3 days');

-- Cross-source disagreement, sampled hourly: each quote vs the GeckoTerminal 1m candle of its minute, and
-- Jupiter vs DexScreener. The M1 accuracy number on the Health tab (§8).
CREATE TABLE micro_reconciliation (
    run_at          TIMESTAMPTZ NOT NULL,
    chain           TEXT NOT NULL,
    token           TEXT NOT NULL,
    source          TEXT NOT NULL,                  -- the quote being checked
    reference       TEXT NOT NULL,                  -- geckoterminal | dexscreener
    ts              TIMESTAMPTZ NOT NULL,           -- the quote's source time
    price           DOUBLE PRECISION NOT NULL,
    ref_price       DOUBLE PRECISION NOT NULL,      -- candle close, or the other quote
    err_pct         DOUBLE PRECISION NOT NULL,      -- (price / ref_price - 1) * 100
    in_range        BOOLEAN,                        -- inside that minute's low..high (±1%); NULL if not a candle
    PRIMARY KEY (run_at, chain, token, source, reference, ts)
);
CREATE INDEX ON micro_reconciliation (run_at DESC);
