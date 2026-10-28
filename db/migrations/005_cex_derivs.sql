-- Binance/Bybit perp metrics via Coinalyze (albatross/venues/coinalyze.py). One row per venue,
-- asset and hour; symbol = our asset symbol (1000PEPE-style contracts normalised to PEPE).
CREATE TABLE cex_derivs_1h (
    venue           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,          -- hour start
    oi_usd          DOUBLE PRECISION,              -- open interest at the hour's close
    liq_long_usd    DOUBLE PRECISION,              -- longs liquidated during the hour
    liq_short_usd   DOUBLE PRECISION,
    long_pct        DOUBLE PRECISION,              -- % of accounts net long (long/short ratio data)
    volume          DOUBLE PRECISION,              -- base units
    buy_volume      DOUBLE PRECISION,              -- taker-buy base units
    close           DOUBLE PRECISION,
    PRIMARY KEY (venue, symbol, ts)
);
SELECT create_hypertable('cex_derivs_1h', 'ts', chunk_time_interval => INTERVAL '30 days');

-- Funding snapshots (percent per 8h interval, as the venue quotes it) taken each run.
CREATE TABLE cex_funding (
    venue           TEXT NOT NULL,
    symbol          TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    funding_pct     DOUBLE PRECISION,
    predicted_pct   DOUBLE PRECISION,
    PRIMARY KEY (venue, symbol, ts)
);
SELECT create_hypertable('cex_funding', 'ts', chunk_time_interval => INTERVAL '30 days');
