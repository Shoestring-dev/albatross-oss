-- "Search X now" on a dossier (2026-10-06): the X reads the owner asks for. The host's deep_x_ask job reads each within a
-- minute or so, past the X Scout's liquidity floor and hourly pace (never past the week's Grok pool), and closes it
-- with the read's status: read | quiet | ungrounded | failed | refused (no room in the week) | expired (not read in
-- 30 minutes, the host was down).
CREATE TABLE IF NOT EXISTS de_x_asks (
    chain     TEXT NOT NULL,
    address   TEXT NOT NULL,
    asked_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    done_at   TIMESTAMPTZ,
    status    TEXT,
    note      TEXT,
    PRIMARY KEY (chain, address, asked_at)
);
CREATE INDEX IF NOT EXISTS de_x_asks_open ON de_x_asks (asked_at) WHERE done_at IS NULL;
