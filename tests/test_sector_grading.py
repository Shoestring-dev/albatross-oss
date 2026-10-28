"""Sector index math on hand-built 1h candles. The signal candle is 2h wide and opens at T0, so each
member's reference is the close of its T0+1h candle, and the window is the candles from T0+2h whose
close is at or before valid_until = T0+5h (T0+2h, T0+3h, T0+4h)."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from albatross.grading import grader

T0 = datetime(2026, 9, 1, tzinfo=UTC)
H = timedelta(hours=1)
BAR, UNTIL = 2 * H, T0 + 5 * H


def closes(*cs, start=T0) -> pd.DataFrame:
    """1h candles from `start`; None leaves that hour out."""
    rows = [(start + i * H, c) for i, c in enumerate(cs) if c is not None]
    return pd.DataFrame([{"close": c, "high": c, "low": c} for _, c in rows],
                        index=pd.DatetimeIndex([t for t, _ in rows]))


def index(*members):
    return grader.sector_index(list(members), T0, BAR, UNTIL, H)


def test_weighted_mean_of_member_returns():
    a = closes(99, 100, 100, 100, 101)              # ref 100 -> +1%
    b = closes(50, 50, 50, 50, 51)                  # ref 50  -> +2%
    c = closes(10, 10, 10, 10, 9.7)                 # ref 10  -> -3%
    assert index((a, 1), (b, 1), (c, 1)).iloc[-1] == pytest.approx(0.0)
    assert index((a, 1), (b, 1), (c, 2)).iloc[-1] == pytest.approx((1 + 2 - 6) / 4)


def test_reference_is_the_close_of_the_signal_candle():
    # The T0 close (99) and anything after T0+2h are not the reference; the T0+1h close is.
    m = closes(1, 100, 110, 110, 110)
    p = index((m, 1), (m, 1), (m, 1))
    assert list(p) == pytest.approx([10.0, 10.0, 10.0])
    assert list(p.index) == [T0 + 2 * H, T0 + 3 * H, T0 + 4 * H]


def test_candles_after_valid_until_are_ignored():
    m = closes(100, 100, 100, 100, 100, 50)         # T0+5h closes after valid_until
    assert index((m, 1), (m, 1), (m, 1)).iloc[-1] == pytest.approx(0.0)


def test_missing_candle_carries_the_last_close():
    a = closes(100, 100, 102, None, 104)
    b = closes(100, 100, 100, 100, 100)
    c = closes(100, 100, 100, 100, 100)
    p = index((a, 1), (b, 1), (c, 1))
    assert p[T0 + 3 * H] == pytest.approx(2 / 3)    # a held at +2% through its missing hour
    assert p[T0 + 4 * H] == pytest.approx(4 / 3)


def test_member_sits_out_until_its_first_candle_and_weights_rescale():
    a = closes(100, 100, 103, 103, 103)
    b = closes(100, 100, 101, 101, 101)
    late = closes(100, 100, None, 90, 90)           # no T0+2h candle
    p = index((a, 1), (b, 1), (late, 2))
    assert p[T0 + 2 * H] == pytest.approx(2.0)      # (3 + 1) / 2: late sits out
    assert p[T0 + 3 * H] == pytest.approx((3 + 1 - 20) / 4)


def test_fewer_than_three_members_with_data_is_ungradeable():
    a, b = closes(100, 100, 101, 101, 101), closes(100, 100, 99, 99, 99)
    no_ref = closes(None, None, 100, 100, 100)      # nothing inside the signal candle
    no_window = closes(100, 100)                    # nothing after it
    assert index((a, 1), (b, 1), (no_ref, 1), (no_window, 1)) is None
    assert index((a, 1), (b, 1), (a, 1)) is not None


def test_grade_sector_signs_the_index_to_the_call():
    path = pd.Series([1.0, -2.0, -1.5])
    short = grader.grade_sector({"id": 7, "bias": -1}, path)
    assert short == (7, None, 1.5, 2.0, -1.0, None, True)       # id, exit, return, mfe, mae, invalidated, hit
    long = grader.grade_sector({"id": 8, "bias": 2}, path)
    assert long == (8, None, -1.5, 1.0, -2.0, None, False)
    # Neutral: right when the net move stays inside half the window's range (3 pts -> 1.5).
    assert grader.grade_sector({"id": 9, "bias": 0}, path)[-1] is False
    assert grader.grade_sector({"id": 9, "bias": 0}, pd.Series([1.0, -2.0, 0.5]))[-1] is True


def test_grade_one_unchanged_for_assets():
    df = pd.DataFrame({"high": [101, 103, 102], "low": [99, 97, 100], "close": [100, 102, 101]},
                      index=pd.DatetimeIndex([T0 + 2 * H, T0 + 3 * H, T0 + 4 * H]))
    sig = {"id": 1, "tf": "1h", "bias": 1, "ref_price": 100.0, "invalidation": 98.0,
           "as_of": T0 + H, "valid_until": UNTIL}
    assert grader.grade_one(sig, df, H) == (1, 101.0, 1.0, 3.0, -3.0, True, True)
    short = {**sig, "bias": -2, "invalidation": 104.0}
    assert grader.grade_one(short, df, H) == (1, 101.0, -1.0, 3.0, -3.0, False, False)
