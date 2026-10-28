-- Daily cross-sectional factor ranking of the liquid Hyperliquid perps (albatross/strategy/factor_rank.py).
-- live = computed on its own day (the honest track record); false = backfilled history.
CREATE TABLE factor_ranks (
    day         DATE NOT NULL,             -- the 1d candle whose close the ranking uses
    symbol      TEXT NOT NULL,
    score       DOUBLE PRECISION NOT NULL, -- mean of the three z-scores; higher = stronger
    rank        INT NOT NULL,              -- 1 = strongest that day
    n           INT NOT NULL,              -- liquid coins ranked that day
    quintile    SMALLINT NOT NULL,         -- 5 = strongest fifth, 1 = weakest
    calm        DOUBLE PRECISION,          -- raw parts (before z-scoring)
    near_high   DOUBLE PRECISION,
    volume      DOUBLE PRECISION,
    vol_30d     DOUBLE PRECISION,
    dv_30d      DOUBLE PRECISION,
    live        BOOLEAN NOT NULL DEFAULT false,
    PRIMARY KEY (day, symbol)
);
CREATE INDEX ON factor_ranks (symbol, day);
