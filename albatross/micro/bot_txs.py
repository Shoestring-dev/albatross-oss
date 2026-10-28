"""When each bot transaction happened on chain, and which way it went (2026-10-06).

The call-feed exporter sends a trade's latest state and the signatures of its transactions (micro_trades.txs), not
the time of each sale, so a partial sale (a quarter of the position on a trail, say) has no time of its own. Every
minute this job reads up to PER_RUN of those transactions from Solana's public RPC (getTransaction), newest trades
first, and keeps for each its block time and what the bot's wallet did: the fee payer's balance of the token (up: a
buy, down: a sale) and its SOL. The dossier chart marks every sale from these (api/deep.py our_markers).

Solana only: the trades on other chains have no RPC here and keep their close (closed_at) as their one sale. The
public endpoint is free and rate-limited, so the job paces itself and stops a run at the first HTTP 429; a lookup
that fails is kept with its error and tried again after RETRY.
"""

import asyncio
import logging
import os
from datetime import UTC, datetime, timedelta

import httpx

from albatross import db
from albatross.deepend.cfg import env_int
from albatross.http import client

log = logging.getLogger(__name__)

PER_RUN = 20
PACE_S = 1.5                        # between calls: the public RPC allows ~40 a 10 s per method; we use ~7
RETRY = timedelta(hours=1)
TIMEOUT = 15.0


def rpc_url() -> str:
    return os.environ.get("SOLANA_PUBLIC_RPC") or "https://api.mainnet-beta.solana.com"


TODO_SQL = """
SELECT t.trader, t.src_id, t.address, s.sig
FROM micro_trades t CROSS JOIN LATERAL unnest(t.txs) WITH ORDINALITY AS s(sig, n)
LEFT JOIN micro_trade_txs x ON x.chain = t.chain AND x.sig = s.sig
WHERE t.chain = 'solana' AND NOT coalesce(t.dry, false)
  AND (x.sig IS NULL OR (x.block_time IS NULL AND x.fetched_at < now() - $2::interval))
ORDER BY t.entry_at DESC NULLS LAST, s.n
LIMIT $1"""


def parse(tx: dict, mint: str) -> dict:
    """A getTransaction (jsonParsed) result -> {block_time, side, token_delta, sol_delta}: the fee payer's (the bot's
    wallet) change in its balance of `mint` and in SOL. A balance missing before (the account was opened in this
    transaction) or after (closed) counts as zero."""
    bt = tx.get("blockTime")
    meta = tx.get("meta") or {}
    keys = ((tx.get("transaction") or {}).get("message") or {}).get("accountKeys") or []
    payer = (keys[0].get("pubkey") if isinstance(keys[0], dict) else keys[0]) if keys else None

    def held(balances: list | None) -> float:
        return sum(float((b.get("uiTokenAmount") or {}).get("uiAmountString") or 0) for b in balances or []
                   if b.get("mint") == mint and b.get("owner") == payer)
    tok = held(meta.get("postTokenBalances")) - held(meta.get("preTokenBalances"))
    pre, post = meta.get("preBalances") or [], meta.get("postBalances") or []
    side = "failed" if meta.get("err") else "buy" if tok > 0 else "sell" if tok < 0 else "none"
    return {"block_time": datetime.fromtimestamp(bt, UTC) if bt else None, "side": side, "token_delta": tok,
            "sol_delta": (post[0] - pre[0]) / 1e9 if pre and post else None}


UPSERT = """
INSERT INTO micro_trade_txs (chain, sig, trader, src_id, block_time, side, token_delta, sol_delta, error, fetched_at)
VALUES ('solana', $1, $2, $3, $4, $5, $6, $7, $8, now())
ON CONFLICT (chain, sig) DO UPDATE SET block_time = $4, side = $5, token_delta = $6, sol_delta = $7, error = $8,
  fetched_at = now()"""


async def run() -> int:
    """micro_bot_txs: the next PER_RUN bot transactions without a time. -> transactions timed."""
    pool = await db.pool()
    try:
        todo = await pool.fetch(TODO_SQL, env_int("BOT_TXS_PER_RUN", PER_RUN), RETRY)
    except Exception as e:  # noqa: BLE001 - before migration 041: nothing to do yet
        log.info("micro_bot_txs: %s", type(e).__name__)
        return 0
    done = 0
    for i, r in enumerate(todo):
        if i:
            await asyncio.sleep(PACE_S)
        try:
            resp = await client().post(rpc_url(), timeout=TIMEOUT, json={
                "jsonrpc": "2.0", "id": 1, "method": "getTransaction",
                "params": [r["sig"], {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}]})
            if resp.status_code == 429:
                log.info("micro_bot_txs: rate-limited after %d; the rest next run", done)
                break
            resp.raise_for_status()
            body = resp.json()
            if body.get("error"):
                raise RuntimeError(f"RPC error {(body['error'] or {}).get('code')}")
            tx = body.get("result")
            if tx is None:
                raise RuntimeError("not found")
            p = parse(tx, r["address"])
            await pool.execute(UPSERT, r["sig"], r["trader"], r["src_id"], p["block_time"], p["side"], p["token_delta"],
                               p["sol_delta"], None)
            done += 1
        except (httpx.HTTPError, RuntimeError, ValueError) as e:
            await pool.execute(UPSERT, r["sig"], r["trader"], r["src_id"], None, None, None, None, str(e)[:200])
    return done
