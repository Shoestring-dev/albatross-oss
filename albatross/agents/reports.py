"""Report writer: a trader-facing brief every 4 hours and a daily wrap.

Code builds a compact summary from the DB (the Strategist's Tier-1 book, bias flips since the
window opened, trade ideas and critic verdicts, a 7-day scorecard against the naive benchmark,
market context); one LLM call only puts it into words. A report that names an asset the summary
doesn't contain is rejected rather than stored, since that asset can only have come from the
model's memory.
"""

import logging
import re
from datetime import UTC, datetime, timedelta

import orjson
from pydantic import BaseModel, Field

from albatross import db
from albatross.strategy import evidence
from albatross.agents.analysts import latest_prices, universe
from albatross.backtest import scorecard as idea_scorecard
from albatross.features.engine import TFS
from albatross.grading import scorecard
from albatross.llm import openrouter

log = logging.getLogger(__name__)

AGENT = "reports"
BUDGET_USD = 1.0
MAX_TOKENS = 16000          # medium effort: on an OpenRouter fallback the reasoning counts against it
WINDOWS = {"brief": timedelta(hours=4), "daily": timedelta(hours=24)}
FLIP_TFS = ["4h", "1d"]
MAX_FLIPS = 12
MAX_HEADLINES = 6
MAX_IDEAS = 25            # newest first; ideas_total still counts every one in the window
SCORECARD_DAYS = 7
# Tickers that are also everyday desk words; the unknown-asset guard doesn't count them.
COMMON_WORDS = {"AI", "APR", "AT", "ATH", "BE", "ME", "NOT", "NOW", "ON", "ONE", "UP"}
TICKER = re.compile(r"(?<![A-Za-z0-9])[A-Z0-9]{2,12}(?![A-Za-z0-9])")
# The model copies labels as given, so it gets words rather than snake_case ids (a brief once read
# "XRP pullback_in_uptrend", and Today's excerpt glued that into "pullbackinuptrend").
LABELS = {"ltf_analyst": "LTF analyst", "htf_analyst": "HTF analyst", "social_pulse": "X sentiment (Social Pulse)"}
SNAKE_ID = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


class Report(BaseModel):
    title: str = Field(description="<= 80 characters")
    body_md: str = Field(description="markdown: ## headings, paragraphs, - bullets, **bold**; no tables or links")


SYSTEM = """You write the market report for a crypto trader. The input is a JSON summary that code built
from the desk's database:
- tier1: the Strategist's current bias per asset and timeframe (-2 strong bearish .. +2 strong
  bullish, 0 neutral), its regime label, price and 24h change
- changes: bias changes on 4h/1d since the window opened (from -> to)
- ideas_total and ideas: counts of trade ideas logged in the window, and the newest of them with
  the devil's-advocate verdict (keep / downgrade / veto / none) and its reason; vetoed ideas are
  logged but not to be taken. graded_ideas: ideas the backtester closed in the window, when present
- idea_scorecard: the backtest of logged ideas (stop/target policy, net of fees, slippage and funding)
  per source and call status (proposed = let through by the critic, vetoed = blocked by it):
  resolved_in_window (exits inside the window) and all_time, each with win rate, avg R +- stderr,
  total R, avg net return, the always_long and random_side baselines on the same windows,
  vs_baselines and a note; critic_all_time compares proposed with vetoed avg R. All computed by code
- scorecard_7d: every source's graded calls over 7 days per timeframe: hit rate, what always-long
  would have scored, edge in points over the better naive side, and vs_naive (better / worse / noise)
- market: Fear & Greed, BTC ensemble score (-1..1) on 4h/1d/1w, high-impact headlines, the X mood
- evidence: what our own data says works (findings), each source's record on direction AND on coin
  picking (source_edges), today's factor rank: the strongest and weakest liquid coins with why, and
  scheduled events for the next 7 days (FOMC/CPI/jobs, and listings, unlocks, TGEs for ranked coins)

Write it like a desk note. Open with the evidence-backed view: the factor rank's strongest and weakest
coins (relative strength is the one thing our data shows an edge on; say it ranks coins against each
other, not the market's direction). Then what the book says now, what changed, the ideas and what the
critic said, what is scheduled this week that could move things, and how far to trust each source. Directional calls from a source whose record is "no
edge" or "too early to tell" are opinions, not signals: say so.
- Use only numbers and assets that appear in the summary. Never add prices, levels, tickers or
  events from memory, and never compute new figures.
- A source marked vs_naive "worse" is underperforming a naive always-long/short caller: say so
  plainly and tell the reader to discount it. "noise" means unproven, not good.
- Say plainly when ideas are not beating the baselines ("behind" or "within noise of baselines") or
  when too few are graded to tell (a note, "too few to tell", "too few to judge the critic"); never
  present a handful of graded ideas as a track record.
- Plain, direct, no hype, no hedging filler, no sizing or leverage advice.
- Write labels as the plain words given ("pullback in uptrend", "LTF analyst"), never snake_case.
  Edges are percentage points: "16.7 pts behind always-long", not "edge -16.7".
- Markdown with only: ## headings, short paragraphs, "- " bullets, **bold**. No tables, links,
  code or images.
- title: at most 80 characters, specific (lead with the main change), no date.
- Length: brief at most 250 words, daily at most 450 words."""


# ─── summary (deterministic) ────────────────────────────────────────────────
def _num(v: float | None, sig: int = 6) -> float | None:
    return None if v is None else float(f"{v:.{sig}g}")


def words(label: str | None) -> str | None:
    """A snake_case id (regime, source) as prose; free-form ids such as callers' handles stay as logged."""
    if not label or not SNAKE_ID.fullmatch(label):
        return label
    return LABELS.get(label, label.replace("_", " "))


async def _book(source: str, symbols: list[str], tfs: list[str], at: datetime) -> dict:
    """(symbol, tf) -> the newest call from `source` that was live at `at`, going by insert time.
    Rows are updated in place while their candle is open, so a past book is as it stood when written."""
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT DISTINCT ON (scope, tf) id, scope, tf, bias, score, regime, valid_until FROM signals
           WHERE source = $1 AND scope = ANY($2::text[]) AND tf = ANY($3::text[])
             AND as_of > $4::timestamptz - interval '14 days' AND created_at <= $4
           ORDER BY scope, tf, as_of DESC""", source, symbols, tfs, at)
    return {(r["scope"], r["tf"]): dict(r) for r in rows if r["valid_until"] > at}


async def _outcomes_table() -> bool:
    pool = await db.pool()
    return await pool.fetchval("SELECT to_regclass('public.trade_call_outcomes') IS NOT NULL")


async def summary(kind: str, now: datetime) -> tuple[dict, list[int]]:
    """-> (summary for the prompt, ids of the signals it reports)."""
    pool = await db.pool()
    start = now - WINDOWS[kind]
    syms = await universe()
    t1 = [r["symbol"] for r in await pool.fetch(
        "SELECT symbol FROM assets WHERE active AND tier = 1 ORDER BY volume_24h_usd DESC NULLS LAST")]
    symbols = [s["symbol"] for s in syms]
    tfs = list(TFS)

    source = "strategist"
    cur = await _book(source, symbols, tfs, now)
    if not cur:   # Strategist down or not yet live: report the ensemble rather than nothing
        source = "ensemble"
        cur = await _book(source, symbols, tfs, now)
    prev = await _book(source, symbols, FLIP_TFS, start)

    prices = await latest_prices(t1)
    ago = {r["symbol"]: r["mark_px"] for r in await pool.fetch(
        """SELECT DISTINCT ON (symbol) symbol, mark_px FROM deriv_snapshots
           WHERE venue = 'hyperliquid' AND symbol = ANY($1::text[]) AND mark_px > 0
             AND ts BETWEEN now() - interval '25 hours' AND now() - interval '24 hours'
           ORDER BY symbol, ts DESC""", t1)}
    tier1, ids = {}, []
    for sym in t1:
        calls = {tf: cur[(sym, tf)] for tf in tfs if (sym, tf) in cur}
        if not calls:
            continue
        ids += [c["id"] for c in calls.values()]
        p = prices.get(sym)
        tier1[sym] = {
            "price": _num(p), "change_24h_pct": round((p / ago[sym] - 1) * 100, 2) if p and ago.get(sym) else None,
            "regime": words(next((calls[t]["regime"] for t in ("4h", "1h") if t in calls and calls[t]["regime"]), None)),
            "bias": {tf: c["bias"] for tf, c in calls.items()},
        }

    sign = lambda b: (b > 0) - (b < 0)
    changes = sorted(
        ({"symbol": sym, "tf": tf, "from": p["bias"], "to": cur[(sym, tf)]["bias"]}
         for (sym, tf), p in prev.items()
         if (sym, tf) in cur and sign(cur[(sym, tf)]["bias"]) != sign(p["bias"])),
        key=lambda c: (-abs(c["to"] - c["from"]), symbols.index(c["symbol"])))[:MAX_FLIPS]

    ideas, totals = [], {"logged": 0, "vetoed": 0, "verdicts": {}}
    for r in await pool.fetch(
        """SELECT scope, direction, tf, source_id, ref_price, stop, targets, reward_risk, confidence, status,
                  thesis, critique FROM trade_calls
           WHERE logged_at >= $1 AND market <> 'dex' AND source_kind <> 'model'   -- micro-cap calls and Jev's paper book have their own lanes
           ORDER BY logged_at DESC""", start):
        crit = orjson.loads(r["critique"]) if r["critique"] else {}
        verdict = crit.get("verdict") or "none"
        totals["logged"] += 1
        totals["vetoed"] += r["status"] == "vetoed"
        totals["verdicts"][verdict] = totals["verdicts"].get(verdict, 0) + 1
        if len(ideas) < MAX_IDEAS:
            ideas.append({
                "symbol": r["scope"], "direction": r["direction"], "tf": r["tf"], "source": words(r["source_id"]),
                "entry": _num(r["ref_price"]), "stop": _num(r["stop"]), "targets": [_num(t) for t in r["targets"]],
                "reward_risk": r["reward_risk"], "confidence": r["confidence"], "status": r["status"],
                "thesis": (r["thesis"] or "")[:200], "verdict": verdict, "critic_reason": (crit.get("reason") or "")[:240],
            })

    out = {"kind": kind, "window_hours": int(WINDOWS[kind].total_seconds() // 3600),
           "as_of_utc": now.strftime("%Y-%m-%d %H:%M"), "book_source": words(source), "tier1": tier1,
           "changes": changes, "ideas_total": totals, "ideas": ideas}
    if await _outcomes_table():
        out["graded_ideas"] = [dict(r) for r in await pool.fetch(
            """SELECT t.scope AS symbol, t.direction, t.tf, o.exit_reason, round(o.net_return_pct::numeric, 2)::float
                      AS net_return_pct, round(o.r_multiple::numeric, 2)::float AS r_multiple
               FROM trade_call_outcomes o JOIN trade_calls t ON t.id = o.call_id
               WHERE o.policy = 'stop_target' AND o.status = 'graded' AND o.exit_at >= $1 AND t.market <> 'dex' AND t.source_kind <> 'model'
               ORDER BY o.exit_at""", start)]   # exit_at, not graded_at: a regrade rewrites graded_at
        out["idea_scorecard"] = await _idea_scorecard(start)

    order = {tf: i for i, tf in enumerate(TFS)}
    sc = []
    for r in sorted(await scorecard.by_source_tf(SCORECARD_DAYS), key=lambda r: (r["source"], order.get(r["tf"], 99))):
        edge, verdict = scorecard.vs_naive(r["hit_rate"], r["long_rate"], r["n"])
        sc.append({"source": words(r["source"]), "tf": r["tf"], "n": r["n"], "hit_rate_pct": round(r["hit_rate"] * 100, 1),
                   "always_long_pct": round(r["long_rate"] * 100, 1), "edge_pts": round(edge * 100, 1),
                   "vs_naive": verdict})
    out["scorecard_7d"] = sc
    out["market"] = await _market(start, now)
    out["evidence"] = await evidence.for_reports()
    return out, ids


async def _idea_scorecard(start: datetime) -> dict:
    """The Ideas tab's backtest panel (stop/target policy) for ideas that exited in the window and for
    all time, trimmed for the prompt."""
    policy = f"o.policy = 'stop_target' AND {idea_scorecard.NOT_DEX} AND c.source_kind <> 'model'"
    window = await idea_scorecard.rows(f"{policy} AND o.exit_at >= $1", (start,), by_tf=False)
    all_time = await idea_scorecard.rows(policy, by_tf=False)
    r2 = lambda v: None if v is None else round(v, 2)
    pct = lambda v: None if v is None else round(v * 100, 1)

    def base(b: dict) -> dict:
        return {"win_rate_pct": pct(b["win_rate"]), "avg_r": r2(b["avg_r"]), "avg_net_return_pct": r2(b["avg_net_return_pct"])}

    def row(r: dict, counts: bool) -> dict:
        out = {"source": words(r["source_id"]), "status": r["status"], "n_graded": r["n"],
               **({"n_open": r["n_open"], "n_ungradeable": r["n_ungradeable"]} if counts else {})}
        if r["n"]:   # with nothing graded every statistic is null; leave them out of the prompt
            out |= {"win_rate_pct": pct(r["win_rate"]), "avg_r": r2(r["avg_r"]), "avg_r_stderr": r2(r["avg_r_stderr"]),
                    "total_r": r2(r["total_r"]), "avg_net_return_pct": r2(r["avg_net_return_pct"]),
                    "always_long": base(r["always_long"]), "random_side": base(r["random"])}
        return out | {"vs_baselines": r["vs_baselines"]} | ({"note": r["note"]} if r["note"] else {})

    return {
        "min_n": idea_scorecard.MIN_N,
        "resolved_in_window": [row(r, False) for r in window],
        "all_time": [row(r, True) for r in all_time],
        "critic_all_time": [{"source": words(c["source_id"]), "proposed_n": c["proposed_n"], "vetoed_n": c["vetoed_n"],
                             "avg_r_diff": r2(c["avg_r_diff"]), "band": r2(c["band"]), "verdict": c["verdict"]}
                            for c in idea_scorecard.critic(all_time)],
    }


async def _market(start: datetime, now: datetime) -> dict:
    pool = await db.pool()
    fg = await pool.fetchrow("SELECT value, ts FROM metric_series WHERE metric = 'fear_greed' ORDER BY ts DESC LIMIT 1")
    btc = await _book("ensemble", ["BTC"], ["4h", "1d", "1w"], now)
    news = await pool.fetch(
        """SELECT title, impact, sentiment, assets, published_at FROM news_items
           WHERE impact >= 4 AND published_at >= $1 ORDER BY impact DESC, published_at DESC LIMIT $2""",
        start, MAX_HEADLINES)
    mood = await pool.fetchrow(
        """SELECT ts, narratives->>'market_mood' AS mood FROM social_snapshots
           WHERE source = 'grok_x' AND narratives ? 'market_mood' ORDER BY ts DESC LIMIT 1""")
    hours = lambda ts: round((now - ts).total_seconds() / 3600, 1)
    return {
        "fear_greed": {"value": fg["value"], "date": fg["ts"].strftime("%Y-%m-%d")} if fg else None,
        "btc_ensemble": {tf: {"score": _num(c["score"], 3), "bias": c["bias"]}
                         for tf in ("4h", "1d", "1w") if (c := btc.get(("BTC", tf)))} or None,
        "headlines": [{"title": r["title"][:160], "impact": r["impact"], "sentiment": r["sentiment"],
                       "assets": [a for a in r["assets"] if a != "MARKET"], "age_h": hours(r["published_at"])}
                      for r in news],
        "x_mood": {"text": mood["mood"], "age_h": hours(mood["ts"])} if mood else None,
    }


# ─── prompt + guard (pure) ──────────────────────────────────────────────────
def prompt(s: dict) -> tuple[str, str]:
    return SYSTEM, orjson.dumps(s).decode()


def _strings(x):
    if isinstance(x, dict):
        for k, v in x.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(x, list):
        for v in x:
            yield from _strings(v)
    elif x is not None:
        yield str(x)


def tickers(text: str, known: set[str]) -> set[str]:
    return {t for t in TICKER.findall(text) if t in known} - COMMON_WORDS


def check(rep: Report, s: dict, known: set[str]) -> str | None:
    """Reason to reject the report, or None."""
    if not rep.title.strip() or not rep.body_md.strip():
        return "empty title or body"
    extra = tickers(f"{rep.title}\n{rep.body_md}", known) - tickers("\n".join(_strings(s)), known)
    return f"names assets not in the summary: {', '.join(sorted(extra))}" if extra else None


# ─── run ────────────────────────────────────────────────────────────────────
async def run(kind: str) -> int:
    now = datetime.now(UTC)
    s, ids = await summary(kind, now)
    if not s["tier1"]:
        log.info("reports: no live calls for Tier 1, no %s written", kind)
        return 0
    system, user = prompt(s)
    try:
        rep, run_id = await openrouter.structured(
            AGENT, Report, system, user, model=openrouter.SOL, daily_budget_usd=BUDGET_USD, max_tokens=MAX_TOKENS,
            reasoning_effort="medium")
    except openrouter.BudgetExceeded as e:
        log.warning("%s", e)
        return 0
    pool = await db.pool()
    if why := check(rep, s, {r["symbol"] for r in await pool.fetch("SELECT symbol FROM assets")}):
        await pool.execute("UPDATE agent_runs SET error = $2 WHERE id = $1", run_id, f"rejected: {why}")
        raise RuntimeError(f"{kind} report rejected: {why}")
    await pool.execute(
        "INSERT INTO reports (kind, title, body_md, signal_ids) VALUES ($1, $2, $3, $4)",
        kind, rep.title.strip()[:200], rep.body_md.strip(), ids)
    return 1
