-- The Deep End's shared plumbing (docs/SPEC-DEEP-END.md): paid-API usage per provider and day (Birdeye compute
-- units, so the monthly allowance is paced), and dossier views (a token a trader opened is hot for 10 minutes).
CREATE TABLE IF NOT EXISTS de_api_usage (
    provider    TEXT NOT NULL,
    day         DATE NOT NULL,
    calls       BIGINT NOT NULL DEFAULT 0,
    units       BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, day)
);
CREATE TABLE IF NOT EXISTS de_interest (
    chain       TEXT NOT NULL,
    address     TEXT NOT NULL,
    viewed_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (chain, address)
);
