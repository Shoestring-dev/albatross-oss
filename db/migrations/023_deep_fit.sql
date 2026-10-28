-- The Deep End, culture fit (docs/SPEC-DEEP-END.md, A3; code in albatross/deepend/fit.py). One row per research
-- call: does this coin's meme land with the trenches right now? Kept as history so the evidence grader can later
-- ask whether fit ever meant anything for outcomes. The dossier serves the latest row per token.
--   report      the model's full report (summary, meme, reference status, meta, audience, originality, virality,
--               risks), the citations dropped by the evidence gate, the trigger, the cost and the inputs it was given
--   citations   only those whose URL the call's own web search returned
--   confidence  'normal', or 'low' when no citation survived the gate
CREATE TABLE IF NOT EXISTS de_fit_reports (
    chain           TEXT NOT NULL,
    address         TEXT NOT NULL,
    researched_at   TIMESTAMPTZ NOT NULL,
    run_id          BIGINT,                        -- agent_runs.id of the call that produced it
    model           TEXT NOT NULL,
    score           SMALLINT NOT NULL CHECK (score BETWEEN 0 AND 10),
    label           TEXT NOT NULL,
    report          JSONB NOT NULL,
    citations       JSONB NOT NULL DEFAULT '[]',
    confidence      TEXT NOT NULL CHECK (confidence IN ('normal', 'low')),
    PRIMARY KEY (chain, address, researched_at)
);
CREATE INDEX IF NOT EXISTS de_fit_reports_recent ON de_fit_reports (researched_at DESC);
