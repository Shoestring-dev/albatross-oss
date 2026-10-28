-- The strategy lab's rules changed after the review of 2026-10-09 (albatross/lab/run.py): factor strategies are now
-- tested on the daily-slice book in fixed blocks (the old grid moved a day every day), memecoin strategies cluster by
-- day and start on Sep 30, each side has its own baseline, and a paper slot needs today's bar. Every strategy tested
-- under the old rules goes back in the queue and is tested again, in its original order; nothing had real money on it.
UPDATE lab_strategies
SET status = 'queued', tested_at = NULL, discovery = NULL, holdout = NULL, forward = NULL, t_needed = NULL,
    score = NULL, paper_at = NULL, decided_at = NULL,
    reason = 'queued again: the lab''s rules changed on 2026-10-09'
WHERE status <> 'queued';
