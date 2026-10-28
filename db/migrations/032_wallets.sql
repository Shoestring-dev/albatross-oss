-- Wallet labels and clusters for The Deep End (albatross/deepend/wallets.py, where every rule and threshold is written
-- down): who the wallets around our coins are, which of them trade together, and who funded the fresh ones.

-- Every wallet seen around a token we track (or trading in the FOMO feed). labels and evidence are recomputed whenever
-- the wallet is seen again; evidence keeps the numbers that earned each label. Insider is never a label here: it is
-- scoped to the token (de_wallet_tokens role 'insider').
CREATE TABLE IF NOT EXISTS de_wallets (
    chain               TEXT NOT NULL,
    address             TEXT NOT NULL,                  -- EVM lowercase
    first_seen          TIMESTAMPTZ NOT NULL,
    labels              TEXT[] NOT NULL DEFAULT '{}',   -- top_trader, sniper, deployer, bot; and the addresses that are
                                                        -- not people (never clustered): exchange, service, launchpad,
                                                        -- mass_launch
    sources             TEXT[] NOT NULL DEFAULT '{}',   -- fomo, fomo-claimed (a lead only: earns nothing), birdeye,
                                                        -- holders, rugcheck, deployer, funder
    evidence            JSONB NOT NULL DEFAULT '{}',    -- label -> the numbers behind it
    fomo_handle         TEXT,
    max_trades_1h       INTEGER,                        -- the most FOMO-feed trades it made in any 60 minutes
    signatures          INTEGER,                        -- Solana signatures the funder walk saw (1000 = 1000 or more)
    first_funder        TEXT,                           -- the wallet whose SOL transfer created this one
    funded_at           TIMESTAMPTZ,
    funded_sol          DOUBLE PRECISION,
    funding_tx          TEXT,                           -- that transfer's signature, for anyone to check
    funder_status       TEXT,                           -- found | not fresh | no transfer | history incomplete | failed
    funder_checked_at   TIMESTAMPTZ,
    cluster_id          TEXT,                           -- its co-trading cluster
    funding_cluster_id  TEXT,                           -- its funding cluster
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain, address)
);

-- How each wallet is around each token. Roles accumulate and are never removed: buyer, seller (FOMO feed), trader (a
-- Birdeye top trader), holder (a top holder), insider (in the token's RugCheck insider graph), deployer (its dev),
-- sniper (Birdeye's tag), bot (Birdeye's tag, or >= 720 trades in its 24 h window).
CREATE TABLE IF NOT EXISTS de_wallet_tokens (
    chain           TEXT NOT NULL,
    token           TEXT NOT NULL,
    address         TEXT NOT NULL,
    roles           TEXT[] NOT NULL DEFAULT '{}',
    first_buy_at    TIMESTAMPTZ,                    -- its first buy in the FOMO feed (FOMO's matched on-chain time if any)
    launch_s        DOUBLE PRECISION,               -- that buy, seconds after the token's creation (when known)
    pnl_usd         DOUBLE PRECISION,               -- Birdeye's PnL for it on the token (24 h window), as of traded_at
    traded_at       TIMESTAMPTZ,                    -- last seen among Birdeye's top traders
    holding_pct     DOUBLE PRECISION,               -- % of supply, as of held_at
    held_at         TIMESTAMPTZ,                    -- the holder report it was last a top holder in
    first_at        TIMESTAMPTZ NOT NULL,
    last_at         TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain, token, address)
);
CREATE INDEX IF NOT EXISTS de_wallet_tokens_wallet ON de_wallet_tokens (chain, address);
CREATE INDEX IF NOT EXISTS de_wallet_tokens_untimed ON de_wallet_tokens (first_buy_at) WHERE launch_s IS NULL;

-- Pairs of wallets that turn up on the same tokens (a < b). cotrade: both entered the token within 10 minutes (first
-- FOMO buys); toptrader: both were among its Birdeye top traders (weaker: same list, no timing). tokens = the distinct
-- tokens shared so far, n = how many. Clusters use cotrade pairs with n >= 3.
CREATE TABLE IF NOT EXISTS de_wallet_edges (
    chain       TEXT NOT NULL,
    a           TEXT NOT NULL,
    b           TEXT NOT NULL,
    kind        TEXT NOT NULL,
    tokens      TEXT[] NOT NULL,
    n           INTEGER NOT NULL,
    first_at    TIMESTAMPTZ NOT NULL,
    last_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain, a, b, kind)
);
CREATE INDEX IF NOT EXISTS de_wallet_edges_strong ON de_wallet_edges (kind) WHERE n >= 3;

-- Clusters, rebuilt on every deep_wallets run. cotrade: a connected component over strong co-trading edges; funding:
-- the wallets one first funder created (never an exchange or a service). cluster_id = kind:chain:anchor (the lowest
-- member address, or the funder).
CREATE TABLE IF NOT EXISTS de_wallet_clusters (
    cluster_id   TEXT PRIMARY KEY,
    chain        TEXT NOT NULL,
    kind         TEXT NOT NULL,                 -- cotrade | funding
    funder       TEXT,
    size         INTEGER NOT NULL,
    members      TEXT[] NOT NULL,
    labels       JSONB NOT NULL,                -- label -> how many members carry it
    tokens       TEXT[] NOT NULL,               -- tokens two or more members bought or held, most shared first (<= 50)
    n_tokens     INTEGER NOT NULL,
    first_seen   TIMESTAMPTZ,                   -- the earliest any member was seen around a token
    last_active  TIMESTAMPTZ,                   -- the latest
    updated_at   TIMESTAMPTZ NOT NULL
);

-- RugCheck's insider graph per token (GET /v1/tokens/{mint}/insiders/graph): networks of wallets and the transfers
-- linking them, [{id, type, wallets: [{address, participant, holdings}], links: [{from, to, amount}]}]. Fetched when
-- the report's insider count (detected) changes, or a day later for a hot token.
CREATE TABLE IF NOT EXISTS de_insider_graphs (
    chain       TEXT NOT NULL,
    token       TEXT NOT NULL,
    detected    INTEGER,                        -- the report's graphInsidersDetected at the fetch
    networks    JSONB NOT NULL,
    fetched_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain, token)
);

-- Where deep_wallets left off in each source: fomo_events (received_at), chain_intel (the sources' fetch times).
CREATE TABLE IF NOT EXISTS de_wallet_cursor (
    source  TEXT PRIMARY KEY,
    at      TIMESTAMPTZ NOT NULL
);
