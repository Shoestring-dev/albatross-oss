-- The built-in Telegram listener (albatross/telegram).
-- micro_calls.source: where a call came from. 'telegram' for the listener, the feed's own "source" for calls pushed to
-- /api/ingest/calls, NULL for calls stored before this migration (all from the feed).
ALTER TABLE micro_calls ADD COLUMN IF NOT EXISTS source TEXT;

-- One row: the listener's state for the Setup page (albatross/telegram/status.py). The listener runs in ingest and the
-- page in the dashboard; updated_at is the listener's heartbeat while it runs.
CREATE TABLE IF NOT EXISTS telegram_listener (
    id                INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    state             TEXT NOT NULL,     -- starting | connecting | listening | reconnecting | signed_out | error | off
    status            TEXT NOT NULL,     -- the sentence the Setup page shows
    account_name      TEXT,              -- cached at sign-in: the page never connects to Telegram to show it
    account_username  TEXT,
    titles            JSONB NOT NULL DEFAULT '{}',   -- watched chat id -> title
    last_message_at   TIMESTAMPTZ,       -- the newest message seen in a watched chat
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
