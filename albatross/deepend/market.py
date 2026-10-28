"""Birdeye candles and trade stats, dex-paid and boosts (owner: B market; docs/SPEC-DEEP-END.md section 2, S3).

1m candles. Birdeye /defi/v3/ohlcv is token-level (every pool and the launchpad curve stitched; checked complete against
GeckoTerminal and DexScreener in a September backtest) and live: measured 2026-09-30, the last trade it knew of was 4 s
old and the forming minute moved between polls 15 s apart. For tokens with heat (albatross.deepend.watch) on the chains
Birdeye serves (birdeye.CHAINS) this writes micro_candles_1m (source 'birdeye') from where the token's candles are
complete, keeps only settled minutes (SETTLE after they end) and advances micro_state.candles_at to the first unsettled
one: "candles complete up to here", the stamp the call prices, the DEX grader and every chart read. GeckoTerminal
(albatross/micro/pipeline.py) stays the fallback without being told: it fetches whatever its tier finds overdue by that
same stamp, so robinhood, cold tokens, a spent budget and Birdeye errors all land there, and it never overwrites a
Birdeye minute.

Cadence by heat: hot every 2 min, warm every 30 min, cold only when a trader opens the dossier (refresh /
refresh_candles, on priority units). A token whose pool had no trade in the last hour is rechecked every 30 min (hot) or
6 h (warm). The units are paced over the UTC day (headroom); when they run short the intervals stretch, most overdue
first within each rank (rank: new calls and open dossiers, then trending and busy tokens, then bot-held bags, then warm
ones), and GeckoTerminal covers the rest.

Compute units, measured 2026-09-30 through /utils/v1/credits (1 CU) around each call:
  /defi/v3/ohlcv                    billed on candles RETURNED (it skips empty minutes): <= 100 12 CU, <= 300 25,
                                    <= 1000 40, <= 2000 75. A quiet token's 900-minute window, 72 candles: 12 CU. Every
                                    request here asks for at most 100 (mode=count), so birdeye.CU's flat 12 is exact.
  /defi/ohlcv (v1)                  pads empty minutes and bills them (250 minutes holding 10 trades: 25 CU); unused
  /defi/v3/token/trade-data/single  10 CU;  /utils/v1/credits 1 CU;  an HTTP 400 costs nothing
Our share is DEEP_MARKET_CU_PER_DAY (default 40,000 of the plan's ~80,000 a day): 3,333 candle requests. Heat on
2026-09-30 18:00 UTC, on Birdeye's chains: 63 hot tokens (33 busy, 30 only bot-held) and 229 warm, all but 24 trading.
At the spec's cadence that needs 262,000 CU a day (hot 5 min, warm 60) to 618,000 (hot 2, warm 30), and 360,000-851,000
with robinhood (Birdeye serves it; birdeye.CHAINS does not list it yet). The share keeps new calls and opened dossiers
at roughly 5-10 min, trending tokens when units remain, and leaves bags and warm tokens to GeckoTerminal.

Market stats: Jupiter tokens v2 on Solana (read through chain.jupiter_intel, free), else Birdeye trade-data. Dex-paid
and boosts: DexScreener /orders/v1 (paid profile orders and boost purchases) and /tokens/v1 (active boosts, logo),
when a token first gets heat, then every 10 min while hot (hourly when only the bot holds it) and hourly while
warm: ~570 checks an hour at 2026-09-30's heat (41 busy hot, 324 other), 40 a minute at most.
"""

import asyncio
import importlib
import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx
import orjson

from albatross import db
from albatross.api import micro as micro_api
from albatross.deepend import birdeye, watch
from albatross.deepend.cfg import env_int, env_str
from albatross.http import WeightLimiter, client
from albatross.micro import pipeline, rules
from albatross.micro.rules import Candle
from albatross.venues import dexscreener, geckoterminal

log = logging.getLogger(__name__)

OHLCV = "/defi/v3/ohlcv"
TRADE_DATA = "/defi/v3/token/trade-data/single"
FRAMES = "5m,1h,360m,24h"           # Birdeye's hour frames are 1h 2h 4h 8h 24h only: six hours is 360m
PAGE = 100                          # candles per request, the 12-CU tier
SETTLE = timedelta(seconds=15)      # a minute counts once it ended this long ago (Birdeye's lag measured at 4 s)
MINUTE = timedelta(minutes=1)
RESUME = timedelta(minutes=90)      # re-read GeckoTerminal minutes after our last Birdeye one when they are this few
YOUNG = timedelta(hours=24)         # a token younger than this is fetched from its birth on first sight
BACKFILL = timedelta(hours=6)       # an older one this far back, and at least from an hour before its first call
PRE_CALL = timedelta(hours=1)

HOT_EVERY = timedelta(minutes=2)
WARM_EVERY = timedelta(minutes=30)
QUIET_HOT = timedelta(minutes=30)   # no trade in the pool's last hour (micro_state.txns_h1 = 0): recheck this often
QUIET_WARM = timedelta(hours=6)
RETRY = timedelta(minutes=10)       # after a failed request
UNKNOWN = timedelta(hours=2)        # Birdeye has no candle for the token at all (a young one: RETRY)
FRESH = timedelta(seconds=60)       # refresh(): candles fetched this recently need nothing
GT_OPEN = timedelta(minutes=10)     # refresh() falls back to GeckoTerminal when its candles are older than this
PAGES_JOB, PAGES_OPEN = 3, 5        # requests per token per job run / per dossier open
HOT_PER_RUN, WARM_PER_RUN = 30, 30

STATS_HOT = timedelta(minutes=15)   # Birdeye trade-data (10 CU): EVM, and hot Solana tokens Jupiter knows nothing of
STATS_WARM = timedelta(hours=2)
STATS_OPEN = timedelta(minutes=2)
STATS_FEED = timedelta(minutes=30)  # feed_rows gives change_h1 only from stats this recent

DEX_CHAINS = {"solana", "base", "bsc", "ethereum", "robinhood"}     # our chain ids are DexScreener's
DEX_HOT, DEX_WARM = timedelta(minutes=10), timedelta(hours=1)
DEX_PER_RUN = 100                   # 2.5 min of orders calls in a 5-min job
PAID = {"tokenProfile", "communityTakeover"}     # a paid DexScreener profile ("dex paid"); tokenAd is an ad
orders_limiter = WeightLimiter(per_minute=40, burst=4)             # /orders/v1 allows 60 a minute; us <= 4 a second

USAGE = "birdeye:market"            # this lane's row in de_api_usage
BURST = timedelta(minutes=30)       # the pace line runs this far ahead of the clock
LOW_LAG = timedelta(hours=1)        # warm candles and stats stay this far behind it: a reserve for hot tokens

TF = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
NEVER = datetime.min.replace(tzinfo=UTC)


def _f(v) -> float | None:
    return float(v) if v is not None else None


def _i(v) -> int | None:
    return int(v) if v is not None else None


def _minute(t: datetime) -> datetime:
    return t.replace(second=0, microsecond=0)


def _unix(t: datetime | None) -> int | None:
    return int(t.timestamp()) if t is not None else None


def _dt(t) -> datetime:
    return t if isinstance(t, datetime) else datetime.fromtimestamp(float(t), UTC)


def _key(chain: str, address: str) -> tuple[str, str]:
    return chain, rules.norm(address)


def _peer(module: str, name: str):
    """A function another Deep End module may provide (chain.jupiter_intel, xintel.markers), or None. Imported late,
    so a peer that is missing it, or fails to import, costs this lane only that piece."""
    try:
        return getattr(importlib.import_module(f"albatross.deepend.{module}"), name, None)
    except Exception:                                    # noqa: BLE001
        log.exception("deep market: albatross.deepend.%s failed to import", module)
        return None


def _iso(s) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except (TypeError, ValueError):
        return None


# ─── the lane's compute units ─────────────────────────────────────────────────
def share() -> int:
    return env_int("DEEP_MARKET_CU_PER_DAY", 40_000)


def pace_line(now: datetime, lag: timedelta = timedelta(0)) -> float:
    """Units this lane may have spent by `now`: its daily share spread over the UTC day, BURST ahead of the clock
    (less `lag`). What a quiet hour leaves unspent, a busy one may use."""
    elapsed = now - now.replace(hour=0, minute=0, second=0, microsecond=0)
    return share() * min(1.0, max(0.0, (elapsed + BURST - lag) / timedelta(days=1)))


async def spent_today() -> int:
    pool = await db.pool()
    return int(await pool.fetchval(
        """SELECT coalesce(sum(units), 0) FROM de_api_usage
           WHERE provider = $1 AND day = (now() AT TIME ZONE 'UTC')::date""", USAGE) or 0)


async def headroom(lag: timedelta = timedelta(0)) -> float:
    return pace_line(datetime.now(UTC), lag) - await spent_today()


def birdeye_serves(chain: str) -> bool:
    return chain in birdeye.CHAINS and bool(env_str("BIRDEYE_API_KEY"))


async def _birdeye(path: str, params: dict, chain: str, priority: bool = False):
    """birdeye.get (the global tally and ceiling), also counted as this lane's share (de_api_usage, provider USAGE,
    beside 'birdeye' itself and the other lanes'). Raises BudgetExceeded."""
    data = await birdeye.get(path, params, chain, priority=priority)
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_api_usage (provider, day, calls, units) VALUES ($1, (now() AT TIME ZONE 'UTC')::date, 1, $2)
           ON CONFLICT (provider, day) DO UPDATE SET calls = de_api_usage.calls + 1, units = de_api_usage.units + $2""",
        USAGE, birdeye.CU.get(path, birdeye.DEFAULT_CU))
    return data


# ─── 1m candles ──────────────────────────────────────────────────────────────
def settle(items: list[dict], asked: datetime) -> tuple[list[Candle], datetime, bool]:
    """One Birdeye page (count mode, oldest first) -> (its settled candles, how far they are complete, caught up).
    A short page, or one reaching a minute still settling, is the newest data: complete up to that minute's start.
    A full page of settled minutes may go on: complete up to the end of its last candle."""
    edge = _minute(asked - SETTLE)
    by_ts = {}
    for x in items:
        ts = datetime.fromtimestamp(int(x["unix_time"]), UTC)
        by_ts[ts] = Candle(ts, float(x["o"]), float(x["h"]), float(x["l"]), float(x["c"]), _f(x.get("v_usd")))
    cs = [by_ts[t] for t in sorted(by_ts)]
    done = [c for c in cs if c.ts < edge]
    if len(items) < PAGE or len(done) < len(cs):
        return done, edge, True
    return done, done[-1].ts + MINUTE, False


def start_at(now: datetime, covered: datetime | None, own: datetime | None, born: datetime | None,
             first_call: datetime | None) -> datetime:
    """Where a token's Birdeye fetch starts: a minute before its candles are complete (a swap indexed late lands in
    the last settled minute), or before our own last Birdeye minute when GeckoTerminal filled in only a little since;
    with no candles at all, from its birth when young, else BACKFILL back and at least an hour before its first call."""
    if covered:
        return _minute(own if own and covered - own <= RESUME else covered) - MINUTE
    if born and now - born <= YOUNG:
        return _minute(born) - MINUTE
    t = now - BACKFILL
    if first_call:
        t = min(t, first_call - PRE_CALL)
    return _minute(t)


async def _pages(chain: str, address: str, since: datetime, priority: bool,
                 max_pages: int) -> tuple[list[Candle], datetime, bool] | None:
    """Settled Birdeye candles from `since` on, how far they are complete, and whether they reached the present.
    None when the first request failed; a later failure keeps what came before it."""
    out, t, until = [], since, None
    for _ in range(max_pages):
        asked = datetime.now(UTC)
        try:
            data = await _birdeye(OHLCV, {"address": address, "type": "1m", "mode": "count", "count_limit": PAGE,
                                          "time_from": int(t.timestamp()) - 1}, chain, priority)
        except httpx.HTTPError as e:
            log.info("deep market birdeye candles %s %s: %r", chain, address, e)
            data = None
        items = data.get("items") if isinstance(data, dict) else None
        if items is None:
            return (out, until, False) if until else None
        cs, until, caught_up = settle(items, asked)
        out += cs
        if caught_up:
            return out, until, True
        t = until
    return out, until, False


STATE_SQL = f"""
SELECT k.chain, k.address, cov.until AS covered, s.created_at, s.pool, s.txns_h1, s.candles_at, s.candles_error,
       d.candles_fetched_at, d.candles_until, coalesce(d.candles_seen, false) AS candles_seen, d.candles_retry_at,
       (SELECT min(t.first_seen) FROM micro_tokens t
        WHERE coalesce(t.resolved_chain, t.chain) = k.chain AND t.address = k.address) AS first_seen,
       (SELECT min(c.called_at) FROM micro_calls c JOIN micro_tokens t ON t.chain = c.chain AND t.address = c.address
        WHERE coalesce(t.resolved_chain, t.chain) = k.chain AND c.address = k.address) AS first_call
FROM unnest($1::text[], $2::text[]) AS k(chain, address)
CROSS JOIN LATERAL (SELECT {micro_api.COVERED.format(c="k")} AS until) cov
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
LEFT JOIN de_market d ON d.chain = k.chain AND d.address = k.address"""


async def _state(keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    pool = await db.pool()
    out = {}
    for r in await pool.fetch(STATE_SQL, [k[0] for k in keys], [k[1] for k in keys]):
        d = dict(r)
        d["covered"] = micro_api.covered_until(d["covered"])
        d["born"] = rules.born(d["created_at"], d["first_seen"]) if d["first_seen"] else d["created_at"]
        out[(d["chain"], d["address"])] = d
    return out


async def _book(chain: str, address: str, fetched: datetime, until: datetime | None = None, seen: bool = False,
                error: str | None = None, retry: datetime | None = None) -> None:
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_market (chain, address, candles_fetched_at, candles_until, candles_seen, candles_error,
                                  candles_retry_at) VALUES ($1, $2, $3, $4, $5, $6, $7)
           ON CONFLICT (chain, address) DO UPDATE SET candles_fetched_at = EXCLUDED.candles_fetched_at,
             candles_until = greatest(de_market.candles_until, EXCLUDED.candles_until),
             candles_seen = de_market.candles_seen OR EXCLUDED.candles_seen,
             candles_error = EXCLUDED.candles_error, candles_retry_at = EXCLUDED.candles_retry_at""",
        chain, address, fetched, until, seen, error, retry)


async def _refresh(chain: str, address: str, s: dict, *, priority: bool, max_pages: int) -> int | None:
    """One token's Birdeye candles from where they are complete; -> candles written, None when Birdeye could not
    serve it (the request failed, or it has no candle of this token at all)."""
    now = datetime.now(UTC)
    since = start_at(now, s["covered"], s["candles_until"], s["born"], s["first_call"])
    got = await _pages(chain, address, since, priority, max_pages)
    if got is None:
        await _book(chain, address, now, error="birdeye: request failed", retry=now + RETRY)
        return None
    cs, until, _ = got
    if not cs and not s["candles_seen"]:
        # Never a candle from Birdeye for this token: unknown to it (or not indexed yet), so silence proves nothing.
        young = s["born"] is not None and now - s["born"] < timedelta(hours=1)
        await _book(chain, address, now, error="birdeye: no candles", retry=now + (RETRY if young else UNKNOWN))
        return None
    n = await pipeline.store_candles(chain, address, None, cs, "birdeye", now)
    await pipeline.cover(chain, address, until)
    await _book(chain, address, now, until=until, seen=True)
    return n


def rank(why: list[str]) -> int:
    """Who goes first when the units run short: a fresh call (its call price) or an open dossier, then trending or
    busy tokens (a discovered token has no GeckoTerminal fallback: the micro lane never priced it), then tokens hot
    only because the bot holds them. Warm tokens have their own job, behind all of these."""
    if "new call" in why or "opened" in why:
        return 0
    return 2 if why == ["bot holds it"] else 1


async def _round(tier: str, every: timedelta, quiet: timedelta, lag: timedelta, limit: int) -> int:
    toks = [t for t in await watch.tokens(tier) if birdeye_serves(t["chain"])]
    if not toks:
        return 0
    state = await _state([(t["chain"], t["address"]) for t in toks])
    now = datetime.now(UTC)
    due = []
    for t in toks:
        s = state[(t["chain"], t["address"])]
        if s["candles_retry_at"] and s["candles_retry_at"] > now:
            continue
        need = quiet if s["txns_h1"] == 0 else every
        last = max((x for x in (s["candles_fetched_at"], s["covered"]) if x), default=None)
        if last and now - last < need - pipeline.SLACK:
            continue
        due.append((rank(t["why"]), -((now - last) / need) if last else float("-inf"), t["chain"], t["address"], s))
    due.sort(key=lambda d: d[:2])
    n = done = 0
    for _, _, chain, address, s in due[:limit]:
        if await headroom(lag) < birdeye.CU[OHLCV]:
            log.info("deep market %s candles: day's pace reached, %d of %d due tokens left to GeckoTerminal",
                     tier, len(due) - done, len(due))
            break
        try:
            n += await _refresh(chain, address, s, priority=False, max_pages=PAGES_JOB) or 0
        except birdeye.BudgetExceeded as e:
            log.info("deep market %s candles: %s", tier, e)
            break
        done += 1
    return n


async def candles_hot() -> int:
    """Hot tokens' Birdeye candles (job deep_market_candles_hot, every minute)."""
    return await _round("hot", HOT_EVERY, QUIET_HOT, timedelta(0), HOT_PER_RUN)


async def candles_warm() -> int:
    """Warm tokens' Birdeye candles, from what the hot ones leave (job deep_market_candles_warm, every 5 min)."""
    return await _round("warm", WARM_EVERY, QUIET_WARM, LOW_LAG, WARM_PER_RUN)


async def refresh_candles(chain: str, address: str, priority: bool = True) -> int:
    """A trader opened this token: its candles now, from Birdeye where it serves the chain (priority units), else
    from GeckoTerminal. Free when fetched within the last minute; -> candles written."""
    chain, address = _key(chain, address)
    s = (await _state([(chain, address)]))[(chain, address)]
    now = datetime.now(UTC)
    if birdeye_serves(chain) and not (s["candles_retry_at"] and s["candles_retry_at"] > now):
        if s["candles_fetched_at"] and now - s["candles_fetched_at"] < FRESH:
            return 0
        try:
            if (n := await _refresh(chain, address, s, priority=priority, max_pages=PAGES_OPEN)) is not None:
                return n
        except birdeye.BudgetExceeded as e:
            log.info("deep market refresh %s %s: %s; GeckoTerminal instead", chain, address, e)
    if s["pool"] and chain in geckoterminal.NETWORK and not (s["covered"] and now - s["covered"] < GT_OPEN):
        limit = 1000 if not s["covered"] else min(1000, int((now - s["covered"]).total_seconds() // 60) + 10)
        try:
            return await pipeline.fetch_candles(chain, address, s["pool"], limit)
        except httpx.HTTPError as e:
            log.info("deep market refresh %s %s: GeckoTerminal %s", chain, address, e)
    return 0


# ─── market stats ────────────────────────────────────────────────────────────
STATS = ["stats_src", "stats_at", "change_m5", "change_h1", "change_h6", "change_h24", "volume_m5", "volume_h1",
         "volume_h6", "volume_h24", "buys_h1", "sells_h1", "buys_h24", "sells_h24", "traders_h1", "traders_h24",
         "net_buyers_h1", "organic_score", "organic_label", "holders", "holders_change_h1_pct"]


def stats_from_jupiter(j: dict) -> dict:
    """Jupiter tokens v2 (as chain.jupiter_intel returns it, with our fetch time in as_of) -> de_market stats. A window
    Jupiter leaves out (a quiet token has no stats5m) stays unknown."""
    w = {k: j.get(f"stats{k}") or {} for k in ("5m", "1h", "6h", "24h")}

    def vol(s: dict) -> float | None:
        b, x = s.get("buyVolume"), s.get("sellVolume")
        return None if b is None and x is None else float(b or 0) + float(x or 0)

    return {"stats_src": "jupiter", "stats_at": j.get("as_of") or _iso(j.get("updatedAt")),
            "change_m5": _f(w["5m"].get("priceChange")), "change_h1": _f(w["1h"].get("priceChange")),
            "change_h6": _f(w["6h"].get("priceChange")), "change_h24": _f(w["24h"].get("priceChange")),
            "volume_m5": vol(w["5m"]), "volume_h1": vol(w["1h"]), "volume_h6": vol(w["6h"]), "volume_h24": vol(w["24h"]),
            "buys_h1": _i(w["1h"].get("numBuys")), "sells_h1": _i(w["1h"].get("numSells")),
            "buys_h24": _i(w["24h"].get("numBuys")), "sells_h24": _i(w["24h"].get("numSells")),
            "traders_h1": _i(w["1h"].get("numTraders")), "traders_h24": _i(w["24h"].get("numTraders")),
            "net_buyers_h1": _i(w["1h"].get("numNetBuyers")), "organic_score": _f(j.get("organicScore")),
            "organic_label": j.get("organicScoreLabel"), "holders": _i(j.get("holderCount")),
            "holders_change_h1_pct": _f(w["1h"].get("holderChange"))}


def holders_change(hist: list[list], at: datetime) -> float | None:
    """% change of the holder count over about an hour: the last reading against the one closest to an hour before
    `at`, when one lies 45-75 minutes back."""
    now = at.timestamp()
    old = [(abs(now - 3600 - ts), n) for ts, n in hist if 2700 <= now - ts <= 4500 and n]
    if not old or not hist or hist[-1][1] is None:
        return None
    return (hist[-1][1] / min(old)[1] - 1) * 100


def stats_from_birdeye(d: dict, at: datetime, hist: list[list]) -> dict:
    """Birdeye trade-data (frames FRAMES) read at `at` -> de_market stats; the holder change comes from our own
    readings of the last 2 h (holders_hist), since Birdeye has none."""
    holders = _i(d.get("holder"))
    hist = [[ts, n] for ts, n in hist if at.timestamp() - ts <= 7200]
    if holders is not None:
        hist.append([int(at.timestamp()), holders])
    g = lambda k: _f(d.get(k))          # noqa: E731
    return {"stats_src": "birdeye", "stats_at": at,
            "change_m5": g("price_change_5m_percent"), "change_h1": g("price_change_1h_percent"),
            "change_h6": g("price_change_360m_percent"), "change_h24": g("price_change_24h_percent"),
            "volume_m5": g("volume_5m_usd"), "volume_h1": g("volume_1h_usd"), "volume_h6": g("volume_360m_usd"),
            "volume_h24": g("volume_24h_usd"), "buys_h1": _i(d.get("buy_1h")), "sells_h1": _i(d.get("sell_1h")),
            "buys_h24": _i(d.get("buy_24h")), "sells_h24": _i(d.get("sell_24h")),
            "traders_h1": _i(d.get("unique_wallet_1h")), "traders_h24": _i(d.get("unique_wallet_24h")),
            "net_buyers_h1": None, "organic_score": None, "organic_label": None, "holders": holders,
            "holders_change_h1_pct": holders_change(hist, at), "holders_hist": hist}


async def _save_stats(chain: str, address: str, st: dict) -> None:
    cols = STATS + (["holders_hist"] if "holders_hist" in st else [])
    vals = [orjson.dumps(st[c]).decode() if c == "holders_hist" else st[c] for c in cols]
    pool = await db.pool()
    await pool.execute(
        f"""INSERT INTO de_market (chain, address, {", ".join(cols)})
            VALUES ($1, $2, {", ".join(f"${i + 3}" for i in range(len(cols)))})
            ON CONFLICT (chain, address) DO UPDATE SET {", ".join(f"{c} = EXCLUDED.{c}" for c in cols)}""",
        chain, address, *vals)


async def jupiter(chain: str, address: str) -> dict | None:
    """Builder A's stored Jupiter tokens v2 object (chain.jupiter_intel), or None; never raises."""
    if chain != "solana" or (fn := _peer("chain", "jupiter_intel")) is None:
        return None
    try:
        return await fn(chain, address)
    except Exception:                                    # noqa: BLE001 - A's module must not break this lane
        log.exception("deep market: chain.jupiter_intel(%s, %s) failed", chain, address)
        return None


async def jupiter_many(keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """The same for many Solana tokens: chain.jupiter_intel_many (one query) when A provides it."""
    keys = [k for k in keys if k[0] == "solana"]
    if not keys:
        return {}
    if fn := _peer("chain", "jupiter_intel_many"):
        try:
            return await fn(keys) or {}
        except Exception:                                # noqa: BLE001
            log.exception("deep market: chain.jupiter_intel_many failed")
            return {}
    return {k: j for k in keys if (j := await jupiter(*k))}


async def _rows(keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    pool = await db.pool()
    out = {}
    for r in await pool.fetch(
            """SELECT d.* FROM de_market d JOIN unnest($1::text[], $2::text[]) AS k(chain, address)
               ON d.chain = k.chain AND d.address = k.address""", [k[0] for k in keys], [k[1] for k in keys]):
        d = dict(r)
        d["holders_hist"] = orjson.loads(d["holders_hist"]) if d["holders_hist"] else []
        out[(d["chain"], d["address"])] = d
    return out


async def _birdeye_stats(chain: str, address: str, row: dict, priority: bool = False) -> int:
    try:
        d = await _birdeye(TRADE_DATA, {"address": address, "frames": FRAMES}, chain, priority)
    except httpx.HTTPError as e:
        log.info("deep market birdeye stats %s %s: %r", chain, address, e)
        return 0
    if not isinstance(d, dict):
        return 0
    await _save_stats(chain, address, stats_from_birdeye(d, datetime.now(UTC), row.get("holders_hist") or []))
    return 1


async def _stats_one(chain: str, address: str, stale: timedelta, row: dict | None, priority: bool = False) -> int:
    """One token's stats: Jupiter's when it has them (stored if newer), else Birdeye's when older than `stale`."""
    row = row or {}
    if j := await jupiter(chain, address):
        st = stats_from_jupiter(j)
        if st["stats_at"] and (row.get("stats_at") is None or st["stats_at"] > row["stats_at"]):
            await _save_stats(chain, address, st)
            return 1
        return 0
    if not birdeye_serves(chain) or (row.get("stats_at") and datetime.now(UTC) - row["stats_at"] < stale):
        return 0
    return await _birdeye_stats(chain, address, row, priority)


async def stats() -> int:
    """Market stats of every token with heat (job deep_market_stats, every 2 min): Jupiter's for Solana, copied as
    chain.py refreshes them; Birdeye's for EVM (hot every 15 min on the pace line, warm every 2 h behind it) and for
    hot Solana tokens Jupiter has nothing on."""
    toks = [t for t in await watch.tokens() if t["chain"] in DEX_CHAINS]
    keys = [(t["chain"], t["address"]) for t in toks]
    rows, jups = await _rows(keys), await jupiter_many(keys)
    now = datetime.now(UTC)
    n, due = 0, []
    for t in toks:
        key, hot = (t["chain"], t["address"]), t["tier"] == "hot"
        row = rows.get(key) or {}
        if j := jups.get(key):
            st = stats_from_jupiter(j)
            if st["stats_at"] and (row.get("stats_at") is None or st["stats_at"] > row["stats_at"]):
                await _save_stats(*key, st)
                n += 1
            continue
        stale = STATS_HOT if hot else STATS_WARM
        if (t["chain"] == "solana" and not hot) or not birdeye_serves(t["chain"]) or (
                row.get("stats_at") and now - row["stats_at"] < stale):
            continue
        due.append((not hot, row.get("stats_at") or NEVER, key, row))
    due.sort(key=lambda d: d[:2])
    for warm, _, key, row in due:
        if await headroom(LOW_LAG if warm else timedelta(0)) < birdeye.CU[TRADE_DATA]:
            break
        try:
            n += await _birdeye_stats(*key, row)
        except birdeye.BudgetExceeded as e:
            log.info("deep market stats: %s", e)
            break
    return n


# ─── dex-paid and boosts (DexScreener) ───────────────────────────────────────
def parse_orders(body) -> dict:
    """DexScreener /orders/v1 -> {paid, at, orders, boosts}. Dex paid = an approved tokenProfile or communityTakeover
    order (the paid profile), `at` its first payment. The endpoint once answered a bare list of orders."""
    if isinstance(body, list):
        body = {"orders": body}
    body = body if isinstance(body, dict) else {}
    orders = [o for o in body.get("orders") or [] if isinstance(o, dict)]
    boosts = [b for b in body.get("boosts") or [] if isinstance(b, dict)]
    ms = [o["paymentTimestamp"] for o in orders
          if o.get("type") in PAID and o.get("status") == "approved" and o.get("paymentTimestamp")]
    paid = any(o.get("type") in PAID and o.get("status") == "approved" for o in orders)
    return {"paid": paid, "at": datetime.fromtimestamp(min(ms) / 1000, UTC) if ms else None,
            "orders": orders, "boosts": boosts}


def pair_info(pairs: list[dict], address: str) -> dict:
    """The token's DexScreener pairs (it on the base side) -> {boosts: active boosts, 0 when none; logo}; both None
    when DexScreener lists no pair for it."""
    mine = [p for p in pairs if rules.norm((p.get("baseToken") or {}).get("address") or "") == address]
    if not mine:
        return {"boosts": None, "logo": None}
    return {"boosts": max(int((p.get("boosts") or {}).get("active") or 0) for p in mine),
            "logo": next((p["info"]["imageUrl"] for p in mine if (p.get("info") or {}).get("imageUrl")), None)}


async def _dexscreener(path: str):
    await dexscreener.limiter.acquire()                  # the process-wide DexScreener bucket the micro lane uses
    r = await client().get(dexscreener.BASE + path, timeout=15)
    r.raise_for_status()
    return r.json()


async def _dex(keys: list[tuple[str, str]]) -> int:
    """Dex-paid orders, boosts and logo of these tokens: one /tokens/v1 call per 30 of a chain, one /orders/v1 each."""
    info: dict[tuple[str, str], dict] = {}
    by_chain = defaultdict(list)
    for chain, address in keys:
        by_chain[chain].append(address)
    for chain, addrs in by_chain.items():
        for i in range(0, len(addrs), dexscreener.MAX_ADDRESSES):
            batch = addrs[i:i + dexscreener.MAX_ADDRESSES]
            try:
                pairs = await _dexscreener(f"/tokens/v1/{chain}/{','.join(batch)}") or []
            except (httpx.HTTPError, ValueError) as e:
                log.warning("deep market dexscreener tokens %s: %s", chain, e)
                continue
            info |= {(chain, a): pair_info(pairs, a) for a in batch}
    pool = await db.pool()
    n = 0
    for chain, address in keys:
        await orders_limiter.acquire()
        try:
            o = parse_orders(await _dexscreener(f"/orders/v1/{chain}/{address}"))
        except (httpx.HTTPError, ValueError) as e:
            log.warning("deep market dexscreener orders %s %s: %s", chain, address, e)
            continue
        pi = info.get((chain, address)) or {}
        await pool.execute(
            """INSERT INTO de_market (chain, address, dex_paid, dex_paid_at, dex_orders, boosts, logo, dex_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, now())
               ON CONFLICT (chain, address) DO UPDATE SET dex_paid = EXCLUDED.dex_paid,
                 dex_paid_at = EXCLUDED.dex_paid_at, dex_orders = EXCLUDED.dex_orders,
                 boosts = coalesce(EXCLUDED.boosts, de_market.boosts), logo = coalesce(EXCLUDED.logo, de_market.logo),
                 dex_at = EXCLUDED.dex_at""",
            chain, address, o["paid"], o["at"], orjson.dumps({"orders": o["orders"], "boosts": o["boosts"]}).decode(),
            pi.get("boosts"), pi.get("logo"))
        n += 1
    return n


async def dexpaid() -> int:
    """Dex-paid and boosts (job deep_market_dexpaid, every 5 min): a token when it first gets heat, then every 10 min
    while hot (hourly when only the bot holds it) and hourly while warm."""
    toks = [t for t in await watch.tokens() if t["chain"] in DEX_CHAINS]
    rows = await _rows([(t["chain"], t["address"]) for t in toks])
    now = datetime.now(UTC)
    due = []
    for t in toks:
        at = (rows.get((t["chain"], t["address"])) or {}).get("dex_at")
        busy = t["tier"] == "hot" and t["why"] != ["bot holds it"]
        if at is None or now - at >= (DEX_HOT if busy else DEX_WARM):
            due.append((at or NEVER, (t["chain"], t["address"])))
    due.sort()
    return await _dex([k for _, k in due[:DEX_PER_RUN]])


# ─── on a dossier open ───────────────────────────────────────────────────────
async def refresh(chain: str, address: str) -> int:
    """Everything the market section and the chart need, now: candles (refresh_candles), stats older than 2 min, and
    dex-paid never checked or older than 10 min. Each part returns at once when fresh, so calling this on every
    dossier open is cheap. -> rows written."""
    chain, address = _key(chain, address)
    row = (await _rows([(chain, address)])).get((chain, address)) or {}
    dex_at = row.get("dex_at")
    parts = [refresh_candles(chain, address), _stats_one(chain, address, STATS_OPEN, row, priority=True)]
    if chain in DEX_CHAINS and (dex_at is None or datetime.now(UTC) - dex_at >= DEX_HOT):
        parts.append(_dex([(chain, address)]))
    n = 0
    for got in await asyncio.gather(*parts, return_exceptions=True):
        if isinstance(got, birdeye.BudgetExceeded):
            log.info("deep market refresh %s %s: %s", chain, address, got)
        elif isinstance(got, BaseException):
            log.warning("deep market refresh %s %s failed: %r", chain, address, got)
        else:
            n += got
    return n


# ─── the dossier's market section and the feed ───────────────────────────────
MARKET_SQL = """
SELECT s.price_usd, s.price_ts, s.mcap_usd, s.fdv_usd, s.liquidity_usd, d.*
FROM (SELECT $1::text AS chain, $2::text AS address) k
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
LEFT JOIN de_market d ON d.chain = k.chain AND d.address = k.address"""


def market_section(r: dict, live: dict | None = None, j: dict | None = None) -> dict | None:
    """The "market" contract (spec section 4). r = micro_state (price, mcap, fdv, liquidity: the micro lane's trusted
    consensus) joined with de_market; live = stats read just now (Jupiter), used when newer than the stored ones; j =
    the Jupiter object, whose mcap/fdv/liquidity fill in where the micro lane has none (a bonding curve's pair on
    DexScreener reports no market cap). None when there is nothing at all."""
    st = live if live and live.get("stats_at") and (r.get("stats_at") is None or live["stats_at"] >= r["stats_at"]) \
        else r
    j = j or {}
    if r.get("price_usd") is None and st.get("stats_at") is None and r.get("dex_at") is None:
        return None
    pick = lambda mine, theirs: mine if mine is not None else _f(theirs)      # noqa: E731
    return {
        "price": r.get("price_usd"), "price_ts": r.get("price_ts"),
        "mcap": pick(r.get("mcap_usd"), j.get("mcap")), "fdv": pick(r.get("fdv_usd"), j.get("fdv")),
        "liquidity": pick(r.get("liquidity_usd"), j.get("liquidity")),
        "change": {"m5": st.get("change_m5"), "h1": st.get("change_h1"), "h6": st.get("change_h6"),
                   "h24": st.get("change_h24")},
        "volume": {"m5": st.get("volume_m5"), "h1": st.get("volume_h1"), "h6": st.get("volume_h6"),
                   "h24": st.get("volume_h24")},
        "txns": {"h1": {"buys": st.get("buys_h1"), "sells": st.get("sells_h1")},
                 "h24": {"buys": st.get("buys_h24"), "sells": st.get("sells_h24")}},
        "traders": {"h1": st.get("traders_h1"), "h24": st.get("traders_h24")},
        "net_buyers_h1": st.get("net_buyers_h1"), "organic_score": st.get("organic_score"),
        "organic_label": st.get("organic_label"), "holders": st.get("holders"),
        "holders_change_h1_pct": st.get("holders_change_h1_pct"),
        "dex_paid": {"paid": r["dex_paid"], "at": r.get("dex_paid_at")} if r.get("dex_paid") is not None else None,
        "boosts": r.get("boosts"),
        "as_of": st.get("stats_at") or r.get("price_ts"),
    }


async def section_market(chain: str, address: str) -> dict | None:
    chain, address = _key(chain, address)
    pool = await db.pool()
    r = dict(await pool.fetchrow(MARKET_SQL, chain, address))
    j = await jupiter(chain, address)
    return market_section(r, stats_from_jupiter(j) if j else None, j)


FEED_SQL = """
SELECT k.chain, k.address, s.symbol, s.name, s.price_usd, s.price_ts, s.mcap_usd, s.liquidity_usd, s.created_at,
       d.change_h1, d.stats_at, d.logo,
       (SELECT min(t.first_seen) FROM micro_tokens t
        WHERE coalesce(t.resolved_chain, t.chain) = k.chain AND t.address = k.address) AS first_seen
FROM unnest($1::text[], $2::text[]) AS k(chain, address)
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
LEFT JOIN de_market d ON d.chain = k.chain AND d.address = k.address"""


async def feed_rows(keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """(chain, address) -> {price, last_price, price_ts, mcap, liquidity, change_h1, symbol, name, logo}, one query.
    price follows the micro lane's freshness budget (None when stale; last_price and price_ts keep the last accepted
    one); change_h1 only from stats under 30 min old."""
    if not keys:
        return {}
    pool = await db.pool()
    now, out = datetime.now(UTC), {}
    for r in await pool.fetch(FEED_SQL, [k[0] for k in keys], [rules.norm(k[1]) for k in keys]):
        if all(r[c] is None for c in ("price_usd", "mcap_usd", "liquidity_usd", "stats_at", "logo", "symbol")):
            continue
        born = rules.born(r["created_at"], r["first_seen"]) if r["first_seen"] else (r["created_at"] or now)
        fresh = r["stats_at"] is not None and now - r["stats_at"] <= STATS_FEED
        out[(r["chain"], r["address"])] = {
            "price": rules.live_price(r["price_usd"], r["price_ts"], born, now), "last_price": r["price_usd"],
            "price_ts": r["price_ts"], "mcap": r["mcap_usd"], "liquidity": r["liquidity_usd"],
            "change_h1": r["change_h1"] if fresh else None, "symbol": r["symbol"], "name": r["name"], "logo": r["logo"]}
    return out


# ─── the candles endpoint ────────────────────────────────────────────────────
POINTS_SQL = f"""
SELECT m.ts, m.open, m.high, m.low, m.close, m.volume_usd, m.source
FROM micro_candles_1m m
WHERE m.chain = $1 AND m.token = $2 AND m.ts >= $3::timestamptz AND m.ts < least($4::timestamptz, $5::timestamptz)
  AND {micro_api.GOOD}
UNION ALL
SELECT q.ingest_ts, q.price_usd, q.price_usd, q.price_usd, q.price_usd, NULL, 'quotes'
FROM micro_snapshots q
WHERE q.chain = $1 AND q.token = $2 AND {micro_api.QUOTE} AND q.ingest_ts >= greatest($3::timestamptz, $5::timestamptz)
  AND q.ingest_ts < $4::timestamptz
ORDER BY 1"""
CALLS_SQL = f"""
WITH {micro_api.CALLS_CTE}
SELECT calls.called_at, calls.group_name, calls.intent, {micro_api.PRICE_COLS}
FROM calls CROSS JOIN LATERAL (SELECT {micro_api.COVERED.format(c="calls")} AS until) cov
{micro_api.CALL_PRICE.format(c="calls")}
WHERE calls.chain = $1 AND calls.address = $2 AND calls.called_at >= $3 AND calls.called_at < $4
ORDER BY calls.called_at"""
BOT_SQL = """
SELECT entry_at, entry_price, strategy FROM micro_trades
WHERE chain = $1 AND address = $2 AND NOT coalesce(dry, false) AND entry_at >= $3 AND entry_at < $4
ORDER BY entry_at"""


def aggregate(points, minutes: int) -> list[list]:
    """1m candles and quote points (ts, open, high, low, close, volume, ...), oldest first -> [[t, o, h, l, c, v]] in
    buckets of `minutes` aligned to the epoch, t in unix seconds: the first open, highest high, lowest low, last close
    and summed volume (None when no point in the bucket had one)."""
    size, out = minutes * 60, []
    for ts, o, h, lo, c, v, *_ in points:
        t = int(ts.timestamp()) // size * size
        if out and out[-1][0] == t:
            b = out[-1]
            b[2], b[3], b[4] = max(b[2], h), min(b[3], lo), c
            if v is not None:
                b[5] = (b[5] or 0.0) + v
        else:
            out.append([t, o, h, lo, c, v])
    return out


def bars_source(sources: set[str]) -> str | None:
    """'birdeye', 'geckoterminal', both joined by '+', or 'quotes' when only our live quotes made the bars."""
    candles_ = sorted(sources - {"quotes"})
    return "+".join(candles_) if candles_ else ("quotes" if sources else None)


async def candles(chain: str, address: str, tf: str, start, end) -> dict:
    """The candles endpoint body: {source, candles: [[t, o, h, l, c, v]], markers: [{t, kind, label, price}], until}.
    Times are unix seconds. 1m candles up to where they are complete (`until`, the micro lane's coverage rule), then
    bars of our own live quotes (volume None), both aggregated to `tf` (1m 5m 15m 1h). Markers: group calls at their
    call price (albatross.api.micro's rule), the bot's real-money entries, and notable X posts when the X module
    provides xintel.markers(chain, address, start, end)."""
    if tf not in TF:
        raise ValueError(f"tf must be one of {', '.join(TF)}")
    chain, address = _key(chain, address)
    start, end = _dt(start), _dt(end)
    pool = await db.pool()
    covered = await pool.fetchval(
        f"SELECT {micro_api.COVERED.format(c='k')} FROM (SELECT $1::text AS chain, $2::text AS address) k",
        chain, address)
    points = await pool.fetch(POINTS_SQL, chain, address, start, end, covered)
    markers = []
    for r in await pool.fetch(CALLS_SQL, chain, address, start, end):
        price, _, _ = micro_api.call_price(dict(r))
        label = r["group_name"] or "call"
        markers.append({"t": _unix(r["called_at"]), "kind": "call", "price": price,
                        "label": f"{label} (sell)" if r["intent"] == "sell" else label})
    for r in await pool.fetch(BOT_SQL, chain, address, start, end):
        markers.append({"t": _unix(r["entry_at"]), "kind": "bot", "price": r["entry_price"],
                        "label": f"bot buy ({r['strategy']})" if r["strategy"] else "bot buy"})
    if fn := _peer("xintel", "markers"):
        try:
            for m in await fn(chain, address, start, end) or []:
                markers.append({"t": _unix(_dt(m["t"])), "kind": "x", "label": m.get("label"),
                                "price": m.get("price")})
        except Exception:                                # noqa: BLE001 - the X module must not break the chart
            log.exception("deep market: xintel.markers(%s, %s) failed", chain, address)
    markers.sort(key=lambda m: m["t"])
    return {"source": bars_source({p["source"] for p in points}), "candles": aggregate(points, TF[tf]),
            "markers": markers, "until": _unix(micro_api.covered_until(covered))}


JOBS = {
    "deep_market_candles_hot": candles_hot,
    "deep_market_candles_warm": candles_warm,
    "deep_market_stats": stats,
    "deep_market_dexpaid": dexpaid,
}
SCHEDULE = [
    ("deep_market_candles_hot", {"second": 25}),
    ("deep_market_candles_warm", {"minute": "*/5", "second": 40}),
    ("deep_market_stats", {"minute": "*/2", "second": 55}),
    ("deep_market_dexpaid", {"minute": "1-59/5", "second": 5}),
]
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE = {"deep_market_candles_hot": 60, "deep_market_candles_warm": 300, "deep_market_stats": 120,
           "deep_market_dexpaid": 300}
