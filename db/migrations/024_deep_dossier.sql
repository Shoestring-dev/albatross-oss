-- The Deep End, owner F (docs/SPEC-DEEP-END.md sections 2 and 4; code in albatross/deepend/discovery.py and
-- dossier.py): the discovery radar, the composed dossier (latest, plus its history for grading), and caller
-- track records graded on the DEX call ledger.

-- Tokens the discovery sensor found: Birdeye trending and new listings, DexScreener profiles and boosts.
-- albatross/deepend/watch.py reads chain, address, first_seen and trending_at for heat. EVM addresses lowercase.
CREATE TABLE IF NOT EXISTS de_watch (
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    first_seen      TIMESTAMPTZ NOT NULL,
    last_seen       TIMESTAMPTZ NOT NULL,
    trending_at     TIMESTAMPTZ,                   -- last seen on Birdeye's trending list
    rank            INTEGER,                       -- its rank there then (1 = top)
    sources         TEXT[] NOT NULL DEFAULT '{}',  -- 'birdeye trending', 'birdeye new listing', 'dexscreener profile',
                                                   -- 'dexscreener boost', 'dexscreener top boost'
    meta            JSONB NOT NULL DEFAULT '{}',   -- latest reading per source, links, logo, pair stats
    PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS de_watch_first_seen ON de_watch (first_seen DESC);
CREATE INDEX IF NOT EXISTS de_watch_trending_at ON de_watch (trending_at DESC);

-- The latest dossier per token (job deep_compose: hot tokens every minute, warm ones every 10 minutes).
-- score is the evidence tally (NULL with fewer than 5 known checklist lines), not a prediction.
CREATE TABLE IF NOT EXISTS de_dossier (
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    score           SMALLINT,
    good            SMALLINT NOT NULL,
    warn            SMALLINT NOT NULL,
    bad             SMALLINT NOT NULL,
    unknown         SMALLINT NOT NULL,
    red_flags       TEXT[] NOT NULL DEFAULT '{}',
    x_heat          JSONB,                         -- {mood, posts_h, label} for the feed
    tier            TEXT,                          -- heat when composed: hot | warm | cold
    sig             TEXT NOT NULL,                 -- the checklist statuses, key:status joined
    take            TEXT,                          -- the Albatross take (DeepSeek), for the evidence at take_sig
    take_at         TIMESTAMPTZ,
    take_sig        TEXT,
    take_score      SMALLINT,
    updated_at      TIMESTAMPTZ NOT NULL,
    snapshot        JSONB NOT NULL,                -- the whole dossier as served
    PRIMARY KEY (chain, address)
);
CREATE INDEX IF NOT EXISTS de_dossier_updated_at ON de_dossier (updated_at DESC);

-- A row whenever a token's score or red flags change, with the evidence then: the grader joins these to the
-- calls' outcomes (what the checklist said at the time of a call vs what happened after).
CREATE TABLE IF NOT EXISTS de_dossier_history (
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    at              TIMESTAMPTZ NOT NULL,
    score           SMALLINT,
    good            SMALLINT NOT NULL,
    warn            SMALLINT NOT NULL,
    bad             SMALLINT NOT NULL,
    unknown         SMALLINT NOT NULL,
    red_flags       TEXT[] NOT NULL DEFAULT '{}',
    checklist       JSONB NOT NULL,                -- [{key, status, value, as_of}]
    evidence        JSONB,                         -- the numbers the rules read
    PRIMARY KEY (chain, address, at)
);

-- Caller track records from the DEX call ledger (job deep_grade, daily). caller '' = the whole group;
-- group_name '*' = every group together (the base rate). Each group's (or caller's) FIRST call of a token counts
-- once. A call is graded once its 24 h horizon is complete; hit_2x = the price touched 2x the $100 entry fill
-- (fee, impact and gas in: trade_call_outcomes.mfe_pct >= 100) within those 24 h.
CREATE TABLE IF NOT EXISTS de_caller_stats (
    group_name      TEXT NOT NULL,
    caller          TEXT NOT NULL,
    calls           INTEGER NOT NULL,              -- first calls logged in the ledger
    graded          INTEGER NOT NULL,              -- of which the 24 h horizon is complete
    hit_2x          INTEGER NOT NULL,
    hit_rate_2x     DOUBLE PRECISION,              -- hit_2x / graded
    median_24h_pct  DOUBLE PRECISION,              -- median net return, sold at 24 h
    median_1h_pct   DOUBLE PRECISION,              -- median net return, sold at 1 h (every call past its first hour)
    first_call      TIMESTAMPTZ,
    last_call       TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (group_name, caller)
);

-- The evidence at the time of each ledger call next to its outcome, for later analysis (job deep_grade).
-- dossier_* = the last de_dossier_history row up to 2 minutes after the ledger entry; lag_s = its time minus the
-- entry (> 0: composed after the entry, so strict analyses keep lag_s <= 0).
CREATE TABLE IF NOT EXISTS de_call_evidence (
    call_id         BIGINT PRIMARY KEY REFERENCES trade_calls(id),
    kind            TEXT NOT NULL,                 -- call | trade (a bot entry)
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    group_name      TEXT,
    caller          TEXT,
    called_at       TIMESTAMPTZ NOT NULL,
    logged_at       TIMESTAMPTZ NOT NULL,
    first_in_group  BOOLEAN,
    dossier_at      TIMESTAMPTZ,
    lag_s           DOUBLE PRECISION,
    score           SMALLINT,
    statuses        JSONB,                         -- {checklist key: status}
    red_flags       TEXT[],
    status          TEXT,                          -- outcome: open | graded | ungradeable
    ret_1h_pct      DOUBLE PRECISION,
    ret_6h_pct      DOUBLE PRECISION,
    ret_24h_pct     DOUBLE PRECISION,
    mfe_pct         DOUBLE PRECISION,
    hit_2x          BOOLEAN,
    updated_at      TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS de_call_evidence_token ON de_call_evidence (chain, address, called_at);
