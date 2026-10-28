"""Backtester rules on hand-built 5m candles. Unless a test says otherwise the call is logged one
minute before T0, so the first candle walked opens at T0, and costs are zero so the arithmetic is exact."""

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from albatross.backtest import sim

T0 = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
BAR = timedelta(minutes=5)
LATER = T0 + timedelta(days=1)


def frame(*ohlc, start=T0) -> pd.DataFrame:
    return pd.DataFrame(list(ohlc), columns=["open", "high", "low", "close"],
                        index=pd.DatetimeIndex([start + i * BAR for i in range(len(ohlc))]))


def call(direction="long", ref=100.0, stop=98.0, targets=(104.0,), logged=T0 - timedelta(minutes=1),
         horizon=T0 + 6 * BAR, market="perp") -> dict:
    return {"market": market, "direction": direction, "ref_price": ref, "stop": stop, "targets": list(targets),
            "logged_at": logged, "horizon_until": horizon}


FLAT = (100, 100.5, 99.5, 100)


def run(c, df, asof=LATER, cost=0.0, funding=None):
    return sim.simulate(c, df, BAR, asof, cost, funding)


# ─── long ──────────────────────────────────────────────────────────────────
def test_long_target_hit():
    o = run(call(), frame(FLAT, (100.5, 104.5, 100, 104), FLAT, FLAT, FLAT, FLAT))["stop_target"]
    assert (o["status"], o["exit_reason"], o["exit_price"]) == ("graded", "target", 104)
    assert o["net_return_pct"] == pytest.approx(4.0)
    assert o["r_multiple"] == pytest.approx(2.0)
    assert o["mfe_pct"] == pytest.approx(4.0)            # 104.5 is beyond the target: clipped
    assert o["mae_pct"] == pytest.approx(-0.5)
    assert o["exit_at"] == T0 + 2 * BAR


def test_long_stop_hit():
    o = run(call(), frame(FLAT, (100, 100.2, 97.5, 98), FLAT, FLAT, FLAT, FLAT))["stop_target"]
    assert (o["exit_reason"], o["exit_price"]) == ("stop", 98)
    assert o["net_return_pct"] == pytest.approx(-2.0)
    assert o["r_multiple"] == pytest.approx(-1.0)
    assert o["mae_pct"] == pytest.approx(-2.0)           # 97.5 was never held: the stop closed it


# ─── short ─────────────────────────────────────────────────────────────────
def test_short_target_hit():
    c = call("short", stop=102, targets=(96,))
    o = run(c, frame(FLAT, (100, 100.5, 95.5, 96), FLAT, FLAT, FLAT, FLAT))["stop_target"]
    assert (o["exit_reason"], o["exit_price"]) == ("target", 96)
    assert o["net_return_pct"] == pytest.approx(4.0)
    assert o["r_multiple"] == pytest.approx(2.0)
    assert o["mfe_pct"] == pytest.approx(4.0)


def test_short_stop_hit():
    c = call("short", stop=102, targets=(96,))
    o = run(c, frame(FLAT, (100, 102.5, 99.8, 102.2), FLAT, FLAT, FLAT, FLAT))["stop_target"]
    assert (o["exit_reason"], o["exit_price"]) == ("stop", 102)
    assert o["net_return_pct"] == pytest.approx(-2.0)
    assert o["r_multiple"] == pytest.approx(-1.0)
    assert o["mae_pct"] == pytest.approx(-2.0)


# ─── fills ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("direction,stop,target,bar", [
    ("long", 98, 104, (100, 104.5, 97.5, 101)),
    ("short", 102, 96, (100, 102.5, 95.5, 99)),
])
def test_same_candle_stop_and_target_assumes_stop(direction, stop, target, bar):
    o = run(call(direction, stop=stop, targets=(target,)), frame(FLAT, bar, FLAT, FLAT, FLAT, FLAT))["stop_target"]
    assert o["exit_reason"] == "stop"
    assert o["r_multiple"] == pytest.approx(-1.0)


def test_gap_through_stop_fills_at_open():
    o = run(call(), frame(FLAT, (97, 97.5, 96.5, 97), FLAT, FLAT, FLAT, FLAT))["stop_target"]
    assert (o["exit_reason"], o["exit_price"]) == ("stop", 97)
    assert o["r_multiple"] == pytest.approx(-1.5)


def test_gap_beyond_target_fills_at_target():
    o = run(call(), frame(FLAT, (106, 107, 105, 106), FLAT, FLAT, FLAT, FLAT))["stop_target"]
    assert (o["exit_reason"], o["exit_price"]) == ("target", 104)


# ─── horizon ───────────────────────────────────────────────────────────────
def test_horizon_exit_when_nothing_is_touched():
    r = run(call(), frame(FLAT, FLAT, FLAT, FLAT, FLAT, (100, 101.5, 99.8, 101)))
    for p in sim.POLICIES:
        assert (r[p]["exit_reason"], r[p]["exit_price"]) == ("horizon", 101)
        assert r[p]["exit_at"] == T0 + 6 * BAR
        assert r[p]["net_return_pct"] == pytest.approx(1.0)
        assert r[p]["r_multiple"] == pytest.approx(0.5)


def test_horizon_policy_ignores_stop_and_target():
    df = frame(FLAT, (100, 100.2, 97.5, 98), (98, 105, 98, 104.5), FLAT, FLAT, (100, 101.5, 99.8, 101))
    r = run(call(), df)
    assert r["stop_target"]["exit_reason"] == "stop"
    h = r["horizon"]
    assert (h["exit_reason"], h["exit_price"]) == ("horizon", 101)
    assert h["mfe_pct"] == pytest.approx(5.0)            # not clipped: nothing closes this policy early
    assert h["mae_pct"] == pytest.approx(-2.5)


def test_candles_after_horizon_are_ignored():
    df = frame(FLAT, FLAT, FLAT, FLAT, FLAT, FLAT, (100, 100, 90, 90))
    assert run(call(), df)["stop_target"]["exit_reason"] == "horizon"


def test_open_until_horizon_candle_is_final():
    df = frame(FLAT, FLAT, (100, 101, 99.8, 101))
    r = run(call(), df, asof=T0 + 3 * BAR)
    for p in sim.POLICIES:
        o = r[p]
        assert (o["status"], o["exit_reason"], o["exit_price"], o["exit_at"]) == ("open", None, None, None)
        assert o["net_return_pct"] == pytest.approx(1.0)  # marked to the last closed candle


def test_a_hit_resolves_before_the_horizon():
    df = frame(FLAT, (100.5, 104.5, 100, 104))
    r = run(call(), df, asof=T0 + 2 * BAR)
    assert r["stop_target"]["status"] == "graded"
    assert r["horizon"]["status"] == "open"


# ─── no look-ahead ─────────────────────────────────────────────────────────
def test_candle_opening_at_logged_at_does_not_count():
    df = frame((100, 100, 90, 95), FLAT, (100.5, 104.5, 100, 104), FLAT, FLAT, FLAT, FLAT)
    o = run(call(logged=T0, horizon=T0 + 7 * BAR), df)["stop_target"]
    assert o["exit_reason"] == "target"


def test_candle_in_progress_at_logged_at_does_not_count():
    df = frame((100, 100, 90, 95), (100.5, 104.5, 100, 104), FLAT, FLAT, FLAT, FLAT, FLAT)
    o = run(call(logged=T0 + timedelta(minutes=2), horizon=T0 + 7 * BAR), df)["stop_target"]
    assert o["exit_reason"] == "target"


def test_nothing_walked_before_first_candle_closes():
    r = run(call(), frame(FLAT), asof=T0 + timedelta(minutes=3))
    assert r["stop_target"]["status"] == "open"
    assert r["stop_target"].get("net_return_pct") is None


def test_holey_source_is_refused():
    df = frame(FLAT, FLAT, FLAT, FLAT, FLAT, FLAT).drop(T0 + 2 * BAR)
    assert run(call(), df) is None


# ─── costs ─────────────────────────────────────────────────────────────────
def test_side_cost_formula():
    # $100M/day: 1 bp spread + 400 * sqrt(1e4 / 1e8) = 4 bp impact -> 5 bp, plus the 4.5 bp HL fee.
    assert sim.side_cost(1e8) == pytest.approx(0.00045 + 0.0005)
    assert sim.side_cost(1e8, "binance") == pytest.approx(0.0005 + 0.0005)
    assert sim.side_cost(None) == pytest.approx(0.00045 + 0.01)
    assert sim.side_cost(1_000) == pytest.approx(0.00045 + 0.01)   # capped


def test_net_return_arithmetic():
    # Long: in at 100 * 1.001 = 100.1, out at 110 * 0.999 = 109.89.
    assert sim.net_return_pct(1, 100, 110, 0.001) == pytest.approx((109.89 / 100.1 - 1) * 100)
    # Short: in at 99.9, out (buy back) at 90.09.
    assert sim.net_return_pct(-1, 100, 90, 0.001) == pytest.approx((99.9 - 90.09) / 99.9 * 100)
    assert sim.net_return_pct(1, 100, 100, 0.001, funding=0.05) == pytest.approx(
        (99.9 / 100.1 - 1) * 100 - 0.05)


def test_costs_flow_into_outcome_and_r():
    c = 0.001
    o = run(call(), frame(FLAT, (100.5, 104.5, 100, 104), FLAT, FLAT, FLAT, FLAT), cost=c)["stop_target"]
    fill_in = 100 * (1 + c)
    net = (104 * (1 - c) / fill_in - 1) * 100
    risk = (fill_in - 98) / fill_in * 100
    assert o["cost_pct"] == pytest.approx(0.2)
    assert o["risk_pct"] == pytest.approx(risk)
    assert o["net_return_pct"] == pytest.approx(net)
    assert o["r_multiple"] == pytest.approx(net / risk)


def test_stop_out_with_costs_is_worse_than_minus_one_r():
    o = run(call(), frame(FLAT, (100, 100.2, 97.5, 98), FLAT, FLAT, FLAT, FLAT), cost=0.001)["stop_target"]
    assert o["r_multiple"] < -1.0


def test_baselines_share_the_window():
    r = run(call("short", stop=102, targets=(96,)), frame(FLAT, FLAT, FLAT, FLAT, FLAT, (100, 101.5, 99.8, 101)))
    h = r["horizon"]
    assert h["long_net_pct"] == pytest.approx(1.0)
    assert h["short_net_pct"] == pytest.approx(-1.0)
    assert h["net_return_pct"] == h["short_net_pct"]


# ─── funding ───────────────────────────────────────────────────────────────
def test_funding_charges_each_hour_boundary_held():
    rates = pd.Series({T0 + timedelta(hours=h): 0.0001 for h in range(1, 4)})
    # Held 10:30 -> 13:00: payments at 11:00 and 12:00 (13:00 is the exit instant, not held through).
    assert sim.funding_pct(rates, T0 + timedelta(minutes=30), T0 + timedelta(hours=3)) == pytest.approx(0.02)
    # A missing hour pays the mean of the stored ones.
    assert sim.funding_pct(rates.drop(T0 + timedelta(hours=2)), T0 + timedelta(minutes=30),
                           T0 + timedelta(hours=3)) == pytest.approx(0.02)
    assert sim.funding_pct(None, T0, T0 + timedelta(hours=3)) is None
    assert sim.funding_pct(rates, T0, T0 + timedelta(minutes=30)) == 0.0          # no payment due
    # No stored rate inside the hold: unknown, not borrowed from later dates (no look-ahead).
    assert sim.funding_pct(rates, T0 - timedelta(days=2), T0 - timedelta(days=1)) is None


def test_funding_sign_follows_direction():
    rates = pd.Series({T0 + timedelta(hours=1): 0.001})
    df = pd.DataFrame([FLAT] * 24, columns=["open", "high", "low", "close"],
                      index=pd.DatetimeIndex([T0 + i * BAR for i in range(24)]))
    kw = {"horizon": T0 + 24 * BAR, "stop": None, "targets": ()}
    long = run(call("long", **kw), df, funding=rates)["horizon"]
    short = run(call("short", **kw), df, funding=rates)["horizon"]
    # Held 09:59 -> 12:00: payments at 10:00 (no stored rate: pays the mean) and 11:00.
    assert long["funding_pct"] == pytest.approx(0.2)       # positive funding: longs pay
    assert short["funding_pct"] == pytest.approx(-0.2)     # ... and shorts receive
    assert long["net_return_pct"] == pytest.approx(-0.2)
    assert short["net_return_pct"] == pytest.approx(0.2)
    assert long["r_multiple"] is None                      # no stop, no R


# ─── gating ────────────────────────────────────────────────────────────────
def test_ungradeable_reasons():
    assert sim.ungradeable_reason(call(market="dex")) == "no_dex_cost_model"
    assert sim.ungradeable_reason(call(market="spot")) == "no_spot_cost_model"
    assert sim.ungradeable_reason(call(stop=101)) == "bad_levels"
    assert sim.ungradeable_reason(call("short", stop=98, targets=(96,))) == "bad_levels"
    assert sim.ungradeable_reason(call(targets=(99,))) == "bad_levels"
    assert sim.ungradeable_reason(call()) is None
