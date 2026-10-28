"""Alpha › Live: the two edges the review found, as calls you can watch play out. Paper only.

  feed    every memecoin call of the last 48 hours: group calls, FOMO buys and the bot's own buys (read from the
          ledger we already keep; nothing is sent to the bot). Each gets the rebuilt checklist's verdict from the
          dossier as it stood at the call: skip with the reasons, pass, or no dossier yet. Beside it: the call price,
          how the call is doing, then its 24-hour result and whether it reached 2x. The same coin called again within
          MERGE by the same kind of caller is one row ("3 groups").
  live    the test: every call from TEST_FROM on, by verdict: finished, still open, and both together, with when the
          open ones finish. The rule was written on Oct 9 from calls whose 24 hours had finished, the newest of them
          made around Oct 8 22:00 UTC, so no call from TEST_FROM on had a known result when it was written. The
          history before that is what the rule was built on and sits in `windows`, labelled as such.
  factor  the lists held this week (picked on the last fixed weekly day, factor.EPOCH steps) with each coin's move
          for the position since then, and what today's ranking would add or drop
"""

from datetime import UTC, datetime, timedelta
from statistics import median

from albatross import db
from albatross.alpha import avoid
from albatross.backtest import books
from albatross.lab import factor
from albatross.lab import run as lab

FEED_HOURS = 48
TEST_FROM = datetime(2026, 10, 8, 22, 0, tzinfo=UTC)   # no call from here on had a known result when the rule was written
MERGE = timedelta(minutes=5)
HOLD = timedelta(hours=24)
def verdict(f: dict) -> tuple[str, list[str]]:
    """(pass | skip | unread, the rebuilt checklist's flags behind a skip; the page words them). unread: no dossier
    existed at the call."""
    flags = avoid.rebuilt_flags(f)
    if flags is None:
        return "unread", []
    return ("skip", flags) if flags else ("pass", [])


def state(c: dict, now: datetime) -> str:
    """finished | open | settling (its 24 hours are up and the grader hasn't closed it yet)."""
    if books.done(c):
        return "finished"
    return "settling" if c["logged_at"] + HOLD < now else "open"


def row(c: dict, now: datetime) -> dict:
    v, why = verdict(c["f"])
    _, net, _ = books.outcome(c, "horizon")
    tp_state, tp_net, _ = books.outcome(c, "take_profit")
    return {"at": c["logged_at"], "kind": c["source_kind"], "scope": c["scope"], "symbol": c.get("symbol"),
            "who": [c.get("source_id")] if c["source_kind"] != "bot" and c.get("source_id") else [],
            "price": float(c["ref_price"]) if c.get("ref_price") else None, "age_h": c["f"].get("age_h"),
            "verdict": v, "why": why, "state": state(c, now), "net": net,
            "hit_2x": tp_state in ("closed", "stalled") and c.get("tp_reason") == "target",
            "test": c["logged_at"] >= TEST_FROM, "calls": 1,
            "each": [[net, books.done(c)]]}     # every call in the row, each at its own price: what the page's sums count


def feed(calls: list[dict], now: datetime) -> list[dict]:
    """The last FEED_HOURS of calls, newest first; a coin called again within MERGE of its first call by the same kind
    of caller, with the same verdict, joins that row ("each" keeps every call's own result)."""
    since, groups = now - timedelta(hours=FEED_HOURS), []
    open_by: dict[tuple, dict] = {}
    for c in calls:
        if c["logged_at"] < since:
            continue
        key = (c["scope"], c["source_kind"], verdict(c["f"])[0])      # a repeat call with another verdict keeps its own row
        g = open_by.get(key)
        if g and c["logged_at"] - g["at"] <= MERGE:
            g["calls"] += 1
            g["each"].append([books.outcome(c, "horizon")[1], books.done(c)])
            if c.get("source_id") and c["source_kind"] != "bot" and c["source_id"] not in g["who"]:
                g["who"].append(c["source_id"])
            continue
        g = row(c, now)
        open_by[key] = g
        groups.append(g)
    return groups[::-1]


def _sum(vals: list[float]) -> dict:
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "mean": round(sum(vals) / len(vals), 2), "median": round(median(vals), 2),
            "lost90": round(sum(v <= -90 for v in vals) / len(vals), 3), "won": round(sum(v > 0 for v in vals) / len(vals), 3)}


def live_test(calls: list[dict], now: datetime) -> dict:
    """Every call since TEST_FROM, per kind and verdict: finished, open (marked now) and both; when the open ones finish."""
    out = {"from": TEST_FROM}
    for kind in ("caller", "bot", "fomo"):
        vals = {v: {"finished": [], "open": []} for v in ("pass", "skip", "unread")}
        opens = []
        for c in calls:
            if c["source_kind"] != kind or c["logged_at"] < TEST_FROM:
                continue
            _, net, _ = books.outcome(c, "horizon")
            done = books.done(c)
            if not done:
                opens.append(c["logged_at"])
            if net is not None:
                vals[verdict(c["f"])[0]]["finished" if done else "open"].append(net)
        out[kind] = {v: {"finished": _sum(x["finished"]), "open": _sum(x["open"]),
                         "so_far": _sum(x["finished"] + x["open"])} for v, x in vals.items()}
        out[kind]["open_calls"] = len(opens)
        out[kind]["next_due"] = min(opens) + HOLD if opens else None
        out[kind]["last_due"] = max(opens) + HOLD if opens else None
    return out


def windows(calls: list[dict], now: datetime) -> list[dict]:
    """Passed, skipped, no dossier and all, per window; each call counted once at its own price."""
    spans = [("open", "Open, called in the last 24 hours", lambda c: not books.done(c) and c["logged_at"] >= now - HOLD),
             ("week", "Finished, last 7 days", lambda c: books.done(c) and c["logged_at"] >= now - timedelta(days=8)),
             ("all", f"Finished since {lab.MEME_START:%b %-d}", lambda c: books.done(c) and c["logged_at"] >= lab.MEME_START)]
    out = []
    for key, label, inside in spans:
        for kind in ("caller", "bot"):
            vals: dict[str, list[float]] = {"pass": [], "skip": [], "unread": []}
            for c in calls:
                if c["source_kind"] != kind or not inside(c):
                    continue
                _, net, _ = books.outcome(c, "horizon")
                if net is not None:
                    vals[verdict(c["f"])[0]].append(net)
            out.append({"window": key, "label": label, "kind": kind, **{v: _sum(xs) for v, xs in vals.items()},
                        "every": _sum(vals["pass"] + vals["skip"] + vals["unread"])})
    return out


async def factor_lists(now: datetime) -> dict:
    """The lists held this week, each coin's move for the position since they were picked, and today's changes."""
    pool = await db.pool()
    latest = await pool.fetchval("SELECT max(day) FROM factor_ranks WHERE live")
    if latest is None:
        return {}
    marks = {r["symbol"]: r["mark_px"] for r in await pool.fetch(
        """SELECT DISTINCT ON (symbol) symbol, mark_px FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND ts > now() - interval '2 hours' AND mark_px IS NOT NULL
           ORDER BY symbol, ts DESC""")}
    marked_at = await pool.fetchval("SELECT max(ts) FROM deriv_snapshots WHERE venue = 'hyperliquid'")
    held = latest - timedelta(days=(latest - factor.EPOCH).days % 7)

    async def lists(day):
        rows = await pool.fetch(
            """SELECT r.symbol, r.rank, r.n, r.quintile, c.close FROM factor_ranks r
               LEFT JOIN candles c ON c.venue = 'hyperliquid' AND c.tf = '1d' AND c.symbol = r.symbol
                                    AND c.ts = (r.day::timestamp AT TIME ZONE 'UTC')
               WHERE r.day = $1 AND r.quintile IN (1, 5) ORDER BY r.rank""", day)
        out = {}
        for side, q, sign in (("own", 5, 1), ("short", 1, -1)):
            coins = []
            for r in rows:
                if r["quintile"] != q:
                    continue
                move = (marks[r["symbol"]] / r["close"] - 1) * 100 if r["close"] and marks.get(r["symbol"]) else None
                coins.append({"symbol": r["symbol"], "rank": r["rank"], "of": r["n"],
                              "move_pct": round(move, 2) if move is not None else None,
                              "for_position_pct": round(sign * move, 2) if move is not None else None})
            got = [c["for_position_pct"] for c in coins if c["for_position_pct"] is not None]
            out[side] = {"coins": coins, "for_position_pct": round(sum(got) / len(got), 2) if got else None}
        both = (out["own"]["for_position_pct"], out["short"]["for_position_pct"])
        out["total_pct"] = round(both[0] + both[1], 2) if None not in both else None   # own's gain plus short's gain
        return out

    now_lists, held_lists = await lists(latest), await lists(held)
    names = {s: {c["symbol"] for c in held_lists[s]["coins"]} for s in ("own", "short")}
    today = {s: {c["symbol"] for c in now_lists[s]["coins"]} for s in ("own", "short")}
    return {"marked_at": marked_at, "held_day": held, "closes": held + timedelta(days=7),
            "days_in": min(7, (now.date() - held).days), "ranked_day": latest, "held": held_lists,
            "changes": {s: {"add": sorted(today[s] - names[s]), "drop": sorted(names[s] - today[s])} for s in ("own", "short")}}


async def report(calls: list[dict], now: datetime) -> dict:
    return {"feed": feed(calls, now), "live": live_test(calls, now), "windows": windows(calls, now),
            "factor": await factor_lists(now),
            "note": "Paper only. The bot's buys are read from the ledger we already keep; nothing is sent to the bot."}
