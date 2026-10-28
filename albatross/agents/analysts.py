"""LLM analysts: read precomputed numbers, return graded directional calls per asset and timeframe.

    ltf_analyst   5m / 15m / 1h   chart features            every 30 min
    htf_analyst   4h / 1d / 1w    chart features            every 4 h
    flow_analyst  1h / 4h         Hyperliquid + Binance/Bybit (Coinalyze) funding, OI, liquidations, hourly

Each call must cite evidence: feature names and values copied from its input. Code checks every
cited value against what the model was given and drops the call on any mismatch, so a call can't
rest on a number the model made up. Surviving calls land in `signals` under the agent's name and
the grader scores them against the quant baseline like any other source.
"""

import asyncio
import logging
import math
import random
import time
from datetime import UTC, datetime, timedelta

import orjson
from pydantic import BaseModel, Field

from albatross import controls, db
from albatross.config import settings
from albatross.features.engine import HORIZON_BARS, TFS
from albatross.llm import codex, openrouter
from albatross.strategy import evidence

log = logging.getLogger(__name__)

# 5 assets x 3 timeframes = 15 calls per request; 10 assets ran past the output limit.
BATCH = 5
SHADOW_MAX_S = 20 * 60          # from the run's start, all its shadows together (the LTF analyst runs every 30 min)
MAX_TOKENS = 20000                         # billed on use; 12k truncated some 4h/1d/1w batches
CONCURRENCY = 4

# Chart features an analyst sees, per timeframe (subset of indicators.compute()).
CHART_KEYS = [
    "close", "ret_1", "ret_5", "ret_20", "dist_ema20_pct", "dist_ema50_pct", "dist_ema200_pct",
    "ema20_slope_pct", "ema_stack", "rsi14", "macd_hist_rising", "atr_pct", "bb_pctb", "vol_z",
    "hi_20", "lo_20", "structure", "last_swing_high", "last_swing_low",
]


class Evidence(BaseModel):
    tf: str
    feature: str
    value: float | str | bool


class Call(BaseModel):
    symbol: str
    tf: str
    bias: int = Field(ge=-2, le=2, description="-2 strong bearish .. +2 strong bullish, 0 = no edge")
    confidence: float = Field(ge=0, le=1)
    thesis: str = Field(description="one sentence, <= 30 words")
    # Default empty so one call missing its evidence is dropped by check(), not the whole batch.
    evidence: list[Evidence] = Field(default_factory=list, description="2-4 inputs copied exactly from the data")
    invalidation: float | None = Field(default=None, description="price that proves the call wrong")


class Views(BaseModel):
    calls: list[Call]


# ─── inputs ─────────────────────────────────────────────────────────────────
async def universe(tiers: tuple[int, ...] = (1, 2)) -> list[dict]:
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT a.symbol, coalesce(c.name, a.primary_category) AS sector FROM assets a
           LEFT JOIN categories c ON c.slug = a.primary_category
           WHERE a.active AND a.tier = ANY($1::smallint[]) ORDER BY a.tier, a.volume_24h_usd DESC NULLS LAST""",
        list(tiers),
    )
    return [dict(r) for r in rows]


async def latest_features(symbols: list[str], tfs: list[str]) -> dict[tuple[str, str], dict]:
    """(symbol, tf) -> {"ts", "close", "data"} for the newest feature row, if fresh enough."""
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT DISTINCT ON (symbol, tf) symbol, tf, ts, close, data FROM features
           WHERE symbol = ANY($1::text[]) AND tf = ANY($2::text[]) AND ts > now() - interval '21 days'
           ORDER BY symbol, tf, ts DESC""",
        symbols, tfs,
    )
    now = datetime.now(UTC)
    out = {}
    for r in rows:
        bar = TFS[r["tf"]][2]
        if r["ts"] + 3 * bar < now:        # stale: the candle sync for this asset has stalled
            continue
        out[(r["symbol"], r["tf"])] = {"ts": r["ts"], "close": r["close"], "data": orjson.loads(r["data"])}
    return out


def _round(v):
    if isinstance(v, float):
        if not math.isfinite(v):
            return None
        return float(f"{v:.5g}")
    return v


def chart_payload(sym: dict, feats: dict, tfs: list[str]) -> dict | None:
    per_tf = {}
    for tf in tfs:
        f = feats.get((sym["symbol"], tf))
        if f:
            per_tf[tf] = {k: _round(f["data"].get(k)) for k in CHART_KEYS if f["data"].get(k) is not None}
    return {"symbol": sym["symbol"], "sector": sym["sector"], "timeframes": per_tf} if per_tf else None


async def cex_blocks(symbols: list[str]) -> dict[str, dict]:
    """Binance+Bybit aggregates per asset from cex_derivs_1h / cex_funding (complete hours only)."""
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT symbol, ts, sum(oi_usd) AS oi, sum(liq_long_usd) AS ll, sum(liq_short_usd) AS ls,
                  sum(volume) AS v, sum(buy_volume) AS bv,
                  max(long_pct) FILTER (WHERE venue = 'binance') AS long_pct
           FROM cex_derivs_1h
           WHERE symbol = ANY($1::text[]) AND ts > now() - interval '26 hours' AND ts < date_trunc('hour', now())
           GROUP BY symbol, ts ORDER BY symbol, ts""",
        symbols,
    )
    fund = {r["symbol"]: r for r in await pool.fetch(
        """SELECT symbol, avg(funding_pct) AS f, avg(predicted_pct) AS p FROM (
             SELECT DISTINCT ON (venue, symbol) venue, symbol, funding_pct, predicted_pct FROM cex_funding
             WHERE symbol = ANY($1::text[]) AND ts > now() - interval '3 hours' ORDER BY venue, symbol, ts DESC) x
           GROUP BY symbol""", symbols)}
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(r["symbol"], []).append(r)
    out = {}
    for sym, hs in series.items():
        last = hs[-1]
        if last["ts"] < datetime.now(UTC) - timedelta(hours=3):
            continue

        def oi_chg(h, hs=hs, last=last):
            return round((last["oi"] / hs[-1 - h]["oi"] - 1) * 100, 3) if len(hs) > h and hs[-1 - h]["oi"] and last["oi"] else None

        def tot(k, h, hs=hs):
            return round(sum(x[k] or 0 for x in hs[-h:]))

        v4, bv4 = sum(x["v"] or 0 for x in hs[-4:]), sum(x["bv"] or 0 for x in hs[-4:])
        f = fund.get(sym)
        b = {
            "cex_oi_usd": round(last["oi"]) if last["oi"] else None,
            "cex_oi_chg_pct_1h": oi_chg(1), "cex_oi_chg_pct_4h": oi_chg(4), "cex_oi_chg_pct_24h": oi_chg(24),
            "cex_liq_long_usd_4h": tot("ll", 4), "cex_liq_short_usd_4h": tot("ls", 4),
            "cex_liq_long_usd_24h": tot("ll", 24), "cex_liq_short_usd_24h": tot("ls", 24),
            "cex_taker_buy_pct_4h": round(bv4 / v4 * 100, 2) if v4 else None,
            "binance_accounts_long_pct": last["long_pct"],
            # percent per 8h -> annualised %, same scale as the Hyperliquid funding fields
            "cex_funding_apr_pct": round(f["f"] * 3 * 365, 2) if f and f["f"] is not None else None,
            "cex_predicted_funding_apr_pct": round(f["p"] * 3 * 365, 2) if f and f["p"] is not None else None,
        }
        out[sym] = {k: _round(v) for k, v in b.items() if v is not None}
    return out


async def flow_payloads(syms: list[dict]) -> tuple[list[dict], dict]:
    """Hyperliquid perp context per asset: funding (annualised %), OI and price changes, premium.
    -> (payloads, anchors)."""
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT symbol, ts, funding_rate, oi_usd, mark_px, premium, day_volume_usd FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND symbol = ANY($1::text[]) AND ts > now() - interval '25 hours'
           ORDER BY symbol, ts""",
        [s["symbol"] for s in syms],
    )
    series: dict[str, list] = {}
    for r in rows:
        series.setdefault(r["symbol"], []).append(r)
    cex = await cex_blocks([s["symbol"] for s in syms])
    now = datetime.now(UTC)
    out, anchors = [], {}
    for s in syms:
        pts = series.get(s["symbol"])
        if not pts or pts[-1]["ts"] < now - timedelta(minutes=10):
            continue
        last = pts[-1]

        def at(hours: float, pts=pts):
            cut = now - timedelta(hours=hours)
            prior = [p for p in pts if p["ts"] <= cut]
            return prior[-1] if prior and prior[-1]["ts"] > cut - timedelta(minutes=20) else None

        def chg(a, b):
            return None if a is None or not b else round((a / b - 1) * 100, 3)

        def apr(hours: float, pts=pts):
            w = [p["funding_rate"] for p in pts if p["ts"] > now - timedelta(hours=hours) and p["funding_rate"] is not None]
            return round(sum(w) / len(w) * 24 * 365 * 100, 2) if w else None

        flow = {
            "funding_apr_pct_now": round(last["funding_rate"] * 24 * 365 * 100, 2) if last["funding_rate"] is not None else None,
            "funding_apr_pct_avg_8h": apr(8), "funding_apr_pct_avg_24h": apr(24),
            "oi_usd": round(last["oi_usd"]) if last["oi_usd"] else None,
            "premium_bps": round(last["premium"] * 1e4, 2) if last["premium"] is not None else None,
            "day_volume_usd": round(last["day_volume_usd"]) if last["day_volume_usd"] else None,
        }
        for h in (1, 4, 24):
            p = at(h)
            flow[f"oi_chg_pct_{h}h"] = chg(last["oi_usd"], p["oi_usd"]) if p else None
            flow[f"px_chg_pct_{h}h"] = chg(last["mark_px"], p["mark_px"]) if p else None
        flow = {k: _round(v) for k, v in flow.items() if v is not None} | cex.get(s["symbol"], {})
        if last["mark_px"]:
            px = _round(last["mark_px"])
            out.append({"symbol": s["symbol"], "sector": s["sector"],
                        "timeframes": {tf: {"price": px} | flow for tf in ("1h", "4h")}})
            anchors.update({(s["symbol"], tf): live_anchor(tf, now, last["mark_px"]) for tf in ("1h", "4h")})
    return out, anchors


def live_anchor(tf: str, now: datetime, price: float) -> tuple[datetime, float]:
    """Anchor for a call made on live (intra-candle) data: as_of = the candle in progress, so grading
    starts after it closes, and ref_price = the price the analyst actually saw. Anchoring to the last
    closed candle instead would grade the call on moves it had already watched happen."""
    bar = TFS[tf][2]
    # Weekly candles start Monday 00:00 UTC (W-MON); 1970-01-05 was a Monday.
    epoch = datetime(1970, 1, 5 if tf == "1w" else 1, tzinfo=UTC)
    return epoch + ((now - epoch) // bar) * bar, float(price)


async def latest_prices(symbols: list[str], max_age: timedelta = timedelta(minutes=12)) -> dict[str, float]:
    """Freshest price per asset: Hyperliquid mark (minute snapshots), else the latest 5m close."""
    pool = await db.pool()
    out = {r["symbol"]: r["mark_px"] for r in await pool.fetch(
        """SELECT DISTINCT ON (symbol) symbol, mark_px FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND symbol = ANY($1::text[]) AND ts > now() - $2::interval AND mark_px > 0
           ORDER BY symbol, ts DESC""", symbols, max_age)}
    missing = [s for s in symbols if s not in out]
    if missing:
        for r in await pool.fetch(
            """SELECT DISTINCT ON (symbol) symbol, close FROM features
               WHERE tf = '5m' AND symbol = ANY($1::text[]) AND ts > now() - $2::interval - interval '5 minutes'
               ORDER BY symbol, ts DESC""", missing, max_age):
            out[r["symbol"]] = r["close"]
    return out


def candle_anchors(feats: dict) -> dict[tuple[str, str], tuple[datetime, float]]:
    """Chart features are frozen at the candle close, so that close is the honest anchor."""
    return {k: (f["ts"], f["close"]) for k, f in feats.items()}


# ─── validation ─────────────────────────────────────────────────────────────
def _matches(given, cited) -> bool:
    if isinstance(given, bool) or isinstance(cited, bool) or isinstance(given, str) or isinstance(cited, str):
        return str(given).lower() == str(cited).lower()
    try:
        g, c = float(given), float(cited)
    except (TypeError, ValueError):
        return False
    return abs(g - c) <= max(0.011 * abs(g), 0.02)


def check(call: Call, payload: dict, allowed_tfs: list[str]) -> str | None:
    """-> None if the call is usable, else the reason it was dropped."""
    if call.tf not in allowed_tfs or call.tf not in payload["timeframes"]:
        return f"no data for tf {call.tf}"
    if not call.evidence:
        return "no evidence"
    for ev in call.evidence:
        block = payload["timeframes"].get(ev.tf)
        if block is None or ev.feature not in block:
            return f"cited unknown input {ev.tf}.{ev.feature}"
        if not _matches(block[ev.feature], ev.value):
            return f"misquoted {ev.tf}.{ev.feature}: said {ev.value}, input {block[ev.feature]}"
    return None


def _invalidation(call: Call, close: float) -> float | None:
    inv = call.invalidation
    if inv is None or not close or call.bias == 0:
        return None
    if (call.bias > 0 and inv >= close) or (call.bias < 0 and inv <= close) or abs(inv / close - 1) > 0.5:
        return None                                       # wrong side of price or absurd: ignore it
    return float(inv)


# ─── run ────────────────────────────────────────────────────────────────────
def shadow_models(agent: str) -> list[str]:
    """ANALYST_SHADOW_MODELS "agent=model,agent=model" -> this analyst's shadow models."""
    out = []
    for part in (settings.analyst_shadow_models or "").split(","):
        name, _, model = part.partition("=")
        if name.strip() == agent and model.strip() and model.strip() not in out:
            out.append(model.strip())
    return out


def shadow_name(agent: str, model: str) -> str:
    return f"{agent}@{model.rsplit('/', 1)[-1]}"


async def run_analyst(agent: str, system: str, payloads: list[dict], tfs: list[str],
                      anchors: dict[tuple[str, str], tuple[datetime, float]], budget_usd: float) -> int:
    shadows_end = time.monotonic() + SHADOW_MAX_S          # one end for all of this run's shadows together
    payloads = await controls.sampled(agent, payloads)      # a paused analyst reads 10% of the coins (cull.py)
    written = await _run(agent, agent, system, payloads, tfs, anchors, budget_usd, openrouter.LUNA)
    # Shadows: the same inputs on another model, stored as their own source and graded like any
    # other, feeding nothing (the ensemble reads only its listed sources). They give way to the regular agents on the
    # ChatGPT plan, and one on Sol runs only there.
    for model in shadow_models(agent):
        name = shadow_name(agent, model)
        plan_only, budget = openrouter.shadow_terms(model, settings.analyst_shadow_budget_usd)
        try:
            if plan_only and not (await codex.pace(experiment=True))[0]:
                log.info("%s: the plan has no room for shadows now, skipped this run", name)   # whole, never a part
                continue
            await _run(name, name, system, payloads, tfs, anchors, budget, model,
                       experiment=True, plan_only=plan_only, not_after=shadows_end)
        except Exception as e:  # noqa: BLE001 - a shadow must never cost the real run anything
            log.warning("%s failed: %s", name, type(e).__name__)
    return written


async def _run(call_agent: str, source: str, system: str, payloads: list[dict], tfs: list[str],
               anchors: dict[tuple[str, str], tuple[datetime, float]], budget_usd: float, model: str, *,
               experiment: bool = False, plan_only: bool = False, not_after: float | None = None) -> int:
    sem = asyncio.Semaphore(CONCURRENCY)              # a shadow's calls also queue for the 2 experiment slots

    async def batch(chunk: list[dict]) -> tuple[int, int]:
        by_sym = {p["symbol"]: p for p in chunk}
        async with sem:
            try:
                views, run_id = await openrouter.structured(
                    call_agent, Views, system, orjson.dumps(chunk).decode(), model=model,
                    daily_budget_usd=budget_usd, max_tokens=MAX_TOKENS, experiment=experiment, plan_only=plan_only,
                    not_after=not_after,               # a shadow never runs into the analyst's next pass
                )
            except openrouter.BudgetExceeded as e:
                log.log(logging.DEBUG if isinstance(e, openrouter.NotConfigured) else logging.WARNING, "%s", e)
                return 0, 0
            except RuntimeError as e:                      # model/parse failure, already logged in agent_runs
                log.warning("%s", e)
                return 0, 0
        rows, reasons = [], []
        seen = set()
        for c in views.calls:
            c.symbol = c.symbol.upper()
            p = by_sym.get(c.symbol)
            why = "unknown symbol" if p is None else check(c, p, tfs)
            if why is None and (c.symbol, c.tf) in seen:
                why = "duplicate"
            if why:
                reasons.append(f"{c.symbol} {c.tf}: {why}")
                continue
            seen.add((c.symbol, c.tf))
            as_of, ref = anchors[(c.symbol, c.tf)]
            bar = TFS[c.tf][2]
            rows.append((
                c.symbol, c.tf, c.bias, round(c.confidence, 3), ref, _invalidation(c, ref),
                orjson.dumps({"thesis": c.thesis, "evidence": [e.model_dump() for e in c.evidence]}).decode(),
                as_of + bar * (1 + HORIZON_BARS), source, as_of, c.bias / 2, run_id,
            ))
        if reasons:
            pool = await db.pool()
            await pool.execute(
                "UPDATE agent_runs SET error = $2 WHERE id = $1",
                run_id, f"dropped {len(reasons)}/{len(views.calls)}: " + "; ".join(reasons)[:900],
            )
        await db.upsert(
            "signals",
            ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers", "valid_until",
             "source", "as_of", "score", "agent_run_id"],
            rows, ["scope", "tf", "source", "as_of"],
        )
        return len(rows), len(reasons)

    chunks = [payloads[i:i + BATCH] for i in range(0, len(payloads), BATCH)]
    if experiment:
        random.shuffle(chunks)                         # a shadow cut short is a random sample, not the first coins
    results = await asyncio.gather(*(batch(c) for c in chunks))
    written = sum(r[0] for r in results)
    dropped = sum(r[1] for r in results)
    if dropped:
        log.info("%s: %d calls written, %d dropped by validation", source, written, dropped)
    return written


COMMON = """
Output: for EVERY asset and EVERY timeframe listed in its data, one call:
- bias: -2 strong bearish, -1 bearish, 0 no edge, +1 bullish, +2 strong bullish
- confidence 0..1: how sure you are of the direction (0.5 = coin flip)
- thesis: one sentence, specific to this asset/timeframe
- evidence: 2-4 inputs that drive the call, each as {tf, feature, value} copied EXACTLY from the data
  (same feature name, same number). Calls citing anything not in the data are discarded.
- invalidation: a price level that would prove the call wrong (below price for longs, above for
  shorts), ideally an existing level from the data (swing high/low, hi_20/lo_20) when given; null for bias 0.
Grading: a directional call hits if price moved your way over the next 6 bars of its timeframe; a 0
call hits if price stayed within half that window's range. Use 0 freely when there is no edge; use +/-2
only with strong confluence. Use only the numbers given; do not assume any other information."""

LTF_SYSTEM = """You are the short-timeframe chart analyst on a crypto desk. For each asset you get
precomputed indicators on 5m, 15m and 1h candles (ret_N = % change over N bars; dist_emaN_pct = %
distance of price from that EMA; atr_pct = ATR as % of price; bb_pctb = position in the Bollinger band,
>1 above the upper band, <0 below the lower; vol_z = volume z-score; hi_20/lo_20 = 20-bar high/low;
structure = swing structure). Horizons: 5m call = next 30 min, 15m = next 90 min, 1h = next 6 h.
""" + evidence.LTF_EVIDENCE + "\n" + COMMON

HTF_SYSTEM = """You are the higher-timeframe chart analyst on a crypto desk. For each asset you get
precomputed indicators on 4h, 1d and 1w candles (ret_N = % change over N bars; dist_emaN_pct = %
distance of price from that EMA; atr_pct = ATR as % of price; bb_pctb = position in the Bollinger band;
vol_z = volume z-score; hi_20/lo_20 = 20-bar high/low; structure = swing structure). Horizons: 4h call
= next 24 h, 1d = next 6 days, 1w = next 6 weeks.
""" + evidence.HTF_EVIDENCE + "\n" + COMMON

FLOW_SYSTEM = """You are the flow & derivatives analyst on a crypto desk. For each asset you get
Hyperliquid perpetual data: funding annualised (%, positive = longs pay shorts) now and averaged over
8h/24h, open interest (USD) and its % change over 1h/4h/24h, mark-price % change over the same windows,
premium of mark over oracle (bps) and 24h volume. Call the 1h timeframe (next 6 h) and 4h (next 24 h);
both blocks carry the same live flow data plus the current mark price.
Readings: price up + OI up = new longs (trend fuel, but crowding if funding is also high); price up + OI
down = short covering (often fades); price down + OI up = new shorts (squeeze fuel if funding turns
negative); price down + OI down = long liquidation/capitulation (often near exhaustion). Funding here
is Hyperliquid's hourly rate annualised: its baseline is the interest component, 0.00125%/h = 10.95%
APR, which is where funding sits when the perp trades at spot, so treat ~11% APR as NEUTRAL and read
crowding from the distance to it. Extreme funding (e.g. > 50% APR or < -20% APR) against a stalling
price is a crowding signal. Hyperliquid is one venue;
say so in the thesis when a read rests on thin OI.
Fields prefixed cex_ aggregate Binance + Bybit perps (via Coinalyze; complete hours only), where most
leverage sits: cex_oi_usd and its % change over 1h/4h/24h, liquidations of longs vs shorts (USD) over
4h/24h, cex_taker_buy_pct_4h (share of volume from aggressive buyers; >55 buyers pressing, <45 sellers),
binance_accounts_long_pct (share of Binance accounts net long; crowd positioning), and cex funding
annualised on the same scale as Hyperliquid (0.01%/8h baseline = 10.95% APR = neutral). Weigh cex_ over
Hyperliquid when they disagree, since cex OI is usually far larger. A large long-liquidation flush with
OI dropping often marks exhaustion of a down move; the mirror for shorts. When cex_ fields are absent,
the read rests on Hyperliquid alone.
""" + COMMON


async def ltf() -> int:
    tfs = ["5m", "15m", "1h"]
    syms = await universe()
    feats = await latest_features([s["symbol"] for s in syms], tfs)
    payloads = [p for s in syms if (p := chart_payload(s, feats, tfs))]
    return await run_analyst("ltf_analyst", LTF_SYSTEM, payloads, tfs, candle_anchors(feats), budget_usd=5.0)


async def htf() -> int:
    tfs = ["4h", "1d", "1w"]
    syms = await universe()
    feats = await latest_features([s["symbol"] for s in syms], tfs)
    payloads = [p for s in syms if (p := chart_payload(s, feats, tfs))]
    return await run_analyst("htf_analyst", HTF_SYSTEM, payloads, tfs, candle_anchors(feats), budget_usd=1.0)


async def flow() -> int:
    payloads, anchors = await flow_payloads(await universe())
    return await run_analyst("flow_analyst", FLOW_SYSTEM, payloads, ["1h", "4h"], anchors, budget_usd=2.0)

