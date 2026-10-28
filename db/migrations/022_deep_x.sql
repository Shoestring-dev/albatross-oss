-- The Deep End X Scout (docs/SPEC-DEEP-END.md, owner C; albatross/deepend/xintel.py): one row per token per X read
-- through the Grok CLI. status: read (cites verifiable recent X posts) | quiet (its searches found nothing, and another
-- token of the same call was grounded) | ungrounded (nothing verifiable) | failed (the Grok call failed). Only read
-- and quiet rows are evidence; the others are kept so a token is not re-read within 20 minutes and for audit.
-- Numbers are computed by code from the verified posts (posts_h: posts about the token in the hour before the read).
CREATE TABLE IF NOT EXISTS de_x_reads (
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    read_at         TIMESTAMPTZ NOT NULL,           -- when the searches ran
    run_id          BIGINT REFERENCES agent_runs(id),
    status          TEXT NOT NULL,
    symbol          TEXT,
    picked_for      TEXT,                           -- new call | opened | unread | calls rising | ...
    window_h        SMALLINT,                       -- hours the contract and talk searches looked back
    mood            SMALLINT,                       -- -5..+5
    posts_h         DOUBLE PRECISION,
    unique_authors  INTEGER,
    posts_seen      INTEGER NOT NULL DEFAULT 0,     -- verified posts, every search
    summary         TEXT,
    why             TEXT,
    notable         JSONB NOT NULL DEFAULT '[]',    -- [{author, followers, text, url, at, stance}]
    kols            JSONB NOT NULL DEFAULT '[]',    -- [{author, likes, well_known, stance, url}]
    account         JSONB,                          -- {handle, followers, created, name_changes, last_post}
    red_flags       JSONB NOT NULL DEFAULT '[]',    -- [{kind, detail, url}]
    shill_share     DOUBLE PRECISION,               -- bots and shills among the posts about it, 0..1
    first_mention   TIMESTAMPTZ,                    -- earliest post about it this read saw
    posts           JSONB NOT NULL DEFAULT '[]',    -- every verified post: {url, author, at, facet, stance, kind, ...}
    searches        JSONB NOT NULL DEFAULT '[]',    -- the queries, as run
    PRIMARY KEY (chain, address, read_at)
);
CREATE INDEX IF NOT EXISTS de_x_reads_read_at ON de_x_reads (read_at DESC);
