"""The Telegram listener's SQL against a real Postgres with the migrations applied (046 included): the repeat rule in
api/ingest.store, the listener's status row, today's count on the Setup page, and the call feed alert leaving the
listener's calls out. Set ALBATROSS_TEST_DSN to a migrated database (never production); each test runs inside a
transaction that is rolled back. Addresses and chat ids are made up."""

import asyncio
import os
from datetime import UTC, datetime, timedelta

import pytest

from albatross import tracker
from albatross.alerts import monitor
from albatross.api import ingest
from albatross.api import telegram_setup as api
from albatross.config import settings
from albatross.telegram import listener, status

DSN = os.environ.get("ALBATROSS_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="set ALBATROSS_TEST_DSN to a migrated Postgres")
M1 = "B6qN8yBtYfxfF9JoTywSr4CZDaQZnDhRf4FKKWHbpump"
E1 = "0x" + "a1" * 20
G1, G2 = -1000000000101, -1000000000202


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class Acq:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False
        return Acq()

    def __getattr__(self, name):
        return getattr(self.conn, name)


def _run(scenario, monkeypatch):
    import asyncpg

    from albatross import db

    async def go():
        conn = await asyncpg.connect(DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            pool = _Pool(conn)

            async def fake_pool():
                return pool
            monkeypatch.setattr(db, "pool", fake_pool)
            await scenario(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def _call(src, address, group, at):
    return ingest.CallIn(src_id=src, chain=None, address=address, group_id=group, group_name="Example Calls",
                         caller="@somecaller", intent="mention", called_at=at.timestamp())


def test_one_call_per_chat_and_token_a_day(monkeypatch):
    t0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)

    async def scenario(conn):
        async def put(*calls):
            return (await ingest.store("telegram", list(calls), repeats=listener.REPEAT))["calls"]
        assert await put(_call(2 ** 52 + 1, M1, G1, t0)) == 1
        assert await put(_call(2 ** 52 + 2, M1, G1, t0 + timedelta(hours=2))) == 0        # a repeat
        assert await put(_call(2 ** 52 + 3, M1, G2, t0 + timedelta(hours=2))) == 1        # another chat
        assert await put(_call(2 ** 52 + 4, M1, G1, t0 + timedelta(hours=25))) == 1       # a day later
        assert await put(_call(2 ** 52 + 5, E1.upper().replace("0X", "0x"), G1, t0),
                         _call(2 ** 52 + 6, E1, G1, t0 + timedelta(minutes=1))) == 1      # one batch, any case
        assert await put(_call(2 ** 52 + 1, M1, G1, t0)) == 0                             # the same message again
        rows = await conn.fetch("SELECT src_id, source FROM micro_calls WHERE address = ANY($1) ORDER BY src_id",
                                [M1, E1])
        assert [(r["src_id"] - 2 ** 52, r["source"]) for r in rows] == [(1, "telegram"), (3, "telegram"),
                                                                       (4, "telegram"), (5, "telegram")]
        # the feed's path keeps everything it sends, under its own source
        out = await ingest.store("feed", [_call(9_000_000_001, M1, G1, t0 + timedelta(hours=3))])
        assert out["calls"] == 1
        assert await conn.fetchval("SELECT source FROM micro_calls WHERE src_id = 9000000001") == "feed"
    _run(scenario, monkeypatch)


def test_the_status_row(monkeypatch):
    async def scenario(conn):
        await conn.execute("DELETE FROM telegram_listener")
        assert await status.read() is None
        await status.save("starting", "Starting.", account_name="My Account", account_username="me_myself",
                          titles={G1: "Example Calls"})
        r = await status.read()
        assert (r["state"], r["status"], r["account_name"], r["titles"]) == (
            "starting", "Starting.", "My Account", {G1: "Example Calls"})
        seen = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
        await status.beat("Listening to 1 chat.", seen)
        await status.beat("Listening to 1 chat.", None)                  # no newer message: kept
        r = await status.read()
        assert (r["state"], r["status"], r["last_message_at"], r["account_username"]) == (
            "listening", "Listening to 1 chat.", seen, "me_myself")
        await status.save("signed_out", status.SIGNED_OUT_TEXT)
        r = await status.read()
        assert (r["state"], r["account_name"], r["titles"]) == ("signed_out", "My Account", {G1: "Example Calls"})
        assert await conn.fetchval("SELECT count(*) FROM telegram_listener") == 1
    _run(scenario, monkeypatch)


def test_calls_today_counts_the_listeners_own(monkeypatch):
    async def scenario(conn):
        now = await conn.fetchval("SELECT now()")
        before = await api._calls_today()
        await ingest.store("telegram", [_call(2 ** 52 + 11, M1, G1, now)], repeats=listener.REPEAT)
        await ingest.store("feed", [_call(9_000_000_011, E1, G1, now)])
        assert await api._calls_today() == before + 1
    _run(scenario, monkeypatch)


def test_the_feed_alert_leaves_the_listeners_calls_out(monkeypatch):
    async def ledger_state():
        return 0, None

    async def agent_stats(hours):
        return []
    monkeypatch.setattr(monitor, "_ledger_state", ledger_state)
    monkeypatch.setattr(tracker, "agent_stats", agent_stats)
    monkeypatch.setattr(settings, "call_feed_token", "t")

    async def scenario(conn):
        await conn.execute("DELETE FROM micro_calls WHERE received_at > now() - interval '9 days'")
        await conn.execute("DELETE FROM micro_trades WHERE updated_at > now() - interval '9 days'")
        now = await conn.fetchval("SELECT now()")

        async def busy(source, base):
            # 40 calls an hour at this time of day on each of the last 7 days, then nothing for 3 hours
            rows = [(base + i, "solana", M1, G1, now - timedelta(days=d, hours=h, minutes=m), source)
                    for i, (d, h, m) in enumerate((d, h, m) for d in range(1, 8) for h in range(4) for m in range(40))]
            rows += [(base + 10_000 + m, "solana", M1, G1, now - timedelta(hours=3, minutes=m), source)
                     for m in range(40)]
            await conn.executemany(
                """INSERT INTO micro_calls (src_id, chain, address, group_id, called_at, received_at, source)
                   VALUES ($1, $2, $3, $4, $5, $5, $6)""", rows)
        await busy("telegram", 2 ** 52)
        assert "call_feed_silent" not in {c.key for c in await monitor.evaluate(now)}
        await busy("feed", 9_000_000_000)
        assert "call_feed_silent" in {c.key for c in await monitor.evaluate(now)}
    _run(scenario, monkeypatch)
