"""Jev trade scout: TypeSafe's Jev scores candidate trades built from our own work, continuously, and a paper
book takes the best of them. Nothing is ever traded; the paper trades go into the ledger and are graded by
the backtester like every other idea, and every candidate (taken or not) is graded too, so Jev's
probabilities can be checked against what actually happened (docs/MODELS.md).

Every 5 minutes, for each tracked coin with a newly closed candle, one Jev call with everything Albatross
knows about it: per timeframe the chart features, the Strategist's call and reasoning, the ensemble and each
source's view; the coin's factor rank and scheduled events; and the fast inputs that change between
candles: Hyperliquid funding, open interest and its 1h/24h change, the last few hours of scored news, and
the latest X read (what X says about the coin and why it matters) with the market pulse. One yes/no
question per candidate: "does this trade reach its target before its stop within 6 candles?" Candidates
are mechanical so they're comparable: for each of 5m/15m/1h/4h/1d and each side, entry at the live price,
stop 1.5 ATR away, target at 2R. Each is scored once, on the first run after its candle's features land. Since
2026-10-02 the candidates are 1h/4h/1d only: 5m and 15m were 94% of the calls, every confidence band of theirs lost
after costs over 85k graded candidates, and their corrected odds never reached the book. Jev still reads 5m and 15m
as context, so the calibration still describes its answers.

Jev ranks candidates in the right order but states its odds 2-3x too high, so every probability is first corrected
against Jev's own graded record (jev_calibration: per timeframe, refit after every grading pass). Expected R and
the paper book use the corrected number; jev_scores keeps both.

Paper book: at most MAX_OPEN open trades; each run adds up to MAX_NEW of the candidates with the highest
expected R that clear the probability and EV floors (settings), never two open on the same coin, timeframe
and side. ~3-4k candidates a day for ~$0.20 at Jev's price (~60k for ~$3 with 5m and 15m).
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

import orjson

from albatross import db, ledger
from albatross.agents import jev_calibration
from albatross.agents.analysts import latest_features, latest_prices, universe
from albatross.config import settings
from albatross.events import context as event_context
from albatross.features.engine import HORIZON_BARS, TFS
from albatross.llm import typesafe
from albatross.llm.openrouter import BudgetExceeded
from albatross.strategy import ensemble, evidence

log = logging.getLogger(__name__)

AGENT = "jev_scout"
SOURCE_ID = "jev"
SCOUT_TFS = ["1h", "4h", "1d"]                # the candidates (5m and 15m too until 2026-10-02, see above)
CONTEXT_TFS = ["5m", "15m", *SCOUT_TFS]        # what Jev reads, unchanged
STOP_ATR = 1.5
RR = 2.0
ROUND_TRIP_COST_PCT = 0.10          # fallback only: each coin's cost comes from the backtester's model (_costs)
BUDGET_USD = 8.0                    # ~$0.20/day since 1h/4h/1d only; this only stops a runaway loop
CONCURRENCY = 8
FEATURE_KEYS = ["close", "ret_1", "ret_5", "ret_20", "dist_ema20_pct", "dist_ema50_pct", "dist_ema200_pct",
                "ema_stack", "rsi14", "atr_pct", "bb_pctb", "vol_z", "hi_20", "lo_20", "structure"]
HORIZON_WORDS = {"5m": "30 minutes", "15m": "90 minutes", "1h": "6 hours", "4h": "24 hours", "1d": "6 days"}


def candidate(tf: str, side: str, price: float, atr_pct: float, now: datetime, bar_ts: datetime) -> dict | None:
    """The mechanical trade for one timeframe and side: stop 1.5 ATR, target 2R, 6 candles from now.
    bar_ts is the closed candle the features describe: one score per candle, however often the scout runs."""
    if not price or not atr_pct or atr_pct <= 0:
        return None
    stop_pct = max(STOP_ATR * atr_pct, 0.3)                 # never a stop inside 0.3%
    sign = 1 if side == "long" else -1
    bar = TFS[tf][2]
    return {"tf": tf, "direction": side, "entry": price, "stop": price * (1 - sign * stop_pct / 100),
            "target": price * (1 + sign * RR * stop_pct / 100), "stop_pct": stop_pct,
            "bar_ts": bar_ts, "horizon_until": now + bar * HORIZON_BARS}


def expected_r(p: float, stop_pct: float, cost_pct: float = ROUND_TRIP_COST_PCT) -> float:
    """p x RR - (1 - p) - the round trip's cost in R. cost_pct is the coin's own round trip (fees plus
    slippage for the backtester's reference size), the same cost the grader charges: 0.1% on BTC, 2% on
    the thinnest coins, where it eats a 5m trade's whole target."""
    return round(p * RR - (1 - p) - cost_pct / stop_pct, 3)


async def _costs(symbols: list[str]) -> dict[str, float]:
    """Round-trip cost in % per coin: sim.side_cost() on Hyperliquid's 24h volume (else CoinGecko's)."""
    from albatross.backtest import sim
    pool = await db.pool()
    hl = {r["symbol"]: r["v"] for r in await pool.fetch(
        """SELECT DISTINCT ON (symbol) symbol, day_volume_usd AS v FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND symbol = ANY($1::text[]) AND ts > now() - interval '1 hour'
           ORDER BY symbol, ts DESC""", symbols)}
    cg = {r["symbol"]: r["v"] for r in await pool.fetch(
        "SELECT symbol, volume_24h_usd AS v FROM assets WHERE symbol = ANY($1::text[])", symbols)}
    return {s: round(2 * sim.side_cost(hl.get(s) or cg.get(s)) * 100, 4) for s in symbols}


def question(c: dict, sym: str) -> dict:
    move = f"{c['stop_pct']:.2f}%"
    return typesafe.noul(
        f"{sym} {c['direction'].upper()} on the {c['tf']} timeframe: entry {c['entry']:.6g}, stop {c['stop']:.6g} "
        f"({move} away), target {c['target']:.6g} ({RR:g}x the risk), held at most 6 {c['tf']} candles "
        f"({HORIZON_WORDS[c['tf']]}). Using everything in the state, does price reach the target before the stop "
        f"within that time?",
        "the target is reached first", "the stop is hit first, or neither is reached in time")


def our_view(tf: str, sym: str, ens: dict, views: dict, strat: dict, rank: dict | None) -> dict:
    e = ens.get((sym, tf)) or {}
    s = strat.get((sym, tf)) or {}
    return {"strategist": s.get("bias"), "ensemble": e.get("score"), "agreement": e.get("agreement"),
            "rank": f"{rank['rank']} of {rank['n']}" if rank else None, "quintile": rank["quintile"] if rank else None}


async def _strategist_calls(symbols: list[str]) -> dict:
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT DISTINCT ON (scope, tf) scope, tf, bias, confidence, drivers->>'thesis' AS thesis, regime
           FROM signals WHERE source = 'strategist' AND scope = ANY($1::text[]) AND valid_until > now()
           ORDER BY scope, tf, as_of DESC""", symbols)
    return {(r["scope"], r["tf"]): dict(r) for r in rows}


async def _scored_bars(symbols: list[str]) -> set[tuple[str, str, datetime]]:
    """(symbol, tf, bar_ts) already scored recently: never ask about the same candle twice. The window covers
    the oldest candle latest_features still serves (3 bars; a daily candle is 48 h old just before 00:27)."""
    pool = await db.pool()
    return {(r["symbol"], r["tf"], r["bar_ts"]) for r in await pool.fetch(
        """SELECT DISTINCT symbol, tf, bar_ts FROM jev_scores
           WHERE symbol = ANY($1::text[]) AND bar_ts > now() - interval '4 days'""", symbols)}


def _pct(a, b) -> float | None:
    return round((a / b - 1) * 100, 2) if a and b else None


async def _derivs(symbols: list[str]) -> dict[str, dict]:
    """Hyperliquid funding, open interest and its 1h / 24h change, premium: moves between candles."""
    pool = await db.pool()
    q = """SELECT DISTINCT ON (symbol) symbol, funding_rate, oi_usd, premium FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND symbol = ANY($1::text[]) AND ts BETWEEN now() - $2::interval - interval '10 minutes'
             AND now() - $2::interval ORDER BY symbol, ts DESC"""
    now_, h1, h24 = [{r["symbol"]: r for r in await pool.fetch(q, symbols, timedelta(minutes=m))} for m in (0, 60, 1440)]
    out = {}
    for sym, r in now_.items():
        out[sym] = {"funding_pct_per_8h": round(r["funding_rate"] * 800, 4) if r["funding_rate"] is not None else None,
                    "open_interest_usd_m": round(r["oi_usd"] / 1e6, 1) if r["oi_usd"] else None,
                    "oi_chg_1h_pct": _pct(r["oi_usd"], (h1.get(sym) or {}).get("oi_usd")),
                    "oi_chg_24h_pct": _pct(r["oi_usd"], (h24.get(sym) or {}).get("oi_usd")),
                    "premium_pct": round(r["premium"] * 100, 4) if r["premium"] is not None else None}
    return out


async def _news(symbols: list[str]) -> dict[str, list[dict]]:
    """The coin's scored headlines of the last 3 hours, most important first."""
    pool = await db.pool()
    now, out = datetime.now(UTC), {}
    for r in await pool.fetch(
            """SELECT title, impact, sentiment, assets, published_at FROM news_items
               WHERE published_at > now() - interval '3 hours' AND impact IS NOT NULL AND assets && $1::text[]
               ORDER BY impact DESC, published_at DESC""", symbols):
        for a in r["assets"]:
            if a in symbols and len(out.setdefault(a, [])) < 3:
                out[a].append({"title": r["title"][:140], "impact": r["impact"], "sentiment": r["sentiment"],
                               "age_min": int((now - r["published_at"]).total_seconds() // 60)})
    return out


async def _x(symbols: list[str]) -> tuple[dict[str, dict], dict | None]:
    """The latest X read per coin (6 h) and the market pulse (3 h), from Social Pulse."""
    pool = await db.pool()
    now, out = datetime.now(UTC), {}
    for r in await pool.fetch(
            """SELECT DISTINCT ON (scope) scope, ts, sentiment, volume, narratives FROM social_snapshots
               WHERE source = 'grok_x' AND scope = ANY($1::text[]) AND ts > now() - interval '6 hours'
               ORDER BY scope, ts DESC""", symbols):
        n = orjson.loads(r["narratives"]) if r["narratives"] else {}
        out[r["scope"]] = {"mood": round((r["sentiment"] or 0) * 5),
                           "summary": n.get("summary") or "; ".join(n.get("narratives", [])[:2]),
                           "why_it_matters": n.get("why_it_matters") or None, "crowding": n.get("crowding"),
                           "chatter": ["low", "normal", "elevated", "extreme"][int(r["volume"] or 0)],
                           "age_h": round((now - r["ts"]).total_seconds() / 3600, 1)}
    m = await pool.fetchrow(
        """SELECT ts, sentiment, narratives->>'market_mood' AS mood FROM social_snapshots
           WHERE source = 'grok_x' AND scope = 'market' AND ts > now() - interval '3 hours' ORDER BY ts DESC LIMIT 1""")
    market = {"mood": round((m["sentiment"] or 0) * 5), "said": m["mood"],
              "age_h": round((now - m["ts"]).total_seconds() / 3600, 1)} if m else None
    return out, market


async def run() -> int:
    now = datetime.now(UTC)
    syms = await universe()
    feats = await latest_features([s["symbol"] for s in syms], CONTEXT_TFS)
    done = await _scored_bars([s["symbol"] for s in syms])
    # Only coins with a candle not yet scored: most runs, the 1h candle that just closed.
    syms = [s for s in syms if any((f := feats.get((s["symbol"], tf))) and (s["symbol"], tf, f["ts"]) not in done
                                   for tf in SCOUT_TFS)]
    symbols = [s["symbol"] for s in syms]
    if not symbols:
        return 0
    prices = await latest_prices(symbols)
    ens, views, _ = await ensemble.compute(symbols)
    strat = await _strategist_calls(symbols)
    ranks = await evidence.factor_ranks()
    events, macro_events = await event_context.upcoming(symbols)
    derivs, news, costs = await _derivs(symbols), await _news(symbols), await _costs(symbols)
    xreads, xmarket = await _x(symbols)
    cal = await jev_calibration.load()
    sem, scored = asyncio.Semaphore(CONCURRENCY), []

    async def one(s: dict) -> None:
        sym, price = s["symbol"], prices.get(s["symbol"])
        if not price:
            return
        state = {"symbol": sym, "sector": s["sector"], "price": price, "timeframes": {},
                 "derivatives": derivs.get(sym), "news_3h": news.get(sym, []), "x": xreads.get(sym),
                 "x_market": xmarket, "factor_rank": evidence.rank_block(ranks.get(sym)),
                 "events": events.get(sym, []), "market_events": macro_events}
        cands = []
        for tf in CONTEXT_TFS:
            f = feats.get((sym, tf))
            if not f:
                continue
            e = ens.get((sym, tf)) or {}
            st = strat.get((sym, tf)) or {}
            state["timeframes"][tf] = {
                "chart": {k: f["data"].get(k) for k in FEATURE_KEYS if f["data"].get(k) is not None},
                "strategist": {"bias": st.get("bias"), "confidence": st.get("confidence"), "why": st.get("thesis")} if st else None,
                "ensemble": {"score": e.get("score"), "bias": e.get("bias"), "agreement": e.get("agreement")} if e else None,
                "views": [{"source": v["source"], "bias": v["bias"]} for v in views.get((sym, tf), [])]}
            if tf not in SCOUT_TFS or (sym, tf, f["ts"]) in done:
                continue                            # context only: a timeframe we don't score, or a candle already scored
            for side in ("long", "short"):
                if (c := candidate(tf, side, price, f["data"].get("atr_pct"), now, f["ts"])):
                    cands.append(c)
        if not cands:
            return
        qs = {f"{c['direction']}_{c['tf']}": question(c, sym) for c in cands}
        async with sem:
            try:
                answers, run_id = await typesafe.decide(AGENT, state, qs, daily_budget_usd=BUDGET_USD)
            except (BudgetExceeded, RuntimeError) as e:
                log.warning("%s %s: %s", AGENT, sym, e)
                return
        for c in cands:
            a = answers.get(f"{c['direction']}_{c['tf']}") or {}
            if a.get("noul") is None:
                continue
            p = float(a["noul"])
            pc = cal(p, c["tf"])                    # Jev's number, corrected against its graded record
            cost = costs.get(sym, ROUND_TRIP_COST_PCT)
            scored.append({**c, "symbol": sym, "p_win": round(p, 4), "p_cal": pc, "ev_r": expected_r(pc, c["stop_pct"], cost),
                           "our_view": {**our_view(c["tf"], sym, ens, views, strat, ranks.get(sym)), "cost_pct": cost},
                           "run_id": run_id})

    await asyncio.gather(*(one(s) for s in syms))
    pool = await db.pool()
    await pool.executemany(
        """INSERT INTO jev_scores (scored_at, bar_ts, symbol, tf, direction, entry, stop, target, horizon_until,
                                   p_win, p_cal, ev_r, our_view, run_id)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
           ON CONFLICT (symbol, tf, direction, bar_ts) DO NOTHING""",
        [(now, c["bar_ts"], c["symbol"], c["tf"], c["direction"], c["entry"], c["stop"], c["target"],
          c["horizon_until"], c["p_win"], c["p_cal"], c["ev_r"], orjson.dumps(c["our_view"]).decode(), c["run_id"])
         for c in scored])
    taken = await _paper_book(scored, now)
    log.info("%s: %d candidates scored on %d coins with a new candle, %d paper trades opened",
             AGENT, len(scored), len(syms), taken)
    return len(scored)


# A paper trade is open until its horizon or until it has exited (stop or target), whichever comes first.
OPEN_SQL = """SELECT c.scope FROM trade_calls c
               WHERE c.source_kind = 'model' AND c.source_id = $1 AND c.horizon_until > now()
                 AND NOT EXISTS (SELECT 1 FROM trade_call_outcomes o
                                 WHERE o.call_id = c.id AND o.policy = 'stop_target' AND o.status = 'graded')"""


def choose(scored: list[dict], open_coins: set[str], room: int) -> list[dict]:
    """The candidates the book takes: corrected probability and expected R over the floors, best expected R
    first, one per coin, never a coin already open."""
    picks = sorted((c for c in scored if c["p_cal"] >= settings.jev_scout_min_p and c["ev_r"] >= settings.jev_scout_min_ev
                    and c["symbol"] not in open_coins), key=lambda c: -c["ev_r"])
    seen, chosen = set(), []
    for c in picks:
        if len(chosen) >= room:
            break
        if c["symbol"] in seen:                     # one trade per coin at a time: the book never hedges itself
            continue
        seen.add(c["symbol"])
        chosen.append(c)
    return chosen


async def _paper_book(scored: list[dict], now: datetime) -> int:
    """Open the best qualifying candidates as paper trades, within the book's limits."""
    pool = await db.pool()
    open_coins = {r["scope"] for r in await pool.fetch(OPEN_SQL, SOURCE_ID)}
    chosen = choose(scored, open_coins, min(settings.jev_scout_max_new, settings.jev_scout_max_open - len(open_coins)))
    if not chosen:
        return 0
    rows = [{
        "source_kind": "model", "source_id": SOURCE_ID, "scope": c["symbol"], "market": "perp",
        "direction": c["direction"], "tf": c["tf"], "entry_type": "market", "ref_price": c["entry"],
        "stop": c["stop"], "targets": [c["target"]], "horizon_until": c["horizon_until"],
        "reward_risk": RR, "confidence": c["p_cal"], "status": "proposed",
        "thesis": f"Jev: {round(c['p_win'] * 100)}% to reach the target first, {round(c['p_cal'] * 100)}% on its record, "
                  f"EV {c['ev_r']:+.2f}R (paper)",
        "evidence": {"mode": "paper", "p_win": c["p_win"], "p_cal": c["p_cal"], "ev_r": c["ev_r"], "stop_atr": STOP_ATR,
                     "our_view": c["our_view"]},
        "critique": None, "parent_id": None, "agent_run_id": c["run_id"]} for c in chosen]
    ids = await ledger.append(rows)
    await pool.executemany(
        "UPDATE jev_scores SET triggered = true, call_id = $1 WHERE symbol = $2 AND tf = $3 AND direction = $4 AND bar_ts = $5",
        [(i, c["symbol"], c["tf"], c["direction"], c["bar_ts"]) for i, c in zip(ids, chosen)])
    return len(ids)


async def grade(limit: int = 20000) -> int | dict:
    """Grade candidates whose horizon has passed with the backtester's simulator (same costs and candles), then
    refit the correction of Jev's odds on the new grades."""
    from albatross.backtest import ideas as bt, sim
    pool = await db.pool()
    rows = [dict(r) for r in await pool.fetch(
        """SELECT id, scored_at, symbol, direction, entry, stop, target, horizon_until FROM jev_scores
           WHERE outcome IS NULL AND horizon_until < now() - $1::interval ORDER BY horizon_until LIMIT $2""",
        bt.SETTLE, limit)]
    now, out = datetime.now(UTC), []
    by: dict[str, list[dict]] = {}
    for r in rows:
        by.setdefault(r["symbol"], []).append(r)
    for sym, items in by.items():
        start, end = min(r["scored_at"] for r in items), max(r["horizon_until"] for r in items)
        sources = [(TFS[tf][2], s[1]) for tf in ("5m", "1h") if (s := await bt._candles(sym, tf, start - timedelta(days=1), end))]
        cost = sim.side_cost(await bt._day_volume(sym, start))
        funding = await bt._funding(sym, start, end)
        for r in items:
            c = {"direction": r["direction"], "ref_price": r["entry"], "stop": r["stop"], "targets": [r["target"]],
                 "logged_at": r["scored_at"], "horizon_until": r["horizon_until"]}
            o = next((res["stop_target"] for bar, df in sources
                      if (res := sim.simulate(c, df, bar, now - bt.SETTLE, cost, funding)) and res["stop_target"]["status"] == "graded"), None)
            if o:
                out.append((r["id"], "graded", o.get("exit_reason"), o.get("r_multiple"), o.get("net_return_pct"), now))
            elif r["horizon_until"] < now - bt.GIVE_UP:
                out.append((r["id"], "ungradeable", "no_candles", None, None, now))
    await pool.executemany(
        """UPDATE jev_scores SET outcome = $2, exit_reason = $3, r_multiple = $4, net_return_pct = $5, graded_at = $6
           WHERE id = $1""", out)
    log.info("jev_grade: %d candidates graded", len(out))
    if not out:
        return 0
    counts = await jev_calibration.refit()          # the correction always reflects the latest grades
    log.info("jev_calibration: refit on %s", counts)
    return {"rows": len(out), "calibration": counts}
