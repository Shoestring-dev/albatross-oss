"""Signal verdicts against the naive always-long/short caller (grading/scorecard.vs_naive)."""

from albatross.grading.scorecard import vs_naive


def test_a_handful_of_misses_is_noise_not_worse():
    # 0 of 4: the raw hit rate has zero variance, which used to make a zero-width band and a "worse" verdict
    edge, verdict = vs_naive(0.0, 0.5, 4)
    assert edge == -0.5 and verdict == "noise"
    assert vs_naive(1.0, 0.5, 4)[1] == "noise"


def test_large_samples_are_judged_against_the_better_naive_side():
    # 35% hits where price rose 60% of the time: always-long scores 60%, so 25 points behind
    edge, verdict = vs_naive(0.35, 0.60, 4000)
    assert round(edge, 2) == -0.25 and verdict == "worse"
    # a falling market: always-short (1 - 0.3 = 70%) is the bar, and 72% sits inside the band
    assert vs_naive(0.72, 0.30, 600)[1] == "noise"
    assert vs_naive(0.80, 0.30, 4000)[1] == "better"
