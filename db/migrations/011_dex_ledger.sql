-- Micro-cap calls into the call ledger (docs/SPEC-LEDGER.md §2, §8 step 3; code in albatross/micro/ledger_feed.py).
-- Every Telegram group call (micro_calls) and every real-money bot entry (micro_trades) gets one row
-- here: logged into trade_calls once our own trusted price exists within 10 minutes of it, else counted as
-- unpriced with the reason. trade_calls itself stays append-only; this is the bookkeeping around it.
CREATE TABLE micro_ledger (
    kind            TEXT NOT NULL,                 -- call | trade
    src_id          BIGINT NOT NULL,               -- micro_calls.src_id | micro_trades.src_id
    at              TIMESTAMPTZ NOT NULL,          -- when the call was made / the bot entered
    status          TEXT NOT NULL,                 -- pending | logged | unpriced | skipped
    reason          TEXT,                          -- unpriced: late | missed | no_price; skipped: sell
    detail          TEXT,                          -- pending/logged/no_price: the last check (ok, no_token, no_pool, no_price, stale)
    call_id         BIGINT REFERENCES trade_calls(id),
    first_checked   TIMESTAMPTZ NOT NULL,
    decided_at      TIMESTAMPTZ,
    PRIMARY KEY (kind, src_id)
);
CREATE INDEX ON micro_ledger (status);

-- DEX calls (NULL for majors). Fixed-horizon exits (SPEC-LEDGER §4: 5m/1h/6h/24h): the net return had the call
-- been sold at each checkpoint, same costs as net_return_pct; NULL until that checkpoint's candles are in.
-- The cost inputs sit next to them, so another position size can be regraded from the row alone
-- (sim.dex_round_trip_pct with the entry pool here and each exit's price and pool in `exits`).
ALTER TABLE trade_call_outcomes
    ADD COLUMN ret_5m_pct DOUBLE PRECISION,
    ADD COLUMN ret_1h_pct DOUBLE PRECISION,
    ADD COLUMN ret_6h_pct DOUBLE PRECISION,
    ADD COLUMN ret_24h_pct DOUBLE PRECISION,
    ADD COLUMN size_usd DOUBLE PRECISION,           -- position size the costs assume
    ADD COLUMN fee_pct DOUBLE PRECISION,            -- pool fee per swap
    ADD COLUMN gas_usd DOUBLE PRECISION,            -- network allowance per swap
    ADD COLUMN liquidity_usd DOUBLE PRECISION,      -- pool liquidity at entry (from the call's evidence)
    ADD COLUMN quote_reserve_usd DOUBLE PRECISION,  -- its quote side, the impact denominator (half)
    ADD COLUMN exits JSONB;                         -- {5m|1h|6h|24h|exit: {at, price, liquidity_usd,
                                                    --   quote_reserve_usd, measured, net_pct}}; exit = horizon or mark
