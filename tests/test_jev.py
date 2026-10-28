"""TypeSafe Jev: the analyst's mapping from a choice answer to a graded call, and the question shapes."""
from albatross.agents import jev_analyst as ja
from albatross.llm import typesafe


def test_choice_answer_maps_to_bias_score_and_confidence():
    bias, score, conf, probs = ja.to_call({"choice": "down", "probabilities":
                                           {"down_strong": 0.03, "down": 0.97, "flat": 0.0, "up": 0.0, "up_strong": 0.0}})
    assert bias == -1 and conf == 0.97
    assert score == round((-2 * 0.03 - 1 * 0.97) / 2, 4)                 # probability-weighted, -1..1
    assert ja._thesis(probs) == "Jev: 0% higher, 100% lower, 0% flat"


def test_unusable_answers_are_dropped():
    assert ja.to_call({}) is None
    assert ja.to_call({"choice": "sideways", "probabilities": {"sideways": 1.0}}) is None


def test_questions_are_typed_choices_over_the_five_outcomes():
    qs = ja.questions(["1h", "4h"])
    assert set(qs) == {"next_1h", "next_4h"}
    assert all(q["type"] == "choice" and set(q["criteria"]) == set(ja.OUTCOMES) for q in qs.values())
    assert typesafe.noul("x", "y", "n") == {"type": "noul", "instructions": "x", "criteria": {"true": "y", "false": "n"}}


def test_cost_is_input_tokens_only():
    assert abs(typesafe.USD_PER_INPUT_TOKEN * 1_000_000 - 0.042) < 1e-12         # $0.042 per million input tokens


def test_scout_candidate_is_mechanical():
    from datetime import UTC, datetime
    from albatross.agents import jev_scout as js
    now, bar = datetime(2026, 9, 30, 4, 20, tzinfo=UTC), datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
    c = js.candidate("1h", "long", 100.0, 1.0, now, bar)
    assert c["stop"] == 100.0 * (1 - 0.015) and c["target"] == 100.0 * (1 + 0.03)      # 1.5 ATR stop, 2R target
    assert c["bar_ts"] == bar                                                             # keyed to the closed candle
    assert c["horizon_until"] == datetime(2026, 9, 30, 10, 20, tzinfo=UTC)               # 6 candles from now
    s = js.candidate("4h", "short", 100.0, 0.1, now, bar)
    assert s["stop_pct"] == 0.3 and s["stop"] > 100 > s["target"]                        # never a stop inside 0.3%
    assert js.candidate("1h", "long", 100.0, None, now, bar) is None
    assert set(js.SCOUT_TFS) <= set(js.HORIZON_WORDS) and set(js.SCOUT_TFS) <= set(js.CONTEXT_TFS)
    assert "5m" not in js.SCOUT_TFS and "5m" in js.CONTEXT_TFS    # 2026-10-02: 5m/15m are context only


def test_expected_r_counts_costs():
    from albatross.agents import jev_scout as js
    assert js.expected_r(0.5, 1.5) == round(0.5 * 2 - 0.5 - 0.10 / 1.5, 3)
    assert js.expected_r(1 / 3, 10.0) < 0                                                 # break-even minus costs
    assert js.expected_r(0.5, 0.3, cost_pct=1.0) < 0 < js.expected_r(0.5, 0.3, cost_pct=0.1)   # a thin coin's costs sink a tight trade


# ─── Jev's odds, corrected against its graded record (albatross/agents/jev_calibration.py) ──────────────
def test_the_correction_shrinks_thin_bins_and_never_falls_as_jev_gets_surer():
    from albatross.agents import jev_calibration as jc
    #        Jev said  graded  hit target
    rows = [(0.10, 1000, 60), (0.20, 1000, 90), (0.30, 1000, 80), (0.40, 200, 50), (0.60, 3, 3)]
    bins = jc.fit(rows)
    ps = [b.p for b in bins]
    assert ps == sorted(ps)                                          # more confidence never reads as less
    assert bins[1].p == bins[2].p                                    # 9% then 8%: pooled to one level
    assert abs(bins[1].p - (91.767 / 1020 + 81.767 / 1020) / 2) < 1e-3
    assert bins[-1].p < 0.25                                         # 3 of 3 at 60% is not a 100% bin
    assert jc.apply(0.05, bins) == bins[0].p and jc.apply(0.95, bins) == bins[-1].p     # flat past the ends
    assert abs(jc.apply(0.175, bins) - (bins[0].p + bins[1].p) / 2) < 1e-9             # linear between centres


def test_a_timeframe_without_enough_grades_borrows_the_nearest_record():
    from albatross.agents import jev_calibration as jc
    one = [jc.Bin(0.2, 0.25, 500, 50, 0.1), jc.Bin(0.4, 0.45, 100, 25, 0.25)]
    c = jc.Calibration({"5m": one, "1h": one, jc.ALL: one}, ratio=0.4)
    assert c.source("5m") == "5m" and c.source("15m") in ("5m", "1h") and c.source("4h") == "1h" and c.source("1d") == "1h"
    assert c.source("2h") == jc.ALL
    assert jc.Calibration({}, ratio=0.4)(0.5, "5m") == 0.2                              # no map yet: overall hit/said


def test_the_paper_book_goes_by_the_corrected_odds(monkeypatch):
    from albatross.agents import jev_scout as js
    from albatross.config import settings
    monkeypatch.setattr(settings, "jev_scout_min_p", 0.35)
    monkeypatch.setattr(settings, "jev_scout_min_ev", 0.2)
    c = lambda sym, p_win, p_cal: {"symbol": sym, "p_win": p_win, "p_cal": p_cal, "ev_r": js.expected_r(p_cal, 2.0)}
    scored = [c("BTC", 0.48, 0.25), c("ETH", 0.30, 0.47), c("ETH", 0.31, 0.45), c("SOL", 0.50, 0.50), c("DOGE", 0.30, 0.46)]
    chosen = js.choose(scored, open_coins={"SOL"}, room=3)
    assert [x["symbol"] for x in chosen] == ["ETH", "DOGE"]          # BTC's 48% reads as 25%: out; SOL already open
    assert chosen[0]["p_cal"] == 0.47                                # one per coin, the best expected R
    assert js.choose(scored, set(), room=1) == [scored[3]]


def test_recent_grades_count_most_when_the_market_changes():
    # A synthetic regime change: the hit rate falls from 20% to 2% halfway through. A 3 h memory follows it while the
    # whole record keeps predicting the old rate (jev_replay: the same walk-forward that chose HALF_LIFE_H).
    import numpy as np

    from albatross.agents import jev_calibration as jc
    from albatross.agents import jev_replay as jr
    rng = np.random.default_rng(7)
    t = np.arange(0, 48 * 3600, 20.0)                                 # a candidate every 20 s for two days
    p = rng.uniform(0.1, 0.45, len(t))
    hit = (rng.uniform(size=len(t)) < np.where(t < 24 * 3600, 0.20, 0.02)).astype(float)
    res = {r["half_life_h"]: r for r in jr.replay({"15m": np.column_stack([p, hit, t, t + 5400])}, half_lives=[None, 3.0])}
    assert res[3.0]["logloss"] < res[None]["logloss"] and res[3.0]["gap_3h"] < res[None]["gap_3h"]
    assert set(jc.HALF_LIFE_H) == set(jc.ORDER)                     # every timeframe has a memory
