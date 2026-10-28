"""Idea scorecard verdicts: notes, the baseline comparison and the proposed-vs-vetoed critic check."""

from albatross.backtest import scorecard as sc


def rec(n=40, avg_r=0.5, se=0.1, long_r=0.0, random_r=-0.1, status="proposed", tf=None, **kw) -> dict:
    """A raw query row as rows() gets it from Postgres."""
    r = {"policy": "stop_target", "source_id": "strategist", "status": status, "tf": tf, "n": n,
         "n_open": 2, "n_ungradeable": 0, "win_rate": 0.55 if n else None, "avg_r": avg_r if n else None,
         "avg_r_stderr": se if n > 1 else None, "total_r": avg_r * n if n and avg_r is not None else None,
         "avg_net_return_pct": 0.4 if n else None,
         "long_win_rate": 0.5 if n else None, "long_avg_r": long_r if n else None,
         "long_avg_net_return_pct": 0.1 if n else None, "random_win_rate": 0.45 if n else None,
         "random_avg_r": random_r if n else None, "random_avg_net_return_pct": -0.2 if n else None}
    r.update(kw)
    return r


def test_nothing_graded_is_all_nulls_and_says_so():
    row = sc.shape(rec(n=0))
    assert row["note"] == "nothing graded yet"
    assert row["vs_baselines"] == "too few to tell"
    assert row["avg_r"] is None and row["always_long"]["avg_r"] is None


def test_small_sample_is_not_judged():
    row = sc.shape(rec(n=5, avg_r=3.0, se=0.1))
    assert row["note"].startswith("only 5 graded")
    assert row["vs_baselines"] == "too few to tell"


def test_beats_behind_and_noise_use_the_better_baseline():
    assert sc.shape(rec(avg_r=0.5, se=0.1, long_r=0.2))["vs_baselines"] == "beats baselines"
    # 0.5 vs the better baseline 0.45 is inside 1.96 x 0.1
    assert sc.shape(rec(avg_r=0.5, se=0.1, long_r=0.45))["vs_baselines"] == "within noise of baselines"
    assert sc.shape(rec(avg_r=-0.4, se=0.1, long_r=0.0, random_r=-0.1))["vs_baselines"] == "behind baselines"


def test_no_stop_means_no_r_and_no_verdict():
    assert sc.shape(rec(avg_r=None, se=None))["vs_baselines"] == "too few to tell"


def test_critic_needs_both_sides_graded():
    rows = [sc.shape(rec(status="proposed")), sc.shape(rec(n=4, status="vetoed", avg_r=-1.0))]
    (c,) = sc.critic(rows)
    assert c["verdict"] == "too few to judge the critic" and c["vetoed_n"] == 4


def test_critic_without_any_vetoes():
    (c,) = sc.critic([sc.shape(rec(status="proposed"))])
    assert c["vetoed_n"] == 0 and c["avg_r_diff"] is None and c["verdict"] == "too few to judge the critic"


def test_critic_verdicts():
    def verdict(p_r, v_r):
        rows = [sc.shape(rec(status="proposed", avg_r=p_r, se=0.1)), sc.shape(rec(status="vetoed", avg_r=v_r, se=0.1))]
        return sc.critic(rows)[0]["verdict"]
    assert verdict(0.5, -0.3) == "vetoes avoided worse trades"     # 0.8 > 1.96 * hypot(0.1, 0.1) = 0.28
    assert verdict(0.2, 0.1) == "no measurable difference"
    assert verdict(-0.3, 0.5) == "vetoed ideas did better"


def test_critic_ignores_per_timeframe_rows():
    assert sc.critic([sc.shape(rec(tf="1h")), sc.shape(rec(tf="1h", status="vetoed"))]) == []


def test_call_type_splits_rows_and_callers_have_no_critic():
    buy = sc.shape(rec(source_id="caller_x", call_type="buy", avg_r=None, se=None))
    assert buy["call_type"] == "buy"
    assert sc.shape(rec())["call_type"] is None                    # every other source
    rows = [sc.shape(rec(source_id="caller_x", call_type=t, avg_r=None, se=None)) for t in ("buy", "mention")]
    rows.append(sc.shape(rec()))
    assert [c["source_id"] for c in sc.critic(rows)] == ["strategist"]
