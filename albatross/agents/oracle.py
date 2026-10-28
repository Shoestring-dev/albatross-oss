"""Oracle: the on-call senior analyst, an OpenAI model with web search through OpenRouter (or the ChatGPT
plan's search when that lane is on). It does
not run on a timer over everything: it is escalated to when something happens, plus a daily market thesis and a
weekly scorecard review.

Triggers (checked every 10 min in the ingest container; each key escalates once per cooldown):
  news     impact-5 headline in the last hour
  move     a Tier 1/2 asset's last closed 1h candle moved >= max(3%, 2.5x its 1h ATR)
  flip     a Tier 1 Strategist call on 4h/1d changed side (long <-> short)
  conflict a Tier 1 asset where weighted sources disagree hard on 4h/1d (strong views both ways,
           ensemble agreement <= 0.6)
The Oracle sees what the Strategist saw (ensemble, every source's view and weight, levels, news, X
read, macro) plus the trigger, and may research the web. Guard rails, all in code:
- web search only (`llm.openrouter.research`: no tools but OpenRouter's search, GitHub never searched)
- every citation must be a URL the call's own search returned; unverifiable ones are dropped, and
  "web" can only be relied on when at least one citation survives
- calls must cite inputs they were given; an idea passes the same checks as the Strategist's
  (side of price, stop >= 1 ATR, reward:risk >= 1.5) and goes to the append-only ledger
Everything is stored in `escalations` (trigger, context, verdict); calls are graded as source
'oracle'; the daily thesis and weekly review are also written to `reports`.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

import orjson
from pydantic import BaseModel, Field

from albatross import db, github_guard, ledger
from albatross.agents import macro, strategist
from albatross.agents.analysts import (
    latest_features,
    latest_prices,
    live_anchor,
    universe,
)
from albatross.features.engine import HORIZON_BARS, TFS
from albatross.llm import openrouter
from albatross.strategy import ensemble

log = logging.getLogger(__name__)

AGENT = "oracle"
# The Oracle's own calls are an ensemble source: it reads the ensemble without them, so its earlier view never
# comes back to it as the desk's (albatross/agents/graph.py).
SELF = frozenset({"oracle"})
MAX_CALLS_PER_DAY = 12          # 2 are kept for the daily/weekly runs
TRIGGERED_PER_DAY = 10
PER_RUN = 2
COOLDOWN = timedelta(hours=6)
MODEL = openrouter.RESEARCH     # LLM_MODEL_OVERRIDES=oracle=<model> switches it
DAILY_USD = 2.0                 # ~$0.10 an escalation with its searches; the Output page scales it


class Citation(BaseModel):
    url: str
    claim: str = Field(description="what this source supports, one sentence")


class OracleCall(BaseModel):
    tf: str
    bias: int = Field(ge=-2, le=2)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(description="<= 30 words")
    relies_on: list[str] = Field(default_factory=list)


class Verdict(BaseModel):
    assessment: str = Field(description="what is happening and what it means, <= 120 words")
    what_would_change_my_mind: str
    calls: list[OracleCall] = Field(default_factory=list)
    idea: strategist.Idea | None = None
    citations: list[Citation] = Field(default_factory=list)
    human_attention: bool = Field(description="true only if the user should look at this now")
    human_note: str = ""


class ThesisCall(OracleCall):
    symbol: Literal["BTC", "ETH"]


class Thesis(BaseModel):
    title: str
    body_md: str = Field(description="markdown, <= 450 words")
    calls: list[ThesisCall] = Field(default_factory=list, description="BTC and ETH, 1d and 1w")
    citations: list[Citation] = Field(default_factory=list)


class Review(BaseModel):
    title: str
    body_md: str = Field(description="markdown, <= 500 words")
    recommendations: list[str] = Field(description="concrete, testable changes to prompts/weights/rules")


SYSTEM = """You are the Oracle: the senior analyst a crypto desk escalates to when something happens.
Below is the trigger and everything the desk's Strategist saw for the asset: the ensemble per timeframe,
each source's view and its measured-accuracy weight (<= 0.2 means worse than a naive caller), price
levels, recent news, the X read, and a macro block. You may search the web to check what happened;
cite every web fact with the exact URL your search returned. Then:
- assess what is going on and whether the desk's calls still make sense (<= 120 words)
- give calls only for the timeframes you have a real view on (bias -2..+2, confidence, <= 30-word
  rationale, relies_on from: the source names shown, "ensemble", "levels", "news", "social", "market",
  "web")
- optionally one trade idea (stop beyond a level AND at least one ATR of its timeframe from price,
  levels <tf>_atr_pct; 1-2 targets; reward:risk >= 1.5)
- set human_attention only if the user should look now (e.g. a hack, a delisting, a regime break).
Be concrete and calibrated. Use only numbers from the inputs or from pages you cite."""


# ─── triggers ───────────────────────────────────────────────────────────────
async def triggers() -> list[dict]:
    pool = await db.pool()
    syms = await universe()
    tier1 = [s["symbol"] for s in syms[:10]]            # universe() is ordered by tier, then volume
    all_syms = [s["symbol"] for s in syms]
    out = []
    for r in await pool.fetch(
        """SELECT id, title, assets, sentiment FROM news_items
           WHERE impact = 5 AND published_at > now() - interval '60 minutes' ORDER BY published_at DESC"""):
        scope = next((a for a in r["assets"] if a in all_syms), "BTC")
        out.append({"kind": "news", "scope": scope, "key": f"news:{r['id']}", "priority": 0,
                     "detail": {"headline": r["title"], "assets": r["assets"], "sentiment": r["sentiment"]}})
    feats = await latest_features(all_syms, ["1h"])
    now = datetime.now(UTC)
    for (sym, _tf), f in feats.items():
        d = f["data"]
        if f["ts"] < now - timedelta(minutes=150) or d.get("ret_1") is None or not d.get("atr_pct"):
            continue
        if abs(d["ret_1"]) >= max(3.0, 2.5 * d["atr_pct"]):
            out.append({"kind": "move", "scope": sym, "key": f"move:{sym}:{f['ts'].isoformat()}", "priority": 1,
                        "detail": {"ret_1h_pct": d["ret_1"], "atr_1h_pct": d["atr_pct"], "candle": f["ts"].isoformat()}})
    for r in await pool.fetch(
        """WITH s AS (
             SELECT scope, tf, bias, as_of, row_number() OVER (PARTITION BY scope, tf ORDER BY as_of DESC) AS rn
             FROM signals WHERE source = 'strategist' AND tf IN ('4h', '1d') AND scope = ANY($1::text[])
               AND as_of > now() - interval '4 days')
           SELECT a.scope, a.tf, a.bias AS now_bias, b.bias AS was_bias, a.as_of
           FROM s a JOIN s b ON a.scope = b.scope AND a.tf = b.tf AND a.rn = 1 AND b.rn = 2
           WHERE a.bias * b.bias < 0""", tier1):
        out.append({"kind": "flip", "scope": r["scope"], "key": f"flip:{r['scope']}:{r['tf']}:{r['as_of'].isoformat()}",
                    "priority": 2, "detail": {"tf": r["tf"], "from": r["was_bias"], "to": r["now_bias"]}})
    ens, views, weights = await ensemble.compute(tier1, exclude=SELF)
    for (sym, tf), e in ens.items():
        if tf not in ("4h", "1d") or e["agreement"] > 0.6:
            continue
        strong = [(v["source"], v["bias"]) for v in views.get((sym, tf), [])
                  if abs(v["bias"]) >= 1 and weights[(v["source"], tf)]["weight"] >= 0.75]
        if any(b > 0 for _, b in strong) and any(b < 0 for _, b in strong):
            out.append({"kind": "conflict", "scope": sym, "key": f"conflict:{sym}:{tf}:{now:%Y-%m-%d}",
                        "priority": 3, "detail": {"tf": tf, "strong_views": strong, "agreement": e["agreement"]}})
    return sorted(out, key=lambda t: t["priority"])


async def _fresh(trigs: list[dict]) -> list[dict]:
    pool = await db.pool()
    seen = {r["key"] for r in await pool.fetch(
        "SELECT context->>'key' AS key FROM escalations WHERE created_at > now() - $1::interval", COOLDOWN)}
    return [t for t in trigs if t["key"] not in seen]


# ─── one escalation ─────────────────────────────────────────────────────────
async def _asset_payload(sym: str) -> dict | None:
    syms = [s for s in await universe() if s["symbol"] == sym]
    if not syms:
        return None
    ens, views, weights = await ensemble.compute([sym], exclude=SELF)
    prices = await latest_prices([sym])
    feats = await latest_features([sym], ["1h", "4h", "1d"])
    if sym not in prices:
        return None
    p = strategist._payload(syms[0], prices[sym], ens, views, weights, feats,
                            await strategist._news([sym]), await strategist._social([sym]))
    if p:
        pool = await db.pool()
        p["strategist_now"] = {r["tf"]: {"bias": r["bias"], "regime": r["regime"], "rationale": r["thesis"]}
                               for r in await pool.fetch(
            """SELECT DISTINCT ON (tf) tf, bias, regime, drivers->>'thesis' AS thesis FROM signals
               WHERE source = 'strategist' AND scope = $1 AND valid_until > now() ORDER BY tf, as_of DESC""", sym)}
    return p


def _grounded(cites: list[Citation], evidence: set[str]) -> tuple[list[Citation], list[str]]:
    """Keep citations whose URL the run actually fetched or saw in search results (never one on GitHub)."""
    ok, dropped = [], []
    for c in cites:
        u = c.url.strip().rstrip("/.,;")
        seen = bool(u) and not github_guard.hosted(u) and any(u == e or e.startswith(u) or u.startswith(e)
                                                               for e in evidence)
        (ok if seen else dropped).append(c)
    return ok, [c.url for c in dropped]


async def escalate(t: dict) -> int:
    p = await _asset_payload(t["scope"])
    if p is None:
        return 0
    ens_btc, _, _ = await ensemble.compute(["BTC"], exclude=SELF)
    market = await strategist._market(ens_btc)          # F&G, BTC ensemble, macro block
    prompt = SYSTEM + "\n\nTRIGGER:\n" + orjson.dumps({"kind": t["kind"], **t["detail"]}).decode() \
        + "\n\nASSET:\n" + orjson.dumps(p).decode() + "\n\nMARKET:\n" + orjson.dumps(market).decode()
    v, run_id, evidence = await openrouter.research(AGENT, Verdict, prompt, model=MODEL, daily_budget_usd=DAILY_USD,
                                                    max_calls_per_day=MAX_CALLS_PER_DAY)
    cites, dropped = _grounded(v.citations, evidence)
    allowed = strategist._allowed(p, "") | ({"web"} if cites else set())
    now = datetime.now(UTC)
    rows, reasons = [], []
    for c in v.calls:
        if c.tf not in p["timeframes"]:
            reasons.append(f"{c.tf}: no data")
            continue
        if not c.relies_on or not set(c.relies_on) <= allowed | strategist._allowed(p, c.tf):
            reasons.append(f"{c.tf}: relies_on {c.relies_on} not all in inputs")
            continue
        as_of, ref = live_anchor(c.tf, now, p["price"])
        rows.append((t["scope"], c.tf, c.bias, round(c.confidence, 3), ref, None,
                     orjson.dumps({"thesis": c.rationale, "relies_on": c.relies_on, "trigger": t["key"]}).decode(),
                     as_of + TFS[c.tf][2] * (1 + HORIZON_BARS), AGENT, as_of, c.bias / 2, run_id))
    await db.upsert("signals", ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers",
                                "valid_until", "source", "as_of", "score", "agent_run_id"],
                    rows, ["scope", "tf", "source", "as_of"])
    idea_id = None
    if v.idea:
        calls = {c.tf: strategist.TfCall(tf=c.tf, bias=c.bias, confidence=c.confidence, rationale=c.rationale,
                                         relies_on=c.relies_on) for c in v.calls}
        why, rr = strategist.check_idea(v.idea, p, calls)
        if why:
            reasons.append(f"idea: {why}")
        else:
            as_of, _ = live_anchor(v.idea.tf, now, p["price"])
            idea_id = (await ledger.append([{
                "source_kind": "oracle", "source_id": AGENT, "scope": t["scope"], "market": "perp",
                "direction": v.idea.direction, "tf": v.idea.tf, "entry_type": "market", "ref_price": p["price"],
                "stop": v.idea.stop, "targets": v.idea.targets,
                "horizon_until": as_of + TFS[v.idea.tf][2] * (1 + HORIZON_BARS), "reward_risk": rr,
                "confidence": calls[v.idea.tf].confidence, "status": "proposed", "thesis": v.idea.thesis,
                "evidence": {"trigger": t["key"], "relies_on": v.idea.relies_on,
                             "citations": [c.model_dump() for c in cites]},
                "critique": None, "parent_id": None, "agent_run_id": run_id}]))[0]
    pool = await db.pool()
    await pool.execute(
        "INSERT INTO escalations (trigger, context, verdict, agent_run_id) VALUES ($1, $2, $3, $4)",
        t["kind"], orjson.dumps({"key": t["key"], "scope": t["scope"], **t["detail"]}).decode(),
        orjson.dumps(v.model_dump() | {"citations": [c.model_dump() for c in cites], "dropped_citations": dropped,
                                       "dropped": reasons, "idea_id": idea_id,
                                       "evidence_urls": len(evidence)}).decode(), run_id)
    return len(rows)


async def watch() -> int:
    """Every 10 min: escalate up to PER_RUN fresh triggers within the daily budget."""
    pool = await db.pool()
    used = await pool.fetchval(
        "SELECT count(*) FROM escalations WHERE created_at >= date_trunc('day', now()) AND trigger <> ALL($1)",
        ["daily_thesis", "weekly_review"])
    todo = (await _fresh(await triggers()))[:max(0, min(PER_RUN, TRIGGERED_PER_DAY - used))]
    done = 0
    for t in todo:
        try:
            done += await escalate(t)
        except (openrouter.CallLimitExceeded, openrouter.BudgetExceeded, RuntimeError) as e:
            log.warning("oracle %s: %s", t["key"], e)
            break
    return done


# ─── daily thesis & weekly review ───────────────────────────────────────────
DAILY = """You are the Oracle writing the desk's daily market thesis for the next 1-7 days. Inputs: the
Strategist's calls and regimes for the largest assets, the macro block, and yesterday's idea scorecard.
Research what matters today on the web (macro calendar, flows, crypto-specific catalysts) and cite
every web fact with the exact URL you read. Give BTC and ETH calls on 1d and 1w (relies_on from
"ensemble", "market", "news", "web"). Markdown body <= 450 words: the thesis, what would invalidate it,
the two or three things to watch today with times (UTC)."""

WEEKLY = """You are the Oracle reviewing the desk's week. Inputs: hit rates by source and timeframe vs
the naive baseline, the idea backtest (proposed vs vetoed, vs always-long/random), and the critic's
record. Say plainly what is working, what is not, and what is too early to tell (small n). Then give
concrete, testable recommendations (a prompt change, a weight prior, a rule), each with how we would
know it worked. No web research needed. Markdown body <= 500 words."""


async def _t1_matrix() -> dict:
    pool = await db.pool()
    syms = [s["symbol"] for s in (await universe())[:10]]
    out: dict = {}
    for r in await pool.fetch(
        """SELECT DISTINCT ON (scope, tf) scope, tf, bias, regime, drivers->>'thesis' AS thesis FROM signals
           WHERE source = 'strategist' AND scope = ANY($1::text[]) AND valid_until > now()
           ORDER BY scope, tf, as_of DESC""", syms):
        out.setdefault(r["scope"], {})[r["tf"]] = {"bias": r["bias"], "regime": r["regime"]}
    return out


async def daily_thesis() -> int:
    from albatross.backtest import scorecard as idea_scorecard
    ctx = {"strategist_tier1": await _t1_matrix(), "macro": await macro.snapshot(),
           "ideas": await idea_scorecard.rows(f"{idea_scorecard.NOT_DEX} AND c.logged_at > now() - interval '7 days'",
                                              by_tf=False)}
    prompt = DAILY + "\n\nINPUTS:\n" + orjson.dumps(ctx, default=str).decode()
    th, run_id, evidence = await openrouter.research(AGENT, Thesis, prompt, model=MODEL, daily_budget_usd=DAILY_USD,
                                                     max_calls_per_day=MAX_CALLS_PER_DAY, searches=5)
    cites, dropped = _grounded(th.citations, evidence)
    prices = await latest_prices(["BTC", "ETH"])
    now = datetime.now(UTC)
    rows = []
    allowed = {"ensemble", "market", "news"} | ({"web"} if cites else set())
    for c in th.calls:
        if c.tf in ("1d", "1w") and c.relies_on and set(c.relies_on) <= allowed and c.symbol in prices:
            as_of, ref = live_anchor(c.tf, now, prices[c.symbol])
            rows.append((c.symbol, c.tf, c.bias, round(c.confidence, 3), ref, None,
                         orjson.dumps({"thesis": c.rationale, "relies_on": c.relies_on,
                                       "trigger": "daily_thesis"}).decode(),
                         as_of + TFS[c.tf][2] * (1 + HORIZON_BARS), AGENT, as_of, c.bias / 2, run_id))
    await db.upsert("signals", ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers",
                                "valid_until", "source", "as_of", "score", "agent_run_id"],
                    rows, ["scope", "tf", "source", "as_of"])
    pool = await db.pool()
    await pool.execute("INSERT INTO reports (kind, title, body_md) VALUES ('oracle_daily', $1, $2)",
                       th.title, th.body_md + _sources_md(cites))
    await pool.execute("INSERT INTO escalations (trigger, context, verdict, agent_run_id) VALUES ($1, $2, $3, $4)",
                       "daily_thesis", orjson.dumps({"key": f"daily:{now:%Y-%m-%d}"}).decode(),
                       orjson.dumps({"title": th.title, "dropped_citations": dropped, "calls": len(rows)}).decode(),
                       run_id)
    return len(rows)


async def weekly_review() -> int:
    from albatross.backtest import scorecard as idea_scorecard
    from albatross.grading import scorecard as sig_scorecard
    ideas = await idea_scorecard.rows(f"{idea_scorecard.NOT_DEX} AND c.logged_at > now() - interval '7 days'",
                                      by_tf=False)
    ctx = {"signals": await sig_scorecard.by_source_tf(days=7), "ideas": ideas,
           "critic": idea_scorecard.critic(ideas)}
    rv, run_id = await openrouter.structured(AGENT, Review, WEEKLY, "INPUTS:\n" + orjson.dumps(ctx, default=str).decode(),
                                             model=MODEL, daily_budget_usd=DAILY_USD, temperature=None,
                                             reasoning_effort="medium", max_tokens=16000, deadline_s=300)
    body = rv.body_md + "\n\n## Recommendations\n" + "\n".join(f"- {r}" for r in rv.recommendations)
    pool = await db.pool()
    await pool.execute("INSERT INTO reports (kind, title, body_md) VALUES ('oracle_weekly', $1, $2)", rv.title, body)
    await pool.execute("INSERT INTO escalations (trigger, context, verdict, agent_run_id) VALUES ($1, $2, $3, $4)",
                       "weekly_review", orjson.dumps({"key": f"weekly:{datetime.now(UTC):%G-W%V}"}).decode(),
                       orjson.dumps(rv.model_dump()).decode(), run_id)
    return len(rv.recommendations)


def _sources_md(cites: list[Citation]) -> str:
    if not cites:
        return ""
    return "\n\n**Sources**\n" + "\n".join(f"- [{c.claim}]({c.url})" for c in cites)

