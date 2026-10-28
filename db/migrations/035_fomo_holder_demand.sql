-- FOMO holder lookups: which hot tokens were due one each UTC day, and which got it (albatross/deepend/fomo.py
-- holders()). The subscription test's credit leg reads it: the free tier runs short when due tokens go unserved.
CREATE TABLE IF NOT EXISTS de_fomo_holder_demand (
    day             DATE NOT NULL,
    chain           TEXT NOT NULL,
    token_address   TEXT NOT NULL,
    first_due_at    TIMESTAMPTZ NOT NULL,
    looked_at       TIMESTAMPTZ,
    PRIMARY KEY (day, chain, token_address)
);
