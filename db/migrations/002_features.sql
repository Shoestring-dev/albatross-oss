-- Per-asset, per-timeframe computed indicators. Agents read these instead of raw candles,
-- so every number they cite is deterministic and auditable.
CREATE TABLE features (
    symbol          TEXT NOT NULL,
    tf              TEXT NOT NULL,                 -- 5m 15m 1h 4h 1d 1w
    ts              TIMESTAMPTZ NOT NULL,          -- open time of the last closed candle used
    venue           TEXT NOT NULL,
    close           DOUBLE PRECISION NOT NULL,
    data            JSONB NOT NULL,
    PRIMARY KEY (symbol, tf, ts)
);
SELECT create_hypertable('features', 'ts', chunk_time_interval => INTERVAL '7 days');
CREATE INDEX ON features (tf, ts DESC);

ALTER TABLE features SET (timescaledb.compress, timescaledb.compress_segmentby = 'symbol, tf');
SELECT add_compression_policy('features', INTERVAL '14 days');
