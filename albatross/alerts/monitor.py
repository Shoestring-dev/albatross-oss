"""The `alerts` job (every 5 min): read the system's own health from the DB, open an alert for each
condition in rules.py, resolve the ones that cleared, and notify (notify.py) when configured."""

import logging
import time
from datetime import UTC, datetime

import asyncpg

from albatross import controls, db, ledger, tracker
from albatross.alerts import notify, rules
from albatross.config import settings
from albatross.llm import xsearch

log = logging.getLogger(__name__)

LEDGER_EVERY_S = 30 * 60        # verify() rehashes the whole ledger; the daily ledger_verify job backs it up
_ledger: tuple[float, int, int | None] | None = None


async def _ledger_state() -> tuple[int, int | None]:
    global _ledger
    if _ledger is None or time.monotonic() - _ledger[0] > LEDGER_EVERY_S:
        _ledger = (time.monotonic(), *await ledger.verify())
    return _ledger[1], _ledger[2]


async def evaluate(now: datetime) -> list[rules.Condition]:
    pool = await db.pool()
    health = [dict(r) for r in await pool.fetch(
        "SELECT job, last_run, last_ok, consecutive_errors, last_error FROM ingest_health")]
    # 402s are counted apart: they get their own alert, not a failure-rate one as well.
    hour = [dict(r) for r in await pool.fetch(
        """SELECT CASE WHEN model LIKE 'codex:%' THEN 'chatgpt_plan' WHEN model LIKE 'claude:%' THEN 'claude_plan'
                       ELSE agent END AS agent, count(*) AS runs,
                  count(*) FILTER (WHERE error LIKE 'HTTP 402%') AS refused,
                  min(started_at) FILTER (WHERE error LIKE 'HTTP 402%') AS first_402,
                  max(started_at) FILTER (WHERE error LIKE 'HTTP 402%') AS last_402,
                  bool_or(model LIKE 'typesafe/%') FILTER (WHERE error LIKE 'HTTP 402%') AS typesafe_402,
                  count(*) FILTER (WHERE NOT ok AND coalesce(error, '') NOT LIKE 'HTTP 402%') AS failures,
                  mode() WITHIN GROUP (ORDER BY left(error, 200))
                    FILTER (WHERE NOT ok AND coalesce(error, '') NOT LIKE 'HTTP 402%') AS top_error
           FROM agent_runs WHERE started_at > now() - interval '1 hour' GROUP BY 1""")]   # the plan's failed
    # attempts are retried on OpenRouter, so they count as the plan lane's failures (one alert), not each agent's
    t1 = {r["symbol"]: r["ts"] for r in await pool.fetch(
        """SELECT a.symbol, max(c.ts) AS ts FROM assets a
           LEFT JOIN candles c ON c.symbol = a.symbol AND c.tf = '5m' AND c.ts > now() - interval '1 day'
           WHERE a.active AND a.tier = 1 GROUP BY a.symbol""")}
    # called_at bounds the scan through its index; a call is received after it is made. The feed is watched only while
    # CALL_FEED_TOKEN is set, and only through its own rows: the built-in Telegram listener's calls (source 'telegram')
    # say nothing about it, and a quiet listener is not a broken feed.
    feed = []
    if settings.call_feed_token:
        feed = await pool.fetch(
            """WITH rx AS (
                 SELECT received_at AS t FROM micro_calls
                 WHERE called_at > now() - interval '9 days' AND received_at > now() - interval '8 days'
                   AND source IS DISTINCT FROM 'telegram'
                 UNION ALL
                 SELECT updated_at FROM micro_trades WHERE updated_at > now() - interval '8 days')
               SELECT date_trunc('hour', t) AS h, count(*) AS n, max(t) AS last FROM rx GROUP BY 1""")
    spent = await pool.fetchval(            # OpenRouter's: the xAI API has its own daily cap (llm/xai.py)
        """SELECT coalesce(sum(cost_usd), 0) FROM agent_runs
           WHERE started_at >= date_trunc('day', now()) AND model NOT LIKE 'xai:%'""")
    try:
        rd = await pool.fetchrow(
            """SELECT count(*) AS reads,
                      count(*) FILTER (WHERE status = 'blind' OR (status = 'unverified' AND chain IS NULL)) AS failed
               FROM de_reddit_reads WHERE read_at > now() - interval '24 hours'""")
    except asyncpg.UndefinedTableError:              # before the Reddit scout's migration
        rd = None
    n, broken = await _ledger_state()
    loop_agents = await tracker.agent_stats(2)
    # With no X search lane on, the X jobs skip on purpose, like a paused job.
    skipped = {h["job"] for h in health if await controls.paused(h["job"])} | await xsearch.idle_jobs()
    return [
        *rules.loops(loop_agents, now),
        *rules.jobs(health, now, skipped),
        *rules.llm(hour, now),
        *rules.candles(t1, now),
        *rules.call_feed(max((r["last"] for r in feed), default=None), {r["h"]: r["n"] for r in feed}, now),
        *rules.ledger(n, broken),
        *rules.spend(float(spent), settings.alert_daily_spend_usd),
        *(rules.reddit(rd["reads"], rd["failed"]) if rd else ()),
    ]


async def sync(conds: list[rules.Condition], now: datetime) -> tuple[int, int]:
    """Open new alerts, refresh the ones still true, resolve the rest. -> (opened, resolved)."""
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        open_ = {r["key"]: r["id"] for r in await conn.fetch("SELECT id, key FROM alerts WHERE resolved_at IS NULL")}
        opened = 0
        for c in conds:
            if c.key in open_:
                await conn.execute(
                    """UPDATE alerts SET last_seen = $2, severity = $3, title = $4, detail = $5,
                                         since = least(since, coalesce($6::timestamptz, since)) WHERE id = $1""",
                    open_[c.key], now, c.severity, c.title, c.detail, c.since)
            else:
                await conn.execute(
                    """INSERT INTO alerts (key, kind, severity, title, detail, first_seen, last_seen, since)
                       VALUES ($1, $2, $3, $4, $5, $6, $6, coalesce($7::timestamptz, $6))""",
                    c.key, c.kind, c.severity, c.title, c.detail, now, min(c.since, now) if c.since else None)
                opened += 1
        gone = [i for k, i in open_.items() if k not in {c.key for c in conds}]
        await conn.execute("UPDATE alerts SET resolved_at = $2 WHERE id = ANY($1::bigint[])", gone, now)
    return opened, len(gone)


async def deliver(now: datetime) -> int:
    chans = await notify.channels()
    if not chans:
        return 0            # nothing configured: stay silent and keep no notice state
    pool = await db.pool()
    open_ = {r["key"]: dict(r) for r in await pool.fetch("SELECT * FROM alerts WHERE resolved_at IS NULL")}
    resolved = {r["key"]: dict(r) for r in await pool.fetch(
        "SELECT DISTINCT ON (key) * FROM alerts WHERE resolved_at IS NOT NULL ORDER BY key, resolved_at DESC")}
    notices = {r["key"]: dict(r) for r in await pool.fetch("SELECT key, state, sent_at FROM alert_notices")}
    sent = 0
    for key, state, a in notify.plan(open_, resolved, notices, now):
        if await notify.send(chans, state, a, now):
            await pool.execute(
                """INSERT INTO alert_notices (key, state, sent_at) VALUES ($1, $2, $3)
                   ON CONFLICT (key) DO UPDATE SET state = $2, sent_at = $3""", key, state, now)
            sent += 1
    return sent


async def run() -> int:
    now = datetime.now(UTC)
    conds = await evaluate(now)
    opened, resolved = await sync(conds, now)
    sent = await deliver(now)
    log.info("alerts: %d open (%d new, %d resolved), %d notifications", len(conds), opened, resolved, sent)
    return len(conds)
