"""Deterministic ensemble: one weighted score per asset and timeframe from every live source.

Weights start from a prior per source and move with that source's *measured* edge on that
timeframe: hit rate minus the better of always-long / always-short over the same windows (last 30
days, live calls only). The edge is shrunk toward zero until a source has 300 graded calls, then
    weight = prior * clamp(1 + 10 * edge, 0.1, 2.0)
so a source 5 points better than naive counts 1.5x, and an anti-predictive one fades to 0.1x rather
than being inverted (inverting a noisy loser is how you overfit). A source the culling system has put on
probation or below (albatross/agents/cull.py) counts 0 until a weekly review shows it earns its place. Written to `signals` as
source='ensemble', anchored at call time (live), and graded like everything else.
"""

import logging
from datetime import UTC, datetime

import orjson

from albatross import controls, db
from albatross.agents.analysts import latest_prices, live_anchor, universe
from albatross.features.engine import HORIZON_BARS, TFS

log = logging.getLogger(__name__)

ALL_TFS = ["5m", "15m", "1h", "4h", "1d", "1w"]
SOURCES = {
    "quant": (0.5, ALL_TFS),
    "ltf_analyst": (1.0, ["5m", "15m", "1h"]),
    "htf_analyst": (1.0, ["4h", "1d", "1w"]),
    "flow_analyst": (1.0, ["1h", "4h"]),
    "social_pulse": (0.5, ["1h", "4h"]),
    "macro_analyst": (0.75, ["1d", "1w"]),          # BTC and ETH only
    # The Oracle is not a source: it reads the ensemble and the Strategist, so its calls in here came back to it as
    # the desk's view (a recursive loop, albatross/agents/graph.py). Its calls are still graded on their own.
}
FULL_TRUST_N = 300
LOOKBACK_DAYS = 30
RUNG_WORDS = {"probation": "is benched", "paused": "is paused", "retired": "is retired"}     # the site's words for the rungs


def uncovered(rungs: dict[str, str], names: dict[str, str] | None = None) -> dict[str, str]:
    """Timeframes every source of the ensemble has been culled from (agents/cull.py), so neither the ensemble nor the
    Strategist calls them -> {tf: why, in words}. Since 2026-10-02 that is 5m and 15m (the quant model on probation,
    the LTF analyst paused)."""
    out = {}
    for tf in ALL_TFS:
        srcs = [s for s, (_, tfs) in SOURCES.items() if tf in tfs]
        if srcs and all(rungs.get(s) in controls.OFF_ENSEMBLE for s in srcs):
            out[tf] = " and ".join(f"the {_lower((names or {}).get(s, s.replace('_', ' ')))} {RUNG_WORDS[rungs[s]]}"
                                   for s in srcs)
    return out


def _lower(name: str) -> str:
    """"Quant model" -> "quant model"; an acronym ("LTF analyst") keeps its capitals."""
    return name[:1].lower() + name[1:] if name[1:2].islower() else name


async def source_weights() -> dict[tuple[str, str], dict]:
    """(source, tf) -> {weight, n, hit, naive, edge}."""
    pool = await db.pool()
    stats = {(r["source"], r["tf"]): r for r in await pool.fetch(
        """SELECT s.source, s.tf, count(*) AS n, avg(o.hit::int) AS hit,
                  avg(((o.return_pct > 0) = (s.bias > 0))::int) AS long_rate
           FROM signals s JOIN outcomes o ON o.signal_id = s.id
           WHERE o.hit IS NOT NULL AND s.bias <> 0 AND s.source = ANY($1::text[])
             AND s.scope NOT LIKE 'sector:%' AND s.as_of > now() - make_interval(days => $2)
           GROUP BY 1, 2""", list(SOURCES), LOOKBACK_DAYS)}
    out = {}
    for src, (prior, tfs) in SOURCES.items():
        for tf in tfs:
            r = stats.get((src, tf))
            if r is None:
                out[(src, tf)] = {"weight": prior, "n": 0, "hit": None, "naive": None, "edge": None}
                continue
            hit, long_rate = float(r["hit"]), float(r["long_rate"])   # avg() of ints comes back as Decimal
            naive = max(long_rate, 1 - long_rate)
            edge = hit - naive
            trust = min(1.0, r["n"] / FULL_TRUST_N)
            w = prior * min(2.0, max(0.1, 1 + 10 * edge * trust))
            out[(src, tf)] = {"weight": round(w, 3), "n": r["n"], "hit": round(hit, 4),
                              "naive": round(naive, 4), "edge": round(edge, 4)}
    return culled(out, await controls.rungs())


def culled(weights: dict[tuple[str, str], dict], rungs: dict[str, str]) -> dict[tuple[str, str], dict]:
    """A source the culling system has on probation or below (albatross/agents/cull.py) counts for nothing: weight 0,
    not the 0.1x floor a weak record earns, until a review shows it earns its place again."""
    for (src, _), w in weights.items():
        if rungs.get(src) in controls.OFF_ENSEMBLE:
            w["weight"], w["culled"] = 0.0, rungs[src]
    return weights


async def latest_views(symbols: list[str], exclude: frozenset[str] = frozenset()) -> dict[tuple[str, str], list[dict]]:
    """(symbol, tf) -> current (unexpired) view from each source, less `exclude`."""
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT DISTINCT ON (scope, tf, source) scope, tf, source, bias, confidence, score, invalidation,
                  drivers->>'thesis' AS thesis, as_of, valid_until
           FROM signals
           WHERE scope = ANY($1::text[]) AND source = ANY($2::text[]) AND valid_until > now()
             AND as_of > now() - interval '60 days'
           ORDER BY scope, tf, source, as_of DESC""", symbols, [s for s in SOURCES if s not in exclude])
    out: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r["tf"] in SOURCES[r["source"]][1]:
            out.setdefault((r["scope"], r["tf"]), []).append(dict(r))
    return out


def combine(views: list[dict], weights: dict) -> dict | None:
    parts = []
    for v in views:
        w = weights[(v["source"], v["tf"])]["weight"]
        score = v["score"] if v["score"] is not None else v["bias"] / 2
        parts.append((v["source"], w, float(score)))
    total = sum(w for _, w, _ in parts)
    if not parts or total <= 0:
        return None
    s = sum(w * sc for _, w, sc in parts) / total
    same = sum(w for _, w, sc in parts if sc * s > 0) / total if s else 0.0
    return {"score": round(s, 4), "bias": max(-2, min(2, round(s * 2.5))), "agreement": round(same, 3),
            "parts": {src: {"w": w, "score": round(sc, 3)} for src, w, sc in parts}}


async def compute(symbols: list[str] | None = None, exclude: frozenset[str] = frozenset()) -> tuple[dict, dict, dict]:
    """-> (ensemble per (symbol, tf), views per (symbol, tf), weights). `exclude` leaves sources out: an agent
    reading the ensemble leaves itself out, so its own earlier calls don't come back to it as the desk's view
    (albatross/agents/graph.py)."""
    symbols = symbols or [s["symbol"] for s in await universe()]
    weights = await source_weights()
    views = await latest_views(symbols, exclude)
    ens = {k: e for k, v in views.items() if (e := combine(v, weights))}
    return ens, views, weights


async def run() -> int:
    ens, _, _ = await compute()
    symbols = sorted({s for s, _ in ens})
    prices = await latest_prices(symbols)
    now = datetime.now(UTC)
    rows = []
    for (sym, tf), e in ens.items():
        if sym not in prices:
            continue
        as_of, ref = live_anchor(tf, now, prices[sym])
        rows.append((sym, tf, e["bias"], e["agreement"], ref, None,
                     orjson.dumps({"parts": e["parts"]}).decode(),
                     as_of + TFS[tf][2] * (1 + HORIZON_BARS), "ensemble", as_of, e["score"]))
    await db.upsert(
        "signals", ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers", "valid_until",
                    "source", "as_of", "score"],
        rows, ["scope", "tf", "source", "as_of"],
    )
    return len(rows)
