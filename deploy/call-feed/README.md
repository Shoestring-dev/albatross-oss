# Call feed

Albatross grades memecoin calls you push to it: Telegram group calls, and a trading bot's own buys and sells. Anything
that can make an HTTPS request can be a feed: a bot's database exporter, a script, your own listener.

You don't need a feed for Telegram groups: the Setup page can sign in with your own Telegram account and read the
chats you pick. Each call's `source` is stored; `telegram` is kept for that built-in listener, so a feed that sends it
is stored as `feed:telegram`.

`POST /api/ingest/calls` with `Authorization: Bearer <CALL_FEED_TOKEN>`. The Caddy gate lets `/api/ingest/*` through
without the site login; the token is checked by the app. Send each row once it exists and again whenever a trade's
state changes. Rows are keyed by your own `src_id`, so re-sending is safe. A malformed row (a bad time, id or chain) is
skipped and reported with the reason, and the rest of the batch is stored.

```json
{
  "source": "my-listener",
  "calls": [
    {"src_id": 1001, "chain": "solana", "address": "<token mint or contract>", "group_id": -1001234567890,
     "group_name": "Example Callers", "caller": "@example_caller", "intent": "buy", "called_at": 1760000000.0}
  ],
  "trades": [
    {"src_id": 501, "trader": "u-1", "chain": "solana", "address": "<token mint>", "symbol": "EXAMPLE",
     "strategy": "default", "dry": false, "entry_at": 1760000030.0, "entry_price": 0.0000123, "size_usd": 25.0,
     "remaining": 1.0, "peak": 1.4, "realized_x": null, "real_pnl_usd": null, "status": "open",
     "closed_at": null, "close_reason": null, "txs": ["<transaction signature>"]}
  ]
}
```

- Times are Unix seconds (not milliseconds). `chain` is one of solana, ethereum, base, bsc, arbitrum, robinhood or evm.
- `src_id` is your own positive integer, under 2^51 and unique within your `source`. Two programs with different
  `source` names never collide, even if both count from 1. A trade re-sent with the same `source` and `src_id` updates
  the stored one.
- `trader` should be a pseudonym. Don't send Telegram user ids or wallet keys.
- `txs` (transaction signatures) let the dossier chart place each buy and sale at its on-chain time.
- An address ending in `#paper` is stored as a paper trade on the plain address.

The answer is `{"ok": true, "calls": n, "trades": n, "tokens": n, "folded": n, "skipped": [...]}`. Advance your
cursor only on a 2xx.

`example_push.py` sends one call, using the standard library only.
