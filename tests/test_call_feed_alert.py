"""The call_feed_silent alert next to the built-in Telegram listener (albatross/alerts/monitor.py): it watches the
external feed only while CALL_FEED_TOKEN is set, and only through the feed's own rows, so it never fires for someone
who uses neither and never mistakes a quiet listener for a broken feed."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from albatross import tracker
from albatross.alerts import monitor
from albatross.config import settings

NOW = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)


def busy_then_silent() -> list[dict]:
    """40 rows an hour at this time of day on each of the previous 7 days, nothing in the last 3 hours."""
    base = NOW.replace(minute=0)
    hours = [base - timedelta(hours=d * 24 + h) for d in range(1, 8) for h in range(4)] + [base - timedelta(hours=3)]
    return [{"h": h, "n": 40, "last": h + timedelta(minutes=30)} for h in hours]


class Pool:
    def __init__(self, feed):
        self.feed, self.sql = feed, []

    async def fetch(self, sql, *a):
        self.sql.append(sql)
        return self.feed if "FROM rx" in sql else []

    async def fetchval(self, *a):
        return 0

    async def fetchrow(self, *a):
        return None


@pytest.fixture
def evaluate(monkeypatch):
    async def ledger_state():
        return 0, None

    async def agent_stats(hours):
        return []
    async def idle_jobs():
        return frozenset()
    monkeypatch.setattr(monitor, "_ledger_state", ledger_state)
    monkeypatch.setattr(tracker, "agent_stats", agent_stats)
    monkeypatch.setattr(monitor.xsearch, "idle_jobs", idle_jobs)       # the X lane has its own tests

    def go(feed):
        pool = Pool(feed)

        async def get_pool():
            return pool
        monkeypatch.setattr(monitor.db, "pool", get_pool)
        return {c.key for c in asyncio.run(monitor.evaluate(NOW))}, pool
    return go


def test_a_silent_feed_still_alerts(evaluate, monkeypatch):
    monkeypatch.setattr(settings, "call_feed_token", "t")
    keys, pool = evaluate(busy_then_silent())
    assert "call_feed_silent" in keys
    (sql,) = [s for s in pool.sql if "FROM rx" in s]
    assert "source IS DISTINCT FROM 'telegram'" in sql     # the listener's calls are not the feed's


def test_no_feed_configured_no_feed_alert(evaluate, monkeypatch):
    # The listener's calls fill micro_calls the same way; with no CALL_FEED_TOKEN there is no feed to watch.
    monkeypatch.setattr(settings, "call_feed_token", None)
    keys, pool = evaluate(busy_then_silent())
    assert "call_feed_silent" not in keys
    assert not any("FROM rx" in s for s in pool.sql)


def test_neither_feed_nor_listener(evaluate, monkeypatch):
    monkeypatch.setattr(settings, "call_feed_token", None)
    assert "call_feed_silent" not in evaluate([])[0]
    monkeypatch.setattr(settings, "call_feed_token", "t")
    assert "call_feed_silent" not in evaluate([])[0]     # a token but no rows yet: nothing to miss
