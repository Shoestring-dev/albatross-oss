"""DEX cost model, DEX grading on 1m candles, and how Telegram calls / bot entries are priced into the ledger."""

import math
from datetime import UTC, datetime, timedelta

import orjson
import pandas as pd
import pytest

from albatross import ledger
from albatross.backtest import sim
from albatross.micro import ledger_feed as feed
from albatross.micro import rules

T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
M = timedelta(minutes=1)
BIG = 1e15                    # a pool so deep that impact vanishes


# ─── cost model ──────────────────────────────────────────────────────────────
def test_pool_fees():
    assert sim.dex_fee("pumpfun", []) == 0.0125
    assert sim.dex_fee("pumpswap", []) == 0.0125
    assert sim.dex_fee("raydium", ["CPMM"]) == 0.0025
    assert sim.dex_fee("uniswap", ["v2"]) == 0.003
    assert sim.dex_fee("uniswap", ["v4"]) == 0.01          # tier unknown to us: pessimistic default
    assert sim.dex_fee(None, None) == 0.01


def test_round_trip_without_costs_is_the_price_move():
    assert sim.dex_round_trip_pct(1.0, 1.5, BIG, BIG, 0, 0) == pytest.approx(50.0)
    assert sim.dex_round_trip_pct(1.0, 0.2, BIG, BIG, 0, 0) == pytest.approx(-80.0)


def test_fee_and_gas_arithmetic():
    # 1.25% on each swap: (1 - f)^2 of the move survives.
    assert sim.dex_round_trip_pct(1.0, 2.0, BIG, BIG, 0.0125, 0) == pytest.approx((0.9875 ** 2 * 2 - 1) * 100)
    # $0.25 per swap on $100 in: pay 100.25, get 100 - 0.25 back on a flat price.
    assert sim.dex_round_trip_pct(1.0, 1.0, BIG, BIG, 0, 0.25) == pytest.approx((99.75 / 100.25 - 1) * 100)


def test_impact_matches_a_constant_product_pool():
    """One leg against explicit x*y=k reserves (no fee): $100 into a $1,000 pool ($500 quote side, $1 tokens)."""
    x, y = 500.0, 500.0
    tokens = y - x * y / (x + 100)
    held = sim.dex_round_trip_pct(1.0, 1.0, 1000, BIG, 0, 0)       # exit into a bottomless pool: entry leg only
    assert held == pytest.approx((tokens * 1.0 / 100 - 1) * 100)
    # Selling straight back into the same pool returns the full $100 in x*y=k; charging impact on both legs
    # (the exit priced at a market that never held our buy) is the pessimistic bound.
    x2, y2 = x + 100, x * y / (x + 100)
    got = x2 - x2 * y2 / (y2 + tokens)
    assert got == pytest.approx(100.0)
    assert sim.dex_round_trip_pct(1.0, 1.0, 1000, 1000, 0, 0) < (got / 100 - 1) * 100


def test_impact_by_pool_depth():
    # $100 into a $9.1k pool (the median at call time on 2026-09-29): ~2.1% per swap.
    x = 9_100 / 2
    one_way = 100 / (x + 100)
    assert one_way == pytest.approx(0.0215, abs=1e-3)
    flat = sim.dex_round_trip_pct(1.0, 1.0, 9_100, 9_100, 0, 0)
    assert flat == pytest.approx(((1 - one_way) * (x / (x + 100 * (1 - one_way))) - 1) * 100)


def test_exit_liquidity_measured_or_sqrt_of_price():
    assert sim.exit_liquidity(1234.0, 10_000, 1.0, 4.0) == 1234.0
    assert sim.exit_liquidity(None, 10_000, 1.0, 4.0) == pytest.approx(20_000)     # price x4 -> reserve x2
    assert sim.exit_liquidity(None, 10_000, 1.0, 0.01) == pytest.approx(1_000)


# ─── grading on 1m candles ───────────────────────────────────────────────────
def call(logged=T0 + timedelta(seconds=20), ref=1.0, hours=24):
    return {"logged_at": logged, "ref_price": ref, "horizon_until": logged + timedelta(hours=hours)}


def bars(*rows):
    """(minutes after T0, high, low, close)"""
    return pd.DataFrame([{"high": h, "low": lo, "close": c} for _, h, lo, c in rows],
                        index=pd.DatetimeIndex([T0 + m * M for m, *_ in rows]))


def grade(c, b, covered, liq=BIG, liquidity=None, fee=0.0, gas=0.0):
    return sim.simulate_dex(c, b, covered, liq, liquidity, fee, gas)["horizon"]


def test_checkpoints_fill_as_candles_cover_them():
    b = bars((3, 1.2, 1.0, 1.1), (30, 2.1, 1.9, 2.0), (200, 0.6, 0.4, 0.5))
    c = call()
    o = grade(c, b, covered=T0 + timedelta(hours=2))
    assert o["status"] == "open" and o["exit_reason"] is None and o["exit_price"] is None
    assert o["ret_5m_pct"] == pytest.approx(10.0)          # close of the 12:03 candle
    assert o["ret_1h_pct"] == pytest.approx(100.0)
    assert o["ret_6h_pct"] is None and o["ret_24h_pct"] is None
    assert o["net_return_pct"] == pytest.approx(100.0)     # marked where the candles end
    o = grade(c, b, covered=T0 + timedelta(hours=30))
    assert (o["status"], o["exit_reason"], o["exit_price"]) == ("graded", "horizon", 0.5)
    assert o["ret_6h_pct"] == o["ret_24h_pct"] == o["net_return_pct"] == pytest.approx(-50.0)
    assert o["mfe_pct"] == pytest.approx(110.0) and o["mae_pct"] == pytest.approx(-60.0)
    assert o["long_net_pct"] == o["net_return_pct"] and o["short_net_pct"] is None and o["r_multiple"] is None


def test_micro_prices_keep_their_digits():
    b = bars((10, 4.2e-06, 3.9e-06, 3.98123e-06))
    o = grade(call(ref=5e-06), b, covered=T0 + timedelta(hours=30))
    assert o["exit_price"] == 3.98123e-06
    assert o["net_return_pct"] == pytest.approx((3.98123 / 5 - 1) * 100)


def test_no_swap_since_the_call_means_the_price_held():
    o = grade(call(), bars(), covered=T0 + timedelta(hours=30), fee=0.01)
    assert o["status"] == "graded"
    assert o["net_return_pct"] == pytest.approx((0.99 ** 2 - 1) * 100)     # fees only


def test_no_look_ahead():
    # The candle in progress when the call was logged (12:00, logged 12:00:20) holds earlier prices: ignored.
    o = grade(call(), bars((0, 9.0, 0.1, 5.0), (10, 1.1, 1.0, 1.05)), covered=T0 + timedelta(hours=30))
    assert o["mfe_pct"] == pytest.approx(10.0)
    assert o["net_return_pct"] == pytest.approx(5.0)
    # A candle that has not closed by the checkpoint does not price it.
    o = grade(call(), bars((4, 3.0, 2.0, 3.0)), covered=T0 + timedelta(hours=1))
    assert o["ret_5m_pct"] == pytest.approx(200.0)                        # 12:04 closes at 12:05:00 <= 12:05:20
    o = grade(call(logged=T0 + timedelta(seconds=50)), bars((5, 3.0, 2.0, 3.0)), covered=T0 + timedelta(hours=1))
    assert o["ret_5m_pct"] == pytest.approx(0.0)                          # 12:05 closes at 12:06 > 12:05:50


def test_rug_exit_uses_the_measured_liquidity():
    b = bars((10, 1.0, 0.01, 0.01))
    liq = pd.Series({T0 + timedelta(hours=23, minutes=30): 300.0})       # pool drained to $300
    o = grade(call(), b, covered=T0 + timedelta(hours=30), liq=10_000, liquidity=liq)
    # $100 in (~2% impact), worth ~$0.98 at the exit spot, sold into $150 of quote side.
    x_in = 5_000
    held = 100 * x_in / (x_in + 100) * 0.01
    assert o["net_return_pct"] == pytest.approx((held * 150 / (150 + held) / 100 - 1) * 100)
    assert o["cost_pct"] == pytest.approx(-99.0 - o["net_return_pct"])


def test_nothing_before_the_first_minute_is_covered():
    o = grade(call(), bars(), covered=T0)
    assert o["status"] == "open" and o.get("net_return_pct") is None
    assert all(o[k] is None for k in sim.CHECKPOINTS)


def take_profit(c, b, covered, liq=BIG, liquidity=None, fee=0.0, gas=0.0):
    return sim.simulate_dex(c, b, covered, liq, liquidity, fee, gas)["take_profit"]


def test_take_profit_sells_at_the_first_close_that_nets_2x():
    b = bars((3, 1.5, 1.0, 1.4), (10, 2.6, 1.9, 2.2), (11, 3.0, 2.0, 2.9), (300, 0.5, 0.3, 0.4))
    o = take_profit(call(), b, covered=T0 + timedelta(hours=30))
    assert (o["status"], o["exit_reason"], o["exit_price"]) == ("graded", "target", 2.2)
    assert o["exit_at"] == T0 + 11 * M                                    # the 12:10 minute closes at 12:11
    assert o["net_return_pct"] == pytest.approx(120.0) and o["mfe_pct"] == pytest.approx(160.0)   # held to 12:11
    assert o["exits"]["exit"]["price"] == 2.2 and o["ret_24h_pct"] == pytest.approx(-60.0)   # the path's checkpoints
    h = grade(call(), b, covered=T0 + timedelta(hours=30))
    assert (h["exit_reason"], h["net_return_pct"]) == ("horizon", pytest.approx(-60.0))       # held: sold at 24 h
    assert h["exits"]["exit"]["price"] == 0.4                             # the horizon row keeps its own exit


def test_take_profit_needs_a_close_not_a_wick_and_counts_every_cost():
    wick = bars((10, 5.0, 1.0, 1.2))
    assert take_profit(call(), wick, covered=T0 + timedelta(hours=30))["exit_reason"] == "horizon"
    # 1% pool fee each way: 2.05x nets 0.99^2 * 2.05 = +100.9%, 2.03x only +99.0%
    assert take_profit(call(), bars((10, 2.1, 2.0, 2.03)), T0 + timedelta(hours=30), fee=0.01)["exit_reason"] == "horizon"
    o = take_profit(call(), bars((10, 2.1, 2.0, 2.05)), T0 + timedelta(hours=30), fee=0.01)
    assert o["exit_reason"] == "target" and o["net_return_pct"] == pytest.approx((0.99 ** 2 * 2.05 - 1) * 100)


def test_take_profit_is_final_before_the_horizon_and_open_until_hit():
    b = bars((10, 2.6, 1.9, 2.2))
    o = take_profit(call(), b, covered=T0 + timedelta(hours=2))
    h = grade(call(), b, covered=T0 + timedelta(hours=2))
    assert o["status"] == "graded" and h["status"] == "open"              # sold at 12:11; the 24 h hold isn't done
    o = take_profit(call(), bars((10, 1.6, 1.4, 1.5)), covered=T0 + timedelta(hours=2))
    assert o["status"] == "open" and o["net_return_pct"] == pytest.approx(50.0)     # not hit: marked like the hold
    assert sim.DEX_POLICIES == ["stop_target", "horizon", "take_profit"]


# ─── pricing a call ──────────────────────────────────────────────────────────
def test_live_price_follows_the_freshness_budget():
    now = T0
    young, day_old, old = now - timedelta(minutes=30), now - timedelta(hours=5), now - timedelta(days=3)
    assert rules.live_price(2.0, now - timedelta(seconds=14), young, now) == 2.0
    assert rules.live_price(2.0, now - timedelta(seconds=16), young, now) is None       # 15 s budget
    assert rules.live_price(2.0, now - timedelta(seconds=59), day_old, now) == 2.0
    assert rules.live_price(2.0, now - timedelta(seconds=61), day_old, now) is None     # 60 s budget
    assert rules.live_price(2.0, now - timedelta(minutes=4), old, now) == 2.0
    assert rules.live_price(2.0, now - timedelta(minutes=6), old, now) is None          # 5 min budget
    assert rules.live_price(None, now, old, now) is None
    assert rules.live_price(2.0, None, old, now) is None
    # Age counts from the earliest evidence: a pool the indexer dated late doesn't make the token younger.
    assert rules.born(now - timedelta(minutes=10), now - timedelta(hours=3)) == now - timedelta(hours=3)
    assert rules.born(None, now - timedelta(hours=3)) == now - timedelta(hours=3)


def pending(**kw):
    r = {"kind": "call", "src_id": 7, "at": T0, "received_at": T0 + timedelta(seconds=20), "chain": "solana",
         "address": "Mint111", "intent": "mention", "who": "alice", "group_id": -100123, "group_name": "Trenches",
         "strategy": None, "size_usd": None, "bot_price": None, "first_checked": None,
         "first_seen": T0 - timedelta(minutes=5), "trade_chain": "solana", "symbol": "CAT",
         "created_at": T0 - timedelta(minutes=8), "pool": "Pool111", "dex": "pumpswap", "labels": [],
         "price_usd": 0.0012, "price_ts": T0 + timedelta(seconds=55), "sources": ["jupiter", "dexscreener"],
         "flags": [], "liquidity_usd": 12_000.0, "liquidity_src": "dexscreener",
         "liquidity_ts": T0 + timedelta(seconds=50)}
    return r | kw


def test_price_check():
    now = T0 + timedelta(minutes=1)
    assert feed.price_check(pending(), now) == (0.0012, "ok")
    assert feed.price_check(pending(first_seen=None), now) == (None, "no_token")
    assert feed.price_check(pending(pool=None), now) == (None, "no_pool")
    assert feed.price_check(pending(price_usd=None), now) == (None, "no_price")
    assert feed.price_check(pending(price_ts=now - timedelta(seconds=20)), now) == (None, "stale")


def test_verdicts():
    at = T0
    ok, none = 0.0012, None
    assert feed.verdict(pending(), ok, at + timedelta(minutes=1)) == ("logged", None)
    assert feed.verdict(pending(), none, at + timedelta(minutes=1)) == ("pending", None)
    assert feed.verdict(pending(first_checked=at + timedelta(minutes=1)), none,
                        at + timedelta(minutes=11)) == ("unpriced", "no_price")
    # A price that shows up after the window is too late: the call is not logged at it.
    assert feed.verdict(pending(first_checked=at + timedelta(minutes=1)), ok,
                        at + timedelta(minutes=11)) == ("unpriced", "no_price")
    assert feed.verdict(pending(received_at=at + timedelta(minutes=12)), ok,
                        at + timedelta(minutes=12)) == ("unpriced", "late")
    assert feed.verdict(pending(received_at=None, kind="trade"), ok,
                        at + timedelta(hours=3)) == ("unpriced", "missed")
    assert feed.verdict(pending(intent="sell"), ok, at + timedelta(minutes=1)) == ("skipped", "sell")


def test_ledger_row_uses_our_price_not_the_bot_fill():
    now = T0 + timedelta(minutes=1)
    r = pending(kind="trade", intent=None, who="trader_ab12", group_name=None, strategy="ladder", size_usd=10.0,
                bot_price=0.0015, chain="evm", trade_chain="robinhood", address="0xabc")
    row = feed.ledger_row(r, 0.0012, now)
    assert (row["source_kind"], row["source_id"], row["scope"]) == ("bot", "trader_ab12", "robinhood:0xabc")
    assert (row["market"], row["direction"], row["ref_price"]) == ("dex", "long", 0.0012)
    assert row["stop"] is None and row["targets"] == [] and row["horizon_until"] == now + timedelta(hours=24)
    assert row["evidence"]["bot_entry_price"] == 0.0015 and row["evidence"]["delay_s"] == 60.0
    caller = feed.ledger_row(pending(), 0.0012, now)
    assert (caller["source_kind"], caller["source_id"], caller["scope"]) == ("caller", "alice", "solana:Mint111")


def test_evidence_survives_the_jsonb_round_trip_so_the_chain_verifies():
    row = feed.ledger_row(pending(), 0.0012, T0 + timedelta(minutes=1)) | {"logged_at": T0 + timedelta(minutes=1)}
    stored = row | {"evidence": orjson.loads(orjson.dumps(row["evidence"]))}
    assert ledger._digest(ledger.GENESIS, row) == ledger._digest(ledger.GENESIS, stored)
    assert not any(isinstance(v, float) and math.isnan(v) for v in row["evidence"].values())


def test_another_size_regrades_from_the_stored_row():
    b = bars((3, 1.2, 1.0, 1.1), (30, 2.1, 1.9, 2.0), (200, 0.6, 0.4, 0.5))
    liq = pd.Series({T0 + timedelta(hours=23, minutes=50): 4_000.0})
    o = grade(call(), b, covered=T0 + timedelta(hours=30), liq=9_100, liquidity=liq, fee=0.0125, gas=0.25)
    assert (o["size_usd"], o["fee_pct"], o["gas_usd"]) == (100.0, 1.25, 0.25)
    assert (o["liquidity_usd"], o["quote_reserve_usd"]) == (9_100, 4_550)
    assert set(o["exits"]) == {"5m", "1h", "6h", "24h", "exit"}
    assert o["exits"]["24h"]["measured"] and not o["exits"]["1h"]["measured"]
    assert o["exits"]["24h"]["quote_reserve_usd"] == 2_000

    def regrade(x: dict, size: float) -> float:
        return sim.dex_round_trip_pct(1.0, x["price"], o["liquidity_usd"], x["liquidity_usd"], o["fee_pct"] / 100,
                                      o["gas_usd"], size)
    for k in sim.CHECKPOINTS:
        x = o["exits"][k.removeprefix("ret_").removesuffix("_pct")]
        assert regrade(x, o["size_usd"]) == pytest.approx(o[k], abs=1e-5)
        assert regrade(x, 1_000) < o[k]                                    # a bigger position pays more impact
    assert regrade(o["exits"]["exit"], 100) == pytest.approx(o["net_return_pct"], abs=1e-5)
    orjson.dumps(o["exits"])                                               # stored as JSONB


def test_call_type_rides_in_the_evidence():
    now = T0 + timedelta(minutes=1)
    assert feed.ledger_row(pending(intent="buy"), 1.0, now)["evidence"]["call_type"] == "buy"
    assert feed.ledger_row(pending(), 1.0, now)["evidence"]["call_type"] == "mention"
    bot = pending(kind="trade", intent=None, who="trader_ab12", strategy="ladder")
    assert feed.ledger_row(bot, 1.0, now)["evidence"]["call_type"] == "bot_entry"
