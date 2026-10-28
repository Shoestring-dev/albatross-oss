"""Macro analyst: daily/weekly directional read on BTC and ETH from rates, the dollar, US liquidity,
risk appetite, spot-ETF flows and stablecoin supply (albatross/ingest/macro.py).

Deterministic code computes every number (levels, changes, sums) here; the model interprets them
through the same evidence-checked machinery as the other analysts (analysts.run_analyst), so each call
must cite inputs it was given. Only 1d and 1w: macro barely moves a 30-minute candle. Calls are
anchored at call time with the live price, like the Flow analyst.
"""

import logging
from datetime import UTC, datetime, timedelta

from albatross import db
from albatross.agents import analysts

log = logging.getLogger(__name__)

AGENT = "macro_analyst"
ASSETS = {"BTC": "btc_etf", "ETH": "eth_etf"}
TFS = ["1d", "1w"]
BUDGET_USD = 0.5


async def _series(metrics: list[str], days: int = 120) -> dict[str, list[tuple[datetime, float]]]:
    pool = await db.pool()
    out: dict[str, list] = {m: [] for m in metrics}
    for r in await pool.fetch(
        """SELECT metric, ts, value FROM metric_series
           WHERE metric = ANY($1::text[]) AND ts > now() - make_interval(days => $2) AND value IS NOT NULL
           ORDER BY metric, ts""", metrics, days):
        out[r["metric"]].append((r["ts"], r["value"]))
    return out


def _last(s):
    return s[-1][1] if s else None


def _back(s, n):
    """Value n observations before the latest (trading days for daily series, weeks for weekly)."""
    return s[-1 - n][1] if len(s) > n else None


def _at_or_before(s, ts):
    prior = [v for t, v in s if t <= ts]
    return prior[-1] if prior else None


def _pct(a, b):
    return None if a is None or not b else (a / b - 1) * 100


async def snapshot() -> dict:
    """Numbers for the prompt (and for the Strategist's market context). Units in the key names."""
    s = await _series(["ust10y_pct", "ust2y_pct", "breakeven10y_pct", "fed_funds_pct", "dollar_index", "vix",
                       "spx", "nasdaq", "fed_assets_musd", "tga_musd", "rrp_busd", "stablecoin_supply_usd",
                       "fear_greed", "btc_etf_net_inflow_usd", "eth_etf_net_inflow_usd",
                       "btc_mvrv_z", "btc_mvrv", "eth_mvrv_z", "eth_mvrv"])
    m: dict = {}
    for k in ("ust10y_pct", "ust2y_pct"):
        v = _last(s[k])
        m[k] = v
        for n in (5, 20):
            b = _back(s[k], n)
            m[f"{k.removesuffix('_pct')}_chg_bps_{n}d"] = None if v is None or b is None else (v - b) * 100
    if m.get("ust10y_pct") is not None and m.get("ust2y_pct") is not None:
        m["curve_2s10s_bps"] = (m["ust10y_pct"] - m["ust2y_pct"]) * 100
    m["breakeven10y_pct"], m["fed_funds_pct"] = _last(s["breakeven10y_pct"]), _last(s["fed_funds_pct"])
    m["real_yield10y_pct"] = (None if m["ust10y_pct"] is None or m["breakeven10y_pct"] is None
                              else m["ust10y_pct"] - m["breakeven10y_pct"])
    for k, label in (("dollar_index", "dollar"), ("spx", "spx"), ("nasdaq", "nasdaq")):
        m[f"{label}_level"] = _last(s[k])
        for n in (5, 20):
            m[f"{label}_chg_pct_{n}d"] = _pct(_last(s[k]), _back(s[k], n))
    m["vix"], m["vix_chg_5d"] = _last(s["vix"]), (None if len(s["vix"]) < 6 else s["vix"][-1][1] - s["vix"][-6][1])

    # US net liquidity ($B) = Fed assets - Treasury General Account - reverse repo, on each Fed week.
    liq = []
    for t, assets in s["fed_assets_musd"]:
        tga, rrp = _at_or_before(s["tga_musd"], t), _at_or_before(s["rrp_busd"], t)
        if tga is not None and rrp is not None:
            liq.append((t, assets / 1000 - tga / 1000 - rrp))
    m["net_liquidity_busd"] = _last(liq)
    for n, label in ((1, "1w"), (4, "4w")):
        b = _back(liq, n)
        m[f"net_liquidity_chg_busd_{label}"] = None if not liq or b is None else liq[-1][1] - b

    now = datetime.now(UTC)
    for prefix in ("btc_etf", "eth_etf"):
        f = s[f"{prefix}_net_inflow_usd"]
        for n in (1, 5, 20):
            m[f"{prefix}_net_inflow_musd_{n}d"] = sum(v for _, v in f[-n:]) / 1e6 if len(f) >= n else None
    sc = s["stablecoin_supply_usd"]
    m["stablecoin_supply_busd"] = _last(sc) / 1e9 if sc else None
    for days in (7, 30):
        m[f"stablecoin_chg_pct_{days}d"] = _pct(_last(sc), _at_or_before(sc, now - timedelta(days=days)))
    # On-chain cycle gauge (albatross/ingest/onchain.py): Z < 0 has marked cycle bottoms, 7+ the 2013/2017 tops.
    for a in ("btc", "eth"):
        m[f"{a}_mvrv"], m[f"{a}_mvrv_z"] = _last(s[f"{a}_mvrv"]), _last(s[f"{a}_mvrv_z"])
        z30 = _at_or_before(s[f"{a}_mvrv_z"], now - timedelta(days=30))
        m[f"{a}_mvrv_z_chg_30d"] = None if m[f"{a}_mvrv_z"] is None or z30 is None else m[f"{a}_mvrv_z"] - z30
    fg = s["fear_greed"]
    m["fear_greed"] = _last(fg)
    m["fear_greed_avg_7d"] = sum(v for _, v in fg[-7:]) / len(fg[-7:]) if fg else None
    m["data_dates"] = {k: s[k][-1][0].date().isoformat() for k in ("ust10y_pct", "fed_assets_musd", "spx",
                                                                    "btc_etf_net_inflow_usd", "stablecoin_supply_usd")
                       if s[k]}
    return {k: (analysts._round(float(v)) if isinstance(v, (int, float)) else v) for k, v in m.items()
            if v is not None}


SYSTEM = """You are the macro analyst on a crypto desk. You get US rates (10y/2y yields and their changes
in basis points, the 2s10s curve, 10y breakeven and real yield, fed funds), the broad dollar index,
S&P 500 / Nasdaq and VIX, US net liquidity (Fed assets minus Treasury General Account minus reverse repo,
$B, weekly), spot-ETF net flows for the asset ($M over 1/5/20 trading days), total stablecoin supply and
its change, Fear & Greed, and the on-chain MVRV and MVRV Z-score for BTC and ETH (market value against
realized value; a Z below 0 has marked cycle bottoms and 7+ the 2013 and 2017 tops, a multi-month gauge,
not a timing signal for days). Call BTC and ETH on 1d (next 6 days) and 1w (next 6 weeks).
Typical relationships, not laws: rising real yields and a rising dollar are headwinds; expanding net
liquidity and stablecoin supply are tailwinds; sustained ETF inflows are direct spot demand; VIX spikes
and equity drawdowns spill into crypto; extreme Fear & Greed readings lean contrarian. Macro rarely
decides a 6-day move: use 0 on 1d unless the inputs are one-sided, and keep 1d confidence modest.
data_dates shows how fresh each series is (FRED weekly data lags); do not cite it as evidence.
""" + analysts.COMMON


async def run() -> int:
    snap = await snapshot()
    if len(snap) < 8:
        log.warning("macro snapshot too thin (%d fields); skipping", len(snap))
        return 0
    prices = await analysts.latest_prices(list(ASSETS))
    now = datetime.now(UTC)
    payloads, anchors = [], {}
    for sym, prefix in ASSETS.items():
        if sym not in prices:
            continue
        other = "eth_etf" if prefix == "btc_etf" else "btc_etf"
        block = {"price": analysts._round(float(prices[sym]))} | {
            k: v for k, v in snap.items() if not k.startswith(other) and k != "data_dates"}
        payloads.append({"symbol": sym, "sector": "macro", "data_dates": snap.get("data_dates", {}),
                         "timeframes": {tf: block for tf in TFS}})
        anchors.update({(sym, tf): analysts.live_anchor(tf, now, prices[sym]) for tf in TFS})
    return await analysts.run_analyst(AGENT, SYSTEM, payloads, TFS, anchors, budget_usd=BUDGET_USD)
