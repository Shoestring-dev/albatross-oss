-- The Deep End Reddit scout (albatross/deepend/reddit.py): one row per coin per Reddit read, made through Google search
-- (Gemini on OpenRouter; Reddit's own API needs approval and a paid contract). A Deep End token is keyed by chain and
-- address; a major (a tier 1-2 coin with fresh news) by its symbol, with chain and address NULL.
-- status: read (at least one thread or red flag passed the evidence gate) | none (no Reddit discussion found: the
-- control search reached Reddit and nothing about this coin passed) | blind (the search could not be shown to reach
-- Reddit; never served) | failed (the call failed; kept so the coin waits before the next try). The dossier serves the
-- latest read or none row. Only the gate's survivors are stored: every thread and red flag URL is the canonical
-- reddit.com thread that a Google grounding link of that very call led to, never the link itself. activity is counted
-- by code from the threads.
CREATE TABLE IF NOT EXISTS de_reddit_reads (
    id                  BIGSERIAL PRIMARY KEY,
    chain               TEXT,                           -- NULL for a major
    address             TEXT,                           -- NULL for a major
    symbol              TEXT,                           -- a major's key; a token's ticker when read
    read_at             TIMESTAMPTZ NOT NULL,           -- when the call started
    status              TEXT NOT NULL CHECK (status IN ('read', 'none', 'blind', 'failed')),
    picked_for          TEXT,                           -- why it was read: "3 groups, 11 calls" | "news impact 4"
    mood                SMALLINT CHECK (mood BETWEEN -5 AND 5),        -- NULL when nothing was found
    activity            TEXT CHECK (activity IN ('none', 'few', 'active')),   -- verified threads of the last 72 h
    summary             TEXT,                           -- what Reddit says, or "No Reddit discussion found."
    threads             JSONB NOT NULL DEFAULT '[]',    -- [{subreddit, title, url, posted, score, comments, stance, notable}]
    flags               JSONB NOT NULL DEFAULT '[]',    -- red flags: [{kind: rug|scam|impersonation|other, detail, url}]
    why                 TEXT,                           -- why it matters for a trader, one sentence ('' if nothing)
    citations_dropped   INTEGER NOT NULL DEFAULT 0,     -- threads and red flags whose link failed the gate
    audit               JSONB,                          -- {dropped: [why], other_coin_threads, control, links, unresolved, reddit_threads}
    agent_run_id        BIGINT REFERENCES agent_runs(id),
    CHECK ((chain IS NULL) = (address IS NULL) AND (chain IS NOT NULL OR symbol IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS de_reddit_reads_token ON de_reddit_reads (chain, address, read_at DESC) WHERE chain IS NOT NULL;
CREATE INDEX IF NOT EXISTS de_reddit_reads_major ON de_reddit_reads (symbol, read_at DESC) WHERE chain IS NULL;
CREATE INDEX IF NOT EXISTS de_reddit_reads_read_at ON de_reddit_reads (read_at DESC);
