"""Factor rank (albatross/strategy/factor_rank.py) and the intel verdicts (albatross/api/intel.py)."""
import math

import numpy as np
import pandas as pd

from albatross.api import intel
from albatross.strategy import factor_rank


def _frames(n_days=80, n_coins=20, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.date_range("2026-01-01", periods=n_days, freq="D")
    cols = [f"C{i}" for i in range(n_coins)]
    vol = np.linspace(0.01, 0.08, n_coins)                       # C0 calmest, C19 wildest
    r = rng.normal(0, 1, (n_days, n_coins)) * vol
    px = pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)), idx, cols)
    dv = pd.DataFrame(1e7, idx, cols)
    return px, px * 1.01, px * 0.99, dv


def test_ranks_every_liquid_coin_once_per_day_and_calm_coins_rank_higher():
    px, hi, lo, dv = _frames()
    out = factor_rank.compute(px, hi, lo, dv)
    last = out[out.day == out.day.max()]
    assert len(last) == 20 and sorted(last["rank"]) == list(range(1, 21))
    assert set(last.quintile) == {1, 2, 3, 4, 5}
    # everything else equal-ish, the calm part pushes C0 above C19 on average
    mean_rank = out.groupby("symbol")["rank"].mean()
    assert mean_rank["C0"] < mean_rank["C19"]


def test_illiquid_coins_are_left_out_and_thin_days_are_not_ranked():
    px, hi, lo, dv = _frames(n_coins=14)
    dv.iloc[:, :4] = 1e5                                         # four coins trade $100k a day
    out = factor_rank.compute(px, hi, lo, dv)
    assert out.empty                                             # 10 liquid < MIN_NAMES (12): no ranking


def test_uses_only_the_days_own_close():
    px, hi, lo, dv = _frames()
    a = factor_rank.compute(px, hi, lo, dv)
    px2 = px.copy(); px2.iloc[-1] *= 3                           # change only the last day
    b = factor_rank.compute(px2, hi, lo, dv)
    d = sorted(a.day.unique())[-2]
    pd.testing.assert_frame_equal(a[a.day == d].reset_index(drop=True), b[b.day == d].reset_index(drop=True))


def test_reasons_are_plain_words():
    r = {"near_high": 0.99, "volume": math.log(2.4), "vol_30d": 0.02}
    assert factor_rank.reasons(r, 0.04) == ["at its 20-day high", "volume 2.4× normal", "calmer than most"]
    assert factor_rank.reasons({"near_high": 0.1, "volume": 0.0, "vol_30d": 0.07}, 0.04) == ["near its 20-day low", "wilder than most"]


def test_edge_verdicts():
    assert intel.verdict(60, 0.7, 0.5)[0] == "early"            # 10 effective windows: too early
    assert intel.verdict(6000, 0.6, 0.5)[0] == "proven"
    assert intel.verdict(6000, 0.4, 0.5)[0] == "worse"
    assert intel.verdict(6000, 0.505, 0.5)[0] == "none"


def test_strategist_rejects_ideas_against_the_factor_rank():
    from albatross.agents import strategist
    from albatross.strategy import evidence
    p = {"price": 100.0, "levels": {"4h_atr_pct": 1.0},
         "factor_rank": evidence.rank_block({"rank": 30, "n": 33, "quintile": 1, "reasons": [], "day": "2026-09-29"})}
    call = strategist.TfCall(tf="4h", bias=2, confidence=0.7, rationale="x", relies_on=["ensemble"])
    idea = strategist.Idea(direction="long", tf="4h", stop=95.0, targets=[112.0], thesis="x", relies_on=["ensemble"])
    why, _ = strategist.check_idea(idea, p, {"4h": call})
    assert why and "factor rank" in why
    p["factor_rank"] = evidence.rank_block({"rank": 2, "n": 33, "quintile": 5, "reasons": [], "day": "2026-09-29"})
    assert strategist.check_idea(idea, p, {"4h": call})[0] is None
