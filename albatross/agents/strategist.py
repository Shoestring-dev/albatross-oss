"""Strategist: turns the ensemble + every source's view + X context + news into the final call per
asset and timeframe, a regime label, and (sometimes) a trade idea; a devil's-advocate pass then
attacks each idea before it is written to the append-only ledger.

Guard rails (code, not prompt):
- a final call may differ from the ensemble by at most one step, and must cite inputs it was given
- an idea must agree with the strategist's own call on that timeframe, with the stop and targets on
  the right side of price; reward:risk is computed here and must be >= MIN_RR
- vetoed ideas are still logged (status 'vetoed') so the backtest can measure whether the critic helps
- an idea already live for the same asset/direction/timeframe is not logged again
Calls are anchored at call time (live inputs) and graded like every other source.
"""

import asyncio
import logging
import random
import time
from datetime import UTC, datetime
from typing import Literal

import orjson
from pydantic import BaseModel, Field

from albatross import db, ledger
from albatross.config import settings
from albatross.agents import macro
from albatross.agents.analysts import (
    latest_features,
    latest_prices,
    live_anchor,
    universe,
)
from albatross.features.engine import HORIZON_BARS, TFS
from albatross.llm import codex, openrouter
from albatross.events import context as event_context
from albatross.strategy import ensemble, evidence

log = logging.getLogger(__name__)

AGENT = "strategist"
# Shadows log their calls under their source name, strategist@<model slug> (before 2026-10-04: "strategist_shadow")
CRITIC = "strategist_critic"
BATCH = 4
CONCURRENCY = 4
MAX_TOKENS = 20000
SHADOW_MAX_S = 40 * 60           # from the hour's start, all its shadows together: never into the next hour's pass
BUDGET_USD = 4.0
CRITIC_BUDGET_USD = 2.0          # GPT-6.1 Sol at list price (~$0.03 a review), counted across both lanes
MIN_RR = 1.5
MIN_STOP_ATR = 1.0                  # stop at least this many ATRs (of the idea's tf) from price
IDEA_TFS = ["1h", "4h", "1d"]
ALL_TFS = ensemble.ALL_TFS
Regime = Literal["trending_up", "trending_down", "range", "pullback_in_uptrend", "bounce_in_downtrend",
                 "breakout_attempt", "breakdown_attempt", "capitulation", "euphoria", "mixed"]


class TfCall(BaseModel):
    tf: str
    bias: int = Field(ge=-2, le=2)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(description="<= 25 words")
    relies_on: list[str] = Field(default_factory=list, description="input names this call rests on")


class Idea(BaseModel):
    direction: Literal["long", "short"]
    tf: str
    stop: float
    targets: list[float] = Field(description="1-2 price levels, nearest first")
    thesis: str = Field(description="<= 40 words")
    relies_on: list[str] = Field(default_factory=list)


class AssetPlan(BaseModel):
    symbol: str
    regime: Regime
    calls: list[TfCall]
    idea: Idea | None = None


class Plan(BaseModel):
    assets: list[AssetPlan]


class Verdict(BaseModel):
    idea_id: int
    verdict: Literal["keep", "downgrade", "veto"]
    reason: str = Field(description="the strongest specific objection, citing the inputs")


class Critique(BaseModel):
    verdicts: list[Verdict]


SYSTEM = """You are the Strategist on a crypto desk. For each asset you get, per timeframe (5m, 15m, 1h,
4h, 1d, 1w): the ENSEMBLE (a weighted blend of every source; score -1..1, bias -2..+2, agreement = share
of weight on the winning side) and each source's own view with its weight. Weights reflect measured
accuracy: a source at weight <= 0.2 has been wrong more often than a naive always-long/short caller, so
discount it. You also get key price levels, recent high-impact news, the X (Twitter) read for large
caps (mood -5..+5, crowding, narratives, named positioning, catalysts) and market context, including
a macro block (yields, real yield, dollar, US net liquidity, VIX, S&P, ETF flows, stablecoin supply).

WHAT THE EVIDENCE SAYS (read this before deciding anything):
""" + evidence.FINDINGS + """
Each asset carries `factor_rank` (its place among the liquid coins today, its fifth, and why) when it is
liquid enough to rank; `market.source_edges` says which sources have beaten a naive caller so far. So:
- Direction: no source has proven it can call direction yet, so treat directional views as weak; bias 0
  is a good answer when sources disagree. Never present a call as stronger than its evidence.
- Relative: lean on the factor rank. Trade ideas should be LONGS in the strongest fifth or SHORTS in the
  weakest fifth; do not propose a long in the weakest two fifths or a short in the strongest two fifths.
  Name the rank in the rationale when it drives the call.
- Scheduled events: an asset's `events` (listings, delistings, unlocks, TGEs, mainnets, votes; 2 days back
  to 7 ahead) and `market.scheduled_macro` (FOMC, CPI, jobs) are known catalysts. Say so in the rationale
  when one falls inside a call's horizon, and keep stops outside the obvious event-day noise; don't
  assume a direction for an event the inputs don't give one.

For EVERY timeframe that has an ensemble, give the final call:
- bias -2..+2, at most ONE step away from the ensemble bias (you refine, you don't override); move it
  only for a reason in the inputs: news or catalysts, crowding, conflict between strong sources, or a
  higher timeframe the ensemble ignores
- confidence 0..1; rationale <= 25 words; relies_on = the names of the inputs you used, chosen from:
  the source names shown, "ensemble", "levels", "news", "social", "market", "factor_rank", "events"
Label the regime. Then OPTIONALLY one trade idea per asset (most assets should have none): only when
the 1h/4h/1d call is strong and consistent with the higher timeframe, with a stop beyond a level from
the inputs AND at least one ATR of the idea's timeframe away from price (levels <tf>_atr_pct is the ATR
as % of price), and 1-2 targets at levels from the inputs. Ideas with reward:risk under 1.5 or a stop
inside one ATR are discarded, so a wider stop needs a farther target.
Use only the numbers given."""

CRITIC_SYSTEM = """You are the devil's advocate on a crypto desk. Each trade idea below comes with the
inputs the Strategist saw. For each idea, find the strongest specific reason it is wrong: a higher
timeframe against it, a strong source against it, the factor rank against it (a long outside the
strongest fifths or a short outside the weakest), crowding in the same direction, a stop inside noise
(compare to typical range), a target beyond obvious resistance/support, or news/catalyst risk.
verdict: keep (no serious objection), downgrade (real but not fatal), veto (the idea should not be
taken). Be tough but fair; do not veto for vague reasons. Cite the input in your reason."""


# ─── inputs ─────────────────────────────────────────────────────────────────
async def _news(symbols: list[str]) -> dict[str, list[dict]]:
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT assets, impact, sentiment, title, published_at FROM news_items
           WHERE impact >= 3 AND published_at > now() - interval '24 hours' AND assets && $1::text[]
           ORDER BY impact DESC, published_at DESC""", symbols + ["MARKET"])
    out: dict[str, list[dict]] = {}
    now = datetime.now(UTC)
    for r in rows:
        item = {"impact": r["impact"], "sentiment": r["sentiment"], "title": r["title"][:140],
                "age_h": round((now - r["published_at"]).total_seconds() / 3600, 1)}
        for a in r["assets"]:
            if len(out.setdefault(a, [])) < 3:
                out[a].append(item)
    return out


async def _social(symbols: list[str]) -> dict[str, dict]:
    pool = await db.pool()
    now = datetime.now(UTC)
    out = {}
    for r in await pool.fetch(
        """SELECT DISTINCT ON (scope) scope, ts, sentiment, narratives FROM social_snapshots
           WHERE scope = ANY($1::text[]) AND ts > now() - interval '6 hours' ORDER BY scope, ts DESC""", symbols):
        n = orjson.loads(r["narratives"]) if r["narratives"] else {}
        out[r["scope"]] = {"mood": round((r["sentiment"] or 0) * 5), "summary": n.get("summary"),
                           "why_it_matters": n.get("why_it_matters"), "crowding": n.get("crowding"),
                           "narratives": n.get("narratives", [])[:3], "positioning": n.get("positioning", [])[:3],
                           "catalysts": n.get("catalysts", [])[:3],
                           "age_h": round((now - r["ts"]).total_seconds() / 3600, 1)}
    return out


async def _market(ens: dict) -> dict:
    pool = await db.pool()
    fg = await pool.fetchrow(
        "SELECT value, ts FROM metric_series WHERE metric = 'fear_greed' ORDER BY ts DESC LIMIT 1")
    m = {"fear_greed": fg["value"] if fg else None}
    for tf in ("4h", "1d", "1w"):
        e = ens.get(("BTC", tf))
        if e:
            m[f"btc_ensemble_{tf}"] = e["score"]
    snap = await macro.snapshot()
    m["macro"] = {k: snap[k] for k in ("ust10y_pct", "ust10y_chg_bps_5d", "real_yield10y_pct", "dollar_chg_pct_5d",
                                       "net_liquidity_chg_busd_4w", "vix", "spx_chg_pct_5d",
                                       "btc_etf_net_inflow_musd_5d", "eth_etf_net_inflow_musd_5d",
                                       "stablecoin_chg_pct_30d") if k in snap}
    return m


def _levels(sym: str, feats: dict) -> dict:
    lv = {}
    for tf in ("1h", "4h", "1d"):
        f = feats.get((sym, tf))
        if f:
            d = f["data"]
            for k in ("last_swing_high", "last_swing_low", "hi_20", "lo_20", "atr_pct"):
                if d.get(k) is not None:
                    lv[f"{tf}_{k}"] = float(f"{d[k]:.6g}")
    return lv


def _payload(sym: dict, price: float, ens: dict, views: dict, weights: dict, feats: dict,
             news: dict, social: dict, ranks: dict | None = None, events: dict | None = None) -> dict | None:
    tfs = {}
    for tf in ALL_TFS:
        e = ens.get((sym["symbol"], tf))
        if not e:
            continue
        tfs[tf] = {
            "ensemble": {"score": e["score"], "bias": e["bias"], "agreement": e["agreement"]},
            "views": [{"source": v["source"], "bias": v["bias"], "confidence": v["confidence"],
                       "weight": weights[(v["source"], tf)]["weight"], "thesis": (v["thesis"] or "")[:200]}
                      for v in views.get((sym["symbol"], tf), [])],
        }
    if not tfs:
        return None
    p = {"symbol": sym["symbol"], "sector": sym["sector"], "price": float(f"{price:.6g}"), "timeframes": tfs,
         "levels": _levels(sym["symbol"], feats)}
    if news.get(sym["symbol"]):
        p["news"] = news[sym["symbol"]]
    if sym["symbol"] in social:
        p["social"] = social[sym["symbol"]]
    if (rb := evidence.rank_block((ranks or {}).get(sym["symbol"]))):
        p["factor_rank"] = rb
    if (ev := (events or {}).get(sym["symbol"])):
        p["events"] = ev
    return p


# ─── validation ─────────────────────────────────────────────────────────────
def _allowed(p: dict, tf: str) -> set[str]:
    a = {"ensemble", "levels", "market"} | {v["source"] for v in p["timeframes"].get(tf, {}).get("views", [])}
    return (a | ({"news"} if "news" in p else set()) | ({"social"} if "social" in p else set())
            | ({"factor_rank"} if "factor_rank" in p else set()) | ({"events"} if "events" in p else set()))


def check_call(c: TfCall, p: dict) -> str | None:
    t = p["timeframes"].get(c.tf)
    if t is None:
        return f"no ensemble for {c.tf}"
    if abs(c.bias - t["ensemble"]["bias"]) > 1:
        return f"{c.tf} bias {c.bias} is >1 step from ensemble {t['ensemble']['bias']}"
    if not c.relies_on or not set(c.relies_on) <= _allowed(p, c.tf):
        return f"{c.tf} relies_on {c.relies_on} not all in inputs"
    return None


def check_idea(i: Idea, p: dict, calls: dict[str, TfCall]) -> tuple[str | None, float | None]:
    price = p["price"]
    sign = 1 if i.direction == "long" else -1
    call = calls.get(i.tf)
    if i.tf not in IDEA_TFS or call is None:
        return f"idea tf {i.tf} not callable", None
    if call.bias * sign < 1:
        return f"idea {i.direction} disagrees with own {i.tf} call {call.bias}", None
    # The factor rank is the one signal with measured edge (docs/RESEARCH-EDGE.md): never trade against it.
    q = (p.get("factor_rank") or {}).get("quintile")
    if q is not None and (q <= 2 if sign > 0 else q >= 4):
        return f"{i.direction} against the factor rank ({p['factor_rank']['group']})", None
    if not i.targets or (i.stop - price) * sign >= 0 or any((t - price) * sign <= 0 for t in i.targets):
        return "stop/targets on the wrong side of price", None
    risk = abs(price - i.stop) / price
    if not 0.002 <= risk <= 0.25:
        return f"stop distance {risk:.2%} outside 0.2%..25%", None
    # A stop inside one ATR of its own timeframe sits in ordinary noise: the critic flagged this on
    # 7 of the first 15 ideas, so it is a rule now, not a judgement call.
    atr = p["levels"].get(f"{i.tf}_atr_pct")
    if atr and risk * 100 < MIN_STOP_ATR * atr:
        return f"stop {risk:.2%} is inside {MIN_STOP_ATR}x the {i.tf} ATR ({atr:.2f}%)", None
    rr = abs(i.targets[0] - price) / abs(price - i.stop)
    if rr < MIN_RR:
        return f"reward:risk {rr:.2f} < {MIN_RR}", round(rr, 2)
    return None, round(rr, 2)


# ─── run ────────────────────────────────────────────────────────────────────
async def run() -> int:
    shadows_end = time.monotonic() + SHADOW_MAX_S       # one end for every shadow of this hour, run one after another
    syms = await universe()
    symbols = [s["symbol"] for s in syms]
    ens, views, weights = await ensemble.compute(symbols)
    prices = await latest_prices(symbols)
    feats = await latest_features(symbols, ["1h", "4h", "1d"])
    news, social = await _news(symbols), await _social(symbols)
    market = await _market(ens)
    ranks = await evidence.factor_ranks()
    market["source_edges"] = evidence.edges_for_prompt(await evidence.source_edges())
    events, market["scheduled_macro"] = await event_context.upcoming(symbols)
    payloads = [p for s in syms if s["symbol"] in prices
                and (p := _payload(s, prices[s["symbol"]], ens, views, weights, feats, news, social, ranks, events))]
    by_sym = {p["symbol"]: p for p in payloads}
    sem = asyncio.Semaphore(CONCURRENCY)

    now = datetime.now(UTC)
    ideas: list = []
    counts = {"calls": 0, "dropped": 0}

    async def batch(chunk: list[dict]) -> None:
        async with sem:
            try:
                plan, run_id = await openrouter.structured(          # GPT-6 Luna on the ChatGPT plan since
                    AGENT, Plan, SYSTEM, orjson.dumps({"market": market, "assets": chunk}).decode(),  # 2026-10-05
                    model=openrouter.LUNA, daily_budget_usd=BUDGET_USD, max_tokens=MAX_TOKENS)
            except (openrouter.BudgetExceeded, RuntimeError) as e:
                log.warning("%s", e)
                return
        # Write each batch as it lands, so one slow batch can't hold back (or lose) the others.
        rows, batch_ideas, reasons = _apply(plan, run_id, by_sym, now)
        if reasons:
            pool = await db.pool()
            await pool.execute("UPDATE agent_runs SET error = $2 WHERE id = $1",
                               run_id, f"dropped {len(reasons)}: " + "; ".join(reasons)[:900])
        await db.upsert(
            "signals", ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers",
                        "valid_until", "source", "as_of", "score", "regime", "agent_run_id"],
            rows, ["scope", "tf", "source", "as_of"],
        )
        ideas.extend(batch_ideas)
        counts["calls"] += len(rows)
        counts["dropped"] += len(reasons)

    await asyncio.gather(*(batch(payloads[i:i + BATCH]) for i in range(0, len(payloads), BATCH)))
    logged = await _log_ideas(ideas, now)
    for model in shadow_models():
        try:
            await _shadow(model, payloads, market, by_sym, now, shadows_end)
        except Exception as e:  # noqa: BLE001 - the shadow must never cost the real run anything
            log.warning("strategist shadow %s failed: %s", model, type(e).__name__)
    log.info("strategist: %d calls, %d ideas proposed, %d logged, %d dropped by validation",
             counts["calls"], len(ideas), logged, counts["dropped"])
    return counts["calls"]


def shadow_source(model: str) -> str:
    """strategist@jev-router for typesafe/jev-router: one graded source per shadow model."""
    return f"{AGENT}@{model.rsplit('/', 1)[-1]}"


def shadow_models() -> list[str]:
    """STRATEGIST_SHADOW_MODELS (comma-separated) plus the older single STRATEGIST_SHADOW_MODEL, each once."""
    out: list[str] = []
    for m in [settings.strategist_shadow_model or "", *(settings.strategist_shadow_models or "").split(",")]:
        if m.strip() and m.strip() not in out:
            out.append(m.strip())
    return out


async def _shadow(model: str, payloads: list[dict], market: dict, by_sym: dict, now: datetime,
                  end: float | None = None) -> int:
    """The same batches on a shadow model, stored as their own source and graded like any other. They feed nothing:
    no ideas, no ensemble, no page reads source 'strategist@...'. Each shadow is its own agent in agent_runs (with its
    own budget at list price) and gives way to the regular agents on the ChatGPT plan; one on Sol runs only there,
    two batches at a time so the plan keeps slots for everyone else."""
    source = shadow_source(model)
    plan_only, budget = openrouter.shadow_terms(model, settings.strategist_shadow_budget_usd)
    if plan_only and not (await codex.pace(experiment=True))[0]:
        log.info("%s: the plan has no room for shadows now, skipped this hour", source)    # whole, never a part
        return 0
    rows, stop_at = [], end or time.monotonic() + SHADOW_MAX_S

    async def batch(chunk: list[dict]) -> None:
        try:                                           # not_after: checked once the call has its slot
            plan, run_id = await openrouter.structured(
                source, Plan, SYSTEM, orjson.dumps({"market": market, "assets": chunk}).decode(),
                model=model, daily_budget_usd=budget, max_tokens=MAX_TOKENS, experiment=True, plan_only=plan_only,
                not_after=stop_at)
        except (openrouter.BudgetExceeded, RuntimeError) as e:
            log.warning("strategist shadow: %s", e)
            return
        rows.extend(_apply(plan, run_id, by_sym, now, source)[0])

    chunks = [payloads[i:i + BATCH] for i in range(0, len(payloads), BATCH)]
    random.shuffle(chunks)                             # a run cut short is a random sample, not the first coins
    await asyncio.gather(*(batch(c) for c in chunks))  # experiments queue for their 2 slots (llm/codex.py)
    await db.upsert(
        "signals", ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers",
                    "valid_until", "source", "as_of", "score", "regime", "agent_run_id"],
        rows, ["scope", "tf", "source", "as_of"])
    log.info("strategist shadow (%s): %d calls as %s", model, len(rows), source)
    return len(rows)


def _apply(plan: Plan, run_id: int, by_sym: dict, now: datetime, source: str = AGENT) -> tuple[list, list, list]:
    """Validate one batch's plan -> (signal rows, ideas, drop reasons)."""
    rows, ideas, reasons = [], [], []
    for a in plan.assets:
        p = by_sym.get(a.symbol.upper())
        if p is None:
            continue
        ok_calls = {}
        for c in a.calls:
            why = check_call(c, p)
            if why:
                reasons.append(f"{p['symbol']} {why}")
                continue
            ok_calls[c.tf] = c
            as_of, ref = live_anchor(c.tf, now, p["price"])
            rows.append((
                p["symbol"], c.tf, c.bias, round(c.confidence, 3), ref, None,
                orjson.dumps({"thesis": c.rationale, "relies_on": c.relies_on,
                              "ensemble": p["timeframes"][c.tf]["ensemble"]}).decode(),
                as_of + TFS[c.tf][2] * (1 + HORIZON_BARS), source, as_of, c.bias / 2, a.regime, run_id))
        if a.idea:
            why, rr = check_idea(a.idea, p, ok_calls)
            if why:
                reasons.append(f"{p['symbol']} idea: {why}")
            else:
                ideas.append((p, a, a.idea, rr, ok_calls[a.idea.tf], run_id))
    return rows, ideas, reasons


async def _log_ideas(ideas: list, now: datetime) -> int:
    if not ideas:
        return 0
    pool = await db.pool()
    live = {(r["scope"], r["direction"], r["tf"]) for r in await pool.fetch(
        """SELECT scope, direction, tf FROM trade_calls
           WHERE source_kind = 'strategist' AND (horizon_until > now() OR logged_at > now() - interval '4 hours')""")}
    fresh = [x for x in ideas if (x[0]["symbol"], x[2].direction, x[2].tf) not in live]
    if not fresh:
        return 0
    verdicts = await _critique(fresh)
    second = await _jev_second_opinion(fresh)
    rows = []
    for n, (p, a, idea, rr, call, run_id) in enumerate(fresh):
        v = verdicts.get(n)
        conf = call.confidence * (0.6 if v and v.verdict == "downgrade" else 1.0)
        as_of, _ = live_anchor(idea.tf, now, p["price"])
        rows.append({
            "source_kind": "strategist", "source_id": AGENT, "scope": p["symbol"], "market": "perp",
            "direction": idea.direction, "tf": idea.tf, "entry_type": "market", "ref_price": p["price"],
            "stop": idea.stop, "targets": idea.targets,
            "horizon_until": as_of + TFS[idea.tf][2] * (1 + HORIZON_BARS),
            "reward_risk": rr, "confidence": round(conf, 3),
            "status": "vetoed" if v and v.verdict == "veto" else "proposed",
            "thesis": idea.thesis,
            "evidence": {"relies_on": idea.relies_on, "regime": a.regime, "call": call.model_dump(),
                         "ensemble": p["timeframes"][idea.tf]["ensemble"]}
                        | ({"jev_critic": second[n]} if n in second else {}),
            "critique": v.model_dump() if v else None,
            "parent_id": None, "agent_run_id": run_id,
        })
    await ledger.append(rows)
    return len(rows)


JEV_CRITIC = {
    "keep": "no serious objection: the idea is consistent with its inputs",
    "downgrade": "a real but not fatal objection: a higher timeframe, a strong source or the factor rank "
                 "against it, crowding, or a stop inside noise",
    "veto": "should not be taken: the inputs contradict the idea, or its stop or target makes no sense",
}


async def _jev_second_opinion(ideas: list) -> dict[int, dict]:
    """TypeSafe Jev's keep/downgrade/veto probabilities for each idea, over the inputs the critic saw; stored in
    the idea's evidence so later we can check whether a high P(veto) marks the losers. Never blocks logging."""
    from albatross.llm import typesafe
    if not settings.typesafe_api_key or not ideas:
        return {}
    state = [{"idea_id": n, "symbol": p["symbol"], "idea": idea.model_dump(), "reward_risk": rr,
              "inputs": {k: p[k] for k in ("price", "timeframes", "levels", "factor_rank", "events") if k in p}}
             for n, (p, _a, idea, rr, _c, _r) in enumerate(ideas)]
    qs = {f"idea_{n}": typesafe.choice(f"Judge trade idea {n} ({state[n]['symbol']}) against its inputs.", JEV_CRITIC)
          for n in range(len(ideas))}
    try:
        answers, _ = await typesafe.decide("jev_critic", state, qs, daily_budget_usd=0.25)
    except Exception as e:  # noqa: BLE001 - a second opinion is optional
        log.warning("jev second opinion unavailable: %s", e)
        return {}
    return {n: {k: round(float(v), 3) for k, v in (answers[f"idea_{n}"].get("probabilities") or {}).items()}
            for n in range(len(ideas)) if f"idea_{n}" in answers}


async def _critique(ideas: list) -> dict[int, Verdict]:
    body = [{"idea_id": n, "symbol": p["symbol"], "idea": idea.model_dump(), "reward_risk": rr,
             "inputs": {k: p[k] for k in ("price", "timeframes", "levels", "news", "social", "factor_rank", "events") if k in p}}
            for n, (p, _a, idea, rr, _c, _r) in enumerate(ideas)]
    try:
        crit, _ = await openrouter.structured(
            CRITIC, Critique, CRITIC_SYSTEM, orjson.dumps(body).decode(), model=openrouter.SOL,
            daily_budget_usd=CRITIC_BUDGET_USD, max_tokens=MAX_TOKENS, reasoning_effort="medium")
    except (openrouter.BudgetExceeded, RuntimeError) as e:
        log.warning("critic unavailable, ideas logged without a verdict: %s", e)
        return {}
    return {v.idea_id: v for v in crit.verdicts if 0 <= v.idea_id < len(ideas)}

