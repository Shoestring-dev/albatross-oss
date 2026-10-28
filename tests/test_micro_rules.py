"""Accuracy rules of the micro-cap lane (albatross/micro/rules.py). Cases mirror the failures in SPEC-MICROCAPS §2."""

from datetime import UTC, datetime, timedelta

from albatross.micro import rules
from albatross.micro.rules import Candle, Pool, Quote

T = "Tok111"
NOW = datetime(2026, 9, 29, 20, 0, tzinfo=UTC)
S = timedelta(seconds=1)


def pool(addr, base=T, quote="SOL", liq=None, dex="pumpswap", vol=None, created=None):
    return Pool(addr, base, quote, dex, (), liq, vol, created)


# ─── base-side resolution ────────────────────────────────────────────────────
def test_quote_side_pool_never_prices_the_token():
    # GMEB: the token sat on the quote side of a far deeper pool and was priced 125x too high from it.
    deep_quote_side = pool("P1", base="OTHER", quote=T, liq=2_000_000)
    own = pool("P2", liq=18_000)
    chosen, _ = rules.choose_pool([deep_quote_side, own], T)
    assert chosen.address == "P2"


def test_no_base_side_pool_means_no_pool():
    chosen, reason = rules.choose_pool([pool("P1", base="OTHER", quote=T, liq=1e6)], T)
    assert chosen is None and reason == "no base-side pool"


def test_deepest_base_pool_wins():
    chosen, reason = rules.choose_pool([pool("A", liq=5_000), pool("B", liq=40_000), pool("C", liq=900)], T)
    assert (chosen.address, reason) == ("B", "initial")


def test_sitting_pool_keeps_its_place_within_margin():
    pools = [pool("A", liq=10_000), pool("B", liq=12_000)]
    assert rules.choose_pool(pools, T, current="A") == (pools[0], "current")
    pools = [pool("A", liq=10_000), pool("B", liq=13_000)]
    assert rules.choose_pool(pools, T, current="A") == (pools[1], "deeper")


def test_bonding_curve_beats_dust_until_it_migrates():
    curve = pool("CURVE", liq=None, dex="pumpfun", vol=50_000)
    dust = pool("DUST", liq=0.72, dex="meteora")
    assert rules.choose_pool([curve, dust], T)[0] is curve
    amm = pool("AMM", liq=36_000, dex="pumpswap")
    assert rules.choose_pool([curve, dust, amm], T, current="CURVE") == (amm, "migrated")


def test_vanished_pool_is_replaced():
    assert rules.choose_pool([pool("B", liq=1_000)], T, current="A")[1] == "previous pool gone"


def test_missing_liquidity_stays_none():
    p = rules.pool_from_pair({"pairAddress": "X", "baseToken": {"address": T}, "quoteToken": {"address": "Q"},
                              "dexId": "pumpfun", "liquidity": None, "priceUsd": None})
    assert p.liquidity_usd is None
    p = rules.pool_from_pair({"pairAddress": "0xAbC", "baseToken": {"address": "0xDeF"},
                              "quoteToken": {"address": "0x01"}, "liquidity": {"usd": 0}})
    assert p.liquidity_usd == 0.0 and p.address == "0xabc" and p.base == "0xdef"


# ─── freshness and tiers ─────────────────────────────────────────────────────
def test_freshness_budget_by_age():
    assert rules.freshness_budget(timedelta(minutes=59)) == 15 * S
    assert rules.freshness_budget(timedelta(hours=1)) == 60 * S
    assert rules.freshness_budget(timedelta(hours=23)) == 60 * S
    assert rules.freshness_budget(timedelta(days=3)) == 300 * S


def test_tiers():
    h = timedelta(hours=1)
    assert rules.tier(10 * S, None, False, None) == "hot"
    assert rules.tier(48 * h, 30 * 60 * S, True, 100.0) == "hot"          # the bot just bought it
    assert rules.tier(5 * h, None, False, 0.0) == "active"                # young tokens stay dense for 24 h
    assert rules.tier(48 * h, None, True, 100.0) == "active"
    assert rules.tier(48 * h, None, False, 100.0) == "watch"
    assert rules.tier(48 * h, None, True, 0.0) == "dormant"
    assert rules.tier(48 * h, None, False, None) == "watch"               # unknown volume is not zero


# ─── consensus ───────────────────────────────────────────────────────────────
def test_median_of_agreeing_sources():
    qs = [Quote("jupiter", 1.00, NOW - 2 * S), Quote("dexscreener", 1.02, NOW - 8 * S),
          Quote("geckoterminal", 1.04, NOW - 20 * S)]
    c = rules.consensus(qs, NOW, 60 * S)
    assert c.price == 1.02 and sorted(c.sources) == ["dexscreener", "geckoterminal", "jupiter"]
    assert c.ts == NOW - 2 * S and c.flags == []


def test_outlier_is_left_out():
    qs = [Quote("jupiter", 1.00, NOW), Quote("dexscreener", 1.01, NOW), Quote("geckoterminal", 3.0, NOW)]
    c = rules.consensus(qs, NOW, 60 * S)
    assert c.price == 1.005 and "outlier:geckoterminal" in c.flags and len(c.sources) == 2


def test_two_sources_must_be_within_5pct_of_each_other():
    assert rules.consensus([Quote("jupiter", 1.00, NOW), Quote("dexscreener", 1.05, NOW)], NOW, 60 * S).flags == []
    c = rules.consensus([Quote("jupiter", 1.00, NOW), Quote("dexscreener", 1.08, NOW)], NOW, 60 * S)
    assert c.flags == ["disagree"] and c.sources == ["dexscreener"] and c.price == 1.08


def test_gross_disagreement_leaves_the_price_unknown():
    # Seen live: a $513 DLMM pool quoted 11x above Jupiter. Neither is trusted.
    c = rules.consensus([Quote("jupiter", 1.7e-6, NOW), Quote("dexscreener", 1.9e-5, NOW)], NOW, 60 * S)
    assert c.price is None and c.flags == ["disagree"] and c.spread_pct > 1000


def test_single_source_is_flagged():
    c = rules.consensus([Quote("dexscreener", 2.0, NOW - 5 * S)], NOW, 60 * S)
    assert c.price == 2.0 and c.flags == ["single_source"]


def test_stale_quotes_are_not_used():
    # A hot token (15 s budget): DexScreener's 30 s CDN copy is too old, Jupiter alone prices it.
    qs = [Quote("jupiter", 1.0, NOW - 4 * S), Quote("dexscreener", 1.3, NOW - 25 * S)]
    c = rules.consensus(qs, NOW, 15 * S)
    assert c.price == 1.0 and c.flags == ["stale:dexscreener", "single_source"]
    c = rules.consensus([Quote("dexscreener", 1.3, NOW - 25 * S)], NOW, 15 * S)
    assert c.price is None and "stale" in c.flags
    assert rules.consensus([], NOW, 15 * S).flags == ["no_quote"]


# ─── sanity gate ─────────────────────────────────────────────────────────────
def test_moves_inside_10x_pass():
    assert rules.gate(9.0, NOW, 1.0, NOW - 10 * S, 1).accept


def test_single_source_10x_jump_is_rejected_then_accepted_once_it_holds():
    g = rules.gate(50.0, NOW, 1.0, NOW - 10 * S, 1)
    assert not g.accept and g.flag == "rejected_print" and g.pending_price == 50.0
    g2 = rules.gate(55.0, NOW + 30 * S, 1.0, NOW - 10 * S, 1, g.pending_price, g.pending_since)
    assert not g2.accept and g2.pending_since == NOW
    g3 = rules.gate(52.0, NOW + 61 * S, 1.0, NOW - 10 * S, 1, g.pending_price, g.pending_since)
    assert g3.accept and g3.flag == "jump_persisted"


def test_jump_confirmed_by_two_sources():
    g = rules.gate(0.05, NOW, 1.0, NOW - 10 * S, 2)
    assert g.accept and g.flag == "jump_confirmed"


def test_old_last_price_does_not_gate():
    assert rules.gate(50.0, NOW, 1.0, NOW - rules.JUMP_WINDOW - S, 1).accept


def test_candle_spike_rejected_but_held_jump_kept():
    m = timedelta(minutes=1)
    cs = [Candle(NOW, 1, 1, 1, 1.0, 10), Candle(NOW + m, 1, 40, 1, 40.0, 5), Candle(NOW + 2 * m, 1, 1.1, 1, 1.05, 10)]
    assert rules.flag_candles(cs, None, None, set()) == [[], ["rejected_print"], []]
    cs[2] = Candle(NOW + 2 * m, 40, 45, 38, 42.0, 10)
    assert rules.flag_candles(cs, None, None, set()) == [[], ["jump_confirmed"], []]


def test_candle_jump_confirmed_by_another_source():
    m = timedelta(minutes=1)
    cs = [Candle(NOW, 1, 1, 1, 1.0, 10), Candle(NOW + m, 30, 32, 29, 30.0, 5)]
    assert rules.flag_candles(cs, None, None, {NOW + m})[1] == ["jump_confirmed"]
    assert rules.flag_candles(cs, None, None, set())[1] == ["rejected_print"]


def test_candle_sources_counts_quotes_in_range():
    c = Candle(NOW, 1.0, 1.2, 0.9, 1.1, 100)
    qs = [Quote("jupiter", 1.15, NOW), Quote("dexscreener", 2.0, NOW), Quote("jupiter", 1.0, NOW)]
    assert rules.candle_sources(c, qs) == 2


def test_duplicate_geckoterminal_minutes_are_merged():
    t = NOW.timestamp()
    raw = [[t + 60, 2.0, 2.1, 1.9, 2.05, 7.0], [t, 1.0, 1.0, 0.98, 0.98, 58.2], [t, 0.98, 0.99, 0.97, 0.98, 58.2]]
    cs = rules.candles_from_ohlcv(raw)
    assert [c.ts for c in cs] == [NOW, NOW + timedelta(minutes=1)]
    assert (cs[0].open, cs[0].high, cs[0].low, cs[0].close, cs[0].volume_usd) == (1.0, 1.0, 0.97, 0.98, 58.2)


def test_reconcile_compares_with_the_candle_of_the_minute_or_the_last_close():
    from albatross.micro.reconcile import compare
    m = timedelta(minutes=1)
    candles = [{"ts": NOW, "low": 0.9, "high": 1.1, "close": 1.0}, {"ts": NOW + 2 * m, "low": 2.0, "high": 2.2, "close": 2.1}]
    quotes = [
        {"source": "jupiter", "source_ts": NOW + 20 * S, "ingest_ts": NOW + 25 * S, "price_usd": 1.05},
        {"source": "dexscreener", "source_ts": NOW + m + 5 * S, "ingest_ts": NOW + m + 30 * S, "price_usd": 1.02},
        {"source": "jupiter", "source_ts": NOW + m + 20 * S, "ingest_ts": NOW + m + 30 * S, "price_usd": 1.0},
    ]
    rows = compare(NOW, "solana", T, candles, quotes)
    vs_gt = [(r[3], round(r[8], 6), r[9]) for r in rows if r[4] == "geckoterminal"]
    assert vs_gt == [("jupiter", 5.0, True), ("dexscreener", 2.0, None), ("jupiter", 0.0, None)]
    vs_ds = [(r[3], round(r[8], 4)) for r in rows if r[4] == "dexscreener"]
    assert vs_ds == [("jupiter", -1.9608)]           # only the second Jupiter quote shares a poll with DexScreener


def test_jupiter_yields_to_the_bot_when_half_the_shared_window_is_used(monkeypatch):
    import time

    import httpx

    from albatross.config import settings
    from albatross.venues import jupiter
    monkeypatch.setattr(settings, "jupiter_api_key", "test-key")
    monkeypatch.setattr(jupiter, "_paused_until", 0.0)
    reset = str(int(time.time()) + 7)
    jupiter._note_limits(httpx.Response(200, headers={"x-ratelimit-remaining": "60", "x-ratelimit-current": "40",
                                                      "x-ratelimit-reset": reset}))
    assert jupiter._paused_until == 0.0
    jupiter._note_limits(httpx.Response(200, headers={"x-ratelimit-remaining": "45", "x-ratelimit-current": "55",
                                                      "x-ratelimit-reset": reset}))
    assert jupiter._paused_until == float(reset)
    monkeypatch.setattr(jupiter, "_paused_until", 0.0)
    jupiter._note_limits(httpx.Response(429))
    assert jupiter._paused_until > time.time()
