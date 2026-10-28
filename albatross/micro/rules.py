"""Accuracy rules for the micro-cap lane (docs/SPEC-MICROCAPS.md §2, §5). Pure functions, see tests/test_micro_rules.py.

Each rule answers a failure mode that corrupted a trading bot's own paper tracker:
  quote-token contamination   price a token only as the BASE side of a pool (choose_pool)
  missing liquidity read as 0 missing stays None/NULL (pool_from_pair)
  no sanity gate              a >10x / <0.1x print needs a second source or to persist (gate, flag_candles)
  stale prices left "open"    every quote has a source time and a budget by token age (freshness_budget, consensus)
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from statistics import median, median_low

HOUR, DAY = timedelta(hours=1), timedelta(days=1)
AGREE = 0.05                       # sources within 5% of the anchor agree
JUMP = 10.0                        # a print > 10x or < 0.1x the last good one needs confirmation
JUMP_WINDOW = timedelta(minutes=5)  # §5 says 1 minute; 5 so a slow poll can't wave a spike through
PERSIST = timedelta(seconds=60)    # a jump seen by one source is accepted once it has held this long
SWITCH_MARGIN = 1.25               # another pool must be 25% deeper to take over: no flapping between twins
DUST_USD = 500.0                   # a pool this shallow never beats a bonding curve whose liquidity isn't reported
CURVE_DEXES = {"pumpfun", "meteoradbc", "launchlab"}   # DexScreener dexIds of launchpad bonding curves
PRIORITY = ("dexscreener", "jupiter", "geckoterminal")  # who prices a disagreement: our own pool's quote first
DISAGREE_MAX = 1.5                 # beyond this spread (max/min) no source is trusted: the price is unknown

# Cadence per tier: price snapshot, GeckoTerminal candles, pool rediscovery. See albatross/micro/pipeline.py.
TIERS = {
    "hot": (timedelta(seconds=10), timedelta(minutes=5), timedelta(minutes=2)),
    "active": (timedelta(seconds=30), timedelta(minutes=45), timedelta(minutes=15)),
    "watch": (timedelta(minutes=2), timedelta(hours=2), timedelta(hours=6)),
    "dormant": (timedelta(hours=1), timedelta(hours=6), timedelta(days=1)),
}


def norm(address: str) -> str:
    """EVM addresses lowercase (the Telegram feed's form; DexScreener returns them checksummed)."""
    return address.lower() if address.startswith("0x") else address


def freshness_budget(age: timedelta) -> timedelta:
    """How old a price may be before it is `stale`, by token age (§5)."""
    if age < HOUR:
        return timedelta(seconds=15)
    if age < DAY:
        return timedelta(seconds=60)
    return timedelta(minutes=5)


def born(created_at: datetime | None, first_seen: datetime) -> datetime:
    """Earliest evidence the token existed: its first pool, or the first call if the indexer lagged."""
    return min(x for x in (created_at, first_seen) if x is not None)


def live_price(price: float | None, price_ts: datetime | None, born_at: datetime, now: datetime) -> float | None:
    """The trusted price now: the last accepted consensus price while it is inside the token's freshness
    budget, else None (unknown; never the last value carried forward)."""
    if price is None or price_ts is None or now - price_ts > freshness_budget(now - born_at):
        return None
    return price


def tier(age: timedelta, since_entry: timedelta | None, open_position: bool, volume_h24: float | None) -> str:
    """age = since the token's first pool (or first call, if earlier); since_entry = last real-money bot buy."""
    if age < HOUR or (since_entry is not None and since_entry < HOUR):
        return "hot"
    if age < DAY:
        return "active"
    if volume_h24 == 0:              # measured zero; unknown (None) is not zero
        return "dormant"
    return "active" if open_position else "watch"


# ─── pools ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Pool:
    address: str
    base: str
    quote: str
    dex: str | None = None
    labels: tuple[str, ...] = ()
    liquidity_usd: float | None = None
    volume_h24_usd: float | None = None
    created_at: datetime | None = None
    quote_symbol: str | None = None

    @property
    def curve(self) -> bool:
        return self.dex in CURVE_DEXES or "DBC" in self.labels or "LaunchLab" in self.labels


def pool_from_pair(p: dict) -> Pool:
    """DexScreener pair -> Pool. Liquidity DexScreener leaves out stays None: never read as $0."""
    liq = (p.get("liquidity") or {}).get("usd")
    created = p.get("pairCreatedAt")
    return Pool(
        address=norm(p["pairAddress"]), base=norm(p["baseToken"]["address"]), quote=norm(p["quoteToken"]["address"]),
        dex=p.get("dexId"), labels=tuple(p.get("labels") or ()),
        liquidity_usd=float(liq) if liq is not None else None,
        volume_h24_usd=(p.get("volume") or {}).get("h24"),
        created_at=datetime.fromtimestamp(created / 1000, UTC) if created else None,
        quote_symbol=p["quoteToken"].get("symbol"),
    )


def choose_pool(pools: list[Pool], token: str, current: str | None = None) -> tuple[Pool | None, str]:
    """The pool to price `token` from, and why. Only pools where the token is the BASE side qualify; the deepest
    wins, a bonding curve (liquidity unreported) beats dust, and the sitting pool keeps its place unless another
    is SWITCH_MARGIN deeper."""
    base = [p for p in pools if p.base == token]
    if not base:
        return None, "no base-side pool"
    known = [p for p in base if p.liquidity_usd is not None]
    unknown = [p for p in base if p.liquidity_usd is None]
    best = max(known, key=lambda p: p.liquidity_usd) if known else None
    if best is None or (best.liquidity_usd < DUST_USD and unknown):
        best = max(unknown, key=lambda p: (p.volume_h24_usd or 0, -(p.created_at or datetime.max.replace(tzinfo=UTC)).timestamp()))
    cur = next((p for p in base if p.address == current), None)
    if cur is None:
        return best, "initial" if current is None else "previous pool gone"
    if best is cur:
        return cur, "current"
    if best.liquidity_usd is None:                       # a curve over a dust pool
        return (best, "deeper") if (cur.liquidity_usd or 0) < DUST_USD else (cur, "current")
    if cur.liquidity_usd is None:
        return best, "migrated" if cur.curve else "deeper"
    if best.liquidity_usd >= cur.liquidity_usd * SWITCH_MARGIN:
        return best, "deeper"
    return cur, "current"


# ─── consensus ───────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Quote:
    source: str
    price: float | None
    ts: datetime                     # source time


@dataclass
class Consensus:
    price: float | None
    ts: datetime | None              # source time of the newest agreeing quote
    sources: list[str]
    spread_pct: float | None         # widest fresh quote vs the anchor
    flags: list[str]


def consensus(quotes: list[Quote], now: datetime, budget: timedelta) -> Consensus:
    """§5: the median of the fresh quotes that agree within 5%. The anchor is the lower median, so it is always a
    price some source actually reported; two sources agree when they are within 5% of each other. When no two
    agree, our own pool's quote stands (flagged), unless the sources are more than DISAGREE_MAX apart."""
    usable = [q for q in quotes if q.price is not None and q.price > 0]
    fresh = [q for q in usable if now - q.ts <= budget]
    flags = [f"stale:{q.source}" for q in usable if q not in fresh]
    if not fresh:
        return Consensus(None, None, [], None, flags + ["stale" if usable else "no_quote"])
    if len(fresh) == 1:
        q = fresh[0]
        return Consensus(q.price, q.ts, [q.source], 0.0, flags + ["single_source"])
    anchor = median_low([q.price for q in fresh])
    agree = [q for q in fresh if abs(q.price / anchor - 1) <= AGREE + 1e-9]   # "within 5%" includes 5% itself
    spread = max(abs(q.price / anchor - 1) for q in fresh) * 100
    if len(agree) >= 2:
        flags += [f"outlier:{q.source}" for q in fresh if q not in agree]
        # Every agreeing quote was inside the budget when polled; the price is as fresh as the newest of them,
        # which it is within 5% of by construction.
        return Consensus(median(q.price for q in agree), max(q.ts for q in agree), [q.source for q in agree],
                         spread, flags)
    prices = [q.price for q in fresh]
    if max(prices) / min(prices) > DISAGREE_MAX:
        return Consensus(None, None, [], spread, flags + ["disagree"])
    q = min(fresh, key=lambda q: PRIORITY.index(q.source) if q.source in PRIORITY else len(PRIORITY))
    return Consensus(q.price, q.ts, [q.source], spread, flags + ["disagree"])


# ─── sanity gate ─────────────────────────────────────────────────────────────
@dataclass
class Gate:
    accept: bool
    pending_price: float | None = None
    pending_since: datetime | None = None
    flag: str | None = None


def is_jump(price: float, last: float) -> bool:
    return not (1 / JUMP <= price / last <= JUMP)


def gate(price: float, now: datetime, last_price: float | None, last_ts: datetime | None, n_sources: int,
         pending_price: float | None = None, pending_since: datetime | None = None) -> Gate:
    """§5: a print > 10x or < 0.1x the last good one (within JUMP_WINDOW) is accepted only if two sources agree on
    it or the same level has held for PERSIST. Until then it is rejected and remembered as pending."""
    if not last_price or last_ts is None or now - last_ts > JUMP_WINDOW or not is_jump(price, last_price):
        return Gate(True)
    if n_sources >= 2:
        return Gate(True, flag="jump_confirmed")
    if pending_price and pending_since and 0.5 <= price / pending_price <= 2:
        if now - pending_since >= PERSIST:
            return Gate(True, flag="jump_persisted")
        return Gate(False, pending_price, pending_since, "rejected_print")
    return Gate(False, price, now, "rejected_print")


@dataclass(frozen=True)
class Candle:
    ts: datetime
    open: float
    high: float
    low: float
    close: float
    volume_usd: float | None = None


def candles_from_ohlcv(raw: list[list[float]]) -> list[Candle]:
    """GeckoTerminal ohlcv_list (newest first) -> candles, oldest first. It sometimes lists a minute twice with the
    same volume (seen 2026-09-29 on pump.fun and Uniswap v4 pools): merged into the widest range, the first-listed
    (newest) open and close, and the volume counted once."""
    merged: dict[float, list] = {}
    for t, o, h, lo, c, v in raw:
        if m := merged.get(t):
            m[1], m[2], m[4] = max(m[1], h), min(m[2], lo), max(m[4] or 0, v or 0)
        else:
            merged[t] = [o, h, lo, c, v]
    return sorted((Candle(datetime.fromtimestamp(t, UTC), *m) for t, m in merged.items()), key=lambda c: c.ts)


def flag_candles(candles: list[Candle], last_close: float | None, last_ts: datetime | None,
                 corroborated: set[datetime]) -> list[list[str]]:
    """Flags per candle, oldest first. A close > 10x / < 0.1x the last good close within JUMP_WINDOW is a
    rejected print unless another source saw that level in the same minute (`corroborated`) or the next traded
    candle holds it. Rejected candles are kept (flagged), never used as the last good close."""
    out = []
    for i, c in enumerate(candles):
        flags = []
        if max(c.open, c.close) > 0 and (c.high > JUMP * max(c.open, c.close) or c.low * JUMP < min(c.open, c.close)):
            flags.append("spike_wick")
        if last_close and last_ts and c.ts - last_ts <= JUMP_WINDOW and is_jump(c.close, last_close):
            nxt = candles[i + 1] if i + 1 < len(candles) else None
            held = nxt is not None and c.close > 0 and (nxt.volume_usd or 0) > 0 and 0.5 <= nxt.close / c.close <= 2
            if c.ts not in corroborated and not held:
                out.append(flags + ["rejected_print"])
                continue
            flags.append("jump_confirmed")
        last_close, last_ts = c.close, c.ts
        out.append(flags)
    return out


def candle_sources(c: Candle, quotes: list[Quote]) -> int:
    """1 (the candle's own source) + distinct other sources whose quote lies inside the candle's range, ±5%."""
    return 1 + len({q.source for q in quotes if q.price and c.low / (1 + AGREE) <= q.price <= c.high * (1 + AGREE)})
