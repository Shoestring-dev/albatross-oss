"""Micro-cap lane M1: pools, prices and 1m candles for every token the feed's groups call or its bot buys
(docs/SPEC-MICROCAPS.md §3-§6). The accuracy rules themselves are in rules.py.

Jobs (albatross/__main__.py)
  micro_pools      every minute  resolve chain-less ('evm') calls; rediscover each due token's pools
                                 (DexScreener /token-pairs); keep the deepest BASE-side pool; log every switch;
                                 fold rows keyed by a chain hint into the chain the token trades on (migration 013)
  micro_snapshots  every 10 s    Jupiter (Solana) + DexScreener (our pool) + our latest 1m candle ->
                                 per-source snapshots, then consensus price, sanity gate and flags in micro_state
  micro_candles    every minute  GeckoTerminal 1m OHLCV for the most overdue pools, for up to RUN_S of the minute
  micro_reconcile  hourly        cross-source disagreement (reconcile.py)
  micro_retire     hourly        retire after 14 days (unless the bot still holds it) or once liquidity has
                                 stayed under $500 for 24 h (§3)

1m candles have two sources. Birdeye (albatross/deepend/market.py) is token-level, every pool and the launchpad curve
stitched, and near-real-time: it serves tokens with Deep End heat on the chains it covers, as far as its compute units
go. GeckoTerminal (keyless, the pricing pool only) is the fallback: robinhood, cold tokens, and any token Birdeye
leaves stale. Both write micro_candles_1m and advance micro_state.candles_at, which means "candles complete up to
here" whichever source got there; GeckoTerminal only fetches tokens overdue for their tier by that stamp, so a token
Birdeye keeps fresh costs it nothing, and it never overwrites a Birdeye minute (store_candles).

Tiers (rules.TIERS; age = since the first pool, or since the first call if that is earlier):
  hot      age < 1 h, or a real-money bot buy < 1 h ago   snapshot 10 s   candles 5 min   pools 2 min
  active   age < 24 h, or an open real-money position     snapshot 30 s   candles 45 min  pools 15 min
  watch    older and still trading                        snapshot 2 min  candles 2 h     pools 6 h
  dormant  older, zero volume over 24 h                   snapshot 1 h    candles 6 h     pools 1 d
Snapshot cadence plus the sources' own caches (Jupiter 5 s, DexScreener up to 30 s) keeps a price inside its
§5 freshness budget (15 s / 60 s / 5 min). DexScreener alone cannot meet 15 s, so a hot token priced only by it
(Robinhood/BSC, or a Solana token Jupiter won't price) reads `stale` until M2's on-chain feed.

Rate-limit arithmetic, measured 2026-09-29 21:05 UTC: 456 live tokens = 16 hot, 240 active, 198 watch, 2 dormant
(333 Solana: 14 / 181 / 136 / 2); 401 have a base-side pool, 331 of them traded in the last hour.
  Jupiter      50 mints/call: hot 1 call/10 s + active 4/30 s + watch 3/2 min = 15.5/min, i.e. ~2.6 per 10 s
               against our cap of 15 per 10 s (the shared key's plan is 100 per 10 s).
  DexScreener  prices, 30 pools/call: hot 1/10 s + active 8/30 s + watch 7/2 min = 25.5/min; rediscovery, one call
               per token: 16/2 min + 240/15 min + 198/6 h = 24.5/min. ~50 of 300/min (our cap 120).
  GeckoTerminal 10/min per IP on paper, ~5 in practice (geckoterminal.py), so we use 5: 300/h, shared with
               reconciliation (20/h at :20). Demand: hot 16 x 12/h + active ~190 traded x 1.33/h + watch ~160
               traded x 0.5/h = ~530/h, more once Birdeye's day's pace is spent, so the most overdue pools go first
               and intervals stretch ~2x. Candles are history, not the live price:
               one call returns up to 1000 minutes and pools with no trades since the last fetch are skipped, so
               even fully stretched no minute is lost; only chart latency varies.
               A run fetches while the next call (its limiter wait plus the slowest call so far) fits in RUN_S
               (55 s): 5 calls, 12 s apart, done by ~50 s, and a 429 ends the round. Until 2026-09-30 a run made a fixed 7 calls,
               paced 7.5 s apart, and sat out a 20 s retry after a 429 about once a run: ~74 s, so every other run
               was skipped and candles got ~2 calls a minute.
"""

import logging
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx

from albatross import db
from albatross.micro import rules
from albatross.micro.rules import Quote, norm
from albatross.venues import dexscreener, geckoterminal, jupiter

log = logging.getLogger(__name__)

EVM_CHAINS = ["robinhood", "base", "bsc", "ethereum"]   # where a chain-less call is looked for, likeliest first
PRICED = {"solana", *EVM_CHAINS}
CANDLE_CALLS_PER_MIN = 5            # the limiter's 5/min, less reconciliation's ~20 calls at :20
RUN_S = 55                          # micro_candles starts no fetch it can't finish inside its minute
CALL_S = 5                          # a call's own time at least: GeckoTerminal ~0.4 s, storing 1000 minutes ~3 s
QUOTE_GAP = timedelta(minutes=1)    # history keeps at most one quote per source and minute (candle resolution)
HEARTBEAT = timedelta(minutes=10)   # an unchanged quote is still written this often
SLACK = timedelta(seconds=3)        # scheduler jitter: a token due in 3 s is due now
GT_LAG = timedelta(seconds=60)      # keyless GeckoTerminal refreshes every 60 s: a fetch covers up to 60 s before it
MINUTE = timedelta(minutes=1)

# One row per (trading chain, token) still tracked, with its state and the bot's exposure to it.
WORK_SQL = """
WITH t AS (
  SELECT DISTINCT ON (coalesce(resolved_chain, chain), address)
         coalesce(resolved_chain, chain) AS chain, address AS token, symbol, first_seen
  FROM micro_tokens WHERE status <> 'retired'
  ORDER BY coalesce(resolved_chain, chain), address, first_seen),
bot AS (
  SELECT chain, address, max(entry_at) AS last_entry, bool_or(status = 'open') AS open_position
  FROM micro_trades WHERE NOT coalesce(dry, false) GROUP BY 1, 2)
SELECT t.chain, t.token, coalesce(t.symbol, s.symbol) AS symbol, t.first_seen, s.created_at, s.pool,
       s.price_usd, s.price_ts, s.pending_price, s.pending_since, s.volume_h24_usd, s.txns_h1,
       s.pools_at, s.snapshot_at, s.candles_at, bot.last_entry, coalesce(bot.open_position, false) AS open_position
FROM t LEFT JOIN micro_state s ON s.chain = t.chain AND s.token = t.token
       LEFT JOIN bot ON bot.chain = t.chain AND bot.address = t.token
"""


def age(r, now: datetime) -> timedelta:
    return now - rules.born(r["created_at"], r["first_seen"])


def tier(r, now: datetime) -> str:
    since_entry = now - r["last_entry"] if r["last_entry"] else None
    return rules.tier(age(r, now), since_entry, r["open_position"], r["volume_h24_usd"])


def _due(r, now: datetime, what: int, stamp: str) -> bool:
    return r[stamp] is None or now - r[stamp] >= rules.TIERS[tier(r, now)][what] - SLACK


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


# ─── pools ───────────────────────────────────────────────────────────────────
async def discover_pools() -> int:
    pool = await db.pool()
    now = datetime.now(UTC)
    await pool.execute("UPDATE micro_tokens SET resolved_chain = chain WHERE resolved_chain IS NULL AND chain <> 'evm'")
    work = await pool.fetch(WORK_SQL)
    if await _resolve_evm([r for r in work if r["chain"] == "evm" and _due(r, now, 2, "pools_at")], now):
        work = await pool.fetch(WORK_SQL)
    due = [r for r in work if r["chain"] in PRICED and _due(r, now, 2, "pools_at")]
    due.sort(key=lambda r: r["pools_at"] or datetime.min.replace(tzinfo=UTC))
    for r in due:
        try:
            pairs, _ = await dexscreener.token_pairs(r["chain"], r["token"])
        except httpx.HTTPError as e:
            log.warning("micro pools %s %s: %s", r["chain"], r["token"], e)
            continue
        await _store_pools(r, pairs, datetime.now(UTC))
    # One token, one row: a resolved 'evm' row, or a chain hint where nothing was found, folds into the row where
    # the token trades (migration 013).
    if folded := await pool.fetchval("SELECT micro_fold_tokens()"):
        log.info("micro: folded %d token rows into the chain they trade on", folded)
    return len(due)


async def _resolve_evm(rows: list, now: datetime) -> int:
    """Find which chain a chain-less EVM call trades on: the first of EVM_CHAINS where DexScreener knows it."""
    left, found = {r["token"] for r in rows}, {}
    for chain in EVM_CHAINS:
        for batch in _chunks(sorted(left - found.keys()), dexscreener.MAX_ADDRESSES):
            try:
                pairs, _ = await dexscreener.tokens(chain, batch)
            except httpx.HTTPError as e:
                log.warning("micro resolve %s: %s", chain, e)
                continue
            for p in pairs:
                for side in ("baseToken", "quoteToken"):
                    a = norm(p[side]["address"])
                    if a in left:
                        found.setdefault(a, chain)
    pool = await db.pool()
    async with pool.acquire() as conn:
        # discover_pools then folds the row into (chain, address) with micro_fold_tokens().
        await conn.executemany(
            "UPDATE micro_tokens SET resolved_chain = $2 WHERE chain = 'evm' AND address = $1", list(found.items()))
        # Not found anywhere yet (indexer lag, or a dead address): try again after the tier's interval.
        await conn.executemany(
            """INSERT INTO micro_state (chain, token, pools_at) VALUES ('evm', $1, $2)
               ON CONFLICT (chain, token) DO UPDATE SET pools_at = EXCLUDED.pools_at""",
            [(a, now) for a in left - found.keys()])
    if found:
        log.info("micro: resolved %d chain-less tokens: %s", len(found), dict(sorted(
            {c: sum(1 for v in found.values() if v == c) for c in set(found.values())}.items())))
    return len(found)


async def _store_pools(r, pairs: list[dict], now: datetime) -> None:
    chain, token = r["chain"], r["token"]
    pools = [p for p in map(rules.pool_from_pair, pairs) if token in (p.base, p.quote)]
    chosen, reason = rules.choose_pool(pools, token, r["pool"])
    meta = next((p["baseToken"] for p in pairs if norm(p["baseToken"]["address"]) == token), {})
    created = min((p.created_at for p in pools if p.base == token and p.created_at), default=None)
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            """INSERT INTO micro_pools (chain, pool, token, quote_token, quote_symbol, dex, labels, is_base,
                                        liquidity_usd, created_at, first_seen, last_seen)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $11)
               ON CONFLICT (chain, pool, token) DO UPDATE SET liquidity_usd = EXCLUDED.liquidity_usd,
                 dex = EXCLUDED.dex, labels = EXCLUDED.labels, is_base = EXCLUDED.is_base,
                 created_at = coalesce(micro_pools.created_at, EXCLUDED.created_at), last_seen = EXCLUDED.last_seen""",
            [(chain, p.address, token, p.quote if p.base == token else p.base,
              p.quote_symbol if p.base == token else None, p.dex, list(p.labels), p.base == token,
              p.liquidity_usd, p.created_at, now) for p in pools],
        )
        if chosen and chosen.address != r["pool"]:
            prev = next((p for p in pools if p.address == r["pool"]), None)
            await conn.execute(
                """INSERT INTO micro_pool_switches (chain, token, ts, from_pool, to_pool, from_liquidity,
                                                    to_liquidity, reason) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)""",
                chain, token, now, r["pool"], chosen.address, prev.liquidity_usd if prev else None,
                chosen.liquidity_usd, reason)
            if prev and prev.curve and not chosen.curve:
                await conn.execute("UPDATE micro_pools SET migrated_from = $4 WHERE chain = $1 AND pool = $2 AND token = $3",
                                   chain, chosen.address, token, prev.address)
            if r["pool"]:
                log.info("micro pool switch %s %s: %s -> %s (%s)", chain, token, r["pool"], chosen.address, reason)
        await conn.execute(
            """INSERT INTO micro_state (chain, token, symbol, name, created_at, pool, dex, pools_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
               ON CONFLICT (chain, token) DO UPDATE SET symbol = coalesce(EXCLUDED.symbol, micro_state.symbol),
                 name = coalesce(EXCLUDED.name, micro_state.name),
                 created_at = least(micro_state.created_at, EXCLUDED.created_at),
                 pool = coalesce(EXCLUDED.pool, micro_state.pool), dex = coalesce(EXCLUDED.dex, micro_state.dex),
                 pools_at = EXCLUDED.pools_at""",
            chain, token, meta.get("symbol"), meta.get("name"), created,
            chosen.address if chosen else None, chosen.dex if chosen else None, now)


# ─── price snapshots ─────────────────────────────────────────────────────────
_written: dict[tuple, tuple] = {}   # (chain, token) -> (values, ingest_ts) of the last rows written


async def snapshots() -> int:
    pool = await db.pool()
    started = now = datetime.now(UTC)   # cadence runs start to start; freshness is judged after the fetches
    work = [r for r in await pool.fetch(WORK_SQL) if r["chain"] in PRICED and _due(r, now, 0, "snapshot_at")]
    if not work:
        return 0
    # Loosest budgets first: the tightest (hot) quotes are fetched last, so they are the freshest when judged.
    work.sort(key=lambda r: rules.freshness_budget(age(r, now)), reverse=True)
    jup = await _jupiter([r["token"] for r in work if r["chain"] == "solana"])
    ds: dict[tuple, tuple[dict, datetime]] = {}
    by_chain = defaultdict(list)
    for r in work:
        if r["pool"]:
            by_chain[r["chain"]].append(r["pool"])
    for chain, pools in by_chain.items():
        for batch in _chunks(pools, dexscreener.MAX_ADDRESSES):
            try:
                pairs, ts = await dexscreener.pairs(chain, batch)
            except httpx.HTTPError as e:
                log.warning("micro snapshots dexscreener %s: %s", chain, e)
                continue
            ds |= {(chain, norm(p["pairAddress"])): (p, ts) for p in pairs}
    last = {(c["chain"], c["token"]): c for c in await pool.fetch(
        """SELECT DISTINCT ON (c.chain, c.token) c.chain, c.token, c.ts, c.close, c.flags, c.source, s.candles_at
           FROM micro_candles_1m c JOIN micro_state s USING (chain, token)
           WHERE c.ts > now() - interval '30 minutes'
             AND (c.chain, c.token) IN (SELECT * FROM unnest($1::text[], $2::text[]))
           ORDER BY c.chain, c.token, c.ts DESC""",
        [r["chain"] for r in work], [r["token"] for r in work])}

    now = datetime.now(UTC)
    state, snaps = [], []
    for r in work:
        chain, token = r["chain"], r["token"]
        budget = rules.freshness_budget(age(r, now))
        quotes, j, p = [], None, None
        if token in jup:
            j, jts = jup[token]
            quotes.append(Quote("jupiter", _f(j.get("usdPrice")), jts))
        hit = ds.get((chain, r["pool"])) if r["pool"] else None
        if hit and norm(hit[0]["baseToken"]["address"]) == token:   # base side re-checked on every quote
            p, pts = hit
            quotes.append(Quote("dexscreener", _f(p.get("priceUsd")), pts))
        if (c := last.get((chain, token))) and "rejected_print" not in c["flags"]:
            quotes.append(Quote(c["source"], c["close"], close_held(c["ts"], c["candles_at"])))
        cons = rules.consensus(quotes, now, budget)
        flags = list(cons.flags)
        g = rules.gate(cons.price, now, r["price_usd"], r["price_ts"], len(cons.sources), r["pending_price"],
                       r["pending_since"]) if cons.price else rules.Gate(False, r["pending_price"], r["pending_since"])
        if g.flag:
            flags.append(g.flag)
        liq, liq_src = None, None
        if p and (p.get("liquidity") or {}).get("usd") is not None:
            liq, liq_src = float(p["liquidity"]["usd"]), "dexscreener"
        elif j and j.get("liquidity") is not None:
            liq, liq_src = float(j["liquidity"]), "jupiter"
        created = _iso(j.get("createdAt")) if j else None
        vol = (p.get("volume") or {}).get("h24") if p else None
        tx = (p.get("txns") or {}).get("h1") if p else None
        state.append((
            chain, token, j.get("launchpad") if j else None, created,
            cons.price if g.accept else None, cons.ts if g.accept else None, len(cons.sources), cons.sources,
            cons.spread_pct, flags, g.pending_price, g.pending_since, liq, liq_src, now if liq is not None else None,
            _f(p.get("marketCap")) if p else None, _f(p.get("fdv")) if p else None, _f(vol),
            (tx["buys"] + tx["sells"]) if tx else None, started,
        ))
        by_src = {q.source: q for q in quotes}
        fresh = {q.source: now - q.ts <= budget for q in quotes}
        if j:
            q = by_src["jupiter"]
            snaps.append((chain, token, "jupiter", now, q.ts, None, q.price, _f(j.get("liquidity")),
                          None, None, None, None, [] if fresh["jupiter"] else ["stale"]))
        if p:
            q = by_src["dexscreener"]
            snaps.append((chain, token, "dexscreener", now, q.ts, r["pool"], q.price,
                          liq if liq_src == "dexscreener" else None, _f(p.get("marketCap")), _f(p.get("fdv")),
                          _f(vol), None, [] if fresh["dexscreener"] else ["stale"]))
        # The per-source rows already give the plain median; a consensus row records the verdicts worth auditing.
        if any(f != "single_source" and not f.startswith("stale:") for f in flags):
            snaps.append((chain, token, "consensus", now, cons.ts, r["pool"], cons.price, liq,
                          None, None, None, len(cons.sources), flags))
    await _write_state(state)
    by_token = defaultdict(list)
    for s in snaps:
        by_token[s[:2]].append(s)
    rows = [s for group in by_token.values() if _changed(group) for s in group]
    await db.upsert("micro_snapshots", ["chain", "token", "source", "ingest_ts", "source_ts", "pool", "price_usd",
                                        "liquidity_usd", "mcap_usd", "fdv_usd", "volume_h24_usd", "n_sources", "flags"],
                    rows, ["chain", "token", "source", "ingest_ts"], update=[])
    return len(work)


def close_held(ts: datetime, candles_at: datetime | None) -> datetime:
    """Until when the close of the latest 1m candle (opened at `ts`) was still the price: an AMM price only moves on
    swaps, so to where the candles are complete (candles_at), and a GeckoTerminal candle still forming then to its
    minute's end, at most GT_LAG later (its fetch). No coverage stamp: the candle's own open."""
    covered = candles_at or ts - GT_LAG
    return min(covered + GT_LAG, max(ts + MINUTE, covered))


def _f(v) -> float | None:
    return float(v) if v is not None else None


def _iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _changed(rows: list[tuple]) -> bool:
    """Write a token's quotes (all sources of one poll together, so they compare like for like) when any changed
    and QUOTE_GAP has passed (a rejected print always), or HEARTBEAT after the last write. The live price is
    micro_state; this history, at most one poll per token and minute, is for audit, reconciliation and candle
    corroboration."""
    key, ts = rows[0][:2], rows[0][3]
    values = tuple((r[2], r[6], r[7], tuple(r[12])) for r in rows)
    last = _written.get(key)
    rejected = any("rejected_print" in r[12] for r in rows)
    if last and ts - last[1] < HEARTBEAT and (last[0] == values or (ts - last[1] < QUOTE_GAP and not rejected)):
        return False
    _written[key] = (values, ts)
    return True


async def _jupiter(mints: list[str]) -> dict[str, tuple[dict, datetime]]:
    out = {}
    for batch in _chunks(mints, jupiter.MAX_IDS):
        try:
            got, ts = await jupiter.prices(batch)
        except jupiter.Busy:
            log.info("micro: Jupiter window busy, yielding to live trading this round")
            break
        except httpx.HTTPError as e:
            log.warning("micro snapshots jupiter: %s", e)
            continue
        out |= {m: (v, ts) for m, v in got.items() if v}
    return out


async def _write_state(rows: list[tuple]) -> None:
    pool = await db.pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO micro_state (chain, token, launchpad, created_at, price_usd, price_ts, n_sources, sources,
                 spread_pct, flags, pending_price, pending_since, liquidity_usd, liquidity_src, liquidity_ts,
                 mcap_usd, fdv_usd, volume_h24_usd, txns_h1, snapshot_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20)
               ON CONFLICT (chain, token) DO UPDATE SET
                 launchpad = coalesce(EXCLUDED.launchpad, micro_state.launchpad),
                 created_at = least(micro_state.created_at, EXCLUDED.created_at),
                 price_usd = coalesce(EXCLUDED.price_usd, micro_state.price_usd),
                 price_ts = coalesce(EXCLUDED.price_ts, micro_state.price_ts),
                 n_sources = EXCLUDED.n_sources, sources = EXCLUDED.sources, spread_pct = EXCLUDED.spread_pct,
                 flags = EXCLUDED.flags, pending_price = EXCLUDED.pending_price,
                 pending_since = EXCLUDED.pending_since,
                 liquidity_usd = CASE WHEN EXCLUDED.liquidity_ts IS NULL THEN micro_state.liquidity_usd
                                      ELSE EXCLUDED.liquidity_usd END,
                 liquidity_src = CASE WHEN EXCLUDED.liquidity_ts IS NULL THEN micro_state.liquidity_src
                                      ELSE EXCLUDED.liquidity_src END,
                 liquidity_ts = coalesce(EXCLUDED.liquidity_ts, micro_state.liquidity_ts),
                 mcap_usd = coalesce(EXCLUDED.mcap_usd, micro_state.mcap_usd),
                 fdv_usd = coalesce(EXCLUDED.fdv_usd, micro_state.fdv_usd),
                 volume_h24_usd = coalesce(EXCLUDED.volume_h24_usd, micro_state.volume_h24_usd),
                 txns_h1 = coalesce(EXCLUDED.txns_h1, micro_state.txns_h1),
                 snapshot_at = EXCLUDED.snapshot_at""",
            rows,
        )


# ─── 1m candles ──────────────────────────────────────────────────────────────
async def candles() -> dict:
    pool = await db.pool()
    now = datetime.now(UTC)
    work = [r for r in await pool.fetch(WORK_SQL) if r["pool"] and r["chain"] in geckoterminal.NETWORK]

    def overdue(r) -> float:
        if r["candles_at"] is None:
            return float("inf")
        if r["txns_h1"] == 0 and now - r["candles_at"] < timedelta(minutes=55):
            return 0.0                              # no trades since the last fetch: nothing new to get
        return (now - r["candles_at"]) / rules.TIERS[tier(r, now)][1]

    due = sorted((r for r in work if overdue(r) >= 1), key=lambda r: (overdue(r), tier(r, now) == "hot"),
                 reverse=True)
    n = calls = 0
    limited = False
    slowest = CALL_S                        # a call's own time (response plus storing), learnt as the run goes
    t0 = time.monotonic()
    for r in due:
        # Start a call only if its wait for the limiter plus the slowest call so far still fits in the run's
        # budget: when the process is busy (a restart's catch-up), the run makes fewer calls instead of overrunning.
        wait = geckoterminal.limiter.wait_s()
        if time.monotonic() - t0 + wait + slowest > RUN_S:
            break
        since = (now - r["candles_at"]) if r["candles_at"] else None
        limit = 1000 if since is None else min(1000, int(since.total_seconds() // 60) + 10)
        calls += 1
        t1 = time.monotonic()
        try:
            n += await fetch_candles(r["chain"], r["token"], r["pool"], limit)
        except httpx.HTTPStatusError:
            log.warning("micro candles: GeckoTerminal rate limit (shared per IP), stopping this round")
            limited = True
            break
        slowest = max(slowest, time.monotonic() - t1 - wait)
    return {"rows": n, "calls": calls, "due": len(due), "slowest_s": round(slowest, 1), "rate_limited": limited}


async def fetch_candles(chain: str, token: str, pool_addr: str, limit: int = 1000) -> int:
    """Fetch, flag and store up to `limit` GeckoTerminal 1m candles of `token` from its pricing pool."""
    pool = await db.pool()
    now = datetime.now(UTC)
    try:
        raw = await geckoterminal.ohlcv_1m(chain, pool_addr, token, limit)
    except httpx.HTTPError as e:
        if isinstance(e, httpx.HTTPStatusError) and e.response.status_code == 429:
            raise                               # still due; the caller stops for this round
        err = f"HTTP {e.response.status_code}" if isinstance(e, httpx.HTTPStatusError) else type(e).__name__
        # Readers ignore candles_at while candles_error is set; it only spaces the retries.
        await pool.execute("UPDATE micro_state SET candles_at = $3, candles_error = $4 WHERE chain = $1 AND token = $2",
                           chain, token, now - GT_LAG, err)
        return 0
    n = await store_candles(chain, token, pool_addr, rules.candles_from_ohlcv(raw), "geckoterminal", now)
    await cover(chain, token, now - GT_LAG)
    return n


async def store_candles(chain: str, token: str, pool_addr: str | None, cs: list[rules.Candle], source: str,
                        now: datetime) -> int:
    """Flag and store 1m candles (oldest first) from one source: held-back prints (rules.flag_candles), and how many
    of our own quotes corroborate each minute. A Birdeye minute (every pool of the token) is never replaced by a
    GeckoTerminal one (the pricing pool only); the reverse always happens."""
    if not cs:
        return 0
    pool = await db.pool()
    prev = await pool.fetchrow(
        """SELECT ts, close FROM micro_candles_1m WHERE chain = $1 AND token = $2 AND ts < $3
             AND NOT 'rejected_print' = ANY(flags) ORDER BY ts DESC LIMIT 1""", chain, token, cs[0].ts)
    by_minute = defaultdict(list)
    for q in await pool.fetch(
            """SELECT source, source_ts, price_usd FROM micro_snapshots
               WHERE chain = $1 AND token = $2 AND source IN ('jupiter', 'dexscreener') AND price_usd > 0
                 AND ingest_ts >= $3""", chain, token, cs[0].ts - MINUTE):
        by_minute[q["source_ts"].replace(second=0, microsecond=0)].append(
            Quote(q["source"], q["price_usd"], q["source_ts"]))
    n_src = [rules.candle_sources(c, by_minute[c.ts]) for c in cs]
    flags = rules.flag_candles(cs, prev["close"] if prev else None, prev["ts"] if prev else None,
                               {c.ts for c, n in zip(cs, n_src) if n > 1})
    rows = [(chain, token, c.ts, pool_addr, c.open, c.high, c.low, c.close, c.volume_usd, source, n,
             f + (["single_source"] if n == 1 else []), now) for c, n, f in zip(cs, n_src, flags)]
    async with pool.acquire() as conn:
        await conn.executemany(
            """INSERT INTO micro_candles_1m (chain, token, ts, pool, open, high, low, close, volume_usd, source,
                                            n_sources, flags, ingest_ts)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
               ON CONFLICT (chain, token, ts) DO UPDATE SET pool = EXCLUDED.pool, open = EXCLUDED.open,
                 high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close, volume_usd = EXCLUDED.volume_usd,
                 source = EXCLUDED.source, n_sources = EXCLUDED.n_sources, flags = EXCLUDED.flags,
                 ingest_ts = EXCLUDED.ingest_ts
               WHERE micro_candles_1m.source <> 'birdeye' OR EXCLUDED.source = 'birdeye'""", rows)
    return len(rows)


async def cover(chain: str, token: str, until: datetime) -> None:
    """Candles of this token are complete up to `until`: advance micro_state.candles_at (never backwards while it
    holds) and clear a failed fetch."""
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO micro_state (chain, token, candles_at) VALUES ($1, $2, $3)
           ON CONFLICT (chain, token) DO UPDATE SET candles_error = NULL,
             candles_at = CASE WHEN micro_state.candles_error IS NULL
                               THEN greatest(micro_state.candles_at, EXCLUDED.candles_at)
                               ELSE EXCLUDED.candles_at END""", chain, token, until)


# ─── retirement ──────────────────────────────────────────────────────────────
async def retire() -> int:
    """§3: retire after 14 days (unless a real-money position is still open) or once DexScreener has measured
    the pool under $500 for a full 24 h. Unknown liquidity (NULL) never counts as low."""
    pool = await db.pool()
    rows = await pool.fetch(
        """WITH low AS (
             SELECT chain, token FROM micro_snapshots
             WHERE source = 'dexscreener' AND ingest_ts > now() - interval '24 hours' AND liquidity_usd IS NOT NULL
             GROUP BY 1, 2
             HAVING max(liquidity_usd) < 500 AND min(ingest_ts) < now() - interval '23 hours')
           UPDATE micro_tokens t SET status = 'retired', retired_at = now()
           WHERE t.status <> 'retired' AND (
             EXISTS (SELECT 1 FROM low WHERE low.chain = coalesce(t.resolved_chain, t.chain) AND low.token = t.address)
             OR (t.first_seen < now() - interval '14 days' AND NOT EXISTS (
                   SELECT 1 FROM micro_trades m WHERE m.address = t.address AND m.status = 'open'
                     AND NOT coalesce(m.dry, false))))
           RETURNING t.chain, t.address""")
    if rows:
        log.info("micro: retired %d tokens", len(rows))
    return len(rows)
