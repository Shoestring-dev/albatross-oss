"""The FOMO subscription test (albatross/deepend/fomo_verdict.py): its bands, its reproducible random draw, its
intervals, the decision rule, and a random entry graded by the ledger's own DEX simulation. No network, no DB."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from albatross.backtest import sim
from albatross.deepend import fomo_verdict as fv

T = datetime(2026, 10, 1, 21, 0, tzinfo=UTC)


def test_bands():
    assert [fv.age_band(timedelta(minutes=m)) for m in (30, 120, 600, 3000, 20000)] == \
        ["<1h", "1-6h", "6-24h", "1-7d", ">7d"]
    assert fv.age_band(None) is None
    assert (fv.rank_band(4, 90), fv.rank_band(None, 37), fv.rank_band(108, None), fv.rank_band(None, None)) == \
        ("1-10", "11-50", "51-150", None)
    assert fv.rank_band(151, None) is None


def test_the_random_draw_is_the_same_for_the_same_call_and_never_more_than_asked():
    cands = [{"token": f"T{i}"} for i in range(20)]
    a, b = fv.picks(cands, 4242), fv.picks(list(reversed(cands)), 4242)
    assert [c["token"] for c in a] == [c["token"] for c in b] and len(a) == fv.PICKS
    assert [c["token"] for c in fv.picks(cands, 4243)] != [c["token"] for c in a]
    assert fv.picks(cands[:2], 1) and len(fv.picks(cands[:2], 1)) == 2 and fv.picks([], 1) == []


def test_intervals_cover_a_real_difference_and_straddle_none():
    a, b = [1.0] * 60 + [0.0] * 40, [1.0] * 20 + [0.0] * 80
    d = fv.interval(fv.share, a, b)
    assert d["value"] == pytest.approx(0.4) and d["lo"] > 0.2 and d["hi"] < 0.6 and fv.clear(d)
    same = fv.interval(fv.share, a, list(a))
    assert same["lo"] < 0 < same["hi"] and not fv.clear(same)
    paired = fv.interval(fv.mean, [x + 0.1 for x in range(50)], [float(x) for x in range(50)], paired=True)
    assert paired["value"] == pytest.approx(0.1) and paired["lo"] == pytest.approx(0.1)
    assert fv.interval(fv.share, [], [1.0]) is None and not fv.clear(None)


def _call(mfe, r24, **kw):
    return {"status": "graded", "mfe_pct": mfe, "ret_24h_pct": r24, **kw}


def _pick(mfe, net, status="graded"):
    return {"status": status, "mfe_pct": mfe, "net_24h_pct": net}


def test_the_report_parts_count_first_calls_and_pair_each_with_its_random_entry():
    fomo = [_call(150, 30, picks=[_pick(10, -20), _pick(20, -30), _pick(5, -50)]),
            _call(10, -40, picks=[_pick(30, -30), _pick(0, -90, status="open")]),
            _call(120, 10, picks=[_pick(130, 5), _pick(10, -20), _pick(15, -10)]), {"status": "open"},
            _call(200, 90, picks=[])]                                     # no random entry matched: left out
    s = fv.summary(fomo)
    assert (s["tokens"], s["graded"], s["hit_2x"], s["median_24h_pct"]) == (5, 4, 0.75, 20.0)
    r = fv.against_random(fomo)
    # FOMO 2 hits of 3, against the picks with each token weighing 1: (0 + 0 + 1/3) / 3
    assert r["tokens"] == 3 and r["hit_2x"]["value"] == pytest.approx(2 / 3 - 1 / 9, abs=1e-3)
    tg = [_call(20, -10), _call(5, -50)]
    c = fv.compare(fomo, tg)
    assert c["hit_2x"]["value"] == pytest.approx(0.75, abs=1e-3) and c["median_24h_pct"]["value"] == 50.0


def _report(n, *, random_lo=0.05, tg_lo=0.02, lift_lo=0.01, running_out=True, paired=150, tg_graded=300):
    i = lambda lo: {"value": lo + 0.1, "lo": lo, "hi": lo + 0.2, "n": n}   # noqa: E731
    return {"fomo": {"graded": n}, "vs_random": {"tokens": paired, "hit_2x": i(random_lo), "net_24h_pct": i(random_lo)},
            "telegram_all": {"graded": tg_graded}, "vs_telegram_all": {"hit_2x": i(tg_lo), "median_24h_pct": i(tg_lo)},
            "lift": {"good": 40, "rest": 400, "hit_2x": i(lift_lo)}, "credits": {"running_out": running_out}}


def test_the_rule_says_too_early_until_every_comparison_has_its_sample_then_needs_every_test():
    assert fv.decide(_report(150))["verdict"] == "too_early"
    assert fv.decide(_report(250))["verdict"] == "buy_starter"
    thin = fv.decide(_report(250, paired=40, tg_graded=60))
    assert thin["verdict"] == "too_early" and len(thin["why"]) == 2       # each comparison needs its own sample
    lift = _report(250) | {"lift": {"good": 3, "rest": 400, "hit_2x": None}}
    assert fv.decide(lift)["verdict"] == "too_early" and "needs 30 each" in fv.decide(lift)["why"][0]
    # Enough data: clearly worse is "don't buy", unclear is "inconclusive", never "don't buy"
    assert fv.decide(_report(250, random_lo=-0.3))["verdict"] == "dont_buy"            # whole interval below zero
    unclear = fv.decide(_report(250, random_lo=-0.05))
    assert unclear["verdict"] == "inconclusive" and "beats random entry: no" in unclear["why"]
    assert fv.decide(_report(250, running_out=False))["verdict"] == "dont_buy"         # nothing more to buy
    assert fv.decide(_report(250, lift_lo=-0.02))["verdict"] == "inconclusive"
    # A clear loss on a full sample decides even while lift is still short of its 30 a side
    lost = _report(250, random_lo=-0.3) | {"lift": {"good": 2, "rest": 400, "hit_2x": None}}
    assert fv.decide(lost) == {"verdict": "dont_buy", "why": ["clearly worse than random entry on 150 tokens"]}


def test_the_random_baseline_is_unbiased_when_fomo_is_no_better():
    """The reviewer's case: right-skewed memecoin returns, FOMO drawn from the very distribution its random entries
    come from. Comparing single entries with single entries, the interval holds zero."""
    import random as rnd
    g = rnd.Random(1)

    def draw():
        u = g.random()
        return g.uniform(-95, -30) if u < 0.55 else g.uniform(-30, 30) if u < 0.9 else g.uniform(100, 900)
    units = [(draw(), [draw(), draw(), draw()]) for _ in range(250)]
    d = fv.cluster_interval(fv.wmedian, units)
    assert d["lo"] < 0 < d["hi"] and abs(d["value"]) < 6
    hits = fv.cluster_interval(fv.wshare, [(a > 100, [b > 100 for b in bs]) for a, bs in units])
    assert hits["lo"] < 0 < hits["hi"]
    # every token counts once on each side, whatever its number of picks
    assert fv.wshare([True, False, False], [1.0, 0.5, 0.5]) == 0.5
    assert fv.wmedian([1.0, 2.0, 30.0], [1.0, 1 / 3, 1 / 3]) == 1.0


def test_credit_pressure_is_measured_against_each_days_own_budget():
    from datetime import date
    days = [{"day": date(2026, 10, d), "units": u} for d, u in ((1, 7_000), (2, 7_900), (3, 2_000))]
    got = fv.budgets(days, 8_000, 240_000)
    assert [d["budget"] for d in got] == [7742, 7767, 7762] and [d["full"] for d in got] == [False, True, False]


def test_covered_until_follows_the_ledger_rule():
    assert fv.covered_until(None) == datetime.min.replace(tzinfo=UTC)
    r = {"retired": False, "liquidity_usd": 9e3, "candles_at": T, "candles_error": None, "last_ts": T}
    assert fv.covered_until(r) == T
    assert fv.covered_until(r | {"candles_error": "429"}) == T + sim.MINUTE
    assert fv.covered_until(r | {"retired": True, "liquidity_usd": 100.0}) == fv.FOREVER


class _Pool:
    """Answers grade_entry's three reads with a token that doubles within the day."""

    def __init__(self, bars):
        self.bars = bars

    async def fetch(self, sql, *a):
        if "FROM micro_candles_1m" in sql:
            return self.bars
        if "FROM micro_snapshots" in sql:
            return [{"ingest_ts": T + timedelta(hours=1), "liquidity_usd": 40_000.0}]
        raise AssertionError(sql)

    async def fetchrow(self, sql, *a):
        return {"retired": False, "liquidity_usd": 40_000.0, "candles_at": T + timedelta(days=2),
                "candles_error": None, "last_ts": T + timedelta(days=2)}


def test_a_random_entry_is_graded_by_the_ledgers_own_simulation():
    bars = [{"ts": T + timedelta(minutes=m), "high": p * 1.01, "low": p * 0.99, "close": p, "source": "birdeye"}
            for m, p in ((5, 1.2), (60, 2.4), (600, 1.8), (1439, 1.5))]
    c = {"price": 1.0, "liquidity_usd": 40_000.0, "liquidity_src": "dexscreener", "dex": "raydium", "labels": []}
    g = asyncio.run(fv.grade_entry(_Pool(bars), "solana", "TOKEN", T, c))
    assert g["status"] == "graded" and g["token"] == "TOKEN"
    assert g["mfe_pct"] > 100 and fv.hit_2x(g["mfe_pct"])                  # touched 2.4x before costs
    assert 30 < g["net_24h_pct"] < 50                                       # out at 1.5x, all costs in


def test_the_system_page_gets_the_latest_verdict_or_nothing(monkeypatch):
    import asyncpg
    import orjson

    from albatross.api import app as api

    report = {"decision": {"verdict": "too_early", "why": ["0 of 200 FOMO tokens graded"]}, "fomo": {"graded": 0},
              "credits": {"days_full": 1, "lookup_coverage": 0.34, "running_out": True, "daily_cap": 8000,
                          "days": [{"day": "2026-10-01"}]}, "min_tokens": 200, "unused": 1}

    class P:
        def __init__(self, row=None, missing=False):
            self.row, self.missing = row, missing

        async def fetchrow(self, sql, *a):
            if self.missing:
                raise asyncpg.UndefinedTableError("relation does not exist")
            return self.row

    for pool, want in ((P(missing=True), None), (P(None), None)):
        async def get(p=pool):
            return p
        monkeypatch.setattr(api.db, "pool", get)
        assert asyncio.run(api._fomo_verdict()) is want

    async def get():
        return P({"computed_at": T, "report": orjson.dumps(report).decode()})
    monkeypatch.setattr(api.db, "pool", get)
    v = asyncio.run(api._fomo_verdict())
    assert v["decision"]["verdict"] == "too_early" and "unused" not in v and "days" not in v["credits"]
    assert v["credits"]["lookup_coverage"] == 0.34 and v["computed_at"] == T


def test_the_credit_leg_turns_on_unserved_demand_not_on_credits_spent():
    days = [{"day": f"2026-10-{d:02d}", "due": due, "looked": looked}
            for d, due, looked in ((1, 40, 30), (2, 50, 20), (3, 45, 10), (4, 60, 25), (5, 5, 0))]
    assert [d["day"] for d in fv.short_days(days)] == ["2026-10-02", "2026-10-03", "2026-10-04"]   # 5 due: too few
    assert fv.short_days([{"day": "x", "due": 40, "looked": 39}]) == []    # credits spent, demand served: no pressure

