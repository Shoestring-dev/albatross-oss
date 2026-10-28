"""Grades expired signals against realised price.

Window: from the close of the signal's candle (ref_price) to valid_until.
  return_pct   close-to-close over the window, signed to the call (+ = the call made money)
  mfe/mae_pct  best/worst excursion in the call's direction within the window
  invalidated  price traded through the invalidation level
  hit          directional calls: signed return > 0.  Neutral calls: |return| stayed inside
               half the window's typical range (i.e. "nothing happened" was right).

Sector signals (quant roll-ups, `sector:<slug>`) are graded the same way against a sector index built
like the roll-up: the sqrt(24h volume)-weighted mean of member returns since the signal candle closed.
Members are the assets whose quant signal fed the roll-up. The index only has a
close path (members' highs and lows don't coincide), so sector MFE/MAE are close-to-close, and there is
no exit price or invalidation.
"""

import logging
import math
from datetime import UTC, datetime, timedelta

import pandas as pd

from albatross import db
from albatross.features.engine import TFS, VENUE_PREFERENCE

log = logging.getLogger(__name__)

# Same floor as the roll-up (engine._sector_signals): fewer members with data = no sector index.
MIN_MEMBERS = 3
# The roll-up takes each member's latest closed candle, which can lag the sector's by a bar or two when
# a candle sync is late. Matching members up to 3 bars back reproduced the stored member count for every
# prod sector signal on 2026-09-29 (2,991 signals, none over); an exact match missed 1.8% at 5m.
MEMBER_LAG_BARS = 3


async def _candles(symbol: str, base_tf: str, start, end) -> pd.DataFrame | None:
    pool = await db.pool()
    for venue in VENUE_PREFERENCE:
        rows = await pool.fetch(
            """SELECT ts, high, low, close FROM candles
               WHERE venue = $1 AND symbol = $2 AND tf = $3 AND ts >= $4 AND ts <= $5 ORDER BY ts""",
            venue, symbol, base_tf, start, end,
        )
        if rows:
            return pd.DataFrame([dict(r) for r in rows]).set_index("ts")
    return None


def grade_one(sig: dict, df: pd.DataFrame, base_bar: timedelta) -> tuple | None:
    bar = TFS[sig["tf"]][2]
    start = sig["as_of"] + bar                         # signal candle closed here
    window = df[(df.index >= start) & (df.index + base_bar <= sig["valid_until"])]
    if window.empty or not sig["ref_price"]:
        return None
    ref = sig["ref_price"]
    direction = _direction(sig)
    inv = sig["invalidation"]
    invalidated = None
    if inv and direction:
        invalidated = bool(window["low"].min() <= inv) if direction > 0 else bool(window["high"].max() >= inv)
    return _outcome(sig, float(window["close"].iloc[-1]), (window["close"].iloc[-1] / ref - 1) * 100,
                    (window["high"].max() / ref - 1) * 100, (window["low"].min() / ref - 1) * 100, invalidated)


def sector_index(members: list[tuple[pd.DataFrame, float]], as_of: datetime, bar: timedelta,
                 valid_until: datetime, base_bar: timedelta) -> pd.Series | None:
    """Cumulative % return of a sector index at each base candle of the window, or None when fewer
    than MIN_MEMBERS members have data. members = (close candles, roll-up weight).

    A member counts when it has a close at the signal candle's close (its reference) and at least one
    candle in the window. A missing candle carries the member's last close forward; before a member's
    first candle in the window it sits out and the other weights are rescaled."""
    start = as_of + bar
    cols, weights = {}, {}
    for i, (df, w) in enumerate(members):
        ref = df[(df.index >= as_of) & (df.index + base_bar <= start)]
        window = df[(df.index >= start) & (df.index + base_bar <= valid_until)]
        if ref.empty or window.empty:
            continue
        cols[i] = (window["close"] / ref["close"].iloc[-1] - 1) * 100
        weights[i] = w
    if len(cols) < MIN_MEMBERS:
        return None
    r = pd.DataFrame(cols).sort_index().ffill()
    w = pd.Series(weights)
    return (r * w).sum(axis=1) / r.notna().mul(w).sum(axis=1)


def grade_sector(sig: dict, path: pd.Series) -> tuple:
    return _outcome(sig, None, path.iloc[-1], path.max(), path.min(), None)


def _direction(sig: dict) -> int:
    return 1 if sig["bias"] > 0 else -1 if sig["bias"] < 0 else 0


def _outcome(sig: dict, exit_price: float | None, ret: float, up: float, down: float,
             invalidated: bool | None) -> tuple:
    direction = _direction(sig)
    mfe, mae = (up, down) if direction >= 0 else (-down, -up)
    signed = ret * (direction or 1)
    hit = signed > 0 if direction else abs(ret) < 0.5 * (up - down)
    # Plain Python types: asyncpg rejects numpy scalars.
    return (sig["id"], exit_price, round(float(signed), 4), round(float(mfe), 4), round(float(mae), 4),
            invalidated, bool(hit))


async def run(limit: int = 20000) -> int:
    pool = await db.pool()
    sigs = await pool.fetch(
        """SELECT s.id, s.scope, s.tf, s.bias, s.ref_price, s.invalidation, s.as_of, s.valid_until
           FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
           WHERE o.signal_id IS NULL AND s.valid_until < now() - interval '5 minutes'
             AND s.scope NOT LIKE 'sector:%' AND s.as_of IS NOT NULL
           ORDER BY s.valid_until LIMIT $1""",
        limit,
    )
    groups: dict[tuple[str, str], list[dict]] = {}
    for s in sigs:
        groups.setdefault((s["scope"], TFS[s["tf"]][0]), []).append(dict(s))

    rows = []
    give_up = datetime.now(UTC) - timedelta(days=2)
    for (symbol, base_tf), items in groups.items():
        start = min(i["as_of"] for i in items)
        end = max(i["valid_until"] for i in items)
        df = await _candles(symbol, base_tf, start, end)
        for i in items:
            r = grade_one(i, df, TFS[base_tf][2]) if df is not None else None
            if r:
                rows.append(r)
            elif i["valid_until"] < give_up:
                # No price data for the window (venue gap/delisting): close it out ungraded.
                rows.append((i["id"], None, None, None, None, None, None))

    rows += await _grade_sectors(limit, give_up)
    await db.upsert(
        "outcomes", ["signal_id", "exit_price", "return_pct", "mfe_pct", "mae_pct", "invalidated", "hit"],
        rows, ["signal_id"],
    )
    return len(rows)


async def _grade_sectors(limit: int, give_up: datetime) -> list[tuple]:
    pool = await db.pool()
    sigs = [dict(r) for r in await pool.fetch(
        """SELECT s.id, s.scope, s.tf, s.bias, s.as_of, s.valid_until
           FROM signals s LEFT JOIN outcomes o ON o.signal_id = s.id
           WHERE o.signal_id IS NULL AND s.valid_until < now() - interval '5 minutes'
             AND s.scope LIKE 'sector:%' AND s.as_of IS NOT NULL
           ORDER BY s.valid_until LIMIT $1""", limit)]
    if not sigs:
        return []
    # Members = assets whose own signal fed the roll-up (engine._sector_signals groups by
    # primary_category, 'other' when unset). Weights as there: sqrt(24h volume), else 1.
    members: dict[int, list[tuple[str, float]]] = {}
    for r in await pool.fetch(
        """SELECT DISTINCT sec.id, a.symbol, a.volume_24h_usd
           FROM unnest($1::bigint[], $2::interval[]) AS x(id, lag)
           JOIN signals sec ON sec.id = x.id
           JOIN assets a ON coalesce(a.primary_category, 'other') = substr(sec.scope, 8)
           JOIN signals s ON s.scope = a.symbol AND s.tf = sec.tf AND s.source = sec.source
                         AND s.as_of BETWEEN sec.as_of - x.lag AND sec.as_of""",
            [s["id"] for s in sigs], [TFS[s["tf"]][2] * MEMBER_LAG_BARS for s in sigs]):
        members.setdefault(r["id"], []).append((r["symbol"], math.sqrt(r["volume_24h_usd"] or 0) or 1.0))

    spans: dict[tuple[str, str], list] = {}
    for s in sigs:
        for sym, _ in members.get(s["id"], []):
            span = spans.setdefault((sym, TFS[s["tf"]][0]), [s["as_of"], s["valid_until"]])
            span[0], span[1] = min(span[0], s["as_of"]), max(span[1], s["valid_until"])
    candles = {key: await _candles(key[0], key[1], *span) for key, span in spans.items()}

    rows = []
    for s in sigs:
        base_tf, _, bar, _ = TFS[s["tf"]]
        have = [(df, w) for sym, w in members.get(s["id"], []) if (df := candles[(sym, base_tf)]) is not None]
        path = sector_index(have, s["as_of"], bar, s["valid_until"], TFS[base_tf][2])
        if path is not None:
            rows.append(grade_sector(s, path))
        elif s["valid_until"] < give_up:
            rows.append((s["id"], None, None, None, None, None, None))
    return rows
