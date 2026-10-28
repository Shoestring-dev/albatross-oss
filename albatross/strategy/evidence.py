"""What our own data says works, in one place, for every LLM layer that reads it (analysts, Strategist,
reports) and for /api/intel. The static findings come from docs/RESEARCH-EDGE.md (2026-09-30); the live
parts (each source's graded edge, today's factor rank) are read from the database.

Prompts must not carry "measured" claims that aren't in here: an earlier prompt told the HTF analyst that
trend-following had a real edge on 1w, when the 5-month replay showed it worse than naive.
"""

import logging

from albatross import db

log = logging.getLogger(__name__)

# Static research findings, worded for a model reading a prompt.
FINDINGS = """Measured on our own data (2022-2026 daily candles, 5-month walk-forward replay of the quant model):
- Market DIRECTION was not predictable from anything we track: the quant chart score, momentum, reversal,
  moving averages, Fear & Greed, ETF flows, stablecoin supply, the dollar, yields and VIX all showed no
  reliable edge over a naive always-long/always-short caller on any timeframe (the quant replay was worse
  than naive on 5m, 15m, 4h and 1w, and a coin flip on 1h and 1d).
- RELATIVE strength WAS predictable: across the liquid coins, those that are calm (low 30-day volatility),
  near their 20-day high and trading unusual volume beat the rest. Ranked on those three, the strongest
  fifth beat the weakest fifth by about 1.9% a week after trading costs over 2025-26 (rank IC 0.15). This
  is the factor rank: it says which coins should do better than others, not whether the market rises.
- The BTC MVRV Z-score (market value against realized value) matters only at its EXTREMES and over MONTHS:
  since 2011, a Z below 0 was followed by a higher BTC price a year later 99% of the time, and a Z of 6 or
  more by a lower one 74% of the time (about four cycles, so history, not proof); between 0 and 3.5 it said
  little. Never use it for calls shorter than weeks."""

LTF_EVIDENCE = """Measured on our data: no short-timeframe chart pattern (trend, momentum, mean reversion)
has shown an edge; the quant chart score was worse than a naive caller on 5m and 15m. So use bias 0 as the
default and give a directional call only when several inputs clearly agree AND the move is not stretched
(large dist_ema20_pct relative to atr_pct, bb_pctb outside 0..1, RSI extremes) or when a failed break of
hi_20/lo_20 is clear. Confidence above 0.6 should be rare."""

HTF_EVIDENCE = """Measured on our data (5-month walk-forward replay): trend-following on these features was
worse than a naive caller on 4h and 1w and a coin flip on 1d. So use bias 0 as the default; a directional
call needs structure, EMA stack and momentum to agree without being stretched into the 20-bar extremes.
What did hold up is relative: coins near their 20-day high (use hi_20/lo_20 and close), calm (low atr_pct)
and on rising volume (vol_z) tended to beat other coins over the next week."""


async def factor_ranks() -> dict[str, dict]:
    """symbol -> the latest factor rank row with plain-word reasons, {} before the table or a ranking exists."""
    pool = await db.pool()
    if not await pool.fetchval("SELECT to_regclass('public.factor_ranks') IS NOT NULL"):
        return {}
    rows = [dict(r) for r in await pool.fetch(
        "SELECT * FROM factor_ranks WHERE day = (SELECT max(day) FROM factor_ranks)")]
    if not rows:
        return {}
    from albatross.strategy import factor_rank
    vols = sorted(r["vol_30d"] for r in rows if r["vol_30d"] is not None)
    med = vols[len(vols) // 2] if vols else None
    return {r["symbol"]: {**r, "reasons": factor_rank.reasons(r, med)} for r in rows}


def rank_block(r: dict | None) -> dict | None:
    """The per-asset block the Strategist sees."""
    if not r:
        return None
    word = {5: "strongest fifth", 4: "second fifth", 3: "middle fifth", 2: "fourth fifth", 1: "weakest fifth"}
    return {"rank": f"{r['rank']} of {r['n']}", "quintile": r["quintile"], "group": word[r["quintile"]],
            "reasons": r["reasons"], "as_of_day": str(r["day"])}


async def source_edges() -> list[dict]:
    """Each signal source's graded edge in words (the /api/intel 'edge' list)."""
    from albatross.api import intel
    try:
        return await intel._edge()
    except Exception as e:  # noqa: BLE001 - evidence is context; never block a run on it
        log.warning("source edges unavailable: %s", type(e).__name__)
        return []


def edges_for_prompt(edges: list[dict]) -> list[dict]:
    return [{"source": e["source"], "direction": e["words"], "timeframe": e["headline_tf"],
             "coin_picking": (e.get("picking") or {}).get("words", "too early to tell")} for e in edges]


async def for_reports() -> dict:
    """The evidence block the report writer leads with."""
    ranks = await factor_ranks()
    rows = sorted(ranks.values(), key=lambda r: r["rank"])
    pick = lambda rs: [{"symbol": r["symbol"], "rank": f"{r['rank']} of {r['n']}", "why": r["reasons"]} for r in rs]
    from albatross.events import context as event_context
    ranks_syms = [r["symbol"] for r in rows]
    per_asset, macro_events = await event_context.upcoming(ranks_syms)
    return {"findings": FINDINGS, "source_edges": edges_for_prompt(await source_edges()),
            "scheduled": {"macro": macro_events, "coins": {k: v for k, v in per_asset.items() if any(e["importance"] >= 3 for e in v)}},
            "factor_rank": {"day": str(rows[0]["day"]), "strongest": pick(rows[:5]), "weakest": pick(rows[-5:][::-1])} if rows else None}
