"""One token = one micro_tokens row (migration 013, albatross/api/ingest.py, the micro_pools job).

The folding itself is SQL (micro_fold_tokens), so the scenarios need a Postgres with the migrations applied:
set ALBATROSS_TEST_DSN to one (never production). Each scenario runs inside a transaction that is rolled back.
"""

import asyncio
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from albatross.api import ingest

DSN = os.environ.get("ALBATROSS_TEST_DSN")
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
A = "0x" + "a1" * 20      # made-up addresses, never in real data
B = "0x" + "b2" * 20


def test_a_call_chain_is_a_hint():
    assert ingest._chain(None, A) == "evm"
    assert ingest._chain("eth", A) == "ethereum"
    assert ingest._chain("robinhood", A) == "robinhood"
    # A feed takes the chain from any word or link in the message: an 0x address is never a Solana mint.
    assert ingest._chain("solana", A) == "evm"
    assert ingest._chain("solana", "So11111111111111111111111111111111111111112") == "solana"


class _Pool:
    """db.pool() stand-in: every acquire() hands out the test's connection, so the endpoint's own transaction
    becomes a savepoint inside the test's."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


def _run(scenario, monkeypatch):
    import asyncpg

    from albatross import db
    from albatross.config import settings

    async def go():
        conn = await asyncpg.connect(DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            pool = _Pool(conn)

            async def fake_pool():
                return pool
            monkeypatch.setattr(db, "pool", fake_pool)
            monkeypatch.setattr(settings, "call_feed_token", "t")
            await scenario(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def _call(src_id, address, chain=None, at=T0):
    return ingest.CallIn(src_id=src_id, chain=chain, address=address, group_name="g", intent="buy",
                         called_at=at.timestamp())


def _trade(src_id, address, chain, at):
    return ingest.TradeIn(src_id=src_id, trader="x", chain=chain, address=address, symbol="TKN", dry=False,
                          entry_at=at.timestamp(), status="open")


async def _post(calls=(), trades=()):
    return await ingest.calls(ingest.Batch(source="feed", calls=[c.model_dump() for c in calls],
                                                trades=[t.model_dump() for t in trades]), authorization="Bearer t")


async def _rows(conn, address):
    return [tuple(r) for r in await conn.fetch(
        "SELECT chain, resolved_chain, first_seen, first_seen_via FROM micro_tokens WHERE address = $1", address)]


async def _call_chains(conn, address):
    return sorted(r[0] for r in await conn.fetch("SELECT chain FROM micro_calls WHERE address = $1", address))


db_only = pytest.mark.skipif(not DSN, reason="set ALBATROSS_TEST_DSN to a migrated Postgres")
SRC = 9_000_000_000        # src_ids far above the feed's


@db_only
def test_chainless_call_then_bot_trade_is_one_token(monkeypatch):
    # The prod pattern: a call with no chain, the bot buys on Robinhood 5 s later, before the resolver runs.
    async def scenario(conn):
        await _post(calls=[_call(SRC + 1, A)])
        assert await _rows(conn, A) == [("evm", None, T0, "call")]
        await _post(trades=[_trade(SRC + 2, A, "robinhood", T0 + timedelta(seconds=5))])
        assert await _rows(conn, A) == [("robinhood", "robinhood", T0, "call")]
        assert await _call_chains(conn, A) == ["robinhood"]
        # A later chain-less call, and the first one re-sent, land on the same row.
        await _post(calls=[_call(SRC + 3, A, at=T0 + timedelta(hours=1)), _call(SRC + 1, A)])
        assert await _rows(conn, A) == [("robinhood", "robinhood", T0, "call")]
        assert await _call_chains(conn, A) == ["robinhood", "robinhood"]
    _run(scenario, monkeypatch)


@db_only
def test_same_batch_call_and_trade(monkeypatch):
    async def scenario(conn):
        out = await _post(calls=[_call(SRC + 1, A)], trades=[_trade(SRC + 2, A, "robinhood", T0 + timedelta(seconds=5))])
        assert out["folded"] >= 1
        assert await _rows(conn, A) == [("robinhood", "robinhood", T0, "call")]
    _run(scenario, monkeypatch)


@db_only
def test_resolver_rekeys_or_merges(monkeypatch):
    async def scenario(conn):
        await _post(calls=[_call(SRC + 1, A), _call(SRC + 2, B)])
        await _post(calls=[_call(SRC + 3, B, "bsc", T0 + timedelta(minutes=30))])   # B also named by chain later
        # The micro_pools job found A and B on bsc (pipeline._resolve_evm), then folds.
        await conn.execute("UPDATE micro_tokens SET resolved_chain = 'bsc' WHERE chain = 'evm' AND address = ANY($1)",
                           [A, B])
        assert await conn.fetchval("SELECT micro_fold_tokens()") == 2
        assert await _rows(conn, A) == [("bsc", "bsc", T0, "call")]          # re-keyed
        assert await _rows(conn, B) == [("bsc", "bsc", T0, "call")]          # merged, earliest first_seen kept
        assert await _call_chains(conn, B) == ["bsc", "bsc"]
    _run(scenario, monkeypatch)


@db_only
def test_wrong_chain_hint_folds_once_nothing_is_found_there(monkeypatch):
    async def scenario(conn):
        # "eth" was in the message, but the token trades on Robinhood (the bot bought it there).
        await _post(calls=[_call(SRC + 1, A, "eth")],
                    trades=[_trade(SRC + 2, A, "robinhood", T0 + timedelta(seconds=5))])
        assert sorted(await _rows(conn, A)) == [("ethereum", None, T0, "call"),
                                                ("robinhood", None, T0 + timedelta(seconds=5), "trade")]
        # Until discovery has looked on ethereum, the hint stands.
        assert await conn.fetchval("SELECT micro_fold_tokens()") == 0
        await conn.execute("INSERT INTO micro_state (chain, token, pools_at) VALUES ('ethereum', $1, $2)", A, T0)
        assert await conn.fetchval("SELECT micro_fold_tokens()") == 1
        assert await _rows(conn, A) == [("robinhood", "robinhood", T0, "call")]
        assert await _call_chains(conn, A) == ["robinhood"]
        assert await conn.fetchval("SELECT count(*) FROM micro_state WHERE token = $1", A) == 0
    _run(scenario, monkeypatch)


@db_only
def test_same_address_traded_on_two_chains_stays_two_tokens(monkeypatch):
    async def scenario(conn):
        await _post(trades=[_trade(SRC + 1, A, "robinhood", T0), _trade(SRC + 2, A, "base", T0)])
        await _post(calls=[_call(SRC + 3, A)])
        assert await conn.fetchval("SELECT micro_fold_tokens()") == 0
        assert sorted(r[0] for r in await _rows(conn, A)) == ["base", "evm", "robinhood"]   # the resolver decides
    _run(scenario, monkeypatch)
