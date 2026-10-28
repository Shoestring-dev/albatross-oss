-- The Deep End chain intel (docs/SPEC-DEEP-END.md, owner A; code and cadences in albatross/deepend/chain.py).
-- de_chain_intel: one row per token. Each source's last payload is kept whole (RugCheck's without its per-market
-- internals) with the time it was fetched, so a better reading of the same data needs no refetch. The columns after
-- `errors` are extracted from those payloads on every write, for the feed's filters; NULL means no source knows,
-- never "fine". Percentages are 0-100.
CREATE TABLE IF NOT EXISTS de_chain_intel (
    chain               TEXT NOT NULL,
    address             TEXT NOT NULL,
    jupiter             JSONB,              -- Jupiter tokens v2 (Solana); market.py reads its trade stats from here
    jupiter_at          TIMESTAMPTZ,
    rugcheck            JSONB,              -- RugCheck report (Solana)
    rugcheck_at         TIMESTAMPTZ,
    birdeye_security    JSONB,              -- /defi/token_security; on EVM the GoPlus record
    birdeye_security_at TIMESTAMPTZ,
    birdeye_holders     JSONB,              -- /defi/v3/token/holder, top 20 (Solana only)
    birdeye_holders_at  TIMESTAMPTZ,
    birdeye_traders     JSONB,              -- /defi/v2/tokens/top_traders: top 10 by 24 h volume, PnL, wallet tags
    birdeye_traders_at  TIMESTAMPTZ,
    birdeye_creation    JSONB,              -- /defi/token_creation_info (Solana)
    birdeye_creation_at TIMESTAMPTZ,
    honeypot            JSONB,              -- honeypot.is IsHoneypot (EVM); its 404 body when it knows no pair
    honeypot_at         TIMESTAMPTZ,
    errors              JSONB NOT NULL DEFAULT '{}',    -- source -> {at, error} of its last failed fetch
    holders             INTEGER,
    top10_pct           DOUBLE PRECISION,   -- top 10 holders other than pools and lockers
    dev_address         TEXT,
    dev_launches        INTEGER,            -- tokens this dev has minted (Jupiter devMints)
    dev_migrated        INTEGER,            -- of which graduated from their launchpad (Jupiter devMigrations)
    dev_balance_pct     DOUBLE PRECISION,
    insiders            INTEGER,            -- wallets in RugCheck's insider graph
    mint_revoked        BOOLEAN,
    freeze_revoked      BOOLEAN,
    lp_locked_pct       DOUBLE PRECISION,   -- the deepest market's LP locked or burned
    rugcheck_score      DOUBLE PRECISION,   -- RugCheck score_normalised, 0-100, higher = riskier
    organic_score       DOUBLE PRECISION,
    organic_label       TEXT,
    ct_likes            INTEGER,
    smart_ct_likes      INTEGER,
    transfer_fee        BOOLEAN,
    transfer_fee_pct    DOUBLE PRECISION,
    is_honeypot         BOOLEAN,
    buy_tax             DOUBLE PRECISION,
    sell_tax            DOUBLE PRECISION,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (chain, address)
);

-- Holder count and concentration over time (the dossier's history, holder-growth checks, later grading): each
-- Jupiter pass (2 min) for hot tokens, every 30 minutes for warm ones.
CREATE TABLE IF NOT EXISTS de_holder_snapshots (
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    holders         INTEGER,
    top10_pct       DOUBLE PRECISION,
    organic_score   DOUBLE PRECISION,
    net_buyers_h1   INTEGER,
    PRIMARY KEY (chain, address, ts)
);
SELECT create_hypertable('de_holder_snapshots', 'ts', chunk_time_interval => INTERVAL '1 day', if_not_exists => TRUE);
ALTER TABLE de_holder_snapshots SET (timescaledb.compress, timescaledb.compress_segmentby = 'chain, address');
SELECT add_compression_policy('de_holder_snapshots', INTERVAL '3 days', if_not_exists => TRUE);
