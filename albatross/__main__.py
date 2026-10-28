"""Albatross CLI.

    python -m albatross migrate      apply DB migrations
    python -m albatross universe     rebuild the asset universe
    python -m albatross backfill     one-off deep history pull (safe to re-run)
    python -m albatross run          24/7 scheduler (migrates + bootstraps on start)
    python -m albatross host         host-side worker for subscription CLIs (Grok, Claude)
    python -m albatross status       ingest health + row counts
    python -m albatross job <name>   run a single job once (see `jobs` below)
"""

import asyncio
import logging
import signal
import sys
from datetime import UTC, datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from albatross import db, http, keys, ledger
from albatross.agents import (
    ceo,
    cull,
    analysts,
    jev_analyst,
    jev_scout,
    macro,
    news_scout,
    oracle,
    reports,
    social_pulse,
    strategist,
    strategy_researcher,
)
from albatross import alpha
from albatross.alerts import monitor
from albatross.backtest import books, ideas
from albatross.llm import codex, xsearch
from albatross.events import listings as event_listings
from albatross.events import macro as event_macro
from albatross.events import polymarket as event_polymarket
from albatross.events import news_extract, oracle_calendar
from albatross.features import engine
from albatross.grading import grader
from albatross.ingest import candles, cex_derivs, market, news
from albatross.ingest import macro as macro_data
from albatross.ingest import onchain
from albatross.ingest.health import record_run, tracked
from albatross.lab import run as lab
from albatross.alerts import bot as alert_bot
from albatross.deepend import registry as deep
from albatross.micro import bot_txs as micro_bot_txs
from albatross.micro import ledger_feed as micro_ledger
from albatross.micro import pipeline as micro
from albatross.micro import reconcile as micro_reconcile
from albatross.strategy import ensemble, factor_rank
from albatross.telegram import listener as telegram_listener
from albatross.universe import builder

log = logging.getLogger("albatross")


def watch_overlaps(sched: AsyncIOScheduler) -> None:
    """A run skipped because the job's last run is still going goes into job_runs as 'overlap' (the Output page's
    "can't keep up"); APScheduler only logs it."""
    from datetime import UTC, datetime

    from apscheduler.events import EVENT_JOB_MAX_INSTANCES

    def on_skip(event) -> None:
        job = sched.get_job(event.job_id)
        name = job.name if job else str(event.job_id)
        asyncio.get_running_loop().create_task(record_run(name, datetime.now(UTC), None, "overlap"))

    sched.add_listener(on_skip, EVENT_JOB_MAX_INSTANCES)

ALL_TIERS = [1, 2, 3]
CORE = [1, 2]

# Every job is named so its health shows up in ingest_health / the dashboard.
jobs = {
    "universe": tracked("universe")(lambda: _count(builder.build())),
    "derivs_hl": tracked("derivs_hl")(market.hyperliquid_contexts),
    "candles_hl_5m": tracked("candles_hl_5m")(lambda: candles.sync_hyperliquid("5m", CORE)),
    "candles_hl_1h": tracked("candles_hl_1h")(lambda: candles.sync_hyperliquid("1h", ALL_TIERS)),
    "candles_hl_1d": tracked("candles_hl_1d")(lambda: candles.sync_hyperliquid("1d", ALL_TIERS)),
    "candles_okx_1h": tracked("candles_okx_1h")(lambda: candles.sync_okx("1h", ALL_TIERS)),
    "candles_okx_1d": tracked("candles_okx_1d")(lambda: candles.sync_okx("1d", ALL_TIERS)),
    "candles_cb_5m": tracked("candles_cb_5m")(lambda: candles.sync_coinbase("5m", [1])),
    "candles_cb_1h": tracked("candles_cb_1h")(lambda: candles.sync_coinbase("1h", [1])),
    "candles_cb_1d": tracked("candles_cb_1d")(lambda: candles.sync_coinbase("1d", [1])),
    "news": tracked("news")(news.ingest),
    "fear_greed": tracked("fear_greed")(market.fear_greed),
    "derivs_cex": tracked("derivs_cex")(cex_derivs.sync),
    "features_ltf": tracked("features_ltf")(lambda: engine.run(["5m", "15m"], CORE)),
    "features_htf": tracked("features_htf")(lambda: engine.run(["1h", "4h"], ALL_TIERS)),
    "features_daily": tracked("features_daily")(lambda: engine.run(["1d", "1w"], ALL_TIERS)),
    # Daily cross-sectional ranking on the factors that held up in our history (docs/RESEARCH-EDGE.md).
    "factor_rank": tracked("factor_rank")(factor_rank.run),
    "factor_rank_backfill": lambda: factor_rank.run(backfill_days=400),
    "news_scout": tracked("news_scout")(news_scout.run),
    "ltf_analyst": tracked("ltf_analyst")(analysts.ltf),
    "htf_analyst": tracked("htf_analyst")(analysts.htf),
    "flow_analyst": tracked("flow_analyst")(analysts.flow),
    "macro_data": tracked("macro_data")(macro_data.sync),
    # MVRV and the MVRV Z-score for BTC and ETH from Coin Metrics community data (albatross/ingest/onchain.py).
    "onchain_mvrv": tracked("onchain_mvrv")(onchain.sync),
    "macro_analyst": tracked("macro_analyst")(macro.run),
    "ensemble": tracked("ensemble")(ensemble.run),
    "strategist": tracked("strategist")(strategist.run),
    # TypeSafe Jev: calibrated direction probabilities per coin and timeframe, graded, not in the ensemble yet.
    "jev_analyst": tracked("jev_analyst")(jev_analyst.run),
    # Jev scores candidate trades built from our own work every 5 min; a paper book takes the best.
    "jev_scout": tracked("jev_scout")(jev_scout.run),
    "jev_grade": tracked("jev_grade")(jev_scout.grade),
    "agent_review": tracked("agent_review")(cull.review),
    "ledger_verify": tracked("ledger_verify")(lambda: _verify_ledger()),
    "reports_brief": tracked("reports_brief")(lambda: reports.run("brief")),
    "reports_daily": tracked("reports_daily")(lambda: reports.run("daily")),
    "grader": tracked("grader")(grader.run),
    "grade_ideas": tracked("grade_ideas")(ideas.run),
    # Paper books: what following each kind of call would have made, in dollars, and whether the dossier picks
    # winners (albatross/backtest/books.py).
    "paper_books": tracked("paper_books")(books.run),
    "codex_limits": tracked("codex_limits")(codex.read_limits),
    "ceo": tracked("ceo")(ceo.run),
    # The strategy lab (albatross/lab/run.py): new strategies tested hourly against a bar that rises with every one
    # tried, the survivors paper-traded; the researcher proposes more once a day.
    "strategy_lab": tracked("strategy_lab")(lab.run),
    "strategy_researcher": tracked("strategy_researcher")(strategy_researcher.run),
    # The Alpha section (review of 2026-10-09): loss avoidance and factor edge, recomputed hourly after the lab.
    "alpha_report": tracked("alpha_report")(alpha.run),
    "alpha_live": tracked("alpha_live")(alpha.run_live),
    "alerts": tracked("alerts")(monitor.run),
    # Events calendar, structured feeds (docs/SPEC-EVENTS.md); dry run: python -m albatross.events <feed>.
    "events_macro": tracked("events_macro")(event_macro.run),
    "events_listings": tracked("events_listings")(event_listings.run),
    "events_polymarket": tracked("events_polymarket")(event_polymarket.run),
    # Micro-cap lane (docs/SPEC-MICROCAPS.md M1); tiers and rate-limit arithmetic in albatross/micro/pipeline.py.
    "micro_pools": tracked("micro_pools")(micro.discover_pools),
    "micro_snapshots": tracked("micro_snapshots")(micro.snapshots),
    "micro_candles": tracked("micro_candles")(micro.candles),
    "micro_reconcile": tracked("micro_reconcile")(micro_reconcile.run),
    "micro_retire": tracked("micro_retire")(micro.retire),
    # Every group call and bot entry into the call ledger, at our own price (albatross/micro/ledger_feed.py).
    "micro_ledger": tracked("micro_ledger")(micro_ledger.run),
    "micro_bot_txs": tracked("micro_bot_txs")(micro_bot_txs.run),
    "social_pulse": tracked("social_pulse")(social_pulse.run),
    "oracle_watch": tracked("oracle_watch")(oracle.watch),
    "oracle_daily": tracked("oracle_daily")(oracle.daily_thesis),
    "oracle_weekly": tracked("oracle_weekly")(oracle.weekly_review),
    # Events calendar, LLM extractors (albatross/events/): dated events from the news, and the Oracle's daily web
    # research for the next 30 days. The _dry variants print what they would write and write nothing.
    "events_news": tracked("events_news")(news_extract.run),
    "events_news_dry": lambda: news_extract.run(write=False),
    "events_oracle": tracked("events_oracle")(oracle_calendar.run),
    "events_oracle_dry": lambda: oracle_calendar.run(write=False),
    # Manual/one-off: walk-forward quant history for an immediate baseline scorecard.
    "replay": tracked("replay")(lambda: engine.replay(["5m", "15m", "1h", "4h", "1d", "1w"], CORE)),
    # Manual: recompute every idea outcome, e.g. after a cost-model change.
    "regrade_ideas": tracked("regrade_ideas")(lambda: ideas.run(regrade=True)),
}
# The Deep End's jobs, each tracked in ingest_health under its own name.
jobs.update({name: tracked(name)(fn) for name, fn in {**deep.JOBS, **deep.HOST_JOBS}.items()})


async def _verify_ledger() -> int:
    n, broken = await ledger.verify()
    if broken is not None:
        raise RuntimeError(f"trade_calls hash chain broken at id {broken} (after {n} good rows)")
    return n


async def _count(coro) -> int:
    summary = await coro
    return summary["assets"]


async def universe_ready() -> bool:
    pool = await db.pool()
    return bool(await pool.fetchval("SELECT count(*) FROM assets WHERE active"))


async def bootstrap() -> None:
    await db.migrate()
    if not await universe_ready():
        log.info("empty universe — building it first (takes a few minutes on the keyless CoinGecko tier)")
        if await jobs["universe"]() is None:
            raise SystemExit("universe build failed; see ingest_health")


# Run at start rather than waiting for their first cron tick: market data that is minutes or a day stale otherwise.
CATCH_UP = ["derivs_hl", "news", "candles_hl_5m", "candles_cb_5m", "candles_hl_1h", "candles_okx_1h", "candles_cb_1h",
            "candles_hl_1d", "candles_okx_1d", "candles_cb_1d", "fear_greed", "onchain_mvrv"]


async def backfill() -> None:
    await bootstrap()
    await tracked("fear_greed_backfill")(lambda: market.fear_greed(limit=0))()
    # Order matters little: every sync resumes from what's stored.
    for name in ["candles_hl_1d", "candles_hl_1h", "candles_hl_5m", "candles_okx_1d", "candles_okx_1h",
                 "candles_cb_1d", "candles_cb_1h", "candles_cb_5m", "derivs_hl", "news",
                 "features_daily", "features_htf", "features_ltf", "replay", "grader"]:
        log.info("backfill: %s", name)
        await jobs[name]()


async def run() -> None:
    await db.migrate()
    await keys.refresh()                        # keys saved on the Setup page, before any job reads settings
    ready = await universe_ready()
    sched = AsyncIOScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1,
                                                           "misfire_grace_time": 120})

    def add(fn, trigger, **kw):
        # A catch-up is the job's own first run (same id), so it can never overlap its next cron tick: a separate
        # one-off job once re-downloaded a year of 1h candles beside it and starved derivs_hl for 50 minutes.
        if (name := next((n for n in CATCH_UP if jobs[n] is fn), None)) is not None:
            kw.setdefault("id", name)
            if ready:
                kw.setdefault("next_run_time", datetime.now(UTC))
        return sched.add_job(fn, trigger, **kw)

    async def first_universe() -> None:
        """A first start builds the coin list in the background (keyless CoinGecko takes up to ~15 minutes) while the
        jobs that need none run; the catch-ups follow it. A failed build tries again in 10 minutes."""
        if await jobs["universe"]() is None:
            sched.add_job(first_universe, "date", run_date=datetime.now(UTC) + timedelta(minutes=10))
            return
        for name in CATCH_UP:
            sched.modify_job(name, next_run_time=datetime.now(UTC))

    watch_overlaps(sched)
    if not ready:
        log.info("empty universe: building it in the background (a few minutes on the keyless CoinGecko tier)")
        sched.add_job(first_universe, next_run_time=datetime.now(UTC), id="universe_first")
    add(keys.refresh, "interval", seconds=30, id="keys_refresh")          # untracked: it isn't a data job
    add(jobs["derivs_hl"], "cron", second=5)                               # every minute
    add(jobs["candles_hl_5m"], "cron", minute="*/5", second=20)
    add(jobs["candles_cb_5m"], "cron", minute="*/5", second=30)
    add(jobs["news"], "cron", minute="*/3", second=40)
    add(jobs["candles_hl_1h"], "cron", minute=1, second=0)
    add(jobs["candles_okx_1h"], "cron", minute=2, second=0)
    add(jobs["candles_cb_1h"], "cron", minute=3, second=0)
    add(jobs["fear_greed"], "cron", minute=15)
    add(jobs["candles_hl_1d"], "cron", hour=0, minute=5)
    add(jobs["candles_okx_1d"], "cron", hour=0, minute=10)
    add(jobs["candles_cb_1d"], "cron", hour=0, minute=15)
    add(jobs["universe"], "cron", day_of_week="sun", hour=0, minute=30)
    # Features run after the candle syncs they depend on.
    add(jobs["features_ltf"], "cron", minute="1-59/5", second=30)
    add(jobs["features_htf"], "cron", minute=8)
    add(jobs["features_daily"], "cron", hour=0, minute=25)
    add(jobs["factor_rank"], "cron", hour=0, minute=30)
    add(jobs["news_scout"], "cron", minute="*/5", second=55)
    # Dated events from the news read since the last pass (a few cents a day; $1 cap in news_extract.py).
    add(jobs["events_news"], "cron", minute="14,44", second=0)
    add(jobs["grader"], "cron", minute="4-59/5", second=10)
    add(jobs["grade_ideas"], "cron", minute="2-59/5", second=20)
    add(jobs["paper_books"], "cron", minute="4-59/15", second=0)          # after a grade_ideas pass
    add(jobs["codex_limits"], "cron", minute="*/5", second=20)            # the ChatGPT allowance, for the pacer
    add(jobs["ceo"], "cron", hour="0,12", minute=55)                      # after the paper books' :49 pass
    add(jobs["strategy_lab"], "cron", minute=21)
    add(jobs["strategy_researcher"], "cron", hour=6, minute=10)           # its proposals are tested at 06:21
    add(jobs["alpha_report"], "cron", minute=24)
    add(jobs["alpha_live"], "cron", minute="4-59/5", second=40)            # after each grader pass (:10)
    # Watches every other job, the LLM agents, Tier-1 candles and the call feed (albatross/alerts).
    add(jobs["alerts"], "cron", minute="*/5", second=45)
    # Analysts read the features the jobs above just wrote (LTF features land at :01:30 past each 5m;
    # 4h at :08; daily/weekly at 00:25, hence HTF at :30 on the 4h boundary).
    # Every 30 min: ~$0.10/run across Tier 1+2, so ~$4.90/day, inside ltf_analyst's $5 cap.
    add(jobs["ltf_analyst"], "cron", minute="3,33", second=0)
    add(jobs["htf_analyst"], "cron", hour="0-23/4", minute=30)
    # Coinalyze pull takes ~23 min at the free limit; the Flow analyst reads it at :30.
    add(jobs["derivs_cex"], "cron", minute=1, second=30)
    add(jobs["flow_analyst"], "cron", minute=30)
    # Ensemble after each LTF analyst pass; Strategist once flow + Coinalyze are in.
    add(jobs["ensemble"], "cron", minute="8,38", second=0)
    add(jobs["strategist"], "cron", minute=42, second=0)
    add(jobs["jev_analyst"], "cron", minute=36, second=0)
    add(jobs["jev_scout"], "cron", minute="2-59/5", second=50)         # after each 5m candle's features (:01:30)
    add(jobs["jev_grade"], "cron", minute="7-59/10", second=0)
    add(jobs["agent_review"], "cron", day_of_week="thu", hour=1, minute=0)    # the culling system's weekly review
    add(jobs["ledger_verify"], "cron", hour=0, minute=55)
    # FRED posts most daily series by ~20:00 UTC and ETF flows land after the US close; twice a day
    # catches both. The Macro analyst follows each pull.
    add(jobs["macro_data"], "cron", hour="1,14", minute=40)
    add(jobs["onchain_mvrv"], "cron", hour="3,15", minute=5)
    add(jobs["macro_analyst"], "cron", hour="2,15", minute=5)
    # Reports read the Strategist's :42 pass; the daily wrap runs just after the 00:50 brief.
    add(jobs["reports_brief"], "cron", hour="0-23/4", minute=50)
    add(jobs["reports_daily"], "cron", hour=0, minute=52)
    # Micro-cap lane: each job picks the tokens its tier makes due (rules.TIERS).
    add(jobs["micro_snapshots"], "cron", second="*/10")
    add(jobs["micro_pools"], "cron", second=45)
    add(jobs["micro_candles"], "cron", second=15)
    add(jobs["micro_reconcile"], "cron", minute=20)
    add(jobs["micro_retire"], "cron", minute=50)
    # A few seconds after a snapshot pass, so a hot token's price is inside its 15 s budget when read.
    add(jobs["micro_ledger"], "cron", second=8)
    add(jobs["micro_bot_txs"], "cron", second=40)                         # when each bot sale happened, on chain
    # Events calendar: FOMC + FRED release dates daily, exchange notices and market diffs every 10 min,
    # Polymarket launch odds hourly.
    add(jobs["events_macro"], "cron", hour=1, minute=10)
    add(jobs["events_listings"], "cron", minute="*/10", second=25)
    add(jobs["events_polymarket"], "cron", minute=17)
    # Oracle (web research on the ChatGPT plan or OpenRouter): triggers every 10 min, the
    # daily thesis after the daily candles and features (00:25), the weekly review on Sunday morning. <= 12
    # calls/day, enforced in oracle.py. Its calendar research for the next 30 days has its own agent name and
    # <= 2 calls/day (albatross/events/oracle_calendar.py), so it never spends the escalations' 12.
    add(jobs["oracle_watch"], "cron", minute="5-59/10")
    add(jobs["oracle_daily"], "cron", hour=0, minute=35)
    add(jobs["oracle_weekly"], "cron", day_of_week="sun", hour=1, minute=30)
    add(jobs["events_oracle"], "cron", hour=6, minute=20)
    # The Deep End (albatross/deepend/registry.py): each module schedules its own collectors.
    for name, kw in deep.SCHEDULE:
        add(jobs[name], "cron", **kw)
    # X search (Social Pulse, the Deep End's X Scout) runs here when its lane is the xAI API, else on the host worker
    # (albatross/llm/xsearch.py).
    add(xsearch.only_in("api", jobs["social_pulse"]), "cron", minute=10)
    for name, kw in deep.HOST_SCHEDULE:
        if name in xsearch.X_JOBS:
            add(xsearch.only_in("api", jobs[name]), "cron", **kw)
    sched.start()
    log.info("scheduler running with %d jobs", len(sched.get_jobs()))
    # The password-gated alerts bot answers /start, the password and /stop (albatross/alerts/bot.py). It restarts
    # when its token or password changes on the Setup page.
    bot_task = asyncio.create_task(alert_bot.run_forever())

    @keys.on_change
    def _restart_bot(changed: set[str]) -> None:
        nonlocal bot_task
        if changed & alert_bot.KEYS:
            bot_task.cancel()
            bot_task = asyncio.create_task(alert_bot.run_forever())

    # The Telegram listener (albatross/telegram/listener.py) logs the token addresses posted in the chats picked on the
    # Setup page as calls. It restarts on a new sign-in, new API keys or a new chat list.
    tg_task = asyncio.create_task(telegram_listener.run_forever())

    @keys.on_change
    def _restart_listener(changed: set[str]) -> None:
        nonlocal tg_task
        if changed & telegram_listener.KEYS:
            tg_task.cancel()
            tg_task = asyncio.create_task(telegram_listener.run_forever())

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows
            pass
    try:
        await stop.wait()
    finally:
        bot_task.cancel()
        tg_task.cancel()
        sched.shutdown(wait=False)


async def host() -> None:
    """Runs on the host (not Docker): agents that use a subscription CLI logged in here (the Grok CLI for X search)."""
    await db.migrate()
    await keys.refresh()
    sched = AsyncIOScheduler(timezone="UTC", job_defaults={"coalesce": True, "max_instances": 1,
                                                           "misfire_grace_time": 600})
    watch_overlaps(sched)
    sched.add_job(keys.refresh, "interval", seconds=30, id="keys_refresh")
    # Hourly: the market pulse plus the coins that matter this hour, paced to settings.social_weekly_usd
    # of the SuperGrok pool (albatross/agents/social_pulse.py). X jobs run here only while their lane is the CLI.
    sched.add_job(xsearch.only_in("cli", jobs["social_pulse"]), "cron", minute=10)
    # The Deep End's CLI agents (the X Scout on Grok).
    for name, kw in deep.HOST_SCHEDULE:
        sched.add_job(xsearch.only_in("cli", jobs[name]) if name in xsearch.X_JOBS else jobs[name], "cron", **kw)
    sched.start()
    log.info("host worker running: %s", [j.name for j in sched.get_jobs()])
    await asyncio.Event().wait()


async def status() -> None:
    pool = await db.pool()
    print(f"{'job':<22}{'last ok (UTC)':<22}{'errs':>5}{'rows':>8}  last error")
    for r in await pool.fetch("SELECT * FROM ingest_health ORDER BY job"):
        ok = r["last_ok"].strftime("%Y-%m-%d %H:%M:%S") if r["last_ok"] else "never"
        print(f"{r['job']:<22}{ok:<22}{r['consecutive_errors']:>5}{r['last_rows'] or 0:>8}  "
              f"{(r['last_error'] or '')[:60] if r['consecutive_errors'] else ''}")
    print()
    for label, sql in [
        ("assets (active)", "SELECT count(*) FROM assets WHERE active"),
        ("candles", "SELECT count(*) FROM candles"),
        ("deriv snapshots", "SELECT count(*) FROM deriv_snapshots"),
        ("news items", "SELECT count(*) FROM news_items"),
        ("metric points", "SELECT count(*) FROM metric_series"),
        ("feature rows", "SELECT count(*) FROM features"),
        ("quant signals", "SELECT count(*) FROM signals WHERE source = 'quant'"),
    ]:
        print(f"{label:<18}{await pool.fetchval(sql):>12,}")


async def main(cmd: str) -> None:
    try:
        match cmd:
            case "migrate":
                await db.migrate()
            case "universe":
                await db.migrate()
                await jobs["universe"]()
            case "backfill":
                await backfill()
            case "run":
                await run()
            case "host":
                await host()
            case "status":
                await status()
            case "job":
                # Run one named job once, e.g. `python -m albatross job news_scout`
                await db.migrate()
                await jobs[sys.argv[2]]()
            case _:
                print(__doc__)
    finally:
        await http.close()
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("apscheduler").setLevel(logging.WARNING)  # our tracked() wrapper already logs each run
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else ""))
