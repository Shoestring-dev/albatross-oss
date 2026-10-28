-- Albatross core schema. Applied by `python -m albatross migrate`.
CREATE EXTENSION IF NOT EXISTS timescaledb;

-- ─── Universe ───────────────────────────────────────────────────────────────
CREATE TABLE assets (
    id              SERIAL PRIMARY KEY,
    symbol          TEXT NOT NULL UNIQUE,          -- canonical base symbol, e.g. BTC, PEPE
    name            TEXT,
    coingecko_id    TEXT,
    market_cap_usd  DOUBLE PRECISION,
    volume_24h_usd  DOUBLE PRECISION,
    tier            SMALLINT NOT NULL DEFAULT 3,   -- 1 core, 2 watch, 3 universe
    primary_category TEXT,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE categories (
    slug            TEXT PRIMARY KEY,              -- our taxonomy, e.g. ai, memes, l1
    name            TEXT NOT NULL,
    directional     BOOLEAN NOT NULL DEFAULT TRUE, -- stablecoins = false (liquidity signal only)
    priority        SMALLINT NOT NULL              -- lower wins when picking primary_category
);

CREATE TABLE asset_categories (
    asset_id        INT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    category_slug   TEXT NOT NULL REFERENCES categories(slug) ON DELETE CASCADE,
    PRIMARY KEY (asset_id, category_slug)
);

CREATE TABLE markets (
    id              SERIAL PRIMARY KEY,
    asset_id        INT NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    venue           TEXT NOT NULL,                 -- hyperliquid, okx, coinbase
    market_type     TEXT NOT NULL,                 -- perp | spot
    venue_symbol    TEXT NOT NULL,                 -- as the venue names it, e.g. kPEPE, BTC-USD
    price_multiplier DOUBLE PRECISION NOT NULL DEFAULT 1, -- kPEPE = 1000 PEPE
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (venue, market_type, venue_symbol)
);

-- ─── Market data ────────────────────────────────────────────────────────────
CREATE TABLE candles (
    venue           TEXT NOT NULL,
    market_type     TEXT NOT NULL,
    symbol          TEXT NOT NULL,                 -- canonical asset symbol
    tf              TEXT NOT NULL,                 -- 5m | 1h | 1d
    ts              TIMESTAMPTZ NOT NULL,          -- candle open time (UTC)
    open            DOUBLE PRECISION NOT NULL,
    high            DOUBLE PRECISION NOT NULL,
    low             DOUBLE PRECISION NOT NULL,
    close           DOUBLE PRECISION NOT NULL,
    volume          DOUBLE PRECISION NOT NULL,     -- base units
    trades          INT,
    PRIMARY KEY (venue, market_type, symbol, tf, ts)
);
SELECT create_hypertable('candles', 'ts', chunk_time_interval => INTERVAL '7 days');
CREATE INDEX ON candles (symbol, tf, ts DESC);

CREATE TABLE deriv_snapshots (
    venue           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    funding_rate    DOUBLE PRECISION,              -- per-hour rate as reported by venue
    open_interest   DOUBLE PRECISION,              -- base units
    oi_usd          DOUBLE PRECISION,
    mark_px         DOUBLE PRECISION,
    oracle_px       DOUBLE PRECISION,
    premium         DOUBLE PRECISION,
    day_volume_usd  DOUBLE PRECISION,
    PRIMARY KEY (venue, symbol, ts)
);
SELECT create_hypertable('deriv_snapshots', 'ts', chunk_time_interval => INTERVAL '1 day');
CREATE INDEX ON deriv_snapshots (symbol, ts DESC);

-- Generic time series: fear & greed, FRED macro, ETF flows, stablecoin supply, etc.
CREATE TABLE metric_series (
    source          TEXT NOT NULL,                 -- alternative.me, fred, farside...
    metric          TEXT NOT NULL,                 -- fear_greed, DXY, btc_etf_netflow...
    ts              TIMESTAMPTZ NOT NULL,
    value           DOUBLE PRECISION,
    meta            JSONB,
    PRIMARY KEY (source, metric, ts)
);
SELECT create_hypertable('metric_series', 'ts', chunk_time_interval => INTERVAL '30 days');

-- ─── News & social ──────────────────────────────────────────────────────────
CREATE TABLE news_items (
    id              BIGSERIAL UNIQUE,
    source          TEXT NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT NOT NULL,
    summary         TEXT,
    published_at    TIMESTAMPTZ NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    assets          TEXT[] NOT NULL DEFAULT '{}',  -- symbols matched in the text
    impact          SMALLINT,                      -- 1-5, set by News Scout
    sentiment       SMALLINT,                      -- -2..+2, set by News Scout
    classification  JSONB,
    PRIMARY KEY (url)
);
CREATE INDEX ON news_items (published_at DESC);
CREATE INDEX ON news_items USING GIN (assets);

CREATE TABLE social_snapshots (
    ts              TIMESTAMPTZ NOT NULL,
    scope           TEXT NOT NULL,                 -- asset symbol or category slug
    source          TEXT NOT NULL,                 -- grok_x, lunarcrush...
    sentiment       DOUBLE PRECISION,              -- -1..+1
    volume          DOUBLE PRECISION,
    narratives      JSONB,
    samples         JSONB,
    PRIMARY KEY (scope, source, ts)
);
SELECT create_hypertable('social_snapshots', 'ts', chunk_time_interval => INTERVAL '30 days');

-- ─── Agents, signals, grading ───────────────────────────────────────────────
CREATE TABLE agent_runs (
    id              BIGSERIAL PRIMARY KEY,
    agent           TEXT NOT NULL,
    model           TEXT NOT NULL,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    latency_ms      INT,
    input_tokens    INT,
    output_tokens   INT,
    cost_usd        DOUBLE PRECISION,
    ok              BOOLEAN NOT NULL,
    error           TEXT,
    input_hash      TEXT,
    output          JSONB
);
CREATE INDEX ON agent_runs (agent, started_at DESC);

CREATE TABLE signals (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    scope           TEXT NOT NULL,                 -- asset symbol or 'sector:<slug>'
    tf              TEXT NOT NULL,                 -- 5m 15m 1h 4h 1d 1w
    bias            SMALLINT NOT NULL,             -- -2..+2
    confidence      DOUBLE PRECISION NOT NULL,     -- 0..1
    ref_price       DOUBLE PRECISION,
    invalidation    DOUBLE PRECISION,
    key_levels      JSONB,
    drivers         JSONB,
    regime          TEXT,
    valid_until     TIMESTAMPTZ NOT NULL,
    source          TEXT NOT NULL,                 -- quant | strategist | oracle
    agent_run_id    BIGINT REFERENCES agent_runs(id)
);
CREATE INDEX ON signals (scope, tf, created_at DESC);

CREATE TABLE outcomes (
    signal_id       BIGINT PRIMARY KEY REFERENCES signals(id) ON DELETE CASCADE,
    graded_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    exit_price      DOUBLE PRECISION,
    return_pct      DOUBLE PRECISION,
    mfe_pct         DOUBLE PRECISION,              -- max favourable excursion
    mae_pct         DOUBLE PRECISION,              -- max adverse excursion
    invalidated     BOOLEAN,
    hit             BOOLEAN
);

CREATE TABLE reports (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    kind            TEXT NOT NULL,                 -- pulse | brief | daily | weekly
    title           TEXT NOT NULL,
    body_md         TEXT NOT NULL,
    signal_ids      BIGINT[] NOT NULL DEFAULT '{}'
);

CREATE TABLE escalations (
    id              BIGSERIAL PRIMARY KEY,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    trigger         TEXT NOT NULL,
    context         JSONB,
    verdict         JSONB,
    agent_run_id    BIGINT REFERENCES agent_runs(id)
);

-- ─── Ops ────────────────────────────────────────────────────────────────────
CREATE TABLE ingest_health (
    job             TEXT PRIMARY KEY,
    last_run        TIMESTAMPTZ,
    last_ok         TIMESTAMPTZ,
    last_error      TEXT,
    consecutive_errors INT NOT NULL DEFAULT 0,
    last_rows       INT
);

-- Compress older market data (keeps 3+ months of history cheap on disk)
ALTER TABLE candles SET (timescaledb.compress, timescaledb.compress_segmentby = 'venue, market_type, symbol, tf');
SELECT add_compression_policy('candles', INTERVAL '14 days');
ALTER TABLE deriv_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'venue, symbol');
SELECT add_compression_policy('deriv_snapshots', INTERVAL '3 days');
