-- When the problem behind an alert began (a job's first failure, the feed's last delivery), which can be
-- well before the monitor opened the alert; messages and the dashboard say "for 36m" from here.
ALTER TABLE alerts ADD COLUMN since TIMESTAMPTZ;
UPDATE alerts SET since = first_seen;
ALTER TABLE alerts ALTER COLUMN since SET NOT NULL;
