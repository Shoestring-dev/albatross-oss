"""/api/overview: everything the Overview (home) page shows, in one call.

KPIs (BTC, Fear & Greed, ETF flows, yields, liquidity, stablecoins), the Strategist's compass for the
ten Tier 1 coins with live prices, the bias distribution ("market flow") per timeframe, a 24h radar
of ideas / Oracle escalations / high-impact news / open alerts, and the latest report. Read-only.
"""

import re
from datetime import UTC, datetime, timedelta

import orjson
from fastapi import APIRouter

from albatross import db
from albatross.agents import macro

router = APIRouter(prefix="/api")
TFS = ["5m", "15m", "1h", "4h", "1d", "1w"]
# What woke the Oracle, in words (albatross/agents/oracle.py triggers)
ORACLE_TRIGGER = {"conflict": "its timeframes disagree", "move": "an outsized move", "flip": "a call flipped",
                  "news": "a big headline", "manual_test": "a manual check"}


async def _prices(pool, symbols: list[str]) -> dict[str, dict]:
    rows = await pool.fetch(
        """WITH now_ AS (
             SELECT DISTINCT ON (symbol) symbol, mark_px FROM deriv_snapshots
             WHERE venue = 'hyperliquid' AND symbol = ANY($1::text[]) AND ts > now() - interval '15 minutes'
             ORDER BY symbol, ts DESC),
           ago AS (
             SELECT DISTINCT ON (symbol) symbol, mark_px FROM deriv_snapshots
             WHERE venue = 'hyperliquid' AND symbol = ANY($1::text[])
               AND ts BETWEEN now() - interval '25 hours' AND now() - interval '24 hours'
             ORDER BY symbol, ts DESC)
           SELECT n.symbol, n.mark_px AS px, a.mark_px AS px_24h FROM now_ n LEFT JOIN ago a USING (symbol)""",
        symbols)
    out = {r["symbol"]: {"price": r["px"], "px_24h": r["px_24h"]} for r in rows}
    # No snapshot from a day ago (the first day of collecting, or a gap): take the price 24h ago from
    # Hyperliquid's own 5-minute candles, so the change is still a true rolling 24h, never the daily candle.
    missing = [s for s, v in out.items() if not v["px_24h"]]
    if missing:
        for r in await pool.fetch(
            """SELECT DISTINCT ON (symbol) symbol, open FROM candles
               WHERE venue = 'hyperliquid' AND tf = '5m' AND symbol = ANY($1::text[])
                 AND ts BETWEEN now() - interval '24 hours 10 minutes' AND now() - interval '24 hours'
               ORDER BY symbol, ts DESC""", missing):
            out[r["symbol"]]["px_24h"] = r["open"]
    for v in out.values():
        ago = v.pop("px_24h")
        v["chg_24h_pct"] = (v["price"] / ago - 1) * 100 if ago and v["price"] is not None else None   # mark_px is nullable
    return out


async def _mvrv(pool) -> dict:
    """MVRV Z-score for BTC and ETH: latest, 30 days before, the MVRV ratio and its date (albatross/ingest/onchain.py)."""
    out = {}
    for a in ("btc", "eth"):
        rows = await pool.fetch(
            """SELECT metric, ts, value FROM metric_series WHERE source = 'coinmetrics' AND metric = ANY($1::text[])
               AND ts > now() - interval '40 days' ORDER BY ts""", [f"{a}_mvrv_z", f"{a}_mvrv"])
        z = [(r["ts"], r["value"]) for r in rows if r["metric"] == f"{a}_mvrv_z"]
        mv = [r["value"] for r in rows if r["metric"] == f"{a}_mvrv"]
        if not z:
            continue
        back = [v for t, v in z if t <= z[-1][0] - timedelta(days=30)]
        out[a] = {"z": z[-1][1], "z_30d_ago": back[-1] if back else None, "mvrv": mv[-1] if mv else None,
                  "as_of": z[-1][0].date().isoformat()}
    return out


def _excerpt(md: str, n: int = 320) -> str:
    # Headings are dropped (the card shows the title; they ran into the text: "Book No bias..."), list
    # markers too; an underscore inside a word is a snake_case label, so it reads as a space ("trending up").
    lines = [re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", ln) for ln in (md or "").splitlines() if not ln.lstrip().startswith("#")]
    text = re.sub(r"(?<=[A-Za-z0-9])_(?=[A-Za-z0-9])", " ", "\n".join(lines))
    text = re.sub(r"[#*_`>\[\]]|\(https?://[^)]+\)", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:n].rsplit(" ", 1)[0] + "…" if len(text) > n else text


def _idea_items(rows) -> list[dict]:
    """Radar entries for trade ideas (rows newest first). The Strategist logs its ideas in batches; a batch
    (one source, each idea within two minutes of the next) is one entry, so it can't bury everything else."""
    batches: list[list] = []
    for r in rows:
        last = batches[-1][-1] if batches else None
        if last and last["source_id"] == r["source_id"] and last["logged_at"] - r["logged_at"] <= timedelta(minutes=2):
            batches[-1].append(r)
        else:
            batches.append([r])
    out = []
    for b in batches:
        r = b[0]
        if len(b) == 1:
            said = "vetoed by the critic" if r["status"] == "vetoed" else f"critic: {r['verdict']}" if r["verdict"] else r["status"]
            out.append({"kind": "idea", "at": r["logged_at"], "scope": r["scope"],
                        "title": f"{r['direction'].title()} {r['scope']} · {r['tf']}",
                        "detail": (f"R:R {r['reward_risk']:.2f} · " if r["reward_risk"] is not None else "") + said,
                        "tone": "bull" if r["direction"] == "long" else "bear", "id": r["id"]})
            continue
        longs = sum(x["direction"] == "long" for x in b)
        shorts, vetoed = len(b) - longs, sum(x["status"] == "vetoed" for x in b)
        tfs = sorted({x["tf"] for x in b}, key=lambda t: TFS.index(t) if t in TFS else len(TFS))
        names = list(dict.fromkeys(x["scope"] for x in b))
        out.append({"kind": "idea", "at": r["logged_at"], "n": len(b),
                    "title": f"{len(b)} new ideas, " + ("all long" if not shorts else "all short" if not longs
                                                       else f"{longs} long, {shorts} short"),
                    "detail": (", ".join(names) if len(names) <= 6 else ", ".join(names[:5]) + f" +{len(names) - 5} more")
                              + f" · {', '.join(tfs)}" + (f" · {vetoed} vetoed by the critic" if vetoed else ""),
                    "tone": "bull" if not shorts else "bear" if not longs else "accent"})
    return out


@router.get("/overview")
async def overview():
    pool = await db.pool()
    tier1 = [r["symbol"] for r in await pool.fetch(
        "SELECT symbol FROM assets WHERE active AND tier = 1 ORDER BY volume_24h_usd DESC NULLS LAST LIMIT 10")]
    prices = await _prices(pool, tier1)

    compass: dict[str, dict] = {s: {"symbol": s, "tfs": {}, "regime": None, **prices.get(s, {})} for s in tier1}
    for r in await pool.fetch(
        """SELECT DISTINCT ON (scope, tf) scope, tf, bias, confidence, score, regime, drivers->>'thesis' AS thesis,
                  valid_until FROM signals
           WHERE source = 'strategist' AND scope = ANY($1::text[]) AND as_of > now() - interval '8 days'
           ORDER BY scope, tf, as_of DESC""", tier1):
        c = compass[r["scope"]]
        c["tfs"][r["tf"]] = {"bias": r["bias"], "score": r["score"], "confidence": r["confidence"],
                             "thesis": r["thesis"], "valid_until": r["valid_until"]}
        if r["regime"] and (r["tf"] == "4h" or c["regime"] is None):
            c["regime"] = r["regime"]

    flow = {tf: {str(b): 0 for b in range(-2, 3)} for tf in TFS}
    for r in await pool.fetch(
        """SELECT tf, bias, count(*) AS n FROM (
             SELECT DISTINCT ON (scope, tf) scope, tf, bias FROM signals
             WHERE source = 'strategist' AND valid_until > now() AND scope NOT LIKE 'sector:%'
             ORDER BY scope, tf, as_of DESC) x GROUP BY 1, 2"""):
        flow.setdefault(r["tf"], {})[str(r["bias"])] = r["n"]

    snap = await macro.snapshot()
    fg = await pool.fetchrow(
        """SELECT l.value, (SELECT value FROM metric_series WHERE metric = 'fear_greed'
                             AND ts BETWEEN l.ts - interval '8 days' AND l.ts - interval '7 days'
                             ORDER BY ts DESC LIMIT 1) AS week_ago
           FROM (SELECT value, ts FROM metric_series WHERE metric = 'fear_greed' ORDER BY ts DESC LIMIT 1) l""")
    kpis = {
        "btc": prices.get("BTC"), "eth": prices.get("ETH"),
        "fear_greed": {"value": fg["value"] if fg else None, "week_ago": fg["week_ago"] if fg else None},
        "macro": {k: snap.get(k) for k in (
            "btc_etf_net_inflow_musd_1d", "btc_etf_net_inflow_musd_5d", "eth_etf_net_inflow_musd_5d",
            "ust10y_pct", "ust10y_chg_bps_5d", "dollar_chg_pct_5d", "net_liquidity_busd",
            "net_liquidity_chg_busd_1w", "stablecoin_supply_busd", "stablecoin_chg_pct_7d", "vix", "vix_chg_5d")}
        # ETF flows are daily, by US trading session: the "1d" figure is the latest session's, not today's
        | {"btc_etf_date": snap.get("data_dates", {}).get("btc_etf_net_inflow_usd")},
        "mvrv": await _mvrv(pool),
    }

    since = datetime.now(UTC) - timedelta(hours=24)
    radar = _idea_items(await pool.fetch(
        """SELECT c.id, c.logged_at, c.scope, c.direction, c.tf, c.status, c.reward_risk, c.source_id,
                  c.critique->>'verdict' AS verdict FROM trade_calls c
           WHERE c.logged_at > $1 AND c.market <> 'dex' AND c.source_kind <> 'model'   -- micro-cap calls and Jev's paper book have their own lanes
           ORDER BY c.logged_at DESC LIMIT 300""", since))
    for r in await pool.fetch(
        """SELECT created_at, trigger, context, verdict FROM escalations
           WHERE created_at > $1 ORDER BY created_at DESC LIMIT 8""", since):
        ctx = orjson.loads(r["context"]) if r["context"] else {}
        v = orjson.loads(r["verdict"]) if r["verdict"] else {}
        why = ORACLE_TRIGGER.get(r["trigger"], r["trigger"].replace("_", " "))
        radar.append({"kind": "oracle", "at": r["created_at"], "scope": ctx.get("scope"),
                      "title": f"{ctx['scope']}: {why}" if ctx.get("scope") else why.capitalize(),
                      "detail": _excerpt(v.get("assessment") or v.get("title") or "", 150), "tone": "accent"})
    for r in await pool.fetch(
        """SELECT title, url, impact, sentiment, published_at, assets FROM news_items
           WHERE impact >= 4 AND published_at > $1 ORDER BY published_at DESC LIMIT 10""", since):
        radar.append({"kind": "news", "at": r["published_at"], "title": r["title"], "url": r["url"],
                      "detail": f"impact {r['impact']}" + (f" · {', '.join(r['assets'][:3])}" if r["assets"] else ""),
                      "tone": "bull" if (r["sentiment"] or 0) > 0 else "bear" if (r["sentiment"] or 0) < 0 else "flat"})
    if await pool.fetchval("SELECT to_regclass('public.alerts') IS NOT NULL"):
        for r in await pool.fetch(
            "SELECT title, severity, first_seen FROM alerts WHERE resolved_at IS NULL ORDER BY first_seen DESC LIMIT 5"):
            radar.append({"kind": "alert", "at": r["first_seen"], "title": r["title"], "detail": r["severity"],
                          "tone": "bear"})
    radar.sort(key=lambda e: e["at"], reverse=True)

    rep = await pool.fetchrow("SELECT id, kind, title, body_md, created_at FROM reports WHERE kind <> 'ceo' "
                              "ORDER BY created_at DESC LIMIT 1")
    ideas = await pool.fetchrow(
        """SELECT count(*) AS total, count(*) FILTER (WHERE status = 'vetoed') AS vetoed,
                  count(*) FILTER (WHERE logged_at > now() - interval '24 hours') AS today FROM trade_calls
           WHERE market <> 'dex' AND source_kind <> 'model'""")
    regimes: dict[str, int] = {}
    for c in compass.values():
        if c["regime"]:
            regimes[c["regime"]] = regimes.get(c["regime"], 0) + 1
    return {
        "kpis": kpis, "compass": [compass[s] for s in tier1], "flow": flow, "regimes": regimes,
        "radar": radar[:25], "ideas": dict(ideas) if ideas else {},
        "report": {"id": rep["id"], "kind": rep["kind"], "title": rep["title"], "created_at": rep["created_at"],
                   "excerpt": _excerpt(rep["body_md"])} if rep else None,
    }
