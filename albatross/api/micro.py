"""Micro-cap lane API (docs/SPEC-MICROCAPS.md): tokens with trusted prices, one token's candles/calls/trades, and
the lane's health. Until migration 008 is applied the endpoints still answer, from micro_tokens/calls/trades only."""

import asyncio
import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException, Query

from albatross import db
from albatross.micro import pipeline, rules

router = APIRouter(prefix="/api/micro")

TOKEN_SQL = """
WITH calls AS (
  SELECT address, count(*) AS n_calls, count(DISTINCT coalesce(group_id::text, group_name)) AS n_groups,
         count(*) FILTER (WHERE called_at > now() - interval '24 hours') AS n_calls_24h,
         min(called_at) AS first_call, max(called_at) AS last_call
  FROM micro_calls GROUP BY 1),
trades AS (
  SELECT address, count(*) FILTER (WHERE NOT coalesce(dry, false)) AS n_trades,
         count(*) FILTER (WHERE status = 'open' AND NOT coalesce(dry, false)) AS n_open,
         max(entry_at) FILTER (WHERE NOT coalesce(dry, false)) AS last_entry
  FROM micro_trades GROUP BY 1)
SELECT DISTINCT ON ({chain}, t.address)
       t.chain AS called_chain, {chain} AS chain, t.address, t.first_seen, t.first_seen_via, t.status,
       t.retired_at, c.n_calls, c.n_groups, c.n_calls_24h, c.first_call, c.last_call, coalesce(tr.n_trades, 0) AS n_trades,
       coalesce(tr.n_open, 0) AS n_open, tr.last_entry, {state}
FROM micro_tokens t
LEFT JOIN calls c ON c.address = t.address
LEFT JOIN trades tr ON tr.address = t.address
{join}
WHERE {where}
ORDER BY {chain}, t.address, t.first_seen
"""
STATE_COLS = """coalesce(s.symbol, t.symbol) AS symbol, s.name, s.launchpad, s.created_at, s.pool, s.dex,
       s.price_usd, s.price_ts, s.n_sources, s.sources, s.spread_pct, s.flags, s.liquidity_usd, s.liquidity_src,
       s.liquidity_ts, s.mcap_usd, s.fdv_usd, s.volume_h24_usd, s.txns_h1, s.snapshot_at, s.pools_at, s.candles_at,
       s.candles_error"""


# ─── the price a token was called at ──────────────────────────────────────────
# A pool's price only moves when someone trades, so the price when a call was posted is the last trade before it:
# the close of the last 1-minute candle that ended before the call's minute ("pool"). Candles only count up to
# where they are complete (micro_state.candles_at: Birdeye's last settled minute for tokens with Deep End heat, else
# the last clean GeckoTerminal fetch less its 60 s lag, often an hour or more behind): past that, "no candle" means
# "not fetched yet", not "no trade", and the price is our own last live quote before the call ("quote", Jupiter or
# DexScreener, within 10 minutes). Else our trusted price from the call ledger, taken within 10 minutes after
# ("ledger"), else the first trade after the call ("after", within 15 minutes). Held-back prints (a >10x move
# nobody confirmed) and stale quotes never count.
GOOD = "NOT ('rejected_print' = ANY(m.flags))"
QUOTE = "q.source IN ('jupiter', 'dexscreener') AND q.price_usd > 0 AND NOT ('stale' = ANY(q.flags))"
# How far a token's candles are complete (the backtester's rule, backtest/ideas.py): candles_at while the last fetch
# worked, else up to the last candle we hold.
COVERED = f"""coalesce(
  (SELECT s.candles_at FROM micro_state s WHERE s.chain = {{c}}.chain AND s.token = {{c}}.address
     AND s.candles_at IS NOT NULL AND s.candles_error IS NULL),
  (SELECT max(m.ts) + interval '1 minute' FROM micro_candles_1m m WHERE m.chain = {{c}}.chain AND m.token = {{c}}.address),
  '-infinity'::timestamptz)"""
CALL_PRICE = f"""
LEFT JOIN LATERAL (SELECT m.close FROM micro_candles_1m m
                   WHERE m.chain = {{c}}.chain AND m.token = {{c}}.address AND {GOOD} AND {{c}}.called_at <= cov.until
                     AND m.ts < date_trunc('minute', {{c}}.called_at) AND m.ts >= {{c}}.called_at - interval '60 minutes'
                   ORDER BY m.ts DESC LIMIT 1) pre ON true
LEFT JOIN LATERAL (SELECT q.price_usd FROM micro_snapshots q
                   WHERE q.chain = {{c}}.chain AND q.token = {{c}}.address AND {QUOTE}
                     AND q.ingest_ts <= {{c}}.called_at AND q.ingest_ts > {{c}}.called_at - interval '10 minutes'
                   ORDER BY q.ingest_ts DESC LIMIT 1) quo ON true
LEFT JOIN LATERAL (SELECT m.open, m.ts FROM micro_candles_1m m
                   WHERE m.chain = {{c}}.chain AND m.token = {{c}}.address AND {GOOD} AND m.ts < cov.until
                     AND m.ts >= date_trunc('minute', {{c}}.called_at) AND m.ts < {{c}}.called_at + interval '15 minutes'
                   ORDER BY m.ts LIMIT 1) post ON true
LEFT JOIN micro_ledger l ON l.kind = 'call' AND l.src_id = {{c}}.src_id AND l.status = 'logged'
LEFT JOIN trade_calls tc ON tc.id = l.call_id"""
PRICE_COLS = ("pre.close AS pre_close, quo.price_usd AS quote_price, post.open AS post_open, post.ts AS post_ts, "
              "tc.ref_price AS ledger_price, tc.logged_at AS ledger_at, cov.until AS covered_until")
# Calls resolved to the chain the token is priced on (a chain-less 'evm' call was folded into one, migration 013).
CALLS_CTE = """calls AS (
  SELECT coalesce(t.resolved_chain, t.chain) AS chain, c.address, c.src_id, c.called_at, c.group_name, c.caller, c.intent
  FROM micro_calls c JOIN micro_tokens t ON t.chain = c.chain AND t.address = c.address)"""


def covered_until(v: datetime | None) -> datetime | None:
    """COVERED as read back: None when nothing is covered (asyncpg reads '-infinity' as a naive datetime.min)."""
    return None if v is None or v.replace(tzinfo=None) == datetime.min else v


def call_price(r: dict) -> tuple[float | None, str | None, float | None]:
    """-> (price, how it was found, minutes after the call it was taken)."""
    if r.get("pre_close"):
        return r["pre_close"], "pool", 0.0
    if r.get("quote_price"):
        return r["quote_price"], "quote", 0.0
    if r.get("ledger_price"):
        return r["ledger_price"], "ledger", (r["ledger_at"] - r["called_at"]).total_seconds() / 60
    if r.get("post_open"):
        return r["post_open"], "after", max(0.0, (r["post_ts"] - r["called_at"]).total_seconds() / 60)
    return None, None, None


log = logging.getLogger(__name__)
_cache: dict[str, tuple[float, object]] = {}
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_refreshing: set[str] = set()
_background: set[asyncio.Task] = set()
# first_calls and sparks run over every token ever called and take 6-7 s each (2026-10-06): for a page (stale_ok),
# past `ttl` a value up to STALE_OK old is served at once while a fresh one is built in the background, so a reader
# never waits on a rebuild once the first one is in. What gets stored (deep_compose's dossiers) asks for fresh.
STALE_OK = 600.0


async def _cached(key: str, ttl: float, fn, stale_ok: bool = True):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl:
        return hit[1]
    if stale_ok and hit and time.monotonic() - hit[0] < STALE_OK:
        if key not in _refreshing:
            _refreshing.add(key)

            async def refresh():
                try:
                    async with _locks[key]:
                        _cache[key] = (time.monotonic(), await fn())
                except Exception as e:  # noqa: BLE001 - the stale value stands; the next read tries again
                    log.warning("micro cache %s: refresh failed: %s", key, type(e).__name__)
                finally:
                    _refreshing.discard(key)
            t = asyncio.create_task(refresh())
            _background.add(t)
            t.add_done_callback(_background.discard)
        return hit[1]
    async with _locks[key]:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        val = await fn()
        _cache[key] = (time.monotonic(), val)
        return val


async def first_calls(stale_ok: bool = True) -> dict[tuple[str, str], dict]:
    """(chain, address) -> the first buy call or mention: when, where, the price then, and the peak since
    (the highest candle up to the candles' coverage, and the highest live quote after it). stale_ok: see _cached."""
    async def build():
        pool = await db.pool()
        rows = await pool.fetch(f"""
            WITH {CALLS_CTE},
            f AS (SELECT DISTINCT ON (chain, address) * FROM calls WHERE intent IS DISTINCT FROM 'sell'
                  ORDER BY chain, address, called_at)
            SELECT f.chain, f.address, f.called_at, f.group_name, {PRICE_COLS}, pk.high AS peak, pk.ts AS peak_at,
                   pq.price_usd AS qpeak, pq.ingest_ts AS qpeak_at
            FROM f CROSS JOIN LATERAL (SELECT {COVERED.format(c="f")} AS until) cov {CALL_PRICE.format(c="f")}
            LEFT JOIN LATERAL (SELECT m.high, m.ts FROM micro_candles_1m m
                               WHERE m.chain = f.chain AND m.token = f.address AND {GOOD}
                                 AND m.ts >= date_trunc('minute', f.called_at) AND m.ts < cov.until
                               ORDER BY m.high DESC LIMIT 1) pk ON true
            LEFT JOIN LATERAL (SELECT q.price_usd, q.ingest_ts FROM micro_snapshots q
                               WHERE q.chain = f.chain AND q.token = f.address AND {QUOTE}
                                 AND q.ingest_ts >= greatest(f.called_at, cov.until)
                               ORDER BY q.price_usd DESC LIMIT 1) pq ON true""")
        out = {}
        for r in rows:
            p, how, after = call_price(dict(r))
            peak, peak_at = max(((r["peak"], r["peak_at"]), (r["qpeak"], r["qpeak_at"])),
                                key=lambda x: x[0] or 0)
            out[(r["chain"], r["address"])] = {
                "call_at": r["called_at"], "call_group": r["group_name"], "call_price": p, "call_price_src": how,
                "call_price_after_min": round(after, 1) if after is not None else None,
                "peak_x": round(peak / p, 3) if p and peak else None, "peak_at": peak_at,
                "candles_until": covered_until(r["covered_until"])}
        return out
    return await _cached("first_calls", 60, build, stale_ok)


SPARK_POINTS = 40


async def sparks(where: str) -> dict[str, dict]:
    """'chain/address' -> the close since just before the first call, in SPARK_POINTS time buckets (None = no trade
    in that bucket: carry the last price forward), for the Micro-caps list's small charts."""
    async def build():
        pool = await db.pool()
        rows = await pool.fetch(f"""
            WITH {CALLS_CTE},
            f AS (SELECT DISTINCT ON (chain, address) chain, address, called_at FROM calls
                  WHERE intent IS DISTINCT FROM 'sell' ORDER BY chain, address, called_at),
            w AS (SELECT f.*, f.called_at - greatest(interval '15 minutes', (now() - f.called_at) * 0.1) AS start
                  FROM f JOIN micro_tokens t ON t.address = f.address AND coalesce(t.resolved_chain, t.chain) = f.chain
                  WHERE {where})
            SELECT w.chain, w.address, w.start, sp.b, sp.v,
                   (SELECT m.close FROM micro_candles_1m m WHERE m.chain = w.chain AND m.token = w.address AND {GOOD}
                      AND m.ts < w.start AND m.ts >= w.start - interval '6 hours' ORDER BY m.ts DESC LIMIT 1) AS before
            FROM w CROSS JOIN LATERAL (SELECT {COVERED.format(c="w")} AS until) cov
            LEFT JOIN LATERAL (
              SELECT array_agg(b ORDER BY b) AS b, array_agg(v ORDER BY b) AS v FROM (
                SELECT least({SPARK_POINTS - 1}, floor(extract(epoch FROM p.ts - w.start)
                             / greatest(60, extract(epoch FROM now() - w.start) / {SPARK_POINTS})))::int AS b,
                       (array_agg(p.px ORDER BY p.ts DESC))[1] AS v
                FROM (SELECT m.ts, m.close AS px FROM micro_candles_1m m
                      WHERE m.chain = w.chain AND m.token = w.address AND m.ts >= w.start AND m.ts < cov.until AND {GOOD}
                      UNION ALL
                      SELECT q.ingest_ts, q.price_usd FROM micro_snapshots q
                      WHERE q.chain = w.chain AND q.token = w.address AND {QUOTE}
                        AND q.ingest_ts >= greatest(w.start, cov.until)) p
                GROUP BY 1) q) sp ON true""")
        now = datetime.now(UTC)
        out = {}
        for r in rows:
            if not r["b"]:
                continue
            vals: list[float | None] = [None] * SPARK_POINTS
            for b, v in zip(r["b"], r["v"]):
                vals[b] = float(f"{v:.5g}")
            out[f"{r['chain']}/{r['address']}"] = {"t0": r["start"], "t1": now, "before": r["before"], "v": vals}
        return out
    return await _cached(f"sparks:{where}", 60, build)


async def _ready() -> bool:
    pool = await db.pool()
    return await pool.fetchval("SELECT to_regclass('micro_state') IS NOT NULL")


async def _tokens(where: str = "TRUE", *args) -> list[dict]:
    pool = await db.pool()
    if await _ready():
        sql = TOKEN_SQL.format(chain="coalesce(t.resolved_chain, t.chain)", state=STATE_COLS, where=where,
                               join="LEFT JOIN micro_state s ON s.chain = coalesce(t.resolved_chain, t.chain) "
                                    "AND s.token = t.address")
    else:
        sql = TOKEN_SQL.format(chain="t.chain", state="t.symbol", join="", where=where)
    now = datetime.now(UTC)
    out = []
    for r in await pool.fetch(sql, *args):
        d = dict(r)
        born = rules.born(d.get("created_at"), d["first_seen"])
        budget = rules.freshness_budget(now - born)
        price_ts = d.get("price_ts")
        d["age_s"] = (now - born).total_seconds()
        d["pool_age_s"] = (now - d["created_at"]).total_seconds() if d.get("created_at") else None
        d["budget_s"] = budget.total_seconds()
        d["price_age_s"] = (now - price_ts).total_seconds() if price_ts else None
        # §5: a price outside its freshness budget is unknown. price_usd is then null; last_price_usd keeps the
        # last accepted value (with price_ts) for display, so nothing reads an old price as current by accident.
        d["last_price_usd"] = d.get("price_usd")
        d["price_usd"] = rules.live_price(d.get("price_usd"), price_ts, born, now)
        d["stale"] = d["price_usd"] is None
        d["tier"] = "retired" if d["status"] == "retired" else rules.tier(
            now - born, now - d["last_entry"] if d["last_entry"] else None, d["n_open"] > 0, d.get("volume_h24_usd"))
        out.append(d)
    return out


@router.get("/tokens")
async def tokens(status: str = Query("live", pattern="^(live|retired|all)$")):
    where = {"live": "t.status <> 'retired'", "retired": "t.status = 'retired'", "all": "TRUE"}[status]
    rows = await _tokens(where)
    rows.sort(key=lambda d: d["first_seen"], reverse=True)
    if await _ready():
        firsts = await first_calls()
        for d in rows:
            d.update(firsts.get((d["chain"], d["address"])) or {})
    return {"ready": await _ready(), "now": datetime.now(UTC), "tokens": rows}


@router.get("/sparks")
async def spark_lines(status: str = Query("live", pattern="^(live|retired|all)$")):
    """Small price charts for the list: each token's close since just before its first call (cached a minute)."""
    if not await _ready():
        return {"points": SPARK_POINTS, "sparks": {}}
    where = {"live": "t.status <> 'retired'", "retired": "t.status = 'retired'", "all": "TRUE"}[status]
    return {"points": SPARK_POINTS, "sparks": await sparks(where)}


@router.get("/token/{chain}/{address}")
async def token(chain: str, address: str, minutes: int = Query(720, ge=10, le=20160)):
    address = rules.norm(address)
    # 'evm' (a chain-less call) matches the address on whichever chain it was folded into (migration 013).
    rows = await _tokens("($1 IN (t.chain, coalesce(t.resolved_chain, t.chain), 'evm')) AND t.address = $2"
                         if await _ready() else "t.chain = $1 AND t.address = $2", chain, address)
    if not rows:
        raise HTTPException(404, "unknown token")
    t = rows[0]
    pool = await db.pool()
    if await _ready():
        rows_ = await pool.fetch(f"""
            WITH {CALLS_CTE}
            SELECT calls.called_at, calls.group_name, calls.caller, calls.intent, calls.chain, calls.address, {PRICE_COLS}
            FROM calls CROSS JOIN LATERAL (SELECT {COVERED.format(c="calls")} AS until) cov {CALL_PRICE.format(c="calls")}
            WHERE calls.address = $1 ORDER BY calls.called_at""", address)
        calls = []
        for r in rows_:
            p, how, after = call_price(dict(r))
            calls.append({"called_at": r["called_at"], "group_name": r["group_name"], "caller": r["caller"],
                          "intent": r["intent"], "price": p, "price_src": how,
                          "price_after_min": round(after, 1) if after is not None else None})
        t.update((await first_calls()).get((t["chain"], t["address"])) or {})
    else:
        calls = await pool.fetch(
            """SELECT called_at, group_name, caller, intent FROM micro_calls WHERE address = $1 ORDER BY called_at""",
            address)
    trades = await pool.fetch(
        """SELECT src_id, trader, strategy, dry, caller, entry_at, entry_price, size_usd, remaining, peak,
                  realized_x, real_pnl_usd, status, closed_at, close_reason, cardinality(txs) AS n_txs
           FROM micro_trades WHERE address = $1 ORDER BY entry_at""", address)
    out = {"ready": await _ready(), "token": t, "calls": [dict(r) for r in calls],
           "trades": [dict(r) for r in trades], "candles": [], "pools": [], "switches": [], "quotes": []}
    if not out["ready"]:
        return out
    c, since = t["chain"], datetime.now(UTC) - timedelta(minutes=minutes)
    # Candles up to their coverage, then bars built from our own live quotes (marked quote=true). Up to 12 hours:
    # 1-minute bars; longer: ~360 bars. Held-back prints and stale quotes are left out before aggregating.
    covered = await pool.fetchval(f"SELECT {COVERED.format(c='k')} FROM (SELECT $1::text AS chain, $2::text AS address) k",
                                  c, address)
    bucket = max(1, -(-minutes // 360)) if minutes > 720 else 1
    out["bucket_min"] = bucket
    out["since"] = since
    out["candles_until"] = covered_until(covered)
    out["candles"] = [dict(r) for r in await pool.fetch(
        f"""WITH p AS (
              SELECT m.ts, m.open, m.high, m.low, m.close, m.volume_usd, 'single_source' = ANY(m.flags) AS single, false AS quote
              FROM micro_candles_1m m WHERE m.chain = $1 AND m.token = $2 AND m.ts >= $3 AND m.ts < $5 AND {GOOD}
              UNION ALL
              SELECT q.ingest_ts, q.price_usd, q.price_usd, q.price_usd, q.price_usd, NULL, false, true
              FROM micro_snapshots q WHERE q.chain = $1 AND q.token = $2 AND {QUOTE} AND q.ingest_ts >= greatest($3, $5))
            SELECT time_bucket(make_interval(mins => $4), ts) AS ts, first(open, ts) AS open, max(high) AS high,
                   min(low) AS low, last(close, ts) AS close, sum(volume_usd) AS volume_usd, bool_or(single) AS single,
                   bool_and(quote) AS quote
            FROM p GROUP BY 1 ORDER BY 1""", c, address, since, bucket, covered)]
    out["rejected"] = await pool.fetchval(
        """SELECT count(*) FROM micro_candles_1m WHERE chain = $1 AND token = $2 AND ts >= $3
           AND 'rejected_print' = ANY(flags)""", c, address, since)
    out["pools"] = [dict(r) for r in await pool.fetch(
        """SELECT pool, dex, labels, quote_symbol, is_base, liquidity_usd, created_at, last_seen, migrated_from
           FROM micro_pools WHERE chain = $1 AND token = $2 ORDER BY is_base DESC, liquidity_usd DESC NULLS LAST""",
        c, address)]
    out["switches"] = [dict(r) for r in await pool.fetch(
        "SELECT * FROM micro_pool_switches WHERE chain = $1 AND token = $2 ORDER BY ts", c, address)]
    out["quotes"] = [dict(r) for r in await pool.fetch(
        """SELECT source, source_ts, ingest_ts, price_usd, n_sources, flags FROM micro_snapshots
           WHERE chain = $1 AND token = $2 AND ingest_ts >= $3 ORDER BY ingest_ts LIMIT 5000""",
        c, address, max(since, datetime.now(UTC) - timedelta(hours=6)))]
    return out


@router.get("/health")
async def health():
    """Coverage and freshness per chain, and the cross-source disagreement measured by micro_reconcile."""
    pool = await db.pool()
    ready = await _ready()
    rows = await _tokens("t.status <> 'retired'")
    cov: dict[str, dict] = {}
    for d in rows:
        k = cov.setdefault(d["chain"], {"chain": d["chain"], "tokens": 0, "with_pool": 0, "priced": 0, "fresh": 0,
                                        "stale": 0, "no_price": 0, "single_source": 0, "disagree": 0,
                                        "rejected_print": 0, "with_candles": 0})
        flags = d.get("flags") or []
        k["tokens"] += 1
        k["with_pool"] += bool(d.get("pool"))
        k["priced"] += d.get("last_price_usd") is not None
        k["fresh"] += not d["stale"]
        k["stale"] += d["stale"] and d.get("last_price_usd") is not None     # had a price, now past its budget
        k["no_price"] += d.get("last_price_usd") is None                     # never priced (no pool, no quote)
        k["single_source"] += "single_source" in flags
        k["disagree"] += "disagree" in flags
        k["rejected_print"] += "rejected_print" in flags
        k["with_candles"] += d.get("candles_at") is not None and d.get("candles_error") is None
    tiers: dict[str, dict] = {}
    for d in rows:
        t = tiers.setdefault(d["tier"], {"tier": d["tier"], "tokens": 0, "fresh": 0})
        t["tokens"] += 1
        t["fresh"] += not d["stale"]
    out = {"ready": ready, "coverage": sorted(cov.values(), key=lambda k: -k["tokens"]),
           "tiers": [tiers[k] | {"snapshot_s": s.total_seconds(), "candles_s": c.total_seconds(),
                                 "pools_s": p.total_seconds()}
                     for k, (s, c, p) in rules.TIERS.items() if k in tiers],
           "candle_calls_per_min": pipeline.CANDLE_CALLS_PER_MIN,
           "retired": await pool.fetchval("SELECT count(*) FROM micro_tokens WHERE status = 'retired'"),
           "reconciliation": [], "last_run": None, "jobs": []}
    out["jobs"] = [dict(r) for r in await pool.fetch(
        "SELECT * FROM ingest_health WHERE job LIKE 'micro%' ORDER BY job")]
    if not ready:
        return out
    out["candles_24h"] = await pool.fetchval("SELECT count(*) FROM micro_candles_1m WHERE ts > now() - interval '24 hours'")
    out["last_run"] = await pool.fetchval("SELECT max(run_at) FROM micro_reconciliation")
    out["reconciliation"] = [dict(r) for r in await pool.fetch(
        """SELECT source, reference, count(*) AS n, count(DISTINCT token) AS tokens,
                  percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(err_pct)) AS median_abs_err_pct,
                  percentile_cont(0.9) WITHIN GROUP (ORDER BY abs(err_pct)) AS p90_abs_err_pct,
                  avg((abs(err_pct) <= 1)::int) AS within_1pct, avg((abs(err_pct) <= 5)::int) AS within_5pct,
                  avg(in_range::int) AS in_range, count(in_range) AS n_in_range,
                  min(run_at) AS since, count(DISTINCT run_at) AS runs
           FROM micro_reconciliation WHERE run_at > now() - interval '24 hours'
           GROUP BY 1, 2 ORDER BY 1, 2""")]
    return out

