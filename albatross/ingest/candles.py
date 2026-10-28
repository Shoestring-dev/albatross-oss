"""Candle sync. Every sync resumes from the newest stored candle, so downtime gaps self-heal
(as far back as each venue keeps history)."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from albatross import db
from albatross.venues import coinbase, hyperliquid, okx

log = logging.getLogger(__name__)

TF_SECONDS = {"5m": 300, "1h": 3600, "1d": 86400}
COLS = ["venue", "market_type", "symbol", "tf", "ts", "open", "high", "low", "close", "volume", "trades"]
PK = ["venue", "market_type", "symbol", "tf", "ts"]

# How far back a first-ever sync (backfill) reaches.
BACKFILL = {"5m": timedelta(days=30), "1h": timedelta(days=365), "1d": timedelta(days=365 * 5)}


async def _markets(venue: str, tiers: list[int], exclude_venue: str | None = None) -> list:
    pool = await db.pool()
    sql = """SELECT a.symbol, m.venue_symbol, m.price_multiplier
             FROM markets m JOIN assets a ON a.id = m.asset_id
             WHERE m.venue = $1 AND m.active AND a.active AND a.tier = ANY($2::smallint[])"""
    args: list = [venue, tiers]
    if exclude_venue:
        sql += """ AND NOT EXISTS (SELECT 1 FROM markets x WHERE x.asset_id = a.id
                                   AND x.venue = $3 AND x.active)"""
        args.append(exclude_venue)
    return await pool.fetch(sql, *args)


async def _last_ts(venue: str, tf: str) -> dict[str, datetime]:
    pool = await db.pool()
    rows = await pool.fetch(
        "SELECT symbol, max(ts) AS ts FROM candles WHERE venue = $1 AND tf = $2 GROUP BY symbol", venue, tf
    )
    return {r["symbol"]: r["ts"] for r in rows}


def _start(last: datetime | None, tf: str) -> datetime:
    now = datetime.now(UTC)
    # Re-fetch the newest stored candle: it may have been written while still open.
    return last if last else now - BACKFILL[tf]


# Markets fetched at once. A first sync (a year of 1h candles per market) otherwise queues every market in the venue's
# rate limiter at once, and the every-minute jobs sharing it (derivs_hl) wait behind all of them.
CONCURRENCY = 6


async def _gather(coros) -> int:
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(c):
        async with gate:
            return await c

    results = await asyncio.gather(*(one(c) for c in coros), return_exceptions=True)
    total = 0
    for r in results:
        if isinstance(r, Exception):
            log.warning("candle fetch failed: %s: %s", type(r).__name__, r)
        else:
            total += r
    if results and all(isinstance(r, Exception) for r in results):
        raise RuntimeError(f"all {len(results)} candle fetches failed")
    return total


async def sync_hyperliquid(tf: str, tiers: list[int]) -> int:
    markets = await _markets("hyperliquid", tiers)
    last = await _last_ts("hyperliquid", tf)
    now_ms = int(datetime.now(UTC).timestamp() * 1000)

    async def one(m) -> int:
        start = _start(last.get(m["symbol"]), tf)
        rows = await hyperliquid.candles(m["venue_symbol"], tf, int(start.timestamp() * 1000), now_ms)
        return await db.upsert(
            "candles", COLS,
            (hyperliquid.candle_row(c, m["symbol"], tf, m["price_multiplier"]) for c in rows), PK,
        )

    return await _gather(one(m) for m in markets)


async def sync_okx(tf: str, tiers: list[int]) -> int:
    """Only for assets Hyperliquid doesn't list."""
    markets = await _markets("okx", tiers, exclude_venue="hyperliquid")
    last = await _last_ts("okx", tf)
    backfill = {"5m": timedelta(days=7), "1h": timedelta(days=90), "1d": timedelta(days=365 * 3)}

    async def one(m) -> int:
        stored = last.get(m["symbol"])
        start = stored or datetime.now(UTC) - backfill[tf]
        start_ms = int(start.timestamp() * 1000)
        batch = await okx.candles(m["venue_symbol"], tf)
        rows = list(batch)
        # Page backwards until we reach `start`.
        while batch and int(batch[-1][0]) > start_ms:
            batch = await okx.candles(m["venue_symbol"], tf, after_ms=int(batch[-1][0]))
            rows.extend(batch)
        rows = [c for c in rows if int(c[0]) >= start_ms]
        return await db.upsert("candles", COLS, (okx.candle_row(c, m["symbol"], tf) for c in rows), PK)

    return await _gather(one(m) for m in markets)


async def sync_coinbase(tf: str, tiers: list[int]) -> int:
    markets = await _markets("coinbase", tiers)
    last = await _last_ts("coinbase", tf)
    step = timedelta(seconds=TF_SECONDS[tf] * 300)

    async def one(m) -> int:
        start = _start(last.get(m["symbol"]), tf)
        now = datetime.now(UTC)
        total = 0
        while start < now:
            end = min(start + step, now)
            rows = await coinbase.candles(m["venue_symbol"], tf, start, end)
            total += await db.upsert(
                "candles", COLS, (coinbase.candle_row(c, m["symbol"], tf) for c in rows), PK
            )
            start = end
        return total

    return await _gather(one(m) for m in markets)
