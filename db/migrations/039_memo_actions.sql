-- The CEO memo's actions on Home (redesign step 3, 2026-10-06): each memo keeps its actions as data, and what the owner did
-- about each one is recorded with the state before, so a change made from Home can be undone.
ALTER TABLE reports ADD COLUMN IF NOT EXISTS meta JSONB;

CREATE TABLE IF NOT EXISTS memo_acts (
    report_id  BIGINT      NOT NULL REFERENCES reports (id),
    idx        SMALLINT    NOT NULL,                -- the action's place in the memo, from 0
    target     TEXT        NOT NULL,                -- the agent it changed
    action     TEXT        NOT NULL,                -- pause | retire | reinstate | budget
    after      JSONB       NOT NULL,                -- what it set: {"rung": ...} or {"scale": ...}
    before     JSONB       NOT NULL,                -- what was there, for undo
    acted_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    undone_at  TIMESTAMPTZ,
    PRIMARY KEY (report_id, idx)
);
