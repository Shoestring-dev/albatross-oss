"""Is FOMO worth paying for? The free fomoapi.io tier is what we use; a paid plan only if the data adds value
(2026-10-01). The test is fixed here, before the data (the intel reviewer's design, 2026-10-01).

What a subscription would buy: Starter ($49.99/mo) buys more holder lookups (who holds a token, their positions and
theses). The calls themselves come from the free live feed either way. So two questions:
1. Are FOMO's ranked traders' buys worth following? Only then is FOMO data worth anything to us.
2. Do we run out of the free tier's lookups on tokens that matter? Only then does paying change what we see.

(a) Per unique token, at its first FOMO buy, graded by the ledger like every call (24 h horizon, all entry costs in):
    the share reaching 2x and the median 24 h net return. Two comparisons:
    - Telegram calls, each token at its first call: buy-intent calls, and all calls.
    - A matched random entry for each first FOMO buy. Up to PICKS tokens on the same chain at the same minute,
      liquidity within +-LIQ_BAND and the same age band, drawn at random (seeded by the call) among the tokens we hold
      1-minute candles for. They are graded by the same simulation (backtest/sim.simulate_dex), entered at the minute's
      candle close.
    Intervals: a bootstrap over tokens, paired for the random baseline.
(b) The same split by the buyer's rank band at the time of the buy (best of the 7d and 30d boards): 1-10, 11-50,
    51-150.
(c) Lift: Telegram calls whose dossier showed the FOMO line good, against the rest (de_call_evidence).
(d) Credit pressure: FOMO-active tokens on our radar a day that got no holder lookup that day, and the credits used
    against the free tier's daily pace. Also each source's unpriced share: a call never priced is never graded.
    Telegram's is about 48%.

Rule (decide()): recommend Starter only if all hold:
- (a) FOMO beats both baselines on both metrics, with the interval clear of zero. The main Telegram bar is all its
  calls; buy-intent calls are shown, not judged (too thin).
- (c) shows lift.
- (d) shows the free lookups running out.
"Too early" until FOMO has MIN_TOKENS graded tokens and every comparison has its own sample (MIN_COMPARE a side,
MIN_LIFT for lift). With enough data, "don't buy" means a clear no: FOMO clearly worse, or nothing more to buy. Neither
clearly better nor worse is "inconclusive". The job runs daily; the latest report is in de_fomo_verdict and on the
System page.
"""

from __future__ import annotations

import logging
import random
from datetime import UTC, date, datetime, timedelta
from statistics import mean, median

import asyncpg
import orjson
import pandas as pd

from albatross import db
from albatross.backtest import sim
from albatross.micro import rules as micro_rules

log = logging.getLogger(__name__)

JOB = "deep_fomo_verdict"
PICKS = 3
LIQ_BAND = 0.5
MIN_TOKENS = 200
BOOT = 2000
PER_RUN = 300
HORIZON = timedelta(hours=24)
RANK_BANDS = ((10, "1-10"), (50, "11-50"), (150, "51-150"))
AGE_BANDS = ((timedelta(hours=1), "<1h"), (timedelta(hours=6), "1-6h"), (timedelta(hours=24), "6-24h"),
             (timedelta(days=7), "1-7d"))
FULL_DAY = 0.95                          # credits used against the day's budget that count as a full day (context)
CREDIT_DAYS = 3                          # days of the last 14 on which the free credits ran short ...
LOOKUP_COVERAGE = 0.5                    # ... serving under half the hot tokens due a holder lookup that day
MIN_DUE = 10                             # a day with fewer due tokens says nothing


def age_band(age: timedelta | None) -> str | None:
    if age is None:
        return None
    return next((name for top, name in AGE_BANDS if age < top), ">7d")


def rank_band(rank_7d: int | None, rank_30d: int | None) -> str | None:
    ranks = [r for r in (rank_7d, rank_30d) if r is not None]
    if not ranks:
        return None
    return next((name for top, name in RANK_BANDS if min(ranks) <= top), None)


def hit_2x(mfe_pct: float | None) -> bool | None:
    """The dossier grader's 2x: the price touched twice the entry fill, all entry costs in."""
    return None if mfe_pct is None else mfe_pct >= 100


def picks(candidates: list[dict], call_id: int, k: int = PICKS) -> list[dict]:
    """A reproducible random draw: the same call always draws the same tokens."""
    pool = sorted(candidates, key=lambda c: c["token"])
    return random.Random(call_id).sample(pool, min(k, len(pool)))


# ─── the matched random entries ─────────────────────────────────────────────
FIRSTS_SQL = """
WITH firsts AS (
  SELECT DISTINCT ON (c.scope) c.id, c.scope, c.logged_at, c.evidence
  FROM trade_calls c WHERE c.source_kind = 'fomo' AND c.market = 'dex'
  ORDER BY c.scope, c.logged_at, c.id)
SELECT f.id, split_part(f.scope, ':', 1) AS chain, split_part(f.scope, ':', 2) AS address, f.logged_at, f.evidence,
       fc.rank_7d, fc.rank_30d, s.created_at
FROM firsts f
JOIN trade_call_outcomes o ON o.call_id = f.id AND o.policy = 'horizon' AND o.status = 'graded'
LEFT JOIN de_fomo_calls fc ON fc.src_id = (f.evidence->>'src_id')::bigint
LEFT JOIN micro_state s ON s.chain = split_part(f.scope, ':', 1) AND s.token = split_part(f.scope, ':', 2)
WHERE NOT EXISTS (SELECT 1 FROM de_fomo_baselines b WHERE b.call_id = f.id)
ORDER BY f.logged_at LIMIT $1"""

CANDIDATES_SQL = """
SELECT DISTINCT ON (m.token) m.token, m.close AS price, m.ts, s.created_at, s.dex, p.labels, l.liquidity_usd,
       l.source AS liquidity_src
FROM micro_candles_1m m
JOIN micro_state s ON s.chain = m.chain AND s.token = m.token
LEFT JOIN micro_pools p ON p.chain = s.chain AND p.pool = s.pool
JOIN LATERAL (SELECT x.liquidity_usd, x.source FROM micro_snapshots x
              WHERE x.chain = m.chain AND x.token = m.token AND x.ingest_ts <= $2
                AND x.ingest_ts > $2 - interval '30 minutes' AND x.liquidity_usd IS NOT NULL
                AND x.source IN ('dexscreener', 'jupiter')
              ORDER BY x.ingest_ts DESC LIMIT 1) l ON TRUE
WHERE m.chain = $1 AND m.ts > $2 - interval '3 minutes' AND m.ts <= $2 AND m.token <> $3
  AND NOT 'rejected_print' = ANY(m.flags) AND l.liquidity_usd BETWEEN $4 AND $5
ORDER BY m.token, m.ts DESC"""

COVERED_SQL = """
SELECT s.candles_at, s.candles_error, s.liquidity_usd,
       (SELECT max(ts) FROM micro_candles_1m m WHERE m.chain = $1 AND m.token = $2) AS last_ts,
       EXISTS (SELECT 1 FROM micro_tokens t WHERE coalesce(t.resolved_chain, t.chain) = $1 AND t.address = $2
               AND t.status = 'retired')
       AND NOT EXISTS (SELECT 1 FROM micro_tokens t WHERE coalesce(t.resolved_chain, t.chain) = $1 AND t.address = $2
                       AND t.status <> 'retired') AS retired
FROM micro_state s WHERE s.chain = $1 AND s.token = $2"""

FOREVER = datetime.max.replace(tzinfo=UTC)


def covered_until(r: dict | None) -> datetime:
    """How far a token's candles are complete: the rule backtest/ideas._dex grades real calls by."""
    if r is None:
        return datetime.min.replace(tzinfo=UTC)
    if r["retired"] and r["liquidity_usd"] is not None and r["liquidity_usd"] < micro_rules.DUST_USD:
        return FOREVER
    if r["candles_at"] and r["candles_error"] is None:
        return r["candles_at"]
    return r["last_ts"] + sim.MINUTE if r["last_ts"] else datetime.min.replace(tzinfo=UTC)


async def grade_entry(pool, chain: str, token: str, at: datetime, c: dict) -> dict:
    """One random entry at `at`, graded by sim.simulate_dex exactly as a ledger DEX call is."""
    end = at + HORIZON
    bars = pd.DataFrame([dict(r) for r in await pool.fetch(
        """SELECT ts, high, low, close, source FROM micro_candles_1m
           WHERE chain = $1 AND token = $2 AND ts > $3 AND ts <= $4 AND NOT 'rejected_print' = ANY(flags)
           ORDER BY ts""", chain, token, at, end)] or None, columns=["ts", "high", "low", "close", "source"])
    bars = bars.set_index("ts")
    liq = pd.DataFrame([dict(r) for r in await pool.fetch(
        """SELECT ingest_ts, liquidity_usd FROM micro_snapshots
           WHERE chain = $1 AND token = $2 AND ingest_ts > $3 AND ingest_ts <= $4 AND liquidity_usd IS NOT NULL
             AND source = $5 ORDER BY ingest_ts""", chain, token, at, end, c["liquidity_src"])] or None,
        columns=["ingest_ts", "liquidity_usd"]).set_index("ingest_ts")["liquidity_usd"]
    cov = await pool.fetchrow(COVERED_SQL, chain, token)
    o = sim.simulate_dex({"logged_at": at, "ref_price": c["price"], "horizon_until": end}, bars,
                         covered_until(dict(cov) if cov else None), c["liquidity_usd"], liq,
                         sim.dex_fee(c.get("dex"), c.get("labels")), sim.GAS_USD.get(chain, sim.GAS_DEFAULT_USD))
    h = o["horizon"]
    return {"token": token, "price": c["price"], "liquidity_usd": c["liquidity_usd"], "status": h["status"],
            "net_24h_pct": h.get("ret_24h_pct"), "mfe_pct": h.get("mfe_pct")}


async def baselines(limit: int = PER_RUN) -> int:
    """Matched random entries for the first FOMO buys graded since the last run. -> baselines written."""
    pool = await db.pool()
    rows = []
    for f in await pool.fetch(FIRSTS_SQL, limit):
        ev = orjson.loads(f["evidence"]) if isinstance(f["evidence"], str) else f["evidence"]
        liq = ev.get("liquidity_usd")
        at = f["logged_at"]
        band = age_band(at - f["created_at"] if f["created_at"] else None)
        cands = []
        if liq and liq > 0:
            cands = [dict(r) for r in await pool.fetch(
                CANDIDATES_SQL, f["chain"], at, f["address"], liq * (1 - LIQ_BAND), liq * (1 + LIQ_BAND))]
            cands = [c for c in cands if band is None or age_band(at - c["created_at"] if c["created_at"] else None)
                     == band]
        graded = [await grade_entry(pool, f["chain"], c["token"], at, c) for c in picks(cands, f["id"])]
        done = [g for g in graded if g["status"] == "graded" and g["net_24h_pct"] is not None]
        rows.append((f["id"], f["chain"], f["address"], at, rank_band(f["rank_7d"], f["rank_30d"]), band, liq,
                     len(cands), orjson.dumps(graded).decode(),
                     mean(g["net_24h_pct"] for g in done) if done else None,
                     mean(hit_2x(g["mfe_pct"]) or False for g in done) if done else None))
    await db.upsert("de_fomo_baselines", ["call_id", "chain", "address", "logged_at", "band", "age_band",
                                          "liquidity_usd", "candidates", "picks", "net_24h_pct", "hit_2x"],
                    rows, ["call_id"])
    return len(rows)


# ─── the statistics ──────────────────────────────────────────────────────────
def interval(fn, a: list, b: list | None = None, *, paired: bool = False, n: int = BOOT,
             seed: int = 7) -> dict | None:
    """fn(a) - fn(b) (or fn(a) alone) with a 95% bootstrap interval over the units in the lists (tokens). Paired:
    a[i] and b[i] are the same token's, resampled together."""
    if not a or (b is not None and not b):
        return None
    rnd = random.Random(seed)
    est = fn(a) - (fn(b) if b is not None else 0.0)
    draws = []
    for _ in range(n):
        if b is None:
            draws.append(fn([rnd.choice(a) for _ in a]))
        elif paired:
            idx = [rnd.randrange(len(a)) for _ in a]
            draws.append(fn([a[i] for i in idx]) - fn([b[i] for i in idx]))
        else:
            draws.append(fn([rnd.choice(a) for _ in a]) - fn([rnd.choice(b) for _ in b]))
    draws.sort()
    return {"value": round(est, 4), "lo": round(draws[int(0.025 * n)], 4), "hi": round(draws[int(0.975 * n) - 1], 4),
            "n": len(a)}


def share(xs: list) -> float:
    return sum(bool(x) for x in xs) / len(xs)


def summary(rows: list[dict]) -> dict:
    """{tokens, graded, hit_2x, median_24h_pct} over first calls."""
    g = [r for r in rows if r.get("status") == "graded" and r.get("ret_24h_pct") is not None]
    return {"tokens": len(rows), "graded": len(g),
            "hit_2x": round(share([hit_2x(r["mfe_pct"]) for r in g]), 4) if g else None,
            "median_24h_pct": round(median(r["ret_24h_pct"] for r in g), 2) if g else None}


def compare(fomo: list[dict], other: list[dict]) -> dict:
    """FOMO minus another source, unpaired: 2x share and median 24 h net, with intervals."""
    a = [r for r in fomo if r.get("status") == "graded" and r.get("ret_24h_pct") is not None]
    b = [r for r in other if r.get("status") == "graded" and r.get("ret_24h_pct") is not None]
    return {"hit_2x": interval(share, [hit_2x(r["mfe_pct"]) for r in a], [hit_2x(r["mfe_pct"]) for r in b]),
            "median_24h_pct": interval(median, [r["ret_24h_pct"] for r in a], [r["ret_24h_pct"] for r in b])}


def wshare(xs: list, ws: list[float]) -> float:
    """The weighted share of true values."""
    return sum(w for x, w in zip(xs, ws) if x) / sum(ws)


def wmedian(xs: list[float], ws: list[float]) -> float:
    """The weighted median: the first value whose cumulative weight reaches half."""
    half, acc = sum(ws) / 2, 0.0
    for x, w in sorted(zip(xs, ws)):
        acc += w
        if acc >= half:
            return x
    return max(xs)


def cluster_interval(fn, units: list[tuple[float, list[float]]], n: int = BOOT, seed: int = 7) -> dict | None:
    """fn(FOMO's values) - fn(every random pick's value), with a 95% bootstrap over tokens: a resampled token brings its
    FOMO entry and all its picks. fn takes (values, weights): each FOMO entry weighs 1 and each of a token's k picks
    1/k, so every token counts once on each side. Each side is single entries, so averaging never makes one side less
    skewed than the other (the mean of 3 picks lifts a skewed baseline's median: about 7 points under the null)."""
    units = [(a, bs) for a, bs in units if bs]
    if not units:
        return None
    rnd = random.Random(seed)

    def diff(us):
        return fn([a for a, _ in us], [1.0] * len(us)) - fn([b for _, bs in us for b in bs],
                                                            [1 / len(bs) for _, bs in us for _ in bs])
    draws = sorted(diff([units[rnd.randrange(len(units))] for _ in units]) for _ in range(n))
    return {"value": round(diff(units), 4), "lo": round(draws[int(0.025 * n)], 4),
            "hi": round(draws[int(0.975 * n) - 1], 4), "n": len(units)}


def against_random(fomo: list[dict]) -> dict:
    """FOMO's first buys against their matched random entries (tokens with at least one graded pick)."""
    p = [r for r in fomo if r.get("status") == "graded" and r.get("ret_24h_pct") is not None and r.get("picks")]
    picks = [[g for g in r["picks"] if g.get("status") == "graded" and g.get("net_24h_pct") is not None] for r in p]
    return {"tokens": sum(bool(g) for g in picks),
            "hit_2x": cluster_interval(wshare, [(hit_2x(r["mfe_pct"]), [hit_2x(g.get("mfe_pct")) for g in gs])
                                                for r, gs in zip(p, picks)]),
            "net_24h_pct": cluster_interval(wmedian, [(r["ret_24h_pct"], [g["net_24h_pct"] for g in gs])
                                                      for r, gs in zip(p, picks)])}


def clear(i: dict | None) -> bool:
    return bool(i) and i["lo"] > 0


MIN_LIFT = 30                            # Telegram calls on each side (FOMO line good / not) before lift is judged
MIN_COMPARE = 100                        # tokens on each side of a comparison before it is judged


def worse(i: dict | None) -> bool:
    """Clearly worse: the whole interval below zero."""
    return bool(i) and i["hi"] < 0


def decide(r: dict) -> dict:
    """The rule in the module docstring, applied to a report:
    - too_early: some comparison is short of its sample (FOMO's MIN_TOKENS, MIN_COMPARE a side, MIN_LIFT a side);
    - buy_starter: every test passes;
    - dont_buy: FOMO is clearly worse than a baseline, or the free lookups never run out (nothing to buy);
    - inconclusive: enough data, but FOMO is neither clearly better nor clearly worse.
    The main bar is all Telegram calls; buy-intent calls (thin: ~5 graded tokens in 3 days) are shown, not judged."""
    n, rnd, tg = r["fomo"]["graded"], r["vs_random"], r["vs_telegram_all"]
    # A clear loss on a full sample decides now: lift or the credits can't rescue a FOMO that loses to random entry.
    if n >= MIN_TOKENS:
        losses = ([f"clearly worse than random entry on {rnd.get('tokens')} tokens"]
                  if (rnd.get("tokens") or 0) >= MIN_COMPARE and (worse(rnd["hit_2x"]) or worse(rnd["net_24h_pct"]))
                  else [])
        losses += (["clearly worse than Telegram calls"]
                   if (r["telegram_all"].get("graded") or 0) >= MIN_COMPARE
                   and (worse(tg["hit_2x"]) or worse(tg["median_24h_pct"])) else [])
        if losses:
            return {"verdict": "dont_buy", "why": losses}
    missing = [f"{n} of {MIN_TOKENS} FOMO tokens graded"] if n < MIN_TOKENS else []
    if (rnd.get("tokens") or 0) < MIN_COMPARE:
        missing.append(f"{rnd.get('tokens') or 0} of {MIN_COMPARE} FOMO tokens with a matched random entry")
    if (r["telegram_all"].get("graded") or 0) < MIN_COMPARE:
        missing.append(f"{r['telegram_all'].get('graded') or 0} of {MIN_COMPARE} Telegram-called tokens graded")
    if min(r["lift"]["good"], r["lift"]["rest"]) < MIN_LIFT:
        missing.append(f"Telegram calls with the FOMO line good and not (needs {MIN_LIFT} each)")
    if missing:
        return {"verdict": "too_early", "why": missing}
    tests = {
        "beats random entry": clear(rnd["hit_2x"]) and clear(rnd["net_24h_pct"]),
        "beats Telegram calls": clear(tg["hit_2x"]) and clear(tg["median_24h_pct"]),
        "lifts Telegram calls": clear(r["lift"]["hit_2x"]),
        "free lookups run out": r["credits"]["running_out"],
    }
    why = [f"{k}: {'yes' if ok else 'no'}" for k, ok in tests.items()]
    if all(tests.values()):
        return {"verdict": "buy_starter", "why": why}
    clearly_worse = any(worse(i) for i in (rnd["hit_2x"], rnd["net_24h_pct"], tg["hit_2x"], tg["median_24h_pct"]))
    if clearly_worse or not tests["free lookups run out"]:
        return {"verdict": "dont_buy", "why": why}
    return {"verdict": "inconclusive", "why": why}


# ─── the report ──────────────────────────────────────────────────────────────
FIRST_CALLS_SQL = """
SELECT DISTINCT ON (c.scope) c.id, c.scope, c.logged_at, c.evidence->>'call_type' AS call_type,
       (c.evidence->>'src_id')::bigint AS src_id, o.status, o.ret_24h_pct, o.mfe_pct
FROM trade_calls c LEFT JOIN trade_call_outcomes o ON o.call_id = c.id AND o.policy = 'horizon'
WHERE c.source_kind = $1 AND c.market = 'dex' AND ($2::text IS NULL OR c.evidence->>'call_type' = $2)
ORDER BY c.scope, c.logged_at, c.id"""
# Telegram's tokens counted at their first call, and only those first called while FOMO was logged (same market).
WINDOWED_SQL = f"SELECT * FROM ({FIRST_CALLS_SQL}) f WHERE f.logged_at >= $3"

LIFT_SQL = """
SELECT statuses->>'fomo' AS fomo, mfe_pct FROM de_call_evidence
WHERE kind = 'call' AND first_in_group AND status = 'graded' AND statuses ? 'fomo'"""

CREDITS_SQL = """SELECT day, units FROM de_api_usage WHERE provider = 'fomoapi' AND day >= $1 ORDER BY day"""


def budgets(days: list[dict], cap: float, monthly: float) -> list[dict]:
    """Each day's credits used against that day's own budget (fomo.daily_budget: the cap, or the month's remainder
    spread over its remaining days), from usage rows in day order covering each month from its first day."""
    from albatross.deepend import fomo
    out, month, used = [], None, 0.0
    for r in days:
        if (r["day"].year, r["day"].month) != month:
            month, used = (r["day"].year, r["day"].month), 0.0
        budget = fomo.daily_budget(cap, monthly, used, r["day"])
        out.append({"day": str(r["day"]), "units": r["units"], "budget": round(budget),
                    "full": (r["units"] or 0) >= FULL_DAY * budget > 0})
        used += r["units"] or 0
    return out

# The holder job's own demand (fomo.demand()): the hot tokens due a lookup each day, and how many got one. The pace
# spends the day's credits whatever happens, so "credits used" can't tell short from enough; unserved demand can.
DEMAND_SQL = """SELECT day, count(*) AS due, count(looked_at) AS looked FROM de_fomo_holder_demand
WHERE day > $1 GROUP BY 1 ORDER BY 1"""


def short_days(days: list[dict]) -> list[dict]:
    """The days the free credits served under LOOKUP_COVERAGE of the hot tokens due a lookup (MIN_DUE+ due)."""
    return [d for d in days if d["due"] >= MIN_DUE and d["looked"] < LOOKUP_COVERAGE * d["due"]]

UNPRICED_SQL = """
SELECT kind, count(*) FILTER (WHERE status = 'logged') AS logged, count(*) FILTER (WHERE status = 'unpriced') AS unpriced
FROM micro_ledger WHERE at > $1 GROUP BY 1"""


async def report(now: datetime) -> dict:
    from albatross.deepend import fomo
    pool = await db.pool()
    since = now - timedelta(days=30)
    fomo_rows = [dict(r) for r in await pool.fetch(FIRST_CALLS_SQL, "fomo", None)]
    bands = {r["src_id"]: rank_band(r["rank_7d"], r["rank_30d"]) for r in await pool.fetch(
        "SELECT src_id, rank_7d, rank_30d FROM de_fomo_calls WHERE src_id = ANY($1::bigint[])",
        [r["src_id"] for r in fomo_rows if r["src_id"] is not None])}
    base = {r["call_id"]: dict(r) for r in await pool.fetch("SELECT call_id, picks FROM de_fomo_baselines")}
    for r in fomo_rows:
        picks = (base.get(r["id"]) or {}).get("picks")
        r |= {"band": bands.get(r["src_id"]), "picks": orjson.loads(picks) if isinstance(picks, str) else picks}
    start = min((r["logged_at"] for r in fomo_rows), default=now)
    tg_buy = [dict(r) for r in await pool.fetch(WINDOWED_SQL, "caller", "buy", start)]
    tg_all = [dict(r) for r in await pool.fetch(WINDOWED_SQL, "caller", None, start)]
    lift = [dict(r) for r in await pool.fetch(LIFT_SQL)]
    good = [hit_2x(r["mfe_pct"]) for r in lift if r["fomo"] == "good" and r["mfe_pct"] is not None]
    rest = [hit_2x(r["mfe_pct"]) for r in lift if r["fomo"] != "good" and r["mfe_pct"] is not None]
    first = (now - timedelta(days=14)).date().replace(day=1)
    days = budgets([dict(r) for r in await pool.fetch(CREDITS_SQL, first)], fomo.daily_cap(), fomo.monthly_cap())
    days = [d for d in days if d["day"] > str((now - timedelta(days=14)).date())]
    full = sum(d["full"] for d in days)
    try:
        wanted = [dict(r) for r in await pool.fetch(DEMAND_SQL, (now - timedelta(days=14)).date())]
    except asyncpg.UndefinedTableError:
        wanted = []
    short = short_days(wanted)
    unpriced = {r["kind"]: {"logged": r["logged"], "unpriced": r["unpriced"],
                            "share": round(r["unpriced"] / ((r["logged"] + r["unpriced"]) or 1), 3)}
                for r in await pool.fetch(UNPRICED_SQL, since)}
    out = {
        "as_of": now.isoformat(), "min_tokens": MIN_TOKENS, "since": start.isoformat(),
        "fomo": summary(fomo_rows), "telegram_buy": summary(tg_buy), "telegram_all": summary(tg_all),
        "vs_random": against_random(fomo_rows), "vs_telegram_buy": compare(fomo_rows, tg_buy),
        "vs_telegram_all": compare(fomo_rows, tg_all),
        "bands": {name: summary([r for r in fomo_rows if r["band"] == name]) for _, name in RANK_BANDS},
        "lift": {"good": len(good), "rest": len(rest), "hit_2x": interval(share, good, rest)},
        "credits": {"daily_cap": fomo.daily_cap(), "days": days, "days_full": full,
                    "demand": [{"day": str(d["day"]), "due": d["due"], "looked": d["looked"]} for d in wanted],
                    "lookup_coverage": round(sum(d["looked"] for d in wanted) / sum(d["due"] for d in wanted), 3)
                    if sum(d["due"] for d in wanted) else None,
                    "days_short": len(short), "running_out": len(short) >= CREDIT_DAYS},
        "unpriced": unpriced,
    }
    out["decision"] = decide(out)
    return out


async def run() -> dict:
    """deep_fomo_verdict, daily: new matched random entries, then the report, kept by day."""
    n = await baselines()
    now = datetime.now(UTC)
    r = await report(now)
    await db.upsert("de_fomo_verdict", ["day", "computed_at", "report"],
                    [(date.fromisoformat(r["as_of"][:10]), now, orjson.dumps(r).decode())], ["day"])
    log.info("%s: %d new baselines; FOMO %s graded tokens; %s", JOB, n, r["fomo"]["graded"], r["decision"]["verdict"])
    return {"rows": n, "verdict": r["decision"]["verdict"]}


# ─── registry (albatross/deepend/registry.py) ────────────────────────────────
JOBS: dict = {JOB: run}
SCHEDULE: list = [(JOB, {"hour": 2, "minute": 10})]          # after deep_grade (01:25) and grade_ideas
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE: dict = {JOB: 86_400}
