"""Dashboard API + static front end.  Run: uvicorn albatross.api.app:app --port 8080"""

import asyncio
import html
import logging
import re
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import orjson
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from albatross import controls, db, keys, ledger
from albatross.agents import graph, social_pulse
from albatross.deepend import reddit as deep_reddit
from albatross.alerts.rules import CADENCE, STALE_FLOOR, STALE_RUNS, hhmm
from albatross.api import agents, alpha, books, calendar, deep, gate, ingest, intel, lab, memo, micro, onchain, output, overview
from albatross.api import guard, setup, subs, telegram_setup, xsetup
from albatross.backtest import scorecard as idea_scorecard
from albatross.config import settings
from albatross.llm import xsearch
from albatross.features.engine import HORIZON_BARS
from albatross.grading import scorecard as sig_scorecard
from albatross.grading.scorecard import ROSE
from albatross.strategy import ensemble

STATIC = Path(__file__).parent / "static"
TFS = ["5m", "15m", "1h", "4h", "1d", "1w"]
MATRIX_SOURCES = ["quant", "ensemble", "strategist"]


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.pool()
    try:
        await keys.refresh()                     # keys saved on the Setup page
    except (OSError, asyncpg.PostgresError) as e:
        log.warning("startup: saved keys not loaded yet: %s", type(e).__name__)
    warm = asyncio.create_task(_warm())      # in the background: startup never waits on it, nor fails with it
    yield
    warm.cancel()
    await db.close()


async def _warm() -> None:
    """The Memecoins feed's two slow builds (every called token's first call and spark, 6-7 s each), done once at
    startup so the first reader after a deploy doesn't wait for them (api/micro.py keeps them fresh after)."""
    try:
        if await micro._ready():
            await asyncio.gather(micro.first_calls(), micro.sparks("t.status <> 'retired'"))
    except Exception as e:  # noqa: BLE001 - a cold cache is only slower
        log.info("startup warm-up skipped: %s", type(e).__name__)


log = logging.getLogger(__name__)
app = FastAPI(title="Albatross", lifespan=lifespan)


@app.middleware("http")
async def local_only(request, call_next):
    """With no site password, the whole dashboard answers only on the host names in ALLOWED_HOSTS (localhost by
    default), so a page on another site that points its own name at this machine (DNS rebinding) gets nothing back.
    With a password, the gate decides. The call feed has its own token."""
    if (not settings.site_password_hash and not request.url.path.startswith("/api/ingest/")
            and guard._hostname(request.headers.get("host", "")) not in guard.allowed_hosts()):
        return PlainTextResponse("Albatross answers only at localhost. To open it at another address, set a site "
                                 "password or add the host name to ALLOWED_HOSTS.", status_code=403)
    return await call_next(request)
app.include_router(gate.router)
app.include_router(ingest.router)
app.include_router(micro.router)
app.include_router(overview.router)
app.include_router(intel.router)
app.include_router(onchain.router)
app.include_router(calendar.router)
app.include_router(deep.router)
app.include_router(output.router)
app.include_router(agents.router)
app.include_router(books.router)
app.include_router(memo.router)
app.include_router(lab.router)
app.include_router(alpha.router)
app.include_router(setup.router)
app.include_router(subs.router)
app.include_router(xsetup.router)
app.include_router(telegram_setup.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _rows(records) -> list[dict]:
    return [dict(r) for r in records]


_cache: dict[str, tuple[float, object]] = {}
_cache_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)


async def _cached(key: str, ttl: float, fn):
    """Reuse an expensive read for `ttl` seconds; concurrent requests wait for one recompute."""
    async with _cache_locks[key]:
        hit = _cache.get(key)
        if hit is None or time.monotonic() - hit[0] > ttl:
            hit = _cache[key] = (time.monotonic(), await fn())
        return hit[1]


async def _latest_signals(scopes: list[str], source: str = "quant") -> list:
    """Newest signal per (scope, tf) from one source for the given scopes."""
    pool = await db.pool()
    return await pool.fetch(
        """SELECT DISTINCT ON (scope, tf) scope, tf, bias, confidence, score, ref_price, invalidation,
                  as_of, valid_until, drivers, regime
           FROM signals
           WHERE source = $2 AND scope = ANY($1::text[]) AND as_of > now() - interval '30 days'
           ORDER BY scope, tf, as_of DESC""",
        scopes, source,
    )


def _view(s) -> dict:
    """A signal as the dashboard shows it. Agents keep their thesis/rationale (and the Strategist its
    inputs, the ensemble its per-source parts) in drivers."""
    d = orjson.loads(s["drivers"]) if s["drivers"] else {}
    return {
        "bias": s["bias"], "confidence": s["confidence"], "score": s["score"],
        "ref_price": s["ref_price"], "invalidation": s["invalidation"],
        "as_of": s["as_of"], "valid_until": s["valid_until"], "regime": s["regime"],
        "thesis": d.get("thesis"), "relies_on": d.get("relies_on"), "parts": d.get("parts"),
    }


async def _sources_with_calls() -> list[str]:
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT src FROM unnest($1::text[]) AS src WHERE EXISTS (
             SELECT 1 FROM signals WHERE source = src AND as_of > now() - interval '30 days')""",
        MATRIX_SOURCES,
    )
    return [r["src"] for r in rows]


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(STATIC / "brand" / "favicon.ico", media_type="image/x-icon")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


async def _uncovered() -> dict[str, str]:
    """Timeframes no source of the ensemble covers any more (every one culled) -> why, in words (ensemble.uncovered)."""
    return ensemble.uncovered(await controls.rungs(), graph.LABEL)


@app.get("/api/matrix")
async def matrix(tiers: str = Query("1,2"), source: str | None = None):
    """source: quant | ensemble | strategist. Default: the Strategist once it has calls, else quant. The ensemble's
    and the Strategist's views leave out the timeframes no live source covers (`uncovered`): their last calls there
    came from sources since culled. The quant view is the model's own and shows everything."""
    tier_list = [int(t) for t in tiers.split(",") if t.strip()]
    available = await _cached("matrix_sources", 300, _sources_with_calls)
    if source not in MATRIX_SOURCES:
        source = "strategist" if "strategist" in available else "quant"
    pool = await db.pool()
    assets = await pool.fetch(
        """SELECT a.symbol, a.name, a.tier, a.primary_category, c.name AS category_name,
                  a.volume_24h_usd, a.market_cap_usd,
                  -- every sector the coin belongs to, so a sector drill-down lists all its members
                  array(SELECT ac.category_slug FROM asset_categories ac WHERE ac.asset_id = a.id) AS categories
           FROM assets a LEFT JOIN categories c ON c.slug = a.primary_category
           WHERE a.active AND a.tier = ANY($1::smallint[])""", tier_list,
    )
    sigs = await _latest_signals([a["symbol"] for a in assets], source)
    derivs = await pool.fetch(
        """WITH now_ AS (
             SELECT DISTINCT ON (symbol) symbol, funding_rate, oi_usd, mark_px, premium, day_volume_usd
             FROM deriv_snapshots WHERE ts > now() - interval '15 minutes' ORDER BY symbol, ts DESC),
           ago AS (
             SELECT DISTINCT ON (symbol) symbol, oi_usd, mark_px
             FROM deriv_snapshots WHERE ts BETWEEN now() - interval '25 hours' AND now() - interval '24 hours'
             ORDER BY symbol, ts DESC)
           SELECT n.*, a.oi_usd AS oi_usd_24h, a.mark_px AS mark_px_24h
           FROM now_ n LEFT JOIN ago a USING (symbol)"""
    )
    # Price and 24h change for every coin, not only those with a Hyperliquid mark: the latest hourly
    # candle (from the venue the features use) against the open of the candle 24 hours before it.
    # Hourly candles sync once an hour, so the read is shared for two minutes.
    candles = await _cached("matrix_candles", 120, lambda: pool.fetch(
        """WITH f AS (
             SELECT DISTINCT ON (symbol) symbol, venue FROM features
             WHERE tf = '1h' AND ts > now() - interval '1 day' ORDER BY symbol, ts DESC),
           c AS (SELECT symbol, venue, ts, open, close FROM candles WHERE tf = '1h' AND ts > now() - interval '26 hours'),
           n AS (SELECT DISTINCT ON (c.symbol) c.symbol, c.venue, c.ts, c.close
                 FROM c JOIN f USING (symbol, venue) ORDER BY c.symbol, c.ts DESC)
           SELECT n.symbol, n.close, p.open AS open_24h
           FROM n LEFT JOIN c p ON p.symbol = n.symbol AND p.venue = n.venue AND p.ts = n.ts - interval '24 hours'"""))
    unc = await _uncovered() if source in ("ensemble", "strategist") else {}
    by_scope: dict[str, dict] = {}
    for s in sigs:
        if s["tf"] not in unc:
            by_scope.setdefault(s["scope"], {})[s["tf"]] = _view(s)
    d_map = {d["symbol"]: dict(d) for d in derivs}
    c_map = {c["symbol"]: c for c in candles}
    out = []
    for a in assets:
        row = dict(a)
        row["tfs"] = tfs = by_scope.get(a["symbol"], {})
        row["derivs"] = d = d_map.get(a["symbol"])
        c = c_map.get(a["symbol"])
        px = (d or {}).get("mark_px") or (c and c["close"])
        px_24h = (d or {}).get("mark_px_24h") or (c and c["open_24h"])
        row["price"] = px or None
        row["chg_24h_pct"] = (px / px_24h - 1) * 100 if px and px_24h else None
        # The Strategist labels the regime itself; for other sources the UI derives one from the scores.
        if source == "strategist":
            row["regime"] = next((tfs[t]["regime"] for t in ("4h", "1h") if tfs.get(t, {}).get("regime")), None)
        out.append(row)
    out.sort(key=lambda r: (r["tier"], -(r["volume_24h_usd"] or 0)))
    names = {r["slug"]: r["name"] for r in await _cached("category_names", 300, lambda: pool.fetch(
        "SELECT slug, name FROM categories"))}
    rung = (await controls.rungs()).get(source)             # a source the culling system benched, shown for reference
    return {"tfs": TFS, "source": source, "available": available, "assets": out, "sector_names": names,
            "uncovered": unc, "source_rung": rung if rung in controls.OFF_ENSEMBLE else None}


@app.get("/api/sectors")
async def sectors():
    pool = await db.pool()
    cats = {r["slug"]: dict(r) for r in await pool.fetch("SELECT slug, name, priority FROM categories")}
    sigs = await _latest_signals([f"sector:{slug}" for slug in cats])
    by_sector: dict[str, dict] = {}
    for s in sigs:
        slug = s["scope"].removeprefix("sector:")
        drivers = orjson.loads(s["drivers"]) if s["drivers"] else {}
        by_sector.setdefault(slug, {})[s["tf"]] = {
            "bias": s["bias"], "score": s["score"], "breadth": s["confidence"],
            "members": drivers.get("members"), "as_of": s["as_of"], "valid_until": s["valid_until"],
        }
    # coins = every active coin tagged with the sector, the same set its drill-down lists. The score
    # itself is the quant roll-up of the coins whose primary sector it is (each coin counts once).
    coins = {r["category_slug"]: r["n"] for r in await pool.fetch(
        """SELECT ac.category_slug, count(*) AS n FROM asset_categories ac JOIN assets a ON a.id = ac.asset_id
           WHERE a.active GROUP BY 1""")}
    rows = [
        {"slug": slug, "name": cats.get(slug, {}).get("name", slug), "coins": coins.get(slug, 0), "tfs": tfs}
        for slug, tfs in by_sector.items()
    ]
    rows.sort(key=lambda r: cats.get(r["slug"], {}).get("priority", 999))
    return {"tfs": TFS, "sectors": rows}


@app.get("/api/news")
async def news(limit: int = 60, min_impact: int = 0, asset: str | None = None, sentiment: str | None = None):
    """sentiment: 'bull' (> 0), 'bear' (< 0), 'flat' (= 0), 'unscored'; anything else = all."""
    cond = {"bull": "sentiment > 0", "bear": "sentiment < 0", "flat": "sentiment = 0",
            "unscored": "impact IS NULL"}.get(sentiment or "", "TRUE")
    pool = await db.pool()
    rows = await pool.fetch(
        f"""SELECT id, source, url, title, published_at, assets, impact, sentiment, classification
            FROM news_items
            WHERE coalesce(impact, 0) >= $1 AND ($2::text IS NULL OR $2 = ANY(assets)) AND {cond}
            ORDER BY published_at DESC LIMIT $3""",
        min_impact, asset.upper() if asset else None, min(limit, 300),
    )
    return fold_duplicates(_rows(rows))


def fold_duplicates(rows: list[dict]) -> list[dict]:
    """One entry per headline, newest first: a story filed twice (the same outlet under two URLs, or
    syndicated across outlets) keeps its first row, and every later copy goes into its `also` list."""
    out, seen = [], {}
    for r in rows:
        r["title"] = html.unescape(r["title"])     # rows stored before ingest unescaped titles ("S&amp;P")
        key = re.sub(r"\W+", " ", r["title"].casefold()).strip()
        if key in seen:
            seen[key]["also"].append({"source": r["source"], "url": r["url"]})
            continue
        r["classification"] = orjson.loads(r["classification"]) if r["classification"] else None
        r["also"] = []
        seen[key] = r
        out.append(r)
    return out


@app.get("/api/asset/{symbol}")
async def asset(symbol: str):
    symbol = symbol.upper()
    pool = await db.pool()
    a = await pool.fetchrow("SELECT * FROM assets WHERE symbol = $1", symbol)
    if not a:
        raise HTTPException(404, "unknown asset")
    cats = await pool.fetch(
        "SELECT c.slug, c.name FROM asset_categories ac JOIN categories c ON c.slug = ac.category_slug "
        "JOIN assets x ON x.id = ac.asset_id WHERE x.symbol = $1", symbol,
    )
    feats = await pool.fetch(
        "SELECT DISTINCT ON (tf) tf, ts, venue, data FROM features WHERE symbol = $1 ORDER BY tf, ts DESC", symbol,
    )
    candles = await pool.fetch(
        """SELECT ts, open, high, low, close, volume FROM candles
           WHERE symbol = $1 AND tf = '1h' AND venue = (
             SELECT venue FROM features WHERE symbol = $1 AND tf = '1h' ORDER BY ts DESC LIMIT 1)
           ORDER BY ts DESC LIMIT 240""", symbol,
    )
    derivs = await pool.fetch(
        """SELECT time_bucket('1 hour', ts) AS ts, last(funding_rate, ts) AS funding_rate,
                  last(oi_usd, ts) AS oi_usd, last(mark_px, ts) AS mark_px
           FROM deriv_snapshots WHERE symbol = $1 AND ts > now() - interval '7 days'
           GROUP BY 1 ORDER BY 1""", symbol,
    )
    views = await pool.fetch(
        """SELECT DISTINCT ON (tf, source) tf, source, bias, confidence, score, ref_price, invalidation,
                  as_of, valid_until, drivers, regime
           FROM signals WHERE scope = $1 AND valid_until > now() AND source <> 'quant_replay'
           ORDER BY tf, source, as_of DESC""", symbol,
    )
    weights = await _cached("source_weights", 300, ensemble.source_weights)
    unc = await _uncovered()
    by_tf: dict[str, list] = {}
    for v in views:
        if v["tf"] in unc and v["source"] in ("ensemble", "strategist"):
            continue                        # made from sources since culled: no live source covers this timeframe
        w = weights.get((v["source"], v["tf"])) or {}
        by_tf.setdefault(v["tf"], []).append(
            {"source": v["source"], **_view(v), "weight": w.get("weight"), "graded": w.get("n"), "edge": w.get("edge")})
    features = {f["tf"]: {"ts": f["ts"], "venue": f["venue"], **orjson.loads(f["data"])} for f in feats}
    return {
        "asset": dict(a),
        "categories": _rows(cats),
        "features": features,
        "views": by_tf,
        "candles_1h": list(reversed(_rows(candles))),
        "derivs_7d": _rows(derivs),
        "news": await news(limit=25, asset=symbol, sentiment=None),
        "reddit": await _reddit(symbol),
        "uncovered": unc,
    }


async def _reddit(symbol: str) -> dict | None:
    """The coin's latest Reddit read (albatross/deepend/reddit.py reads tier 1-2 coins with big news), or None."""
    try:
        return await deep_reddit.section_major(symbol)
    except Exception as e:  # noqa: BLE001 - a missing read must never cost the coin page
        log.info("asset %s: reddit read failed: %s", symbol, e)
        return None


@app.get("/api/scorecard")
async def scorecard(source: str = "quant", days: int = 90):
    """Graded directional calls: hit rate by timeframe, by bias strength and by sector.
    days <= 0 means all history. long_rate = share of the same windows in which price rose,
    i.e. what an always-long caller would have scored: the benchmark a call has to beat.
    by_tf/by_bias/by_sector are asset calls; sector_calls are the sector roll-ups, graded against
    a sector index (grading/grader.py)."""
    pool = await db.pool()
    since = "TRUE" if days <= 0 else "s.as_of > now() - make_interval(days => $2)"
    graded = f"""FROM signals s JOIN outcomes o ON o.signal_id = s.id
                 WHERE s.source = $1 AND s.bias <> 0 AND o.hit IS NOT NULL AND ({since})"""
    base = graded + " AND s.scope NOT LIKE 'sector:%'"
    args = (source,) if days <= 0 else (source, days)
    by_tf = await pool.fetch(
        f"""SELECT s.tf, count(*) AS n, avg(o.hit::int) AS hit_rate, avg({ROSE}) AS long_rate,
                   avg(o.return_pct) AS avg_return_pct,
                   avg(o.mfe_pct) AS avg_mfe_pct, avg(o.mae_pct) AS avg_mae_pct,
                   avg(o.invalidated::int) AS invalidated_rate,
                   count(*) FILTER (WHERE abs(s.bias) = 2) AS n_strong,
                   avg(o.hit::int) FILTER (WHERE abs(s.bias) = 2) AS hit_rate_strong,
                   min(s.as_of) AS first_call, max(s.as_of) AS last_call
            {base} GROUP BY s.tf""", *args,
    )
    by_bias = await pool.fetch(
        f"""SELECT s.tf, s.bias, count(*) AS n, avg(o.hit::int) AS hit_rate, avg(o.return_pct) AS avg_return_pct
            {base} GROUP BY 1, 2""", *args,
    )
    by_sector = await pool.fetch(
        f"""SELECT a.primary_category AS sector, coalesce(c.name, a.primary_category) AS name, s.tf,
                   count(*) AS n, avg(o.hit::int) AS hit_rate, avg({ROSE}) AS long_rate,
                   avg(o.return_pct) AS avg_return_pct
            {base.replace("WHERE", "JOIN assets a ON a.symbol = s.scope LEFT JOIN categories c ON c.slug = a.primary_category WHERE")}
            GROUP BY 1, 2, 3 HAVING count(*) >= {sig_scorecard.MIN_N}""", *args,
    )
    sector_calls = await pool.fetch(
        f"""SELECT substr(s.scope, 8) AS sector, coalesce(c.name, substr(s.scope, 8)) AS name, s.tf,
                   count(*) AS n, avg(o.hit::int) AS hit_rate, avg({ROSE}) AS long_rate,
                   avg(o.return_pct) AS avg_return_pct
            {graded.replace("WHERE", "LEFT JOIN categories c ON c.slug = substr(s.scope, 8) WHERE")}
              AND s.scope LIKE 'sector:%' GROUP BY 1, 2, 3""", *args,
    )
    sources = [r["source"] for r in await pool.fetch(
        "SELECT DISTINCT s.source FROM signals s JOIN outcomes o ON o.signal_id = s.id ORDER BY 1")]
    order = {tf: i for i, tf in enumerate(TFS)}
    return {
        "source": source, "days": days, "sources": sources, "horizon_bars": HORIZON_BARS,
        "min_n": sig_scorecard.MIN_N, "by_tf": sorted(_rows(by_tf), key=lambda r: order.get(r["tf"], 99)),
        "by_bias": _rows(by_bias),
        "by_sector": _rows(by_sector),
        "sector_calls": _rows(sector_calls),
    }


@app.get("/api/scorecard/sources")
async def scorecard_sources(days: int = 90):
    """Every live source x timeframe against the naive always-long/short caller, judged exactly as the
    reports judge it (grading/scorecard.vs_naive): the Track record's "which sources beat naive" answer.
    days <= 0 means all history."""
    async def judged():
        rows = await sig_scorecard.by_source_tf(days if days > 0 else 36500)
        for r in rows:
            r["edge"], r["verdict"] = sig_scorecard.vs_naive(r["hit_rate"], r["long_rate"], r["n"])
        order = {tf: i for i, tf in enumerate(TFS)}
        return sorted(rows, key=lambda r: (r["source"], order.get(r["tf"], 99)))
    rows = await _cached(f"scorecard_sources:{days}", 60, judged)
    return {"days": days, "horizon_bars": HORIZON_BARS, "min_n": sig_scorecard.MIN_N, "rows": rows}


# Micro-cap group calls and bot entries (~600 a day, market 'dex') live in the same ledger as the
# Strategist's ideas; the Ideas page and its backtest show the majors unless asked (market=dex | all).
MARKET_WHERE = {"majors": "{c}.market <> 'dex'", "dex": "{c}.market = 'dex'", "all": "TRUE"}


@app.get("/api/ideas/scorecard")
async def ideas_scorecard(days: int = 90, by_tf: bool = True, market: str = "majors"):
    """Backtested trade ideas (trade_call_outcomes) by exit policy x source x call status (proposed
    vs vetoed), and x timeframe unless by_tf=false, net of costs; n counts graded outcomes only.
    days <= 0 means all history. Rules, baselines and verdicts: backtest/scorecard.py. `critic`
    compares proposed with vetoed ideas over the same period; `open` marks the ideas still open
    (unrealised, after exit costs)."""
    where, args = ("TRUE", ()) if days <= 0 else ("c.logged_at > now() - make_interval(days => $1)", (days,))
    where = f"{where} AND {MARKET_WHERE.get(market, MARKET_WHERE['majors']).format(c='c')}"
    rows = await idea_scorecard.rows(where, args, by_tf)
    totals = await idea_scorecard.rows(where, args, by_tf=False) if by_tf else rows
    return {"days": days, "min_n": idea_scorecard.MIN_N, "rows": rows, "critic": idea_scorecard.critic(totals),
            "open": await idea_scorecard.open_marks(where, args)}


@app.get("/api/social")
async def social(days: int = 7):
    """What X is saying (Social Pulse): the latest market pulse (mood, headlines, trending coins), each
    coin's latest read with its one-line summary and why it matters, the mood history and the change
    since the read before, the chart's bias next to it, and how much of the X allowance the last day used."""
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT ts, scope, sentiment, volume, narratives, samples FROM social_snapshots
           WHERE source = 'grok_x' AND ts > now() - make_interval(days => $1) ORDER BY scope, ts""",
        min(max(days, 1), 90),
    )
    names = {f"sector:{r['slug']}": r["name"] for r in await pool.fetch("SELECT slug, name FROM categories")}
    scopes: dict[str, dict] = {}
    market, mood, mood_ts = None, None, None
    for r in rows:
        meta = orjson.loads(r["narratives"]) if r["narratives"] else {}
        if r["scope"] == social_pulse.MARKET:
            market = {"ts": r["ts"], "sentiment": r["sentiment"], "mood": meta.get("market_mood"),
                      "headlines": meta.get("headlines") or [], "trending": meta.get("trending") or [],
                      "coins_read": meta.get("coins_read") or [],
                      "trenches": meta.get("trenches") or [], "memecoin_narratives": meta.get("memecoin_narratives") or []}
            mood, mood_ts = meta.get("market_mood"), r["ts"]
            continue
        s = scopes.setdefault(r["scope"], {
            "scope": r["scope"], "kind": "sector" if r["scope"].startswith("sector:") else "asset",
            "name": names.get(r["scope"], r["scope"]), "history": []})
        s["history"].append({"ts": r["ts"], "sentiment": r["sentiment"], "volume": r["volume"]})
        s["latest"] = {
            "ts": r["ts"], "sentiment": r["sentiment"], "volume": r["volume"],
            "summary": meta.get("summary") or "", "why_it_matters": meta.get("why_it_matters") or "",
            "notable": bool(meta.get("notable")), "picked_for": meta.get("picked_for") or [],
            "crowding": meta.get("crowding"), "narratives": meta.get("narratives") or [],
            "posts_reviewed": meta.get("posts_reviewed"),
            "positioning": meta.get("positioning") or [], "catalysts": meta.get("catalysts") or [],
            "samples": orjson.loads(r["samples"]) if r["samples"] else [],
        }
        # Reads from before the hourly pulse carried the market mood on every coin row.
        if market is None and meta.get("market_mood") and (mood_ts is None or r["ts"] >= mood_ts):
            mood, mood_ts = meta["market_mood"], r["ts"]
    quant: dict[str, dict] = {}
    for q in await _latest_signals(list(scopes)):
        quant.setdefault(q["scope"], {})[q["tf"]] = {"bias": q["bias"], "score": q["score"], "valid_until": q["valid_until"]}
    for sc, s in scopes.items():
        s["quant"] = quant.get(sc, {})
        h = s["history"]
        s["previous"] = h[-2]["sentiment"] if len(h) > 1 else None
    out = sorted(scopes.values(), key=lambda s: (s["kind"] != "asset", s["name"]))
    spent = await pool.fetchrow(                 # in the X lane's dollars (llm/xsearch.py)
        f"""SELECT count(*) FILTER (WHERE ok) AS reads, coalesce(sum({xsearch.spend_sql()}), 0) AS usd
            FROM agent_runs WHERE agent = $1 AND started_at > now() - interval '24 hours'""", social_pulse.AGENT)
    return {"market": market, "market_mood": mood, "market_mood_ts": mood_ts, "scopes": out,
            "allowance": {"reads_24h": spent["reads"], "spent_24h": round(spent["usd"], 2),
                          "daily": round(xsearch.week(settings.social_weekly_usd) / 7, 2),
                          "lane": await xsearch.lane_now()}}


ALERTS_WATCHDOG = timedelta(minutes=15)


@app.get("/api/alerts")
async def alerts(days: int = 7):
    """Open alerts (critical first) and incidents resolved in the last `days`. The monitor can't report
    its own death, so an `alerts` job that has stopped succeeding shows here as one more open alert."""
    pool = await db.pool()
    if not await pool.fetchval("SELECT to_regclass('public.alerts') IS NOT NULL"):
        return {"open": [], "resolved": [], "checked_at": None}
    cols = "id, key, kind, severity, title, detail, first_seen, last_seen, resolved_at, since"
    open_ = _rows(await pool.fetch(
        f"SELECT {cols} FROM alerts WHERE resolved_at IS NULL ORDER BY severity = 'critical' DESC, first_seen"))
    resolved = _rows(await pool.fetch(
        f"""SELECT {cols} FROM alerts WHERE resolved_at > now() - make_interval(days => $1)
            ORDER BY resolved_at DESC LIMIT 50""", min(max(days, 1), 90)))
    checked = await pool.fetchval("SELECT last_ok FROM ingest_health WHERE job = 'alerts'")
    now = datetime.now(UTC)
    if checked is not None and now - checked > ALERTS_WATCHDOG:
        open_.insert(0, {
            "id": None, "key": "alerts_stale", "kind": "alerts_stale", "severity": "critical",
            "title": "Alert checks have stopped",
            "detail": f"The alerts job last succeeded at {hhmm(checked, now)}: the ingest scheduler may be down, "
                      "and nothing else on this page is being watched or notified.",
            "first_seen": checked, "last_seen": now, "resolved_at": None, "since": checked})
    return {"open": open_, "resolved": resolved, "checked_at": checked}


def job_state(job: dict, now: datetime) -> str:
    """ok | late | failing for a scheduled job, by the rule the alerts use (alerts/rules.py): late = no success
    within three cadences (15 min at least). Manual and one-off jobs are 'manual': a failed backfill isn't
    an ongoing problem."""
    cad = CADENCE.get(job["job"])
    if cad is None:
        return "manual"
    if job["consecutive_errors"]:
        return "failing"
    last_ok = job["last_ok"]
    return "late" if last_ok is None or now - last_ok > max(STALE_RUNS * cad, STALE_FLOOR) else "ok"


async def _health_counts() -> dict:
    pool = await db.pool()
    return dict(await pool.fetchrow(
        """SELECT (SELECT count(*) FROM assets WHERE active) AS assets,
                  (SELECT count(*) FROM candles) AS candles,
                  (SELECT count(*) FROM deriv_snapshots) AS deriv_snapshots,
                  (SELECT count(*) FROM news_items) AS news,
                  (SELECT count(*) FROM features) AS features,
                  (SELECT count(*) FROM signals) AS signals,
                  (SELECT min(ts) FROM deriv_snapshots) AS collecting_since"""
    ))


@app.get("/api/health")
async def health():
    """Jobs with their cadence and state, today's agent spend, and row counts (counting the candles takes
    most of a second, and every open page asks each minute, so the counts are reused for 5 minutes)."""
    pool = await db.pool()
    now = datetime.now(UTC)
    jobs = _rows(await pool.fetch("SELECT * FROM ingest_health ORDER BY job"))
    idle = await xsearch.idle_jobs()            # the X jobs while no X search lane is on
    for j in jobs:
        cad = CADENCE.get(j["job"])
        j["every_s"] = cad.total_seconds() if cad else None
        # a job paused on the Output page, or a retired agent's, is skipped on purpose: never "late"
        j["state"] = "paused" if j["job"] in idle or await controls.paused(j["job"]) else job_state(j, now)
    spend = await pool.fetch(
        """SELECT agent, count(*) AS calls, count(*) FILTER (WHERE NOT ok) AS failures,
                  coalesce(sum(cost_usd), 0) AS cost_usd
           FROM agent_runs WHERE started_at >= date_trunc('day', now()) GROUP BY agent ORDER BY agent"""
    )
    counts = await _cached("health_counts", 300, _health_counts)
    return {"now": now, "jobs": jobs, "agent_spend_today": _rows(spend), "counts": counts,
            "fomo_verdict": await _fomo_verdict()}


async def _fomo_verdict() -> dict | None:
    """The FOMO subscription test's latest daily report (albatross/deepend/fomo_verdict.py), trimmed to what the
    System page shows; None before its first run."""
    pool = await db.pool()
    try:
        row = await pool.fetchrow("SELECT computed_at, report FROM de_fomo_verdict ORDER BY day DESC LIMIT 1")
    except asyncpg.UndefinedTableError:
        return None
    if row is None:
        return None
    r = orjson.loads(row["report"]) if isinstance(row["report"], str | bytes) else row["report"]
    keep = ("decision", "fomo", "telegram_all", "telegram_buy", "vs_random", "vs_telegram_all", "lift", "bands",
            "unpriced", "min_tokens", "since")
    return {"computed_at": row["computed_at"], **{k: r.get(k) for k in keep},
            "credits": {k: (r.get("credits") or {}).get(k) for k in ("days_full", "days_short", "lookup_coverage",
                                                                    "running_out", "daily_cap")}}


@app.get("/api/ideas")
async def ideas(status: str | None = None, source: str | None = None, limit: int = 200, market: str = "majors",
                book: str = "ideas"):
    """The call ledger, newest first. Joins the backtester's stop/target grading once its table exists."""
    mkt = MARKET_WHERE.get(market, MARKET_WHERE["majors"]).format(c="t")
    # Jev's paper book (source_kind 'model', albatross/agents/jev_scout.py) is its own list: book=jev shows it.
    mkt += {"ideas": " AND t.source_kind <> 'model'", "jev": " AND t.source_kind = 'model'"}.get(book, "")
    pool = await db.pool()
    graded = await pool.fetchval("SELECT to_regclass('public.trade_call_outcomes') IS NOT NULL")
    outcome, join = ("NULL AS outcome", "") if not graded else (
        """o.status AS outcome, o.exit_reason, o.exit_price, o.net_return_pct, o.r_multiple,
           o.mfe_pct, o.mae_pct, o.graded_at""",
        "LEFT JOIN trade_call_outcomes o ON o.call_id = t.id AND o.policy = 'stop_target'")
    rows = await pool.fetch(
        f"""SELECT t.id, t.logged_at, t.source_kind, t.source_id, t.scope, t.market, t.direction, t.tf,
                   t.entry_type, t.ref_price, t.stop, t.targets, t.horizon_until, t.reward_risk, t.confidence,
                   t.status, t.thesis, t.evidence, t.critique, t.parent_id, {outcome}
            FROM trade_calls t {join}
            WHERE ($1::text IS NULL OR t.status = $1) AND ($2::text IS NULL OR t.source_id = $2)
              AND {mkt}
            ORDER BY t.logged_at DESC, t.id DESC LIMIT $3""",
        status or None, source or None, min(max(limit, 1), 500),
    )
    out = _rows(rows)
    for r in out:
        for k in ("evidence", "critique"):
            r[k] = orjson.loads(r[k]) if r[k] else None
    sources = await pool.fetch(
        f"SELECT source_kind, source_id, count(*) AS n FROM trade_calls t WHERE {mkt} GROUP BY 1, 2 ORDER BY 3 DESC")
    return {"graded": graded, "ideas": out, "sources": _rows(sources)}


async def _verify_ledger() -> dict:
    n, broken = await ledger.verify()
    return {"calls": n, "broken_id": broken, "checked_at": datetime.now(UTC)}


@app.get("/api/ledger/verify")
async def ledger_verify():
    """Rehashes the whole ledger, so the result is reused for 5 minutes."""
    return await _cached("ledger_verify", 300, _verify_ledger)


@app.get("/api/reports")
async def reports(limit: int = 50):
    pool = await db.pool()
    rows = await pool.fetch(
        "SELECT id, kind, title, created_at FROM reports ORDER BY created_at DESC, id DESC LIMIT $1",
        min(max(limit, 1), 200),
    )
    return _rows(rows)


@app.get("/api/reports/{report_id}")
async def report(report_id: int):
    pool = await db.pool()
    r = await pool.fetchrow(
        "SELECT id, kind, title, body_md, created_at, signal_ids FROM reports WHERE id = $1", report_id)
    if not r:
        raise HTTPException(404, "unknown report")
    return dict(r)
