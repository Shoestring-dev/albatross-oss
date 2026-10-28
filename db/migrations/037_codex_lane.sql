-- The ChatGPT-subscription lane (albatross/llm/codex.py): each call's cost in OpenAI's Codex credits (its rate card;
-- the plan's weekly allowance is drawn in the same units), and the allowance readings the pacer works from.
ALTER TABLE agent_runs ADD COLUMN IF NOT EXISTS credits DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS codex_limits (
    checked_at    TIMESTAMPTZ PRIMARY KEY,
    used_percent  DOUBLE PRECISION NOT NULL,
    window_mins   INTEGER NOT NULL,
    resets_at     TIMESTAMPTZ NOT NULL,
    plan          TEXT,
    limit_reached TEXT,
    other_percent DOUBLE PRECISION          -- the fullest of any shorter window (some plans have none)
);
