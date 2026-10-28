-- wallets.prune() deletes readings not seen for 30 days every 15 minutes: index the column it scans.
CREATE INDEX IF NOT EXISTS de_wallet_tokens_last_at ON de_wallet_tokens (last_at);
