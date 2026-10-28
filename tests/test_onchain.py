"""MVRV Z-score (albatross/ingest/onchain.py)."""
from datetime import date, timedelta

from albatross.ingest import onchain


def _rows(n=60, mc0=100.0, growth=1.02, mvrv=1.5):
    d0 = date(2020, 1, 1)
    return [{"day": d0 + timedelta(days=i), "CapMrktCurUSD": mc0 * growth ** i, "CapMVRVCur": mvrv} for i in range(n)]


def test_realized_cap_is_market_cap_over_mvrv_and_z_waits_for_history():
    out = onchain.mvrv_z(_rows())
    day, mc, rc, mv, z = out[0]
    assert rc == mc / 1.5 and mv == 1.5 and z is None                 # a single day has no deviation
    assert all(x[4] is None for x in out[:onchain.MIN_DAYS - 1]) and out[onchain.MIN_DAYS - 1][4] is not None


def test_z_uses_the_deviation_up_to_each_day():
    rows = _rows()
    full = onchain.mvrv_z(rows)
    head = onchain.mvrv_z(rows[:45])
    assert full[44][4] == head[44][4]                                 # adding later days never changes an earlier Z


def test_below_realized_value_is_negative():
    out = onchain.mvrv_z(_rows(mvrv=0.8))
    assert out[-1][4] < 0
