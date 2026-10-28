-- Deterministic signals are keyed by the candle they were computed on, so re-runs are idempotent.
ALTER TABLE signals ADD COLUMN as_of TIMESTAMPTZ;
ALTER TABLE signals ADD COLUMN score DOUBLE PRECISION;   -- continuous -1..+1 behind the discrete bias
CREATE UNIQUE INDEX signals_scope_tf_source_as_of ON signals (scope, tf, source, as_of);
