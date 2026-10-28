-- Paper books and the dossier report (albatross/backtest/books.py, job paper_books every 15 minutes). Each is rebuilt
-- whole from the ledger, the outcomes, the dossier history and the daily candles, so this holds only the latest: one
-- row per report, overwritten. Derived data: dropping it loses nothing the next run can't rebuild.
CREATE TABLE IF NOT EXISTS paper_reports (
    key             TEXT PRIMARY KEY,              -- books | dossier
    computed_at     TIMESTAMPTZ NOT NULL,
    report          JSONB NOT NULL
);
