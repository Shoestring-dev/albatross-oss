"""Alpha › Factor edge: is factor rank still ordering the coins, and what could trip the book this week.

  weekly      each week's rank correlation between the ranking and the next 7 days' return (IC), and the top fifth
              minus the bottom fifth, on fixed weekly days (factor.EPOCH, a Monday) so no week is picked twice
  sides       the lab's three versions of today's ranking (long and short, long only, long hedged with BTC and ETH)
              and the best each side has found
  exceptions  unlocks, delistings, listings and upgrades in the next 7 days on coins in the top or bottom fifth: the
              exceptions the review suggested the Strategist should flag
"""

from collections import defaultdict
from datetime import datetime, timedelta

import orjson
import pandas as pd

from albatross import db
from albatross.lab import factor, stats

WEEKS = 62
EXCEPTION_KINDS = ("unlock", "delisting", "listing", "upgrade", "mainnet", "tge", "airdrop")


def weekly(ranks: pd.DataFrame, px: pd.DataFrame) -> list[dict]:
    """One row per fixed weekly day with a ranking and a closed day 7 later: {day, ic, spread, n, live}."""
    out = []
    for day, g in ranks.groupby("day"):
        if (day - factor.EPOCH).days % 7:
            continue
        t0, t1 = pd.Timestamp(day), pd.Timestamp(day + timedelta(days=7))
        if t0 not in px.index or t1 not in px.index:
            continue
        fwd = (px.loc[t1] / px.loc[t0] - 1).reindex(g.symbol.to_numpy())
        g = g.assign(fwd=fwd.to_numpy()).dropna(subset=["fwd"])
        if len(g) < 10:
            continue
        ic = g.score.rank().corr(g.fwd.rank())
        spread = g.loc[g.quintile == 5, "fwd"].mean() - g.loc[g.quintile == 1, "fwd"].mean()
        out.append({"day": day, "ic": round(float(ic), 4), "spread": round(float(spread) * 100, 3), "n": len(g),
                    "live": bool(g.live.all())})
    return out[-WEEKS:]


def quarters(rows: list[dict]) -> list[dict]:
    by = defaultdict(list)
    for r in rows:
        by[f"{r['day'].year} Q{(r['day'].month - 1) // 3 + 1}"].append(r["ic"])
    out = []
    for q, ics in sorted(by.items()):
        s = stats.clustered(ics, list(range(len(ics))))
        out.append({"quarter": q, "weeks": len(ics), "right_order": sum(v > 0 for v in ics),
                    "ic": round(sum(ics) / len(ics), 3), "t": s["t"] if s else None})
    return out


async def report(now: datetime) -> dict:
    pool = await db.pool()
    since = now.date() - timedelta(days=7 * WEEKS + 14)
    ranks = pd.DataFrame([dict(r) for r in await pool.fetch(
        "SELECT day, symbol, score, quintile, live FROM factor_ranks WHERE day >= $1", since)])
    px = (await factor.load())[0]
    rows = weekly(ranks, px) if not ranks.empty and not px.empty else []
    ics = [r["ic"] for r in rows]
    overall = stats.clustered(ics, list(range(len(ics)))) if ics else None
    spreads = [r["spread"] for r in rows]
    sp = stats.clustered(spreads, list(range(len(spreads)))) if spreads else None
    live = [r for r in rows if r["live"]]

    latest = await pool.fetchval("SELECT max(day) FROM factor_ranks WHERE live")
    held = latest - timedelta(days=(latest - factor.EPOCH).days % 7) if latest else None

    async def fifths(day) -> dict[str, str]:
        return {r["symbol"]: ("top" if r["quintile"] == 5 else "bottom") for r in await pool.fetch(
            "SELECT symbol, quintile FROM factor_ranks WHERE day = $1 AND quintile IN (1, 5)", day)} if day else {}
    # the lists held this week (Alpha › Live's); a coin only today's ranking has is marked as one that would join
    picks, today = await fifths(held), await fifths(latest)
    joining = {s: side for s, side in today.items() if s not in picks}
    events = await pool.fetch(
        """SELECT kind, title, starts_at, precision, assets, importance, status FROM events
           WHERE kind = ANY($1) AND starts_at >= $2 AND starts_at < $3 AND assets && $4 AND status <> 'cancelled'
           ORDER BY starts_at""", list(EXCEPTION_KINDS), now - timedelta(hours=12), now + timedelta(days=7),
        list(picks) + list(joining))
    exceptions = [{"kind": e["kind"], "title": e["title"], "at": e["starts_at"], "precision": e["precision"],
                   "importance": e["importance"],
                   "coins": [{"symbol": a, "side": picks.get(a) or joining[a], "joining": a not in picks}
                             for a in e["assets"] if a in picks or a in joining]}
                  for e in events]
    lab_rep = await pool.fetchval("SELECT report FROM paper_reports WHERE key = 'lab'")
    lab_rep = orjson.loads(lab_rep) if isinstance(lab_rep, str) else (lab_rep or {})
    windows = ((lab_rep.get("families") or {}).get("factor") or {})

    sides = []
    for side in ("long_short", "long_only", "long_hedged"):
        seed = await pool.fetchrow(
            """SELECT id, name, status, discovery, holdout, forward, score FROM lab_strategies
               WHERE family = 'factor' AND origin = 'seed' AND config->>'side' = $1""", side)
        best = await pool.fetchrow(
            """SELECT id, name, status, discovery, holdout, forward, score, origin FROM lab_strategies
               WHERE family = 'factor' AND config->>'side' = $1 AND score IS NOT NULL ORDER BY score DESC LIMIT 1""", side)
        sides.append({"side": side, "today": _row(seed), "best": _row(best)})
    return {"weekly": rows, "quarters": quarters(rows),
            "ic": {"weeks": len(ics), "right_order": sum(v > 0 for v in ics), "top_beat_bottom": sum(v > 0 for v in spreads),
                   "first": rows[0]["day"] if rows else None, "last": rows[-1]["day"] if rows else None,
                   "mean": overall["mean"] if overall else None, "t": overall["t"] if overall else None,
                   "spread_mean": sp["mean"] if sp else None, "spread_t": sp["t"] if sp else None,
                   "live_weeks": len(live), "live_ic": round(sum(r["ic"] for r in live) / len(live), 3) if live else None},
            "ranked_on": latest, "held_day": held, "exceptions": exceptions, "sides": sides,
            "lab_windows": {"older": windows.get("discovery"), "recent": windows.get("holdout")}}


def _row(r) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("discovery", "holdout", "forward"):
        if isinstance(d.get(k), str):
            d[k] = orjson.loads(d[k])
    return d
