-- Jev's probabilities corrected against its own graded record (albatross/agents/jev_calibration.py): per
-- timeframe, each bin of what Jev said maps to the share that reached the target (shrunk toward the timeframe's
-- rate, monotone). Refit after every grading pass; "all" is every timeframe together.
CREATE TABLE IF NOT EXISTS jev_calibration (
    tf          TEXT NOT NULL,
    bin_lo      DOUBLE PRECISION NOT NULL,       -- what Jev said, from ...
    bin_hi      DOUBLE PRECISION NOT NULL,       -- ... up to (exclusive)
    n           INT NOT NULL,                    -- graded candidates in the bin
    hits        INT NOT NULL,                    -- of which reached the target first
    p_cal       DOUBLE PRECISION NOT NULL,       -- what that bin is read as
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tf, bin_lo)
);

-- The corrected probability each candidate was scored with (NULL before 2026-09-30). From then on ev_r and the
-- paper book use p_cal; p_win stays Jev's own number.
ALTER TABLE jev_scores ADD COLUMN IF NOT EXISTS p_cal DOUBLE PRECISION;
