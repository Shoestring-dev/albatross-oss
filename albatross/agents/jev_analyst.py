"""Jev analyst: TypeSafe's Jev decision model reads each coin's chart features and answers, per timeframe,
where price is likely to be after the next 6 candles, as a probability over five outcomes.

It is graded like every other source (source 'jev_analyst', directional and coin-picking), and stays OUT of
the ensemble until its record earns a place (docs/MODELS.md): Jev's first answers were very confident
(0.97 on one bearish-looking snapshot), and confidence isn't evidence here until the grades say so.

Deterministic mapping: bias = the most likely outcome (-2..+2), score = the probability-weighted outcome / 2
(-1..1), confidence = that outcome's probability. Anchored at the candle close like the chart analysts.
"""

import asyncio
import logging

import orjson

from albatross import db
from albatross.agents.analysts import candle_anchors, chart_payload, latest_features, universe
from albatross.features.engine import HORIZON_BARS, TFS
from albatross.llm import typesafe
from albatross.llm.openrouter import BudgetExceeded

log = logging.getLogger(__name__)

AGENT = "jev_analyst"
CALL_TFS = ["1h", "4h", "1d"]
BUDGET_USD = 0.50                       # a whole day is ~$0.01 at Jev's price; this only stops a runaway loop
CONCURRENCY = 8                         # Jev allows 40 requests/s
OUTCOMES = {"down_strong": -2, "down": -1, "flat": 0, "up": 1, "up_strong": 2}
HORIZON = {"1h": "6 hours", "4h": "24 hours", "1d": "6 days"}
CRITERIA = {
    "down_strong": "falls clearly, by more than about one and a half times the typical candle range (atr_pct)",
    "down": "ends lower, by less than that",
    "flat": "ends within about half a typical candle range of the current close",
    "up": "ends higher, by less than one and a half typical ranges",
    "up_strong": "rises clearly, by more than about one and a half times the typical candle range (atr_pct)",
}


def questions(tfs: list[str]) -> dict[str, dict]:
    return {f"next_{tf}": typesafe.choice(
        f"From the {tf} indicators in the state (ret_N = % change over N candles, dist_emaN_pct = % from that EMA, "
        f"atr_pct = typical candle range as % of price, bb_pctb = place in the Bollinger band, vol_z = volume "
        f"z-score, structure = swing structure), where will price most likely be after the next 6 {tf} candles "
        f"({HORIZON[tf]})?", CRITERIA) for tf in tfs}


def to_call(answer: dict) -> tuple[int, float, float, dict] | None:
    """A choice answer -> (bias, score -1..1, confidence, probabilities), None if it isn't usable."""
    probs = {k: float(v) for k, v in (answer.get("probabilities") or {}).items() if k in OUTCOMES}
    if not probs or answer.get("choice") not in OUTCOMES:
        return None
    total = sum(probs.values()) or 1.0
    expected = sum(OUTCOMES[k] * p for k, p in probs.items()) / total
    return OUTCOMES[answer["choice"]], round(expected / 2, 4), round(max(probs.values()) / total, 3), probs


def _thesis(probs: dict) -> str:
    up = probs.get("up", 0) + probs.get("up_strong", 0)
    down = probs.get("down", 0) + probs.get("down_strong", 0)
    return f"Jev: {round(up * 100)}% higher, {round(down * 100)}% lower, {round(probs.get('flat', 0) * 100)}% flat"


async def run() -> int:
    syms = await universe()
    feats = await latest_features([s["symbol"] for s in syms], CALL_TFS)
    anchors = candle_anchors(feats)
    payloads = [p for s in syms if (p := chart_payload(s, feats, CALL_TFS))]
    sem, rows, failed = asyncio.Semaphore(CONCURRENCY), [], [0]

    async def one(p: dict) -> None:
        tfs = [tf for tf in CALL_TFS if tf in p["timeframes"]]
        async with sem:
            try:
                answers, run_id = await typesafe.decide(AGENT, p, questions(tfs), daily_budget_usd=BUDGET_USD)
            except (BudgetExceeded, RuntimeError) as e:
                failed[0] += 1
                log.warning("%s %s: %s", AGENT, p["symbol"], e)
                return
        for tf in tfs:
            call = to_call(answers.get(f"next_{tf}") or {})
            if not call or (p["symbol"], tf) not in anchors:
                continue
            bias, score, conf, probs = call
            as_of, ref = anchors[(p["symbol"], tf)]
            rows.append((p["symbol"], tf, bias, conf, ref, None,
                         orjson.dumps({"thesis": _thesis(probs), "probabilities": probs}).decode(),
                         as_of + TFS[tf][2] * (1 + HORIZON_BARS), AGENT, as_of, score, run_id))

    await asyncio.gather(*(one(p) for p in payloads))
    await db.upsert(
        "signals", ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers", "valid_until",
                    "source", "as_of", "score", "agent_run_id"], rows, ["scope", "tf", "source", "as_of"])
    log.info("%s: %d calls on %d coins (%d failed)", AGENT, len(rows), len(payloads), failed[0])
    return len(rows)
