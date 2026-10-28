"""The Deep End API (owner F; contract in docs/SPEC-DEEP-END.md section 4, fixtures in docs/deepend/fixtures/).

  GET /api/deep/feed?view=hot|new|called|trending&chain=      tokens on our radar (cached 15 s)
  GET /api/deep/token/{chain}/{address}                        the dossier, composed on demand (cached 20 s)
  GET /api/deep/candles/{chain}/{address}?tf=&minutes=         candles and markers for the pro chart: group calls,
                                                                the bot's buys, every verified X post, X red flags,
                                                                red flags raised or cleared (2026-10-06: each with
                                                                the detail its hover card shows)
  POST /api/deep/x-read {chain, address}                        "Search X now" (2026-10-06): queue an X read for
  GET  /api/deep/x-read/{chain}/{address}                       this token (the host reads it within a minute or so)
  GET /api/deep/search?q=                                       search as you type (2026-10-06): tokens on our radar
  POST /api/deep/watch {address}                                search (2026-10-06): a pasted contract address that
                                                                nothing on our radar has, put on the watch list (its
                                                                pool read from DexScreener) so its dossier can be built

Opening a dossier marks the token hot for 10 minutes (watch.touch) and asks the chain and market collectors to
refresh it (their refresh(chain, address), when they have one): we wait up to REFRESH_WAIT_S for them, the rest
lands in the next compose. Candles come from market.candles(); while it has none, our own micro candles up to their
coverage and bars from our live quotes after it (the Micro-caps chart's rule, albatross/api/micro.py).
"""

import asyncio
import logging
import re
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import httpx
import orjson
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from albatross import db
from albatross.api import micro as microapi
from albatross.deepend import chain as chain_intel
from albatross.deepend import birdeye, discovery, dossier, market, watch, xintel
from albatross.micro import rules

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/deep")

REFRESH_WAIT_S = 6.0
TF_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60}
MAX_BARS = 5000
NOT_ON_RADAR = ("We have no evidence on this token yet: it hasn't been called in the groups we follow or picked up "
                "by discovery (Birdeye trending, DexScreener boosts and profiles).")

_cache: dict[str, tuple[float, object]] = {}
_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
_background: set[asyncio.Task] = set()


async def _cached(key: str, ttl: float, fn):
    async with _locks[key]:
        hit = _cache.get(key)
        if hit and time.monotonic() - hit[0] < ttl:
            return hit[1]
        val = await fn()
        _cache[key] = (time.monotonic(), val)
    if len(_cache) > 500:                  # one entry per dossier opened: drop the expired ones
        old = [k for k, (t, _) in _cache.items() if time.monotonic() - t > 60]
        for k in old:
            _cache.pop(k, None)
            _locks.pop(k, None)
    return val


@router.get("/feed")
async def feed(view: str = Query("hot", pattern="^(hot|new|called|trending)$"), chain: str | None = None):
    return await _cached(f"feed:{view}:{chain or ''}", 15, lambda: dossier.feed(view, chain or None))


def _done(t: asyncio.Task) -> None:
    _background.discard(t)
    if not t.cancelled() and t.exception() is not None:
        log.warning("deep refresh failed: %s: %s", type(t.exception()).__name__, str(t.exception())[:200])


async def refresh(chain: str, address: str) -> None:
    """Ask the chain and market collectors to refresh this token now; wait up to REFRESH_WAIT_S."""
    tasks = []
    for mod in (chain_intel, market):
        fn = getattr(mod, "refresh", None) or getattr(mod, "refresh_token", None)
        if fn is None:
            continue
        try:
            t = asyncio.ensure_future(fn(chain, address))
        except Exception as e:  # noqa: BLE001 - a broken refresh leaves the stored data
            log.warning("deep refresh %s: %s", mod.__name__, type(e).__name__)
            continue
        _background.add(t)
        t.add_done_callback(_done)
        tasks.append(t)
    if tasks:
        await asyncio.wait(tasks, timeout=REFRESH_WAIT_S)       # the stragglers keep running


async def _dossier(chain: str, address: str) -> dict:
    await refresh(chain, address)
    doc = await dossier.compose(chain, address)
    if doc is None:
        raise HTTPException(404, NOT_ON_RADAR)
    return doc


@router.get("/token/{chain}/{address}")
async def token(chain: str, address: str):
    key = await dossier.resolve(chain, rules.norm(address))
    if key is None:
        raise HTTPException(404, NOT_ON_RADAR)
    try:
        await watch.touch(*key)
    except Exception as e:  # noqa: BLE001 - a missed heat mark must never cost the reader the dossier
        log.info("deep: touch %s/%s failed: %s", *key, e)
    return await _cached(f"token:{key[0]}:{key[1]}", 20, lambda: _dossier(*key))


# ─── search: a token nothing has called yet ─────────────────────────────────────
SOLANA_ADDR = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
EVM_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
EVM_TRY = ("base", "bsc", "ethereum")               # where an EVM address is looked for, in this order
SEARCHED = "searched"                               # its de_watch source


SEARCH_SQL = """
WITH t AS (
    SELECT coalesce(m.resolved_chain, m.chain) AS chain, m.address, m.symbol, NULL::text AS name, m.first_seen
    FROM micro_tokens m WHERE m.status <> 'retired'
    UNION ALL
    SELECT w.chain, w.address, w.meta->>'symbol', w.meta->>'name', w.first_seen FROM de_watch w
), named AS (               -- the dossier's symbol and name win over the lists' (cheap fields only: filter first)
    SELECT DISTINCT ON (t.chain, t.address) t.chain, t.address, t.first_seen,
           coalesce(d.snapshot->'token'->>'symbol', t.symbol) AS symbol, coalesce(d.snapshot->'token'->>'name', t.name) AS name
    FROM t LEFT JOIN de_dossier d ON d.chain = t.chain AND d.address = t.address
    ORDER BY t.chain, t.address, t.name IS NULL
), hit AS (                -- rank: the exact ticker, then tickers that start with q, then names that hold it
    SELECT *, CASE WHEN lower(symbol) = $6 THEN 0 WHEN lower(symbol) LIKE $4 ESCAPE '\\' THEN 1 ELSE 2 END AS rank
    FROM named
    WHERE CASE WHEN $3 THEN address = $1
               ELSE lower(symbol) LIKE $4 ESCAPE '\\' OR (length($1) >= 3 AND lower(coalesce(name, '')) LIKE $5 ESCAPE '\\') END
), top AS (                -- the best 40 of however many match, before the costlier fields
    SELECT h.*, (d.snapshot->'market'->>'mcap')::float8 AS mcap
    FROM hit h LEFT JOIN de_dossier d ON d.chain = h.chain AND d.address = h.address
    ORDER BY h.rank, mcap DESC NULLS LAST
    LIMIT 40
)
SELECT h.chain, h.address, h.symbol, h.name, h.mcap, h.rank,
       coalesce((d.snapshot->'token'->>'created_at')::timestamptz, h.first_seen) AS born, d.red_flags, d.snapshot IS NOT NULL AS has_dossier,
       (SELECT count(*) FROM jsonb_array_elements(d.snapshot->'checklist') c
         WHERE c->>'key' = ANY($2::text[]) AND c->>'status' IN ('good', 'warn', 'bad'))::int AS flag_ran
FROM top h LEFT JOIN de_dossier d ON d.chain = h.chain AND d.address = h.address"""


@router.get("/search")
async def search(q: str = Query(min_length=1, max_length=80)):
    """Memecoins search as you type: every token on our radar (called, priced or discovered) whose ticker starts with
    q, or whose name holds it (3 letters or more), or whose address is q: the exact ticker first, then tickers that
    start with q, then names, each the largest first. Each with its red-flag state, so a shared ticker can be told
    apart before anything opens."""
    q = q.strip().lstrip("$").strip()               # "$IMD", as Telegram writes a ticker
    if not q:
        return {"q": q, "address": False, "hits": []}
    addr = bool(SOLANA_ADDR.match(q) or EVM_ADDR.match(q))
    like = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pool = await db.pool()
    rows = await pool.fetch(SEARCH_SQL, rules.norm(q) if addr else q, list(dossier.CORE_FLAG_KEYS), addr, f"{like}%",
                            f"%{like}%", q.lower())
    now = datetime.now(UTC)
    hits = sorted((dict(r) for r in rows), key=lambda r: (r["rank"], -(r["mcap"] or 0)))[:8]
    return {"q": q, "address": addr, "hits": [{
        "chain": r["chain"], "address": r["address"], "symbol": r["symbol"], "name": r["name"], "mcap": r["mcap"],
        "age_s": (now - r["born"]).total_seconds() if r["born"] else None,
        "red_flags": list(r["red_flags"] or []) if r["has_dossier"] else None,
        "flag_checks": {"ran": r["flag_ran"], "of": len(dossier.CORE_FLAG_KEYS)} if r["has_dossier"] else None,
    } for r in hits]}


class Watch(BaseModel):
    address: str = Field(min_length=20, max_length=80)


def _action_header(request: Request) -> None:
    """A dependency, so a POST without the header is refused before its body is read."""
    if request.headers.get("x-albatross-action") != "1":
        raise HTTPException(403, "missing the X-Albatross-Action header")


@router.post("/watch", dependencies=[Depends(_action_header)])
async def add_watch(body: Watch):
    """Memecoins search: a contract address we already know opens its dossier; one we don't is looked up on
    DexScreener and, when it has a pool, goes on the watch list like a discovery find (no liquidity floor: a pasted address is
    wanted on purpose) and is marked hot, so its dossier is composed now. Needs the X-Albatross-Action header."""
    a = body.address.strip()
    chains = ["solana"] if SOLANA_ADDR.match(a) else list(EVM_TRY) if EVM_ADDR.match(a) else []
    if not chains:
        raise HTTPException(422, "That isn't a Solana or EVM contract address.")
    a = rules.norm(a)
    for c in chains + (["evm"] if a.startswith("0x") else []):
        key = await dossier.resolve(c, a)
        if key:
            return {"chain": key[0], "address": key[1], "new": False}
    for c in chains:
        stats = (await discovery._pairs(c, [a])).get(a)
        if not stats:
            continue
        now = datetime.now(UTC)
        info = {k: stats.get(k) for k in ("symbol", "name", "logo", "links")}
        row = discovery.merge(None, [discovery._find(c, a, SEARCHED, pair=stats, info=info)], now)
        await db.upsert("de_watch", ["chain", "address", "first_seen", "last_seen", "trending_at", "rank", "sources", "meta"],
                        [(c, a, row["first_seen"], row["last_seen"], row["trending_at"], row["rank"], row["sources"],
                          orjson.dumps(row["meta"]).decode())],
                        ["chain", "address"], update=["last_seen", "sources", "meta"])
        try:
            await watch.touch(c, a)
        except Exception as e:  # noqa: BLE001 - the heat mark is a nicety; the token is on the list either way
            log.info("deep: touch %s/%s failed: %s", c, a, e)
        log.info("deep: searched token %s/%s added to the watch list", c, a)
        return {"chain": c, "address": a, "new": True}
    raise HTTPException(404, "No trading pool found for this address on Solana, Base, BNB Chain or Ethereum.")


# ─── candles ─────────────────────────────────────────────────────────────────
def iso(t) -> str | None:
    """Unix seconds (or ms), a datetime or ISO text -> ISO 8601 UTC, the fixtures' form."""
    if t is None:
        return None
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t / 1000 if t > 1e11 else t, UTC).isoformat()
    if isinstance(t, datetime):
        return (t if t.tzinfo else t.replace(tzinfo=UTC)).isoformat()
    return str(t)


def _epoch(t) -> float | None:
    s = iso(t)
    try:
        return datetime.fromisoformat(s).timestamp() if s else None
    except ValueError:
        return None


def merge_markers(*groups: list[dict]) -> list[dict]:
    """All markers, ISO times, once each (the same id, else the same time, kind and label), in time order, each with
    its id (None when it has none) and the detail its hover card shows (None when it has none)."""
    out, seen = [], set()
    for m in (m for g in groups for m in g or []):
        t = _epoch(m.get("t"))
        if t is None:
            continue
        if m.get("id"):
            k = (m.get("kind"), m["id"])
        elif m.get("kind") == "bot":           # one entry from two sources: the engine names it "bot buy (strategy)"
            k = (int(t), "bot", m.get("price"))
        else:                                  # to the second: the engine's times are whole seconds, ours aren't
            k = (int(t), m.get("kind"), m.get("label"))
        if k in seen:
            continue
        seen.add(k)
        out.append({"t": iso(m["t"]), "kind": m.get("kind"), "label": m.get("label"), "price": m.get("price"),
                    "id": m.get("id"), "detail": m.get("detail")})
    return sorted(out, key=lambda m: _epoch(m["t"]))


# the red-flag lines' names (deepend/dossier.py _sources), for "raised" and "cleared" markers
FLAG_LINE = {"mint": "Mint & freeze revoked", "lp": "Liquidity locked", "dev": "Deployer history",
             "insiders": "Insider network", "wallets": "Wallet network", "top10": "Top 10 holders",
             "tax": "Transfer fee", "liquidity": "Liquidity", "organic": "Organic volume", "reddit": "Reddit",
             "github": "GitHub"}


def flag_events(rows: list[dict], start: datetime, evm: bool = False) -> list[dict]:
    """Red-flag checks that failed (kind "flag") or stopped failing ("flag_clear") between one dossier version and the
    next, from `start`; rows: de_dossier_history (at, checklist) in time order, the first one before `start` when the
    token has one (its flags were raised before the chart begins). A token's first version raises what it fails."""
    out, prev = [], None
    for r in rows:
        lines = {c["key"]: c for c in (_json(r["checklist"]) or []) if c.get("key") in dossier.RED_FLAG_KEYS}
        bad = {k for k, c in lines.items() if c.get("status") == "bad"}
        if r["at"] >= start:
            before = prev if prev is not None else set()
            for k in sorted(bad - before) + sorted(before - bad):
                name = "Honeypot & tax" if k == "tax" and evm else FLAG_LINE.get(k, k)
                c = lines.get(k) or {}
                out.append({"t": r["at"], "kind": "flag" if k in bad else "flag_clear", "label": name, "price": None,
                            "id": f"{k}:{r['at'].isoformat()}", "detail": {"key": k, "value": c.get("value"),
                                                                           "first": prev is None}})
        prev = bad
    return out


def _json(v):
    return orjson.loads(v) if isinstance(v, str | bytes) else v


BOT_TRADES_SQL = """
SELECT src_id, trader, strategy, caller, entry_at, entry_price, size_usd, status, closed_at, close_reason, realized_x,
       real_pnl_usd
FROM micro_trades WHERE chain = $1 AND address = $2 AND NOT coalesce(dry, false)"""
TRADE_TXS_SQL = """
SELECT x.trader, x.src_id, x.sig, x.block_time, x.side, x.token_delta, x.sol_delta
FROM micro_trade_txs x JOIN micro_trades t ON t.trader = x.trader AND t.src_id = x.src_id
WHERE t.chain = $1 AND t.address = $2 AND NOT coalesce(t.dry, false) AND x.block_time IS NOT NULL ORDER BY x.block_time"""
CLOSE_IS_TX = timedelta(minutes=10)      # a trade's last sale this near its close is that close


def bot_markers(trades: list[dict], txs: list[dict], start: datetime) -> list[dict]:
    """The bot's real trades as markers from `start`: each buy, and each sale, from the trade's transactions
    timed on chain (micro_trade_txs: partial sales too, each with the share of the position it sold and the SOL it
    brought in) or, when none is timed (other chains, or not looked up yet), from the trade's close (bot_sale). The
    last sale near the close carries the close's title, result and reason."""
    by: dict[tuple, list[dict]] = defaultdict(list)
    for x in txs:
        by[(x["trader"], x["src_id"])].append(x)
    out = []
    for r in trades:
        mine = sorted(by.get((r["trader"], r["src_id"]), []), key=lambda x: x["block_time"])
        buys, sells = [x for x in mine if x["side"] == "buy"], [x for x in mine if x["side"] == "sell"]
        bought = sum(x["token_delta"] or 0 for x in buys)
        buy = {"size_usd": r.get("size_usd"), "caller": r.get("caller"), "account": r["trader"],
               "sol": -buys[0]["sol_delta"] if buys and buys[0]["sol_delta"] is not None else None,
               "sig": buys[0]["sig"] if buys else None}
        if r.get("entry_at") and r["entry_at"] >= start:
            out.append({"t": r["entry_at"], "kind": "bot", "label": r.get("strategy") or "bot", "price": r.get("entry_price"),
                        "detail": buy})
        elif r.get("entry_at") and (r.get("status") != "closed" or (r.get("closed_at") and r["closed_at"] >= start)):
            # bought before the chart begins and held into it: the buy at the chart's left edge, with its own time
            out.append({"t": start, "kind": "bot", "label": r.get("strategy") or "bot", "price": r.get("entry_price"),
                        "id": f"{r['trader']}:{r['src_id']}:earlier", "detail": {**buy, "earlier": True, "bought_at": r["entry_at"]}})
        held = {"bought_at": r.get("entry_at"), "buy_price": r.get("entry_price"), "caller": r.get("caller")}
        close, closed_by_tx = bot_sale(r, start), False
        for j, x in enumerate(sells):
            last = j == len(sells) - 1 and close is not None and abs(close["t"] - x["block_time"]) <= CLOSE_IS_TX
            closed_by_tx |= last
            if x["block_time"] < start:
                continue
            share = min(1.0, -x["token_delta"] / bought) if bought > 0 and x["token_delta"] else None
            detail = {"title": f"Our bot sold {round(share * 100)}%" if share is not None and share < 0.995 else "Our bot sold the rest"
                      if share is not None else "Our bot sold", "share": share, "sol": x["sol_delta"], "sig": x["sig"],
                      "account": r["trader"], **held}
            if last:
                detail.update({k: close["detail"][k] for k in ("title", "reason", "realized_x", "pnl_usd", "size_usd", "held_s")})
            out.append({"t": x["block_time"], "kind": "bot_sale", "label": r.get("strategy") or "bot", "price": None,
                        "id": f"{r['trader']}:{r['src_id']}:{x['sig'][:16]}", "detail": detail})
        if close and not closed_by_tx:
            out.append({**close, "detail": {**close["detail"], **held}})
    return out


def bot_sale(r: dict, start: datetime) -> dict | None:
    """A real trade's close as a marker (kind "bot_sale", at its close), titled by how it closed: the bot's own exits,
    a sale by hand, tokens that left the wallet, a wallet reset. None while it is open, before `start`, or when the
    buy never landed (no position was ever held, so nothing was sold)."""
    why, at = r.get("close_reason") or "", r.get("closed_at")
    if r.get("status") != "closed" or at is None or at < start or "never" in why or "no position" in why:
        return None
    title = "Sold by hand" if why.startswith(("manual", "reconciled — manual")) else \
        "Sold outside the bot" if "outside the bot" in why else "Closed: wallet reset" if why == "wallet reset" else "Our bot sold"
    held = (at - r["entry_at"]).total_seconds() if r.get("entry_at") else None
    return {"t": at, "kind": "bot_sale", "label": r.get("strategy") or "bot", "price": None,
            "id": f"{r.get('trader')}:{r.get('src_id')}",
            "detail": {"title": title, "reason": why, "realized_x": r.get("realized_x"), "pnl_usd": r.get("real_pnl_usd"),
                       "size_usd": r.get("size_usd"), "held_s": held, "account": r.get("trader")}}


FLAG_HISTORY_SQL = """
(SELECT at, checklist FROM de_dossier_history WHERE chain = $1 AND address = $2 AND at < $3 ORDER BY at DESC LIMIT 1)
UNION ALL
(SELECT at, checklist FROM de_dossier_history WHERE chain = $1 AND address = $2 AND at >= $3 ORDER BY at)"""


covered_until = microapi.covered_until          # one reading of COVERED for the whole site


async def our_candles(chain: str, address: str, tf_min: int, start: datetime,
                      end: datetime | None = None) -> tuple[list[list], datetime | None]:
    """Our micro candles up to their coverage, then bars from our live quotes (rejected prints and stale quotes out),
    from start to end (now when None) -> (bars, until): until is the coverage, so every bar from there on is built
    from quotes (None: all of them)."""
    end = end or datetime.now(UTC)
    pool = await db.pool()
    covered = await pool.fetchval(               # '-infinity' when nothing is covered; it round-trips as a parameter
        f"SELECT {microapi.COVERED.format(c='k')} FROM (SELECT $1::text AS chain, $2::text AS address) k", chain, address)
    rows = await pool.fetch(
        f"""WITH p AS (
              SELECT m.ts, m.open, m.high, m.low, m.close, m.volume_usd FROM micro_candles_1m m
              WHERE m.chain = $1 AND m.token = $2 AND m.ts >= $3 AND m.ts < least($5::timestamptz, $6::timestamptz)
                AND {microapi.GOOD}
              UNION ALL
              SELECT q.ingest_ts, q.price_usd, q.price_usd, q.price_usd, q.price_usd, NULL FROM micro_snapshots q
              WHERE q.chain = $1 AND q.token = $2 AND {microapi.QUOTE} AND q.ingest_ts >= greatest($3, $5)
                AND q.ingest_ts < $6::timestamptz)
            SELECT time_bucket(make_interval(mins => $4), ts) AS ts, first(open, ts) AS open, max(high) AS high,
                   min(low) AS low, last(close, ts) AS close, sum(volume_usd) AS volume_usd
            FROM p GROUP BY 1 ORDER BY 1""", chain, address, start, tf_min, covered, end)
    return [[iso(r["ts"]), r["open"], r["high"], r["low"], r["close"], r["volume_usd"]] for r in rows], \
        covered_until(covered)


async def our_markers(chain: str, address: str, start: datetime) -> list[dict]:
    """Group calls (at the price the call was made at), the bot's real-money buys and their closes, red flags raised or
    cleared, and the X Scout's posts and red flags (xintel.markers)."""
    calls = (await dossier.priced_calls([(chain, address)])).get((chain, address), [])
    out = [{"t": c["at"], "kind": "call", "label": c["group"], "price": c["price"],
            "detail": {"caller": c.get("caller"), "intent": c.get("intent")}} for c in calls if c["at"] >= start]
    pool = await db.pool()
    trades = [dict(r) for r in await pool.fetch(BOT_TRADES_SQL, chain, address)]
    try:
        txs = [dict(r) for r in await pool.fetch(TRADE_TXS_SQL, chain, address)] if trades else []
    except Exception as e:  # noqa: BLE001 - before migration 041 the sales fall back to the closes
        log.info("deep trade txs: %s", type(e).__name__)
        txs = []
    out += bot_markers(trades, txs, start)
    try:
        out += flag_events([dict(r) for r in await pool.fetch(FLAG_HISTORY_SQL, chain, address, start)], start,
                           evm=chain in dossier.EVM)
    except Exception as e:  # noqa: BLE001 - the flags' history is optional on the chart
        log.warning("deep flag markers: %s", type(e).__name__)
    fn = getattr(xintel, "markers", None)
    if fn:
        try:
            out += list(await fn(chain, address, start, datetime.now(UTC)) or [])
        except Exception as e:  # noqa: BLE001 - X markers are optional
            log.warning("deep x markers: %s", type(e).__name__)
    return out


# The candles we never stored (the price pipeline starts a token's candles when it comes onto the radar): before a
# window's first stored candle, Birdeye's at the chart's timeframe, so a days-old coin charts back to its first trades.
BIRDEYE_TF = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1H"}
HISTORY_KEEP = 1800.0
_history: dict[tuple, tuple[float, float, list]] = {}     # (chain, address, tf, upto) -> (fetched, from, bars)


async def history(chain: str, address: str, tf: str, start: datetime, upto: datetime) -> list[list]:
    """Candles from `start` to `upto` that we never stored, from Birdeye at the chart's timeframe: one call (up to 1000
    candles, 40 units, a dossier's priority share), kept HISTORY_KEEP per token, timeframe and edge, so the chart's
    15-second refresh never asks twice. [] where Birdeye doesn't serve the chain, has none, or the day's units are spent."""
    if not market.birdeye_serves(chain) or upto - start < timedelta(minutes=2 * TF_MINUTES[tf]):
        return []
    k, now = (chain, address, tf, int(upto.timestamp())), time.monotonic()
    hit = _history.get(k)
    if hit and now - hit[0] < HISTORY_KEEP and hit[1] <= start.timestamp():
        return [b for b in hit[2] if _epoch(b[0]) >= start.timestamp()]
    try:
        d = await market._birdeye(market.OHLCV, {"address": address, "type": BIRDEYE_TF[tf], "mode": "range",
                                                 "time_from": int(start.timestamp()), "time_to": int(upto.timestamp()) - 1},
                                  chain, priority=True)
    except (birdeye.BudgetExceeded, httpx.HTTPError) as e:
        log.info("deep candles history %s %s: %s", chain, address, type(e).__name__)
        return []
    items = d.get("items") if isinstance(d, dict) else None
    bars = sorted(([iso(datetime.fromtimestamp(int(x["unix_time"]), UTC)), float(x["o"]), float(x["h"]), float(x["l"]),
                    float(x["c"]), float(x["v_usd"]) if x.get("v_usd") is not None else None]
                   for x in items or [] if int(x["unix_time"]) < upto.timestamp()), key=lambda b: b[0])
    if len(_history) > 500:                               # an hour of browsing: drop what has expired
        for old in [x for x, v in _history.items() if now - v[0] >= HISTORY_KEEP]:
            _history.pop(old, None)
    _history[k] = (now, start.timestamp(), bars)
    return bars


@router.get("/candles/{chain}/{address}")
async def candles(chain: str, address: str, tf: str = Query("1m", pattern="^(1m|5m|15m|1h)$"),
                  minutes: int = Query(720, ge=10, le=20160), before: str | None = Query(None, max_length=40)):
    """`minutes` of `tf` candles up to now, or up to `before` (an ISO time): the chart pages back with it as the
    reader scrolls left. Markers are ours (our_markers: every call, bot trade, X post and red flag with its detail),
    inside the same window."""
    key = await dossier.resolve(chain, rules.norm(address))
    if key is None:
        raise HTTPException(404, NOT_ON_RADAR)
    tf_min = TF_MINUTES[tf]
    end = datetime.now(UTC)
    if before:
        try:
            end = min(end, datetime.fromisoformat(before.replace("Z", "+00:00")).astimezone(UTC))
        except ValueError:
            raise HTTPException(422, "before must be an ISO time") from None
    start = end - timedelta(minutes=min(minutes, tf_min * MAX_BARS))
    try:
        body = await market.candles(*key, tf, start, end) or {}
    except Exception as e:  # noqa: BLE001 - fall back to our own candles
        log.warning("deep candles: market.candles failed: %s: %s", type(e).__name__, str(e)[:200])
        body = {}
    bars = [[iso(c[0]), *c[1:6]] for c in body.get("candles") or []]
    source, until = body.get("source"), body.get("until")
    if not bars and await microapi._ready():
        bars, until = await our_candles(*key, tf_min, start, end)
        source = "albatross" if bars else None
    first = datetime.fromtimestamp(_epoch(bars[0][0]), UTC) if bars else end
    if older := await history(*key, tf, start, first):
        bars = older + bars
        source = f"{source} + birdeye history" if source else "birdeye"
    # the engine's own markers (calls, bot buys, X posts by label) are the same events without their detail: ours only
    markers = [m for m in merge_markers(await our_markers(*key, start)) if _epoch(m["t"]) < end.timestamp()]
    return {"source": source, "candles": bars, "markers": markers, "until": iso(until)}


# ─── "Search X now" on a dossier ───────────────────────────────────────────────
class XRead(BaseModel):
    chain: str = Field(min_length=2, max_length=20)
    address: str = Field(min_length=20, max_length=80)


@router.post("/x-read", dependencies=[Depends(_action_header)])
async def x_read(body: XRead):
    """Queue an X read of this token for the host's deep_x_ask job: past the X Scout's liquidity floor and pace,
    never past the week's Grok pool. A token already waiting answers with its ask. -> the ask (xintel.ask_state)."""
    key = await dossier.resolve(body.chain, rules.norm(body.address))
    if key is None:
        raise HTTPException(404, NOT_ON_RADAR)
    st = await xintel.ask_state(*key)
    if st and st["done_at"] is None:
        return st
    last = await xintel.last_attempt(*key)
    if last and datetime.now(UTC) - last < xintel.ASK_AGAIN:
        raise HTTPException(409, "X was read for this token a moment ago")
    if await xintel.ask_room() < xintel.USD_PER_CALL + xintel.USD_PER_TOKEN:
        raise HTTPException(409, "this week's X budget is spent")
    st = await xintel.ask(*key)
    _cache.pop(f"token:{key[0]}:{key[1]}", None)         # the next dossier load shows it waiting
    return st


@router.get("/x-read/{chain}/{address}")
async def x_read_state(chain: str, address: str):
    key = await dossier.resolve(chain, rules.norm(address))
    if key is None:
        raise HTTPException(404, NOT_ON_RADAR)
    st = await xintel.ask_state(*key)
    if st and st["done_at"] is not None:
        _cache.pop(f"token:{key[0]}:{key[1]}", None)      # the page's next dossier load has the read
    return {"ask": st}
