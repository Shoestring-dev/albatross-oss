"""Cross-source reconciliation: M1's accuracy number (docs/SPEC-MICROCAPS.md §5, §8).

Hourly, for a random sample of up to 20 tokens quoted in the last hour: refetch the GeckoTerminal 1m series of the
pricing pool (one call each, ~2.5 min at our 8/min), then compare
  - every Jupiter and DexScreener quote with the GeckoTerminal candle of its minute: error vs the close, and whether
    the quote sits inside that minute's low..high. A minute without trades is compared with the last close before
    it, since a pool's price only moves on swaps;
  - each Jupiter quote with the DexScreener quote of the same poll.
Rows go to micro_reconciliation and /api/micro/health summarises them. The spec's re-derivation of candles from raw
on-chain swaps needs M2's swap feed; until then this measures how far the free sources are from each other.
"""

import bisect
import random
from datetime import UTC, datetime, timedelta

import httpx

from albatross import db
from albatross.micro import pipeline

SAMPLE = 20
WINDOW = timedelta(hours=1)
IN_RANGE_TOL = 0.01                 # quote inside low..high of its minute, give or take 1%
ASOF_LIMIT = timedelta(minutes=30)  # compare with an earlier close at most this old
SETTLE = timedelta(minutes=2)       # GeckoTerminal lags up to 60 s: skip quotes it can't have caught up with


async def run() -> int:
    pool = await db.pool()
    run_at = datetime.now(UTC)
    since = run_at - WINDOW
    cands = await pool.fetch(
        """SELECT DISTINCT s.chain, s.token, st.pool FROM micro_snapshots s
           JOIN micro_state st ON st.chain = s.chain AND st.token = s.token
           WHERE s.ingest_ts > $1 AND s.source IN ('jupiter', 'dexscreener') AND st.pool IS NOT NULL""", since)
    rows = []
    for r in random.sample(list(cands), min(SAMPLE, len(cands))):
        chain, token = r["chain"], r["token"]
        try:
            await pipeline.fetch_candles(chain, token, r["pool"], limit=120)
        except httpx.HTTPStatusError:
            pass                                # rate-limited: compare with the candles already stored
        candles = await pool.fetch(
            """SELECT ts, low, high, close FROM micro_candles_1m WHERE chain = $1 AND token = $2 AND ts >= $3
                 AND NOT 'rejected_print' = ANY(flags) ORDER BY ts""", chain, token, since - ASOF_LIMIT)
        quotes = await pool.fetch(
            """SELECT source, source_ts, ingest_ts, price_usd FROM micro_snapshots
               WHERE chain = $1 AND token = $2 AND ingest_ts > $3 AND ingest_ts < $4 AND price_usd > 0
                 AND source IN ('jupiter', 'dexscreener') ORDER BY ingest_ts""", chain, token, since, run_at - SETTLE)
        rows += compare(run_at, chain, token, candles, quotes)
    await db.upsert("micro_reconciliation", ["run_at", "chain", "token", "source", "reference", "ts", "price",
                                             "ref_price", "err_pct", "in_range"],
                    rows, ["run_at", "chain", "token", "source", "reference", "ts"], update=[])
    return len(rows)


def compare(run_at: datetime, chain: str, token: str, candles: list, quotes: list) -> list[tuple]:
    rows = []
    ts = [c["ts"] for c in candles]
    for q in quotes:
        minute = q["source_ts"].replace(second=0, microsecond=0)
        i = bisect.bisect_right(ts, minute) - 1
        if i < 0 or minute - ts[i] > ASOF_LIMIT:
            continue
        c, p = candles[i], q["price_usd"]
        in_range = (c["low"] * (1 - IN_RANGE_TOL) <= p <= c["high"] * (1 + IN_RANGE_TOL)) if ts[i] == minute else None
        rows.append((run_at, chain, token, q["source"], "geckoterminal", q["source_ts"], p, c["close"],
                     (p / c["close"] - 1) * 100, in_range))
    # A token's quotes from one poll are written together, so the same poll = the same ingest_ts.
    ds = {q["ingest_ts"]: q["price_usd"] for q in quotes if q["source"] == "dexscreener"}
    for q in quotes:
        if q["source"] == "jupiter" and (ref := ds.get(q["ingest_ts"])):
            rows.append((run_at, chain, token, "jupiter", "dexscreener", q["source_ts"], q["price_usd"], ref,
                         (q["price_usd"] / ref - 1) * 100, None))
    return rows
