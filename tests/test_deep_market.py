"""The Deep End market lane (albatross/deepend/market.py): Birdeye candle pages and how far they make the candles
complete, aggregation, trade stats, dex-paid parsing and the market contract, on payloads recorded 2026-09-30
(tests/fixtures/deep_market: Birdeye v3 OHLCV and trade-data, DexScreener orders and pairs, Jupiter objects as
chain.jupiter_intel returns them)."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from albatross.deepend import market
from albatross.micro import pipeline
from albatross.micro.rules import Candle

FIX = Path(__file__).parent / "fixtures" / "deep_market"
CONTRACT = Path(__file__).parents[1] / "docs" / "deepend" / "fixtures"
M = timedelta(minutes=1)


def load(name: str):
    return json.loads((FIX / name).read_text())


def at(unix: float) -> datetime:
    return datetime.fromtimestamp(unix, UTC)


def page(name: str) -> tuple[list[dict], datetime]:
    p = load(name)
    return p["data"]["items"], at(p["asked"])


# ─── candle pages and coverage ───────────────────────────────────────────────
def test_short_page_is_complete_up_to_the_minute_still_settling():
    items, asked = page("birdeye_ohlcv_recent.json")            # asked 17:46:29; the 17:46 minute still forming
    cs, until, caught_up = market.settle(items, asked)
    assert caught_up and until == datetime(2026, 9, 30, 17, 46, tzinfo=UTC)
    assert len(items) == 20 and len(cs) == 19 and cs[-1].ts == until - M
    assert all(a.ts < b.ts for a, b in zip(cs, cs[1:]))
    c = items[0]
    assert cs[0] == Candle(at(c["unix_time"]), c["o"], c["h"], c["l"], c["c"], c["v_usd"])


def test_a_minute_just_ended_waits_for_late_swaps():
    items, _ = page("birdeye_ohlcv_recent.json")
    cs, until, _ = market.settle(items, datetime(2026, 9, 30, 17, 46, 5, tzinfo=UTC))
    assert until == datetime(2026, 9, 30, 17, 45, tzinfo=UTC)   # 17:45 ended 5 s ago: not yet counted
    assert cs[-1].ts == datetime(2026, 9, 30, 17, 44, tzinfo=UTC)


def test_full_page_goes_on_from_its_last_minute():
    items, asked = page("birdeye_ohlcv_full.json")               # 100 minutes from 11:47, asked at 17:46
    cs, until, caught_up = market.settle(items, asked)
    assert not caught_up and len(cs) == 100
    assert until == datetime(2026, 9, 30, 13, 27, tzinfo=UTC)    # the next page starts where this one ends


def test_full_page_reaching_the_present_is_caught_up():
    items, _ = page("birdeye_ohlcv_full.json")
    last = at(items[-1]["unix_time"])
    cs, until, caught_up = market.settle(items, last + timedelta(seconds=40))
    assert caught_up and until == last and len(cs) == 99


def test_nothing_new_is_complete_up_to_now():
    cs, until, caught_up = market.settle([], datetime(2026, 9, 30, 17, 46, 29, tzinfo=UTC))
    assert (cs, until, caught_up) == ([], datetime(2026, 9, 30, 17, 46, tzinfo=UTC), True)


def test_where_a_fetch_starts():
    now = datetime(2026, 9, 30, 18, 0, 30, tzinfo=UTC)
    covered = datetime(2026, 9, 30, 17, 55, 12, tzinfo=UTC)
    # Resume a minute before the coverage; from our own last Birdeye minute if GeckoTerminal filled in a little.
    assert market.start_at(now, covered, None, None, None) == datetime(2026, 9, 30, 17, 54, tzinfo=UTC)
    own = datetime(2026, 9, 30, 17, 40, tzinfo=UTC)
    assert market.start_at(now, covered, own, None, None) == own - M
    assert market.start_at(now, covered, covered - 3 * market.RESUME, None, None) == datetime(2026, 9, 30, 17, 54,
                                                                                               tzinfo=UTC)
    # Nothing yet: a young token from its birth; an old one 6 h back, or an hour before its first call if earlier.
    born = now - timedelta(hours=3, seconds=20)
    assert market.start_at(now, None, None, born, None) == born.replace(second=0) - M
    old = now - timedelta(days=4)
    assert market.start_at(now, None, None, old, None) == (now - market.BACKFILL).replace(second=0)
    call = now - timedelta(hours=10)
    assert market.start_at(now, None, None, old, call) == (call - market.PRE_CALL).replace(second=0)


def test_the_last_close_holds_where_the_candles_are_complete():
    fetched = datetime(2026, 9, 30, 12, 0, 30, tzinfo=UTC)
    # GeckoTerminal: complete 60 s before its fetch; its forming minute's close held until the fetch (as before).
    assert pipeline.close_held(datetime(2026, 9, 30, 12, 0, tzinfo=UTC), fetched - pipeline.GT_LAG) == fetched
    assert pipeline.close_held(datetime(2026, 9, 30, 11, 30, tzinfo=UTC), fetched - pipeline.GT_LAG) == \
        fetched - pipeline.GT_LAG
    # Birdeye: settled minutes only, complete to the next minute's start.
    edge = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    assert pipeline.close_held(edge - M, edge) == edge
    ts = datetime(2026, 9, 30, 11, 0, tzinfo=UTC)
    assert pipeline.close_held(ts, None) == ts                   # no stamp: the candle's own open


# ─── aggregation ─────────────────────────────────────────────────────────────
def points(name: str) -> list[tuple]:
    return [(at(x["unix_time"]), x["o"], x["h"], x["l"], x["c"], x["v_usd"], "birdeye")
            for x in load(name)["data"]["items"]]


def test_one_minute_bars_are_the_candles():
    p = points("birdeye_ohlcv_full.json")
    bars = market.aggregate(p, 1)
    assert [b[0] for b in bars] == [int(x[0].timestamp()) for x in p]
    assert bars[0][1:] == list(p[0][1:6])


def test_bars_aggregate_on_the_clock():
    p = points("birdeye_ohlcv_full.json")                        # 11:47 .. 13:26, one candle a minute
    bars = market.aggregate(p, 15)
    assert [at(b[0]).strftime("%H:%M") for b in bars[:3]] == ["11:45", "12:00", "12:15"]
    assert len(bars) == 7 and all(b[0] % 900 == 0 for b in bars)
    first = [x for x in p if x[0] < datetime(2026, 9, 30, 12, 0, tzinfo=UTC)]   # 11:47 .. 11:59
    assert bars[0] == [bars[0][0], first[0][1], max(x[2] for x in first), min(x[3] for x in first), first[-1][4],
                       pytest.approx(sum(x[5] for x in first))]
    hour = market.aggregate(p, 60)
    assert [at(b[0]).strftime("%H:%M") for b in hour] == ["11:00", "12:00", "13:00"]
    assert hour[1][4] == [x for x in p if x[0] < datetime(2026, 9, 30, 13, 0, tzinfo=UTC)][-1][4]


def test_live_quotes_extend_the_bars_without_inventing_volume():
    t = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
    p = [(t, 1.0, 1.2, 0.9, 1.1, 500.0, "birdeye"),
         (t + timedelta(minutes=1, seconds=10), 1.3, 1.3, 1.3, 1.3, None, "quotes"),
         (t + timedelta(minutes=5, seconds=40), 1.05, 1.05, 1.05, 1.05, None, "quotes")]
    assert market.aggregate(p, 5) == [[int(t.timestamp()), 1.0, 1.3, 0.9, 1.3, 500.0],
                                      [int(t.timestamp()) + 300, 1.05, 1.05, 1.05, 1.05, None]]
    assert market.bars_source({"birdeye", "quotes"}) == "birdeye"
    assert market.bars_source({"geckoterminal", "birdeye"}) == "birdeye+geckoterminal"
    assert market.bars_source({"quotes"}) == "quotes" and market.bars_source(set()) is None


# ─── trade stats ─────────────────────────────────────────────────────────────
def test_birdeye_trade_data():
    p = load("birdeye_trade_data_bsc.json")
    st = market.stats_from_birdeye(p["data"], at(p["asked"]), [])
    d = p["data"]
    assert st["stats_src"] == "birdeye" and st["stats_at"] == at(p["asked"])
    assert st["change_h6"] == d["price_change_360m_percent"] and st["volume_h6"] == d["volume_360m_usd"]
    assert (st["change_m5"], st["change_h1"], st["change_h24"]) == (d["price_change_5m_percent"],
                                                                     d["price_change_1h_percent"],
                                                                     d["price_change_24h_percent"])
    assert (st["buys_h1"], st["sells_h1"], st["traders_h1"], st["traders_h24"]) == (6824, 5876, 1956, 5361)
    assert st["holders"] == 4284 and st["holders_hist"] == [[int(p["asked"]), 4284]]
    assert st["net_buyers_h1"] is st["organic_score"] is st["holders_change_h1_pct"] is None   # Jupiter-only


def test_holder_change_needs_a_reading_about_an_hour_old():
    now = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    t = int(now.timestamp())
    assert market.holders_change([[t - 3500, 1000], [t - 1800, 1100], [t, 1200]], now) == pytest.approx(20.0)
    assert market.holders_change([[t - 1800, 1100], [t, 1200]], now) is None       # nothing 45-75 min back
    st = market.stats_from_birdeye({"holder": 1500}, now, [[t - 9000, 10], [t - 3600, 1200]])
    assert st["holders_hist"] == [[t - 3600, 1200], [t, 1500]] and st["holders_change_h1_pct"] == pytest.approx(25.0)


def jup(name: str) -> dict:
    j = load(name)
    return j | {"as_of": datetime.fromisoformat(j["as_of"])}       # chain.jupiter_intel gives a datetime


def test_jupiter_stats():
    j = jup("jupiter_token.json")
    st = market.stats_from_jupiter(j)
    h1 = j["stats1h"]
    assert st["stats_src"] == "jupiter" and st["stats_at"] == j["as_of"]
    assert st["change_h1"] == h1["priceChange"] and st["holders_change_h1_pct"] == h1["holderChange"]
    assert st["volume_h1"] == pytest.approx(h1["buyVolume"] + h1["sellVolume"])
    assert (st["buys_h1"], st["sells_h1"], st["traders_h1"], st["net_buyers_h1"]) == (20977, 16768, 5552, 2692)
    assert (st["holders"], st["organic_label"]) == (8925, "high") and st["organic_score"] == pytest.approx(85.68, 0.01)


def test_a_window_jupiter_leaves_out_stays_unknown():
    st = market.stats_from_jupiter(jup("jupiter_token_quiet.json"))    # no stats5m; numBuys missing in 1h
    assert st["change_m5"] is None and st["volume_m5"] is None
    assert st["buys_h1"] is None and st["sells_h1"] == 1


# ─── dex-paid and boosts ─────────────────────────────────────────────────────
def test_dex_paid_is_an_approved_profile_order():
    o = market.parse_orders(load("dexscreener_orders_paid.json"))
    assert o["paid"] and o["at"] == at(1790722813.048) and o["boosts"] == []
    o = market.parse_orders(load("dexscreener_orders_unpaid.json"))                 # cancelled, then on hold
    assert not o["paid"] and o["at"] is None and len(o["orders"]) == 2
    o = market.parse_orders(load("dexscreener_orders_boosted.json"))
    assert o["paid"] and o["boosts"][0]["amount"] == 50
    legacy = [{"type": "communityTakeover", "status": "approved", "paymentTimestamp": 1790000000000},
              {"type": "tokenAd", "status": "approved", "paymentTimestamp": 1789000000000}]
    o = market.parse_orders(legacy)                                  # the endpoint's old shape, a bare list
    assert o["paid"] and o["at"] == at(1790000000)
    assert market.parse_orders({"orders": [], "boosts": []})["paid"] is False
    assert market.parse_orders(None) == {"paid": False, "at": None, "orders": [], "boosts": []}


def test_active_boosts_and_logo_from_the_pairs():
    pairs = load("dexscreener_tokens_boosted.json")
    a = pairs[0]["baseToken"]["address"]
    info = market.pair_info(pairs, a)
    assert info["boosts"] == 500 and info["logo"] == pairs[0]["info"]["imageUrl"]
    plain = [{**pairs[1], "boosts": None}]
    assert market.pair_info(plain, pairs[1]["baseToken"]["address"])["boosts"] == 0
    assert market.pair_info(pairs, "NotListed111") == {"boosts": None, "logo": None}


# ─── the market contract ─────────────────────────────────────────────────────
def shape(d):
    return {k: shape(v) for k, v in d.items()} if isinstance(d, dict) else None


def test_market_section_has_the_contract_shape():
    want = json.loads((CONTRACT / "token.json").read_text())["market"]
    now = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    stored = {"price_usd": 5e-4, "price_ts": now, "mcap_usd": 5e5, "fdv_usd": 5e5, "liquidity_usd": 8e4,
              "dex_paid": True, "dex_paid_at": now, "boosts": 0, "dex_at": now}
    got = market.market_section(stored, market.stats_from_jupiter(jup("jupiter_token.json")))
    assert shape(got) == shape(want)
    assert got["dex_paid"] == {"paid": True, "at": now} and got["txns"]["h1"] == {"buys": 20977, "sells": 16768}


def test_market_section_sources():
    now = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    assert market.market_section({}) is None
    # The newer of the stats read now and those stored wins; the price stays the micro lane's.
    stored = {"price_usd": 2.0, "price_ts": now, "stats_at": now - timedelta(minutes=5), "change_h1": 1.0}
    live = {"stats_at": now, "change_h1": 9.0}
    assert market.market_section(stored, live)["change"]["h1"] == 9.0
    assert market.market_section(stored, live | {"stats_at": now - timedelta(hours=1)})["change"]["h1"] == 1.0
    assert market.market_section(stored)["as_of"] == now - timedelta(minutes=5)
    # A bonding curve's DexScreener pair has no market cap: Jupiter's fills in; nothing checked yet is None.
    got = market.market_section(stored, None, {"mcap": 12345, "fdv": 23456, "liquidity": 999})
    assert (got["mcap"], got["fdv"], got["liquidity"]) == (12345.0, 23456.0, 999.0)
    assert got["dex_paid"] is None and got["boosts"] is None


# ─── the units ───────────────────────────────────────────────────────────────
def test_the_day_is_paced(monkeypatch):
    monkeypatch.setenv("DEEP_MARKET_CU_PER_DAY", "48000")          # 2,000 an hour
    day = datetime(2026, 9, 30, tzinfo=UTC)
    assert market.pace_line(day) == pytest.approx(1000)               # half an hour of credit at midnight
    assert market.pace_line(day + timedelta(hours=12)) == pytest.approx(25_000)
    assert market.pace_line(day + timedelta(minutes=20), market.LOW_LAG) == 0          # warm work waits
    assert market.pace_line(day + timedelta(hours=23, minutes=50)) == 48_000


def test_short_units_go_to_fresh_calls_and_open_dossiers_first():
    ranked = sorted([["bot holds it"], ["trending"], ["new call", "bot holds it"], ["calls this hour"],
                     ["bot holds it", "opened"], ["last 24 h"]], key=market.rank)
    assert [market.rank(w) for w in ranked] == [0, 0, 1, 1, 1, 2]
    assert ranked[-1] == ["bot holds it"]


def test_every_request_stays_in_the_12_unit_tier():
    from albatross.deepend import birdeye
    assert market.PAGE <= 100 and birdeye.CU[market.OHLCV] == 12 and birdeye.CU[market.TRADE_DATA] == 10


def test_jobs_are_registered_with_cadences():
    assert set(market.JOBS) == {n for n, _ in market.SCHEDULE} == set(market.CADENCE)
    assert all(n.startswith("deep_market_") for n in market.JOBS)
    assert all(isinstance(v, int) for v in market.CADENCE.values())


# ─── the shared tables (needs ALBATROSS_TEST_DSN, a migrated Postgres; each scenario is rolled back) ────────────
DSN = os.environ.get("ALBATROSS_TEST_DSN")
db_only = pytest.mark.skipif(not DSN, reason="set ALBATROSS_TEST_DSN to a migrated Postgres")
TOK = "DeepMarketTest1111111111111111111111111111"      # made up, never in real data


class _Pool:
    """db.pool() stand-in: every acquire() hands out the test's connection."""

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


@db_only
def test_a_geckoterminal_minute_never_replaces_a_birdeye_one(monkeypatch):
    t0 = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(hours=1)

    def cs(first, n, price):
        return [Candle(t0 + i * M, price, price, price, price, 10.0) for i in range(first, first + n)]

    async def scenario(conn):
        await pipeline.store_candles("solana", TOK, None, cs(0, 3, 1.0), "birdeye", t0)
        await pipeline.store_candles("solana", TOK, "POOL", cs(1, 3, 1.02), "geckoterminal", t0)
        rows = await conn.fetch("SELECT ts, source, close FROM micro_candles_1m WHERE chain = 'solana' AND token = $1"
                                " ORDER BY ts", TOK)
        assert [(r["source"], r["close"]) for r in rows] == [("birdeye", 1.0)] * 3 + [("geckoterminal", 1.02)]
        await pipeline.store_candles("solana", TOK, None, cs(3, 1, 1.01), "birdeye", t0)
        assert await conn.fetchval("SELECT source FROM micro_candles_1m WHERE chain = 'solana' AND token = $1"
                                   " AND ts = $2", TOK, t0 + 3 * M) == "birdeye"
    _run(scenario, monkeypatch)


@db_only
def test_coverage_only_advances_and_a_success_clears_an_error(monkeypatch):
    from albatross.api import micro as micro_api
    t = datetime.now(UTC).replace(microsecond=0)
    covered = f"SELECT {micro_api.COVERED.format(c='k')} FROM (SELECT 'solana'::text AS chain, $1::text AS address) k"

    async def scenario(conn):
        assert micro_api.covered_until(await conn.fetchval(covered, TOK)) is None        # nothing yet
        await pipeline.cover("solana", TOK, t)
        assert await conn.fetchval(covered, TOK) == t                   # candles_at is the coverage itself
        await pipeline.cover("solana", TOK, t - timedelta(minutes=5))   # an older fetch never moves it back
        assert await conn.fetchval(covered, TOK) == t
        await conn.execute("UPDATE micro_state SET candles_error = 'HTTP 404' WHERE chain = 'solana' AND token = $1",
                           TOK)
        assert micro_api.covered_until(await conn.fetchval(covered, TOK)) is None        # a failed fetch: unknown
        await pipeline.cover("solana", TOK, t - M)
        assert await conn.fetchval("SELECT (candles_at, candles_error) FROM micro_state WHERE chain = 'solana'"
                                   " AND token = $1", TOK) == (t - M, None)
    _run(scenario, monkeypatch)
