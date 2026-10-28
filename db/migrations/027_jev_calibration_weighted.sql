-- Jev's correction weights recent grades most (albatross/agents/jev_calibration.py: each graded candidate counts
-- 0.5^(age / its timeframe's half-life)), so a bin's n and hits are the weighted counts its p_cal rests on.
ALTER TABLE jev_calibration ALTER COLUMN n TYPE DOUBLE PRECISION, ALTER COLUMN hits TYPE DOUBLE PRECISION;
