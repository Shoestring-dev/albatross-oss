"""News Scout: classifies fresh headlines (impact, direction, horizon, assets) in cheap batches.

High-impact items (>= 4) are what later trigger immediate re-runs of the analysts.
"""

import logging
from typing import Literal

import orjson
from pydantic import BaseModel, Field

from albatross import db
from albatross.llm import openrouter

log = logging.getLogger(__name__)

AGENT = "news_scout"
BATCH = 25
DAILY_BUDGET_USD = 2.0

SYSTEM = """You are the News Scout on a crypto market-intelligence desk. For each headline, judge how it
is likely to move crypto prices. Be calibrated and conservative: most news is noise.

impact (1-5):
  1 = noise / opinion / recap          2 = minor, single small-cap
  3 = notable for one asset or sector  4 = market-moving for a major asset or a whole sector
  5 = systemic (major hack/insolvency, ETF approval/denial, sweeping regulation, macro shock)
sentiment (-2..+2): expected price direction for the affected assets (0 = neutral/unclear).
horizon: how long the effect plausibly lasts: minutes | hours | days | weeks.
assets: canonical tickers affected (BTC, ETH, SOL...). Use ["MARKET"] for market-wide items. Keep the
  pre-tagged assets when correct, drop wrong ones, add missing ones.
kind: regulation | hack_exploit | etf_institutional | macro | listing_delisting | unlock_supply |
  partnership_adoption | onchain_flow | legal | tech_upgrade | market_commentary | other
Never invent facts beyond the headline and summary."""


class Item(BaseModel):
    id: int
    impact: int = Field(ge=1, le=5)
    sentiment: int = Field(ge=-2, le=2)
    horizon: Literal["minutes", "hours", "days", "weeks"]
    assets: list[str]
    kind: str
    reason: str = Field(description="one short sentence")


class Batch(BaseModel):
    items: list[Item]


async def run(max_batches: int = 8) -> int:
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT id, source, title, summary, assets FROM news_items
           WHERE classification IS NULL AND published_at > now() - interval '2 days'
           ORDER BY published_at DESC LIMIT $1""",
        BATCH * max_batches,
    )
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        payload = [
            {"id": r["id"], "source": r["source"], "title": r["title"],
             "summary": (r["summary"] or "")[:400], "pre_tagged": r["assets"]}
            for r in chunk
        ]
        try:
            result, run_id = await openrouter.structured(
                AGENT, Batch, SYSTEM, orjson.dumps(payload).decode(), model=openrouter.LUNA,
                daily_budget_usd=DAILY_BUDGET_USD, queue_s=5,
            )
        except openrouter.BudgetExceeded as e:
            log.log(logging.DEBUG if isinstance(e, openrouter.NotConfigured) else logging.WARNING, "%s", e)
            break
        valid_ids = {r["id"] for r in chunk}
        updates = [
            (it.impact, it.sentiment, sorted({a.upper() for a in it.assets}),
             orjson.dumps({"horizon": it.horizon, "kind": it.kind, "reason": it.reason,
                           "agent_run_id": run_id}).decode(), it.id)
            for it in result.items if it.id in valid_ids
        ]
        await pool.executemany(
            "UPDATE news_items SET impact = $1, sentiment = $2, assets = $3, classification = $4 WHERE id = $5",
            updates,
        )
        done += len(updates)
    return done
