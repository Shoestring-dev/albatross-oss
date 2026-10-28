-- FOMO (fomo.family, the social memecoin trading app) through fomoapi.io, an unofficial relay of the app's data
-- (albatross/deepend/fomo.py): the traders on the app's public boards, the boards' daily ranks, the app's live trade
-- feed as we filter it, and which FOMO traders hold the hot tokens. The wallet-clustering module reads the first four
-- tables: keep their names and columns. EVM addresses are lowercase; chain is ours (solana, ethereum, base, bsc,
-- robinhood): events on other chains are not stored.

-- One row per trader seen on a board or among a hot token's FOMO holders (wallets, no PnL); the PnL columns hold the
-- latest board of that window (NULL while off it).
CREATE TABLE IF NOT EXISTS de_fomo_traders (
    handle              TEXT PRIMARY KEY,              -- as FOMO shows it; FOMO matches handles case-insensitively
    user_id             TEXT,                          -- FOMO's stable id: survives a rename (the handle does not)
    display_name        TEXT,
    solana_wallet       TEXT,
    solana_evidence     TEXT,                          -- onchain-holdings (proven) | provider-claimed (a lead only)
    evm_wallet          TEXT,
    evm_evidence        TEXT,
    pnl_24h             DOUBLE PRECISION,              -- USD, realized, as FOMO reports it
    pnl_7d              DOUBLE PRECISION,
    pnl_30d             DOUBLE PRECISION,
    pnl_all             DOUBLE PRECISION,
    volume_usd          DOUBLE PRECISION,              -- from the longest window they rank on
    trades              INT,
    followers           INT,
    account_created_at  TIMESTAMPTZ,
    clan                JSONB,                         -- {id, name, icon, role} or NULL
    best_rank           INT,                           -- best rank on any window's current board; NULL when on none
    first_seen          TIMESTAMPTZ,
    updated_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS de_fomo_traders_user_id ON de_fomo_traders (user_id);

-- Each board as fetched once a UTC day. "window" is a reserved word in Postgres: quote it in SQL.
CREATE TABLE IF NOT EXISTS de_fomo_leaderboard (
    day                 DATE NOT NULL,
    "window"            TEXT NOT NULL,                 -- 24h | 7d | 30d | all
    rank                INT NOT NULL,
    handle              TEXT NOT NULL,
    pnl_usd             DOUBLE PRECISION,
    volume_usd          DOUBLE PRECISION,
    PRIMARY KEY (day, "window", rank)
);
CREATE INDEX IF NOT EXISTS de_fomo_leaderboard_handle ON de_fomo_leaderboard (handle, day);

-- The app's live feed (wss://api.fomoapi.io/ws/alerts), filtered: buys and sells by anyone on a current board, whale
-- trades, theses, and any event on a token on the Deep End watch list. The feed itself only carries FOMO's LARGE
-- events (about $3,000 of position value and up), so this is never a complete trade list for a wallet.
CREATE TABLE IF NOT EXISTS de_fomo_events (
    event_id            TEXT PRIMARY KEY,              -- FOMO's eventId, the dedupe key
    ts                  TIMESTAMPTZ NOT NULL,          -- FOMO's event time: execution, floored to 5 s (raw->>'execTs'
                                                       -- is the block time when fomoapi matched the fill)
    type                TEXT NOT NULL,                 -- alertType: buy | sell | thesis | listing | ...
    handle              TEXT,
    user_id             TEXT,
    chain               TEXT NOT NULL,
    token_address       TEXT,
    symbol              TEXT,
    usd_value           DOUBLE PRECISION,              -- FOMO's usdValue, NOT a trade size: on a buy the position's
                                                       -- value after it, on a sell the position's realized PnL so far
                                                       -- (cumulative); the fill is raw->>'tradeUsd' when matched
    text                TEXT,
    raw                 JSONB NOT NULL,                -- the message as received
    received_at         TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS de_fomo_events_token ON de_fomo_events (token_address, ts);
CREATE INDEX IF NOT EXISTS de_fomo_events_handle ON de_fomo_events (handle, ts);
CREATE INDEX IF NOT EXISTS de_fomo_events_user ON de_fomo_events (user_id, ts);
CREATE INDEX IF NOT EXISTS de_fomo_events_received ON de_fomo_events (received_at);     -- readers of new events

-- Which FOMO traders hold a hot token (GET /token/{address}/holders, the token's top FOMO holders), one snapshot per
-- lookup.
CREATE TABLE IF NOT EXISTS de_fomo_holders (
    snapshot_at         TIMESTAMPTZ NOT NULL,          -- when the data was taken: fomoapi serves its last good answer,
                                                       -- hours old, when FOMO does not answer (dated by its age)
    chain               TEXT NOT NULL,
    token_address       TEXT NOT NULL,
    handle              TEXT NOT NULL,
    amount              DOUBLE PRECISION,
    value_usd           DOUBLE PRECISION,
    PRIMARY KEY (snapshot_at, chain, token_address, handle)
);
CREATE INDEX IF NOT EXISTS de_fomo_holders_token ON de_fomo_holders (chain, token_address, snapshot_at DESC);

-- The latest holder lookup per token, found or not (an empty answer leaves no de_fomo_holders rows): it paces the
-- lookups, and its snapshot_at picks the current holders.
CREATE TABLE IF NOT EXISTS de_fomo_holder_checks (
    chain               TEXT NOT NULL,
    token_address       TEXT NOT NULL,
    checked_at          TIMESTAMPTZ NOT NULL,          -- our call
    snapshot_at         TIMESTAMPTZ NOT NULL,          -- the data's time (earlier when fomoapi served a stale answer)
    holders             INT NOT NULL,
    status              TEXT NOT NULL,                 -- ok | stale | none (no FOMO trader holds it) | unavailable
    details             JSONB,                         -- {lowercase handle: {is_dev, pnl_usd, cost_basis_usd, thesis}}
    PRIMARY KEY (chain, token_address)
);

-- A top trader's buy (top 50 on the 7d or 30d board), one per position: the calls that albatross/micro/ledger_feed.py
-- logs into trade_calls (source_kind 'fomo', source_id the handle) once our own trusted price exists within 10 minutes,
-- with micro_ledger kind 'fomo' and src_id = src_id here. They never enter micro_calls: they are not group calls.
CREATE TABLE IF NOT EXISTS de_fomo_calls (
    src_id              BIGSERIAL PRIMARY KEY,
    event_id            TEXT NOT NULL UNIQUE,
    position            TEXT NOT NULL UNIQUE,          -- FOMO's tradeId: adding to a position is not a new call
    at                  TIMESTAMPTZ NOT NULL,          -- the buy's ts
    received_at         TIMESTAMPTZ NOT NULL,
    chain               TEXT NOT NULL,
    address             TEXT NOT NULL,
    symbol              TEXT,
    handle              TEXT NOT NULL,
    user_id             TEXT,
    rank_7d             INT,                           -- the trader's ranks on the boards current at the buy
    rank_30d            INT,
    size_usd            DOUBLE PRECISION               -- the matched fill (tradeUsd), else the position's value after
);
