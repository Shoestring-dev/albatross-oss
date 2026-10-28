-- Micro-cap lane, M1 part 1: plays pushed from an external call feed (docs/SPEC-MICROCAPS.md).
-- EVM addresses are stored lowercase; Solana mints as-is. chain = 'evm' when a call gave no chain.

CREATE TABLE micro_tokens (
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    symbol          TEXT,
    first_seen      TIMESTAMPTZ NOT NULL,
    first_seen_via  TEXT NOT NULL,                 -- call | trade
    status          TEXT NOT NULL DEFAULT 'active',
    retired_at      TIMESTAMPTZ,
    PRIMARY KEY (chain, address)
);

-- One row per contract-address mention in a monitored Telegram group (the feed's own id).
CREATE TABLE micro_calls (
    src_id          BIGINT PRIMARY KEY,
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    group_id        BIGINT,
    group_name      TEXT,
    caller          TEXT,
    intent          TEXT,                          -- buy | sell | mention
    called_at       TIMESTAMPTZ NOT NULL,
    received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON micro_calls (chain, address, called_at);
CREATE INDEX ON micro_calls (called_at DESC);

-- Latest state of each bot position (the feed's own id); re-sent whenever it changes.
-- Albatross is meant to become the source of truth for positions, so fills must be verifiable:
-- txs carries the on-chain signatures, and M2 re-derives fills from the chain itself.
-- trader = stable pseudonym computed by the feed; Telegram ids never leave it.
CREATE TABLE micro_trades (
    src_id          BIGINT PRIMARY KEY,
    trader          TEXT NOT NULL,
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    symbol          TEXT,
    strategy        TEXT,
    dry             BOOLEAN,
    group_id        BIGINT,
    caller          TEXT,
    entry_at        TIMESTAMPTZ,
    entry_price     DOUBLE PRECISION,
    size_usd        DOUBLE PRECISION,
    remaining       DOUBLE PRECISION,              -- fraction still held
    peak            DOUBLE PRECISION,
    realized_x      DOUBLE PRECISION,
    real_pnl_usd    DOUBLE PRECISION,
    status          TEXT,
    closed_at       TIMESTAMPTZ,
    close_reason    TEXT,
    txs             TEXT[] NOT NULL DEFAULT '{}',  -- on-chain signatures/hashes, to verify fills later
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON micro_trades (chain, address);
CREATE INDEX ON micro_trades (entry_at DESC);
