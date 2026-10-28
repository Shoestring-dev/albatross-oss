"""The strategy researcher (daily): new strategies for the lab to test (albatross/lab/run.py), on GPT-6.1 Sol at high
effort on the ChatGPT plan.

It reads the lab's outcomes only: the counts, the bar, the baselines, the best strategies so far with their numbers,
and the latest rejections with the reason. Never another agent's reasoning, and never its own earlier wording, only
how its earlier proposals scored. Each proposal is a config in the lab's grammar and one sentence on the idea behind
it. Code checks every config (a broken one is dropped and counted) and the lab tests it like any other, against the
same rising bar. It decides nothing: what survives is the lab's call, in code.
"""

import logging
from datetime import UTC, datetime
from typing import Literal

import orjson
from pydantic import BaseModel, Field

from albatross import db
from albatross.lab import factor, meme
from albatross.lab import run as lab
from albatross.llm import openrouter

log = logging.getLogger(__name__)

AGENT = "strategy_researcher"
DAILY_USD = 1.0
PER_FAMILY = 15

SYSTEM = """You design trading strategies for Albatross's strategy lab. The lab tests every strategy in code on the
same data and decides what survives; you propose what to test next. You never see money or place trades.

Two families:
- meme: which memecoin calls to take and how to exit. A call is a Telegram group's call (caller), a FOMO-app trader's
  buy (fomo) or the bot's entry (bot). Every trade is $100 after every cost, one position per coin, held
  24 hours (hold_24h) or sold at 2x if it gets there first (take_profit_2x). Filters read what was known when the call
  came in; a call missing a field fails that filter.
- factor: rank the liquid Hyperliquid coins against each other on weighted signals, buy the top slice and short the
  bottom slice (long_short), buy the top slice against the whole market (long_only) or against BTC and ETH
  (long_hedged), hold each day's picks for hold_days, after trading costs.

How the lab judges: the older data (discovery) needs a positive mean and a t-statistic above the bar; the bar rises
with how many strategies of the family have been tried, so a near-copy of a strategy that failed adds nothing. The
recent data (holdout), which you never see, must also be positive and at least as good as the baseline. Survivors go
on paper and get judged on new data.

Propose strategies that test a distinct idea each, explained in one plain sentence. Learn from the reasons strategies
were rejected: too few trades means the filters were too tight; a negative mean means the idea loses money. Prefer
ideas a trader could explain over thresholds tuned to the last decimal. Don't resubmit a config already listed. Use
only the fields, operators and values in the grammar."""


class RawFilter(BaseModel):
    field: str = Field(description="A field from the grammar, e.g. score or check:mint.")
    op: Literal[">=", "<=", "==", "!="]
    value: float | bool | str


class MemeIdea(BaseModel):
    idea: str = Field(description="One plain sentence: what this tests and why it might work.")
    kinds: list[Literal["caller", "fomo", "bot"]]
    filters: list[RawFilter] = Field(description="At most 6.")
    exit: Literal["hold_24h", "take_profit_2x"]


class FactorIdea(BaseModel):
    idea: str = Field(description="One plain sentence: what this tests and why it might work.")
    calm: float = Field(description="Weight from -1 to 1; 0 leaves the signal out.")
    near_high: float
    volume: float
    momentum: float
    vol_days: Literal[14, 20, 30, 60]
    high_days: Literal[10, 20, 40]
    volume_days: Literal[14, 30, 60]
    mom_days: Literal[5, 14, 30, 60]
    hold_days: Literal[3, 7, 14]
    slice: Literal[3, 5, 10]
    side: Literal["long_short", "long_only", "long_hedged"]


class Proposals(BaseModel):
    meme: list[MemeIdea] = Field(description=f"About {PER_FAMILY}.")
    factor: list[FactorIdea] = Field(description=f"About {PER_FAMILY}.")


MEME_FIELDS = {
    "score": "the token's dossier score, 0-100, at its last version before the call",
    "n_red_flags": "how many red flags its dossier had",
    "mcap": "market cap in dollars",
    "age_h": "the token's age in hours",
    "n_groups": "how many Telegram groups had called it so far",
    "n_calls": "how many calls it had so far",
    "calls_24h": "its calls in the last 24 hours",
    "smart_holders": "tracked smart wallets holding it",
    "hour_utc": "the hour of the call, UTC",
    "caller_prior_n": "how many of this caller's earlier calls were already graded (callers only)",
    "caller_prior_mean": "the caller's average net % a trade on those (callers only)",
    "caller_prior_win": "the share of those that made money, 0-1 (callers only)",
    "dex_paid": "true when its DexScreener listing was paid for",
}
FACTOR_SIGNALS = {
    "calm": "low day-to-day volatility over vol_days (a positive weight favours the calm coins)",
    "near_high": "where the price sits in its high_days range, 0 at the low and 1 at the high",
    "volume": "today's dollar volume against its volume_days average",
    "momentum": "the return over mom_days",
}


def grammar() -> dict:
    return {"meme": {"fields": {k: {"means": v, **({"range": list(meme.NUMERIC[k])} if k in meme.NUMERIC else {"values": [True, False], "ops": ["==", "!="]})}
                                for k, v in MEME_FIELDS.items()},
                     "checks": {"fields": list(meme.CHECKS), "values": list(meme.STATUSES), "ops": ["==", "!="],
                                "means": "the status of one line of the token's dossier checklist (mint authority, "
                                         "liquidity lock, dev wallet, insiders, top-10 holders and so on)"},
                     "kinds": ["caller", "fomo", "bot"], "exits": list(meme.EXITS), "max_filters": 6},
            "factor": {"signals": FACTOR_SIGNALS, "weights": "each -1 to 1, at least one not 0",
                       "options": {"vol_days": [14, 20, 30, 60], "high_days": [10, 20, 40], "volume_days": [14, 30, 60],
                                   "mom_days": [5, 14, 30, 60], "hold_days": [3, 7, 14], "slice": [3, 5, 10],
                                   "side": ["long_short", "long_only", "long_hedged"]}}}


def _brief(r: dict, with_reason: bool = False) -> dict:
    """What it may see of a strategy: its config and the older data's numbers. Never the recent data, its status or a
    reason that comes from the recent data (a pass or fail there is one bit of the holdout each time)."""
    d = {"name": r["name"], "config": orjson.loads(r["config"]) if isinstance(r["config"], str) else r["config"],
         "origin": r["origin"]}
    v = r.get("discovery")
    v = orjson.loads(v) if isinstance(v, str) else v
    if v:
        d["discovery"] = {x: v.get(x) for x in ("n", "mean", "t")}
    if with_reason:
        d["reason"] = r["reason"] or ""
    return d


async def digest() -> dict:
    """What it may use: the lab's outcomes per family."""
    pool = await db.pool()
    rep = await pool.fetchval("SELECT report FROM paper_reports WHERE key = 'lab'")
    rep = orjson.loads(rep) if isinstance(rep, str) else (rep or {})
    fams = {}
    for f in ("meme", "factor"):
        counts = {"tested": await pool.fetchval(
            "SELECT count(*) FROM lab_strategies WHERE family = $1 AND status <> 'queued'", f)}
        best = await pool.fetch(
            """SELECT * FROM lab_strategies WHERE family = $1 AND score IS NOT NULL ORDER BY score DESC LIMIT 15""", f)
        rejected = await pool.fetch(                     # failures on the older data only
            """SELECT * FROM lab_strategies WHERE family = $1 AND status = 'rejected' AND reason NOT LIKE '%recent data%'
                 AND reason NOT LIKE 'passed the older data%' ORDER BY tested_at DESC LIMIT 25""", f)
        mine = {"tested": await pool.fetchval(
            """SELECT count(*) FROM lab_strategies WHERE family = $1 AND origin = 'researcher' AND status <> 'queued'""", f)}
        fam = (rep.get("families") or {}).get(f) or {}
        fams[f] = {"counts": counts, "bar_now": fam.get("bar"), "your_earlier_proposals": mine,
                   "discovery_window": fam.get("discovery"),
                   "best_so_far": [_brief(dict(r)) for r in best],
                   "latest_rejections": [_brief(dict(r), True) for r in rejected]}
    return {"grammar": grammar(), "families": fams}


def configs(p: Proposals) -> tuple[list[tuple[str, object, str]], int]:
    """([(family, config, idea)], how many were broken). Code checks every config; the model's schema can't."""
    out, bad = [], 0
    for m in p.meme[:PER_FAMILY + 5]:
        try:
            out.append(("meme", meme.MemeConfig(kinds=m.kinds, filters=[f.model_dump() for f in m.filters], exit=m.exit),
                        m.idea.strip()[:300]))
        except ValueError:
            bad += 1
    for x in p.factor[:PER_FAMILY + 5]:
        try:
            w = {k: getattr(x, k) for k in factor.PARTS if getattr(x, k)}
            out.append(("factor", factor.FactorConfig(weights=w, **x.model_dump(include={
                "vol_days", "high_days", "volume_days", "mom_days", "hold_days", "slice", "side"})), x.idea.strip()[:300]))
        except ValueError:
            bad += 1
    return out, bad


async def run() -> int:
    """Job strategy_researcher (daily) -> how many new strategies it queued."""
    d = await digest()
    p, run_id = await openrouter.structured(AGENT, Proposals, SYSTEM, "INPUTS:\n" + orjson.dumps(d, default=str).decode(),
                                            model=openrouter.SOL, daily_budget_usd=DAILY_USD, temperature=None,
                                            reasoning_effort="high", max_tokens=32000, deadline_s=420)
    found, bad = configs(p)
    new = 0
    for family, cfg, idea in found:
        new += await lab.queue(family, cfg, "researcher", idea=idea)
    log.info("%s: %d proposed, %d broken, %d new, %d already tried (run %s, %s)", AGENT, len(found) + bad, bad, new,
             len(found) - new, run_id, datetime.now(UTC).isoformat(timespec="minutes"))
    return new
