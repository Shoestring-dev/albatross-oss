-- The bot's transactions with their time on chain and which way they went (2026-10-06): the dossier chart
-- marks every sale, partial ones included, when it happened. Filled by the ingest job micro_bot_txs from Solana's
-- public RPC (trades on other chains keep their close only). side: buy | sell | none (the bot's balance of the token
-- didn't move) | failed (the transaction failed on chain); a row with an error and no block_time is tried again.
CREATE TABLE IF NOT EXISTS micro_trade_txs (
    chain        TEXT NOT NULL,
    sig          TEXT NOT NULL,
    trader       TEXT NOT NULL,
    src_id       BIGINT NOT NULL,
    block_time   TIMESTAMPTZ,
    side         TEXT,
    token_delta  DOUBLE PRECISION,
    sol_delta    DOUBLE PRECISION,
    error        TEXT,
    fetched_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain, sig)
);
CREATE INDEX IF NOT EXISTS micro_trade_txs_trade ON micro_trade_txs (trader, src_id);
