-- Chats that sent the alerts bot's password (albatross/alerts/bot.py). Failure alerts go to every row here.
CREATE TABLE alert_subscribers (
    chat_id         BIGINT PRIMARY KEY,
    username        TEXT,
    subscribed_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
