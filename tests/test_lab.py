"""The strategy lab (albatross/lab): the statistics and the rising bar, the strategy grammar, the pass, retire and
champion rules, a whole hourly run against an in-memory table, and the researcher's proposals checked in code."""

import asyncio
import random
from datetime import UTC, datetime, timedelta

import orjson
import pandas as pd
import pytest

from albatross.agents import strategy_researcher as sr
from albatross.lab import evolve, factor, meme, run, stats


# ─── statistics ──────────────────────────────────────────────────────────────
def test_clustered_t_counts_each_cluster_once():
    s = stats.clustered([1.0, 1.0, 3.0, 3.0], ["a", "a", "b", "b"])
    assert s["n"] == 4 and s["mean"] == 2.0 and s["clusters"] == 2
    # two clusters, each 2 trades sitting 1 from the mean: se = sqrt(2 * 2^2 * 2/1) / 4 = 1
    assert s["se"] == 1.0 and s["t"] == 2.0
    assert stats.clustered([5.0], ["a"]) is None
    assert stats.clustered([1.0, 2.0], ["a", "a"])["t"] is None          # one cluster: no standard error


def test_the_bar_rises_with_every_strategy_tried():
    assert stats.t_needed(1) == 2.0 and stats.t_needed(7) == 2.0       # never under 2
    bars = [stats.t_needed(n) for n in (10, 100, 1000, 10000)]
    assert bars == sorted(bars) and 2.1 < bars[0] < 2.2 and 3.7 < bars[2] < 3.8
    assert run.CHAMPION_BAR == stats.t_needed(20) and 2.4 < run.CHAMPION_BAR < 2.5


# ─── the grammar ─────────────────────────────────────────────────────────────
def test_a_meme_config_takes_only_fields_and_values_the_grammar_knows():
    ok = meme.MemeConfig(filters=[{"field": "score", "op": ">=", "value": 80},
                                  {"field": "check:mint", "op": "==", "value": "good"},
                                  {"field": "dex_paid", "op": "!=", "value": True}])
    assert len(ok.filters) == 3
    for bad in ({"field": "whale_vibes", "op": ">=", "value": 1}, {"field": "score", "op": ">=", "value": 140},
                {"field": "check:mint", "op": ">=", "value": "good"}, {"field": "check:mint", "op": "==", "value": "great"},
                {"field": "dex_paid", "op": ">=", "value": True}, {"field": "score", "op": ">=", "value": True}):
        with pytest.raises(ValueError):
            meme.MemeConfig(filters=[bad])
    with pytest.raises(ValueError):
        meme.MemeConfig(filters=[{"field": "score", "op": ">=", "value": 1}] * 7)


def test_a_factor_config_needs_a_weight_and_drops_the_zeros():
    c = factor.FactorConfig(weights={"calm": 1, "momentum": 0, "volume": -0.333})
    assert c.weights == {"calm": 1.0, "volume": -0.33}
    for w in ({"calm": 0}, {"calm": 1.5}):
        with pytest.raises(ValueError):
            factor.FactorConfig(weights=w)


def test_the_same_strategy_hashes_the_same_whatever_the_order():
    a = meme.MemeConfig(kinds=["fomo", "caller"], filters=[{"field": "score", "op": ">=", "value": 80},
                                                          {"field": "mcap", "op": "<=", "value": 500000}])
    b = meme.MemeConfig(kinds=["caller", "fomo"], filters=[{"field": "mcap", "op": "<=", "value": 500000},
                                                          {"field": "score", "op": ">=", "value": 80}])
    assert evolve.canonical("meme", a)[1] == evolve.canonical("meme", b)[1]
    assert evolve.canonical("meme", a)[1] != evolve.canonical("meme", meme.MemeConfig())[1]
    assert evolve.canonical("factor", factor.FactorConfig())[1] != evolve.canonical("factor", factor.FactorConfig(side="long_only"))[1]


def test_random_mutated_and_crossed_strategies_are_always_valid():
    rng = random.Random(3)
    for _ in range(400):
        m1, m2 = evolve.random_meme(rng), evolve.random_meme(rng)
        for c in (evolve.mutate_meme(m1, rng), evolve.cross_meme(m1, m2, rng)):
            meme.MemeConfig.model_validate(c.model_dump(mode="json"))
        f1, f2 = evolve.random_factor(rng), evolve.random_factor(rng)
        for c in (evolve.mutate_factor(f1, rng), evolve.cross_factor(f1, f2, rng)):
            factor.FactorConfig.model_validate(c.model_dump(mode="json"))
    assert len({evolve.canonical(f, c)[1] for f, c in evolve.seeds()}) == len(evolve.seeds())
    for _ in range(300):                                  # never the same field twice in one direction
        fs = [(f.field, f.op) for f in evolve.mutate_meme(evolve.random_meme(rng), rng).filters]
        assert len(fs) == len(set(fs))


def test_a_filter_on_a_missing_field_leaves_the_call_out():
    pick = meme.picker(meme.MemeConfig(filters=[{"field": "smart_holders", "op": ">=", "value": 1}]))
    assert pick({"source_kind": "caller", "f": {"smart_holders": 2.0}})
    assert not pick({"source_kind": "caller", "f": {"smart_holders": None}})
    assert not pick({"source_kind": "fomo", "f": {"smart_holders": 2.0}})          # only the kinds it names


# ─── the rules ───────────────────────────────────────────────────────────────
def s(n, mean, t):
    return {"n": n, "mean": mean, "se": 1.0, "t": t, "clusters": n}


def test_a_strategy_survives_only_both_tests():
    base = s(300, -40.0, -11.0)
    assert run.verdict("meme", s(150, 4.0, 3.2), s(60, 2.0, 1.0), base, 3.0)[0] == "survivor"
    assert "too few trades in the older" in run.verdict("meme", s(99, 4.0, 3.2), s(60, 2.0, 1.0), base, 3.0)[1]
    assert "t of at least 3.0" in run.verdict("meme", s(150, 4.0, 2.9), s(60, 2.0, 1.0), base, 3.0)[1]
    assert "not positive" in run.verdict("meme", s(150, 4.0, 3.2), s(60, -0.1, -0.1), base, 3.0)[1]
    assert "too few trades in the recent" in run.verdict("meme", s(150, 4.0, 3.2), s(49, 2.0, 1.0), base, 3.0)[1]
    assert "behind the baseline" in run.verdict("factor", s(90, 1.0, 2.5), s(11, 0.3, 0.1), s(11, 0.35, 0.1), 2.2)[1]
    assert run.verdict("factor", s(90, 1.0, 2.5), s(11, 0.35, 0.1), s(11, 0.35, 0.1), 2.2)[0] == "survivor"


def test_a_lucky_handful_of_trades_gets_no_score():
    assert run.score("meme", s(3, 90.0, 40.3)) is None and run.score("meme", s(100, 1.0, 1.5)) == 1.5
    assert run.score("factor", s(19, 1.0, 9.0)) is None and run.score("factor", None) is None


def test_paper_strategies_are_retired_early_or_ruled_at_their_checkpoint():
    now = datetime(2026, 11, 1, tzinfo=UTC)
    fresh, due = now - timedelta(days=3), now - timedelta(days=15)
    assert run.ruling("meme", s(60, -5.0, -2.5), None, fresh, now)[0] == "retired"            # clearly losing
    assert run.ruling("meme", s(40, -5.0, -2.5), None, fresh, now) is None                    # too few to say
    assert run.ruling("meme", s(150, 3.0, 3.0), s(150, -30, -9), fresh, now) is None          # before its checkpoint
    assert run.ruling("meme", s(90, 3.0, 3.0), s(90, -30, -9), due, now) is None              # due, but under 100 trades
    assert run.ruling("meme", s(150, 3.0, 3.0), s(150, -30, -9), due, now)[0] == "champion"
    assert run.ruling("meme", s(150, 3.0, 2.0), s(150, -30, -9), due, now)[0] == "retired"    # under the champion bar
    assert run.ruling("meme", s(150, 3.0, 3.0), s(150, 4.0, 3.0), due, now)[0] == "retired"   # behind the baseline
    assert run.ruling("factor", s(3, 2.0, 3.0), None, due, now) is None                       # six weeks for factor
    assert run.ruling("factor", s(6, 2.0, 3.0), s(6, 0.1, 0.1), now - timedelta(days=43), now) is None    # 12 periods
    assert run.ruling("factor", s(12, 2.0, 3.0), s(12, 0.1, 0.1), now - timedelta(days=85), now)[0] == "champion"
    few_days = {**s(150, 3.0, 3.0), "clusters": 9}                                            # 150 trades on 9 days
    assert run.ruling("meme", few_days, s(150, -30, -9), due, now) is None


def test_the_memecoin_holdout_is_the_newest_quarter_and_never_unsettled():
    now = datetime(2026, 11, 30, tzinfo=UTC)
    w = run.meme_windows([{"logged_at": now - timedelta(days=41)}], now)
    assert w["holdout"][1] == now - timedelta(days=1) and w["holdout"][0] == now - timedelta(days=11)
    assert w["discovery"] == (now - timedelta(days=41), w["holdout"][0])
    short = run.meme_windows([{"logged_at": now - timedelta(days=6)}], now)
    assert short["holdout"][1] - short["holdout"][0] == timedelta(days=3)                     # at least three days
    early = run.meme_windows([{"logged_at": datetime(2026, 9, 29, 3, tzinfo=UTC)}], now)
    assert early["discovery"][0] == run.MEME_START                                            # Sep 29 left out


# ─── a whole run, against an in-memory table ─────────────────────────────────
class Table:
    """Just enough of lab_strategies for run.run's statements."""

    def __init__(self):
        self.rows: dict[int, dict] = {}
        self.report = None

    def _new(self, family, config, h, name, origin, parents, idea):
        if any(r["config_hash"] == h for r in self.rows.values()):
            return "INSERT 0 0"
        i = len(self.rows) + 1
        self.rows[i] = {"id": i, "family": family, "config": config, "config_hash": h, "name": name, "origin": origin,
                        "parent_ids": parents, "idea": idea, "status": "queued", "created_at": datetime.now(UTC),
                        "tested_at": None, "discovery": None, "holdout": None, "forward": None, "t_needed": None,
                        "score": None, "paper_at": None, "decided_at": None, "reason": None}
        return "INSERT 0 1"

    async def execute(self, sql, *a):
        if sql.lstrip().startswith("INSERT INTO lab_strategies"):
            return self._new(*a)
        r = self.rows[a[0]]
        if "tested_at = now()" in sql:
            if r["status"] == "queued":
                r.update(status=a[1], tested_at=datetime.now(UTC), discovery=a[2], holdout=a[3], t_needed=a[4],
                         score=a[5], reason=a[6])
        elif "forward = $2" in sql:
            r["forward"] = a[1]
            if a[2] is not None:
                r.update(status=a[2], decided_at=datetime.now(UTC), reason=a[3])
        else:
            raise AssertionError(sql)
        return "UPDATE 1"

    async def fetch(self, sql, *a):
        rows = list(self.rows.values())
        if "WHERE score IS NOT NULL" in sql:
            return [r for r in rows if r["score"] is not None]
        if "status = 'queued'" in sql:
            return sorted((r for r in rows if r["status"] == "queued"), key=lambda r: r["id"])
        if "IN ('paper', 'champion')" in sql:
            return [r for r in rows if r["status"] in ("paper", "champion")]
        if "SET status = 'paper'" in sql:
            pick = sorted((r for r in rows if r["family"] == a[0] and r["status"] == "survivor" and r["score"] >= a[2]),
                          key=lambda r: (-(r["score"] or -1e9), r["id"]))[:a[1]]
            for r in pick:
                r.update(status="paper", paper_at=datetime.now(UTC))
            return [{"id": r["id"]} for r in pick]
        raise AssertionError(sql)

    async def fetchval(self, sql, *a):
        rows = [r for r in self.rows.values() if r["family"] == a[0]]
        if "status <> 'queued'" in sql:
            return sum(r["status"] != "queued" for r in rows)
        if "status = 'paper'" in sql:
            return sum(r["status"] == "paper" for r in rows)
        raise AssertionError(sql)


GOOD, BAD = s(200, 5.0, 4.5), s(200, -3.0, -3.0)


@pytest.fixture
def lab(monkeypatch):
    t = Table()

    async def pool():
        return t

    async def upsert(table, cols, rows, conflict):
        t.report = orjson.loads(rows[0][2])

    async def calls():
        return [{"logged_at": datetime.now(UTC) - timedelta(days=12)}]

    async def frames():
        return (pd.DataFrame({"BTC": [1.0, 2.0]}, index=pd.to_datetime(["2024-09-01", "2024-09-02"])),)
    # what wins, by rule: memecoin strategies that sell at 2x behind a filter, factor strategies that go long only
    monkeypatch.setattr(run.db, "pool", pool)
    monkeypatch.setattr(run.db, "upsert", upsert)
    monkeypatch.setattr(run.meme, "load", calls)
    monkeypatch.setattr(run.factor, "load", frames)
    monkeypatch.setattr(run, "meme_stats", lambda cfg, c, a, b, base=None: GOOD if cfg.exit == "take_profit_2x" and cfg.filters else BAD)
    # the baseline's own trades: -3% on each of 25 days
    monkeypatch.setattr(run.meme, "trades", lambda cfg, c, a, b: [(-3.0, f"c{i}", (datetime(2026, 10, 1) + timedelta(days=i % 25)).date())
                                                                  for i in range(200)])
    monkeypatch.setattr(run.factor, "periods", lambda cfg, fr: cfg)
    monkeypatch.setattr(run, "factor_stats", lambda cfg, a, b: GOOD if cfg.side == "long_only" else BAD)
    return t


def test_a_run_tests_everything_queued_and_fills_the_paper_slots(lab):
    out = asyncio.run(run.run(seed=1))
    rows = list(lab.rows.values())
    assert out["tested"] == len(rows) == len(evolve.seeds()) + out["new"] and out["new"] == 10    # seeds and ten new
    assert not [r for r in rows if r["status"] == "queued"]
    seed = {r["name"]: r for r in rows if r["origin"] == "seed"}
    assert seed[factor.name(factor.FactorConfig(side="long_hedged"))]["status"] == "rejected"
    assert seed["group calls · score >= 85 · sold at 2x"]["status"] == "paper"
    assert seed["group calls · held 24 h"]["status"] == "rejected"
    assert seed[factor.name(factor.FactorConfig(side="long_only"))]["status"] == "paper"
    assert seed[factor.name(factor.FactorConfig())]["status"] == "rejected"
    papers = [r for r in rows if r["status"] == "paper"]
    assert out["to_paper"] == len(papers) and all(r["paper_at"] for r in papers)
    assert sum(r["family"] == "meme" for r in papers) <= run.PAPER_SLOTS["meme"]
    assert lab.report["families"]["meme"]["tried"] == sum(r["family"] == "meme" for r in rows)
    assert lab.report["families"]["meme"]["baseline"]["holdout"]["mean"] == -3.0


def test_the_next_run_breeds_from_the_best_and_rules_on_what_is_due(lab):
    asyncio.run(run.run(seed=1))
    for r in lab.rows.values():
        if r["status"] == "paper":
            r["paper_at"] -= timedelta(days=15)
    out = asyncio.run(run.run(seed=2))
    rows = list(lab.rows.values())
    bred = [r for r in rows if r["origin"] in ("mutation", "crossover")]
    assert bred and all(r["parent_ids"] for r in bred)
    champs = [r for r in rows if r["status"] == "champion"]
    assert champs and {r["family"] for r in champs} == {"meme"}                          # factor waits six weeks
    assert out["champions"] == len(champs) and all("ahead of the baseline" in r["reason"] for r in champs)
    graded = [r for r in rows if r["status"] == "paper" and r["forward"]]                # a new one: from the next run
    assert graded and all(r["family"] == "factor" for r in graded)
    # each side against its own baseline: long-only strategies against long-only factor rank (GOOD here)
    assert all(orjson.loads(r["forward"])["baseline"]["mean"] == 5.0 for r in graded)


# ─── the researcher ──────────────────────────────────────────────────────────
def test_the_researchers_configs_are_checked_one_by_one():
    p = sr.Proposals(
        meme=[sr.MemeIdea(idea="Callers with a record", kinds=["caller"], exit="hold_24h",
                          filters=[sr.RawFilter(field="caller_prior_mean", op=">=", value=5)]),
              sr.MemeIdea(idea="Made-up field", kinds=["caller"], exit="hold_24h",
                          filters=[sr.RawFilter(field="moon_score", op=">=", value=5)])],
        factor=[sr.FactorIdea(idea="Momentum only", calm=0, near_high=0, volume=0, momentum=1, vol_days=30, high_days=20,
                              volume_days=30, mom_days=14, hold_days=7, slice=5, side="long_only"),
                sr.FactorIdea(idea="Nothing", calm=0, near_high=0, volume=0, momentum=0, vol_days=30, high_days=20,
                              volume_days=30, mom_days=14, hold_days=7, slice=5, side="long_only")])
    found, bad = sr.configs(p)
    assert bad == 2 and [f for f, _, _ in found] == ["meme", "factor"]
    assert found[1][1].weights == {"momentum": 1.0} and found[0][2] == "Callers with a record"


def test_the_researcher_never_sees_the_recent_data():
    r = {"name": "x", "config": "{}", "origin": "random", "status": "survivor", "discovery": orjson.dumps(GOOD).decode(),
         "holdout": orjson.dumps(BAD).decode(), "forward": None, "reason": "too few trades in the older data (3 of 100)"}
    b = sr._brief(r, with_reason=True)
    assert b.keys() == {"name", "config", "origin", "discovery", "reason"}                  # no status, no holdout
    assert b["discovery"] == {"n": 200, "mean": 5.0, "t": 4.5}


# ─── the factor book's periods ───────────────────────────────────────────────
def frames(days=260, coins=40, seed=5):
    import numpy as np
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2025-06-01", periods=days, freq="D")
    cols = ["BTC", "ETH"] + [f"C{i}" for i in range(coins - 2)]
    px = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.04, (days, coins)), axis=0)), index=idx, columns=cols)
    dv = pd.DataFrame(rng.uniform(2e7, 9e7, (days, coins)), index=idx, columns=cols)
    return px, px * 1.02, px * 0.98, dv


def test_the_factor_test_gives_the_same_periods_whichever_day_it_runs():
    fr = frames()
    for cfg in (factor.FactorConfig(), factor.FactorConfig(side="long_only", hold_days=3),
                factor.FactorConfig(side="long_hedged", hold_days=14, weights={"momentum": 1})):
        today = dict(factor.periods(cfg, fr))
        assert today and all((d - factor.EPOCH).days % cfg.hold_days == 0 for d in today)
        for cut in range(1, cfg.hold_days + 2):                  # the same test run 1..hold+1 days earlier
            earlier = dict(factor.periods(cfg, tuple(f.iloc[:-cut] for f in fr)))
            assert all(abs(today[d] - v) < 1e-9 for d, v in earlier.items())


def test_the_factor_book_holds_every_days_slice_and_pays_its_costs():
    fr = frames()
    flat = tuple(f.copy() for f in fr)
    flat[0].loc[:, :] = 100.0                                    # nothing moves: the book makes exactly minus its costs
    for side, legs in (("long_short", 4), ("long_only", 2), ("long_hedged", 2)):
        cfg = factor.FactorConfig(side=side, weights={"volume": 1})
        d = factor.daily(cfg, flat)
        assert len(d) and (abs(d + legs * factor.COST_PER_SIDE / cfg.hold_days * 100) < 1e-9).all()


def test_memecoin_strategies_count_each_day_once():
    day = lambda i: (datetime(2026, 10, 1) + timedelta(days=i)).date()     # noqa: E731
    strat = [(10.0, f"a{i}", day(i % 4)) for i in range(40)]               # 40 trades on 4 days
    base = [(-5.0, f"b{i}", day(i % 4)) for i in range(40)]
    p = run.paired(strat, base)
    assert p["days"] == 4 and p["mean"] == 15.0 and p["share_up"] == 1.0
