-- The events calendar (albatross/events/): TGEs, token launches, exchange listings, unlocks, mainnets and upgrades,
-- governance votes, macro releases. Every collector writes through events.store.upsert(), which de-duplicates on
-- `key` and merges sources, so one real-world event is one row however many feeds report it.
CREATE TABLE events (
    id              BIGSERIAL PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,          -- store.make_key(kind, subject, day, venue)
    kind            TEXT NOT NULL,                 -- tge | listing | delisting | unlock | mainnet | upgrade | airdrop |
                                                   -- governance | macro | conference | other
    title           TEXT NOT NULL,                 -- plain words, <= 120 chars: "OKX lists VVV (Venice) for spot"
    subject         TEXT NOT NULL,                 -- what it is about: a symbol, a project or a release name
    venue           TEXT,                          -- exchange / chain / institution, when it matters (okx, upbit, fed)
    starts_at       TIMESTAMPTZ NOT NULL,          -- best known time; midnight UTC when only the day is known
    precision       TEXT NOT NULL DEFAULT 'day',   -- exact | day | week | month | quarter
    ends_at         TIMESTAMPTZ,
    assets          TEXT[] NOT NULL DEFAULT '{}',  -- tracked symbols it touches (upper case)
    chains          TEXT[] NOT NULL DEFAULT '{}',
    importance      SMALLINT NOT NULL DEFAULT 2,   -- 1 minor .. 5 market-moving
    status          TEXT NOT NULL DEFAULT 'scheduled',   -- rumored | scheduled | happened | cancelled
    probability     DOUBLE PRECISION,              -- 0..1 when a market prices it (Polymarket)
    summary         TEXT,                          -- one or two plain sentences
    sources         JSONB NOT NULL DEFAULT '[]',   -- [{collector, name, url, seen_at, excerpt}] every feed that reported it
    first_seen      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON events (starts_at);
CREATE INDEX ON events USING gin (assets);
CREATE INDEX ON events (kind, starts_at);
