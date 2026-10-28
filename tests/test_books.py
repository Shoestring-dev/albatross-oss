"""Paper books (albatross/backtest/books.py): the position rule, the dollars, the verdicts and when they are due, the
intervals, the factor slices, and the dossier report. The SQL runs at the bottom on a migrated Postgres
(ALBATROSS_TEST_DSN, never production), inside a transaction that is rolled back."""

import asyncio
import os
import random
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import orjson
import pytest

from albatross.backtest import books as bk

T = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
H, D = timedelta(hours=1), timedelta(days=1)


def call(i, coin="solana:A", at=T, kind="caller", status="graded", net=10.0, exit_in=D, score=80, red=(), tp=None,
         statuses=None):
    """A row shaped like DEX_SQL's."""
    done = status == "graded"
    tp = tp or {}
    return {"id": i, "logged_at": at, "horizon_until": at + D, "source_kind": kind, "scope": coin,
            "status": status, "net_return_pct": net, "exit_at": at + exit_in if done else None,
            "exit_reason": "horizon" if done else None,
            "tp_status": tp.get("status", status), "tp_net": tp.get("net", net),
            "tp_exit_at": tp.get("exit_at", at + exit_in if done else None), "tp_reason": tp.get("reason", "horizon" if done else None),
            "dossier_at": at - H if score is not None or red else None, "score": score, "red_flags": list(red),
            "statuses": statuses or {}, "symbol": coin.split(":")[1]}


def ids(trades):
    return [t["call_id"] for t in trades]


# ─── the position rule ───────────────────────────────────────────────────────
def test_one_position_per_coin_and_unpriced_calls_are_left_out():
    calls = [call(1), call(2, at=T + H), call(3, at=T + 25 * H), call(4, coin="solana:B", at=T + 26 * H, status="ungradeable", net=None),
             call(5, coin="solana:C", at=T + 27 * H, status="open", net=-20.0)]
    trades, skipped = bk.take(calls, bk.PICK["calls"], "horizon")
    assert ids(trades) == [1, 3, 5] and skipped == Counter(holding=1, unpriced=1)    # 2 came while 1 was still held
    assert (trades[2]["state"], trades[2]["value"]) == ("open", -20.0)                  # marked where it stands


def test_an_open_position_holds_its_coin_until_its_horizon_at_the_latest():
    calls = [call(1, status="open", net=None), call(2, at=T + 23 * H), call(3, at=T + D)]
    trades, skipped = bk.take(calls, bk.PICK["calls"], "horizon")
    assert ids(trades) == [1, 3] and skipped["holding"] == 1


def test_selling_at_2x_frees_the_coin_for_the_next_call():
    tp = {"status": "graded", "net": 100.5, "exit_at": T + 2 * H, "reason": "target"}
    calls = [call(1, tp=tp), call(2, at=T + 3 * H)]
    assert ids(bk.take(calls, bk.PICK["take_profit"], "take_profit")[0]) == [1, 2]
    assert ids(bk.take(calls, bk.PICK["calls"], "horizon")[0]) == [1]
    (first, _), _ = bk.take(calls, bk.PICK["take_profit"], "take_profit")[0], None
    assert first["value"] == 100.5 and first["exit_at"] == T + 2 * H


def test_a_trade_counts_once_its_24_hours_are_graded_in_every_book_alike():
    """Review 2026-10-02: selling at 2x banked its winners hours before the other books' twins closed."""
    tp = {"status": "graded", "net": 120.0, "exit_at": T + 2 * H, "reason": "target"}
    young = [call(1, status="open", net=40.0, tp=tp), call(2, coin="solana:B")]      # 1: sold at 2x, its 24 h still running
    (t1, t2), _ = bk.take(young, bk.PICK["take_profit"], "take_profit")
    assert (t1["state"], t1["settled"], t1["value"]) == ("closed", False, 120.0)     # sold, not counted yet
    assert t2["settled"] and t2["tp"] == 10.0
    s = bk.summarize(bk.BY_KEY["take_profit"], [t1, t2], Counter(), T + 3 * H, unit="%", stake=100.0, step=bk.HOUR, min_n=100)
    assert (s["closed"], s["open"], s["pnl_usd"], s["open_pnl_usd"], s["now_pnl_usd"]) == (1, 1, 10.0, 120.0, 130.0)


def gave_up(i, coin, net, tp=None):
    """A call the grader gave up on: the coin stopped trading 5 h in; its rows keep the last mark (ideas.GIVE_UP)."""
    c = call(i, coin=coin, status="open", net=net, tp=tp)
    return c | {"status": "ungradeable", "exit_reason": "no_candles", "net_return_pct": net, "exit_at": T + 5 * H} | (
        {} if tp else {"tp_status": "ungradeable", "tp_reason": "no_candles", "tp_net": net, "tp_exit_at": T + 5 * H})


def test_a_coin_that_stopped_trading_counts_at_its_last_price_in_every_book():
    """Review 2026-10-02: leaving given-up calls out dropped exactly the pump-then-die ones, and never counted a 2x
    sale made on them."""
    sold = {"status": "graded", "net": 148.0, "exit_at": T + H, "reason": "target"}
    calls = [gave_up(1, "solana:A", -73.0), gave_up(2, "solana:B", -60.0, tp=sold),
             call(3, coin="solana:C", status="ungradeable", net=None) | {"exit_reason": "no_liquidity"}]
    hold, skipped = bk.take(calls, bk.PICK["calls"], "horizon")
    assert [(t["value"], t["settled"], t["stalled"]) for t in hold] == [(-73.0, True, True), (-60.0, True, True)]
    assert skipped["unpriced"] == 1                                              # never priced at all: still left out
    tp, _ = bk.take(calls, bk.PICK["take_profit"], "take_profit")
    assert [(t["value"], t["settled"]) for t in tp] == [(-73.0, True), (148.0, True)]     # the 2x sale counts
    s = bk.summarize(bk.BY_KEY["calls"], hold, skipped, T + 3 * D, unit="%", stake=100.0, step=bk.HOUR, min_n=100)
    assert (s["closed"], s["stalled"], s["pnl_usd"]) == (2, 2, -133.0)


def test_each_book_is_set_against_what_it_should_beat_on_the_same_days():
    early = [call(i, coin=f"solana:E{i}", at=T - 3 * D + i * H, net=500.0, score=None) for i in range(5)]   # before dossiers
    later = [call(10 + i, coin=f"solana:L{i}", at=T + i * H, net=(20.0 if i % 2 else -40.0), score=90 if i % 2 else 70,
                  tp={"status": "graded", "net": 100.0, "exit_at": T + i * H + H, "reason": "target"} if i % 2 else None)
             for i in range(10)]
    books = {b["key"]: b for b in bk.dex_books(early + later, T + 5 * D)}
    v = books["score"]["vs_baseline"]
    assert v["against"] == "every call that had a scored dossier since this book began"
    # 85+ made +20 a call; every scored call since its first entry (L1 to L9: five at +20, four at -40) averaged
    # -6.7. The +500 calls from before dossiers existed are left out.
    assert v["value"] == pytest.approx(20.0 - (5 * 20.0 - 4 * 40.0) / 9, abs=1e-3)
    tp = books["take_profit"]["vs_baseline"]
    assert tp["against"] == "holding the same calls 24 hours"
    pairs = [(100.0 - 20.0) if i % 2 else 0.0 for i in range(10)] + [0.0] * 5           # the 5 early calls held = sold at 2x
    assert tp["value"] == pytest.approx(sum(pairs) / len(pairs), abs=1e-3)


def test_the_books_added_after_the_first_results_count_only_from_their_start():
    before, after = bk.START_V2 - H, bk.START_V2 + H
    calls = [call(1, coin="solana:A", at=before, score=90), call(2, coin="solana:B", at=after, score=90),
             call(3, coin="solana:C", at=after, score=80)]
    assert [c["id"] for c in calls if bk.PICK["score_tp"](c)] == [2]
    assert bk.BY_KEY["score_tp"].start == bk.BY_KEY["fade_quant"].start == bk.START_V2
    waiting = bk.summarize(bk.BY_KEY["score_tp"], [], Counter(), bk.START_V2 - H, unit="%", stake=100.0, step=bk.HOUR,
                           min_n=100)
    assert waiting["verdict"]["words"] == "Counts from Oct 3, 00:00 UTC."


def signal(i, scope, bias, as_of, ret):
    return {"id": i, "scope": scope, "bias": bias, "as_of": as_of, "valid_until": as_of + 7 * H, "return_pct": ret}


def test_the_opposite_of_the_quant_model_is_timed_and_costed_like_its_grading():
    t0 = bk.START_V2
    rows = [signal(1, "BTC", 1, t0, -2.0),            # a long call that lost 2%: the opposite made 2%
            signal(2, "BTC", 1, t0 + H, 1.0),          # BTC still held (until t0 + 7 h): skipped
            signal(3, "ETH", -2, t0, -1.5),           # a short call that lost 1.5%: the coin rose 1.5%
            signal(4, "SOL", 1, t0 - 2 * H, 3.0),     # its candle closed before the start: not counted
            signal(5, "XRP", -1, t0 + 2 * H, None)]   # not graded yet: open
    dv = {(sym, (t0 - D).date()): 1e15 for sym in ("BTC", "ETH", "XRP")}
    trades, skipped = bk.fade_trades(rows, dv, t0)
    cost = 2 * bk.sim.side_cost(1e15) * 100
    assert [t["call_id"] for t in trades] == [1, 3, 5] and skipped == Counter(holding=1, not_listed=0)
    assert trades[0]["at"] == t0 + H and trades[0]["value"] == pytest.approx(2.0 - cost)
    assert trades[1]["value"] == pytest.approx(1.5 - cost)                       # long ETH, which rose
    assert (trades[2]["state"], trades[2]["settled"]) == ("open", False)
    # In the t0 hour the model called BTC (it fell 2%) and ETH (it rose 1.5%): the average coin moved -0.25%.
    # Shorting BTC beat shorting the average coin by 1.75; buying ETH beat buying it by 1.75.
    assert trades[0]["vs_avg"] == pytest.approx(1.75) and trades[1]["vs_avg"] == pytest.approx(1.75)
    s = bk.fade_book(trades, skipped, t0 + 2 * D)
    assert s["vs_baseline"]["against"] == "the same side on an average coin the model called that hour, before costs"
    assert s["vs_baseline"]["value"] == pytest.approx(1.75)
    # a coin that only moved with the market adds nothing on either side
    flat = bk.fade_trades([signal(1, "BTC", 1, t0, 1.0), signal(2, "ETH", -1, t0, -1.0)], dv, t0)[0]
    assert [t["vs_avg"] for t in flat] == pytest.approx([0.0, 0.0])
    # a coin Hyperliquid didn't list the day before (OKX-only: costs we don't model, no US short) is left out, and of
    # the hour's average; judged per call, so a listing later doesn't bring its past calls in
    listed, skipped = bk.fade_trades(rows + [signal(9, "1INCH", 1, t0, -10.0)], dv, t0)
    assert [t["call_id"] for t in listed] == [1, 3, 5] and skipped["not_listed"] == 1
    assert listed[0]["vs_avg"] == pytest.approx(1.75)                            # 1INCH's -10% moved no average
    later = dv | {("1INCH", t0.date()): 1e15}                                   # listed from t0's day on
    assert [t["call_id"] for t in bk.fade_trades([signal(9, "1INCH", 1, t0, -10.0), signal(10, "1INCH", 1, t0 + D, -1.0)],
                                                 later, t0)[0]] == [10]


def test_the_dossier_rules_see_only_a_scored_dossier():
    calls = [call(1, coin="solana:A", score=90), call(2, coin="solana:B", score=None), call(3, coin="solana:C", score=84),
             call(4, coin="solana:D", score=88, red=("Thin liquidity: $3K",)), call(5, coin="solana:E", kind="fomo", score=95),
             call(6, coin="solana:F", kind="bot")]
    picked = {k: [c["id"] for c in calls if bk.PICK[k](c)] for k in bk.PICK}
    assert picked["score"] == [1, 4] and picked["clean"] == [1, 3] and picked["calls"] == picked["take_profit"] == [1, 2, 3, 4]
    assert picked["fomo"] == [5] and picked["bot"] == [6]


# ─── the numbers on a card ───────────────────────────────────────────────────
def trade(i, value, at, exit_at, state="closed"):
    return {"call_id": i, "coin": f"solana:{'ABC'[i - 1]}", "symbol": "ABC"[i - 1], "at": at, "exit_at": exit_at,
            "state": state, "value": value, "settled": state == "closed"}


def test_the_dollars_the_line_the_worst_dip_and_the_most_at_work():
    trades = [trade(1, 50.0, T, T + D), trade(2, -80.0, T + H, T + D + H), trade(3, -10.0, T + 2 * H, None, "open")]
    s = bk.summarize(bk.BY_KEY["calls"], trades, Counter(holding=2), T + 2 * D, unit="%", stake=100.0, step=bk.HOUR,
                     min_n=100)
    assert (s["pnl_usd"], s["staked_usd"], s["open_pnl_usd"], s["open_marked"]) == (-30.0, 200.0, -10.0, 1)
    assert s["now_pnl_usd"] == -40.0                                                     # with the open one sold now
    assert (s["win_rate"], s["max_in_use_usd"], s["max_drawdown_usd"]) == (0.5, 300.0, -80.0)
    assert s["curve"] == [[T, 0.0], [T + D, 50.0], [T + D + H, -30.0]]
    assert s["verdict"]["status"] == "too_early" and s["skipped"] == {"holding": 2}
    assert [b["symbol"] for b in s["best"]] == ["A", "B"] and s["worst"][0]["value"] == -80.0
    r = bk.summarize(bk.BY_KEY["strategist"], trades, Counter(), T + 2 * D, unit="R", stake=100.0, step=bk.HOUR, min_n=60)
    assert r["pnl_usd"] == 100.0 * (50 - 80)                                              # R: $100 a unit


def test_a_verdict_waits_for_its_sample_then_reads_the_interval():
    iv = {"value": 5.0, "lo": 1.0, "hi": 9.0}
    assert bk.verdict(None, 0, 0, None, 100, 14, "%")["status"] == "too_early"
    assert bk.verdict(iv, 150, 20, None, 100, 14, "%")["status"] == "making"
    assert bk.verdict({**iv, "lo": -1.0}, 150, 20, None, 100, 14, "%")["status"] == "unclear"
    assert bk.verdict({"value": -5.0, "lo": -9.0, "hi": -1.0}, 150, 20, None, 100, 14, "%")["status"] == "losing"
    assert bk.verdict(iv, 150, 3, date(2026, 10, 16), 100, 14, "%")["words"] == \
        "3 of the 14 days needed. First read around Oct 16."
    assert bk.verdict(iv, 9, 3, None, 100, 14, "R")["words"] == "9 of the 100 closed ideas needed and 3 of the 14 days needed."
    assert bk.verdict(iv, 9, 30, None, 100, 14, "%")["words"] == "9 of the 100 counted trades needed."


def test_the_first_read_comes_from_the_entries_plus_a_day_to_grade():
    today = date(2026, 10, 2)
    hourly = [datetime(2026, 9, 30, 12, tzinfo=UTC) + i * H for i in range(150)]      # Sep 30 12:00 to Oct 6 17:00
    assert bk.first_read(hourly, 100, 14, today) == date(2026, 10, 14)                   # the 14th day of entries, graded
    assert bk.first_read(hourly, 100, 1, today) == date(2026, 10, 5)                     # the 100th entry (Oct 4), graded
    assert bk.first_read([], 100, 14, today) is None
    ten = [datetime(2026, 10, 1, tzinfo=UTC)] * 10                                        # 10 a day: 9 more days, then graded
    assert bk.first_read(ten, 100, 1, today) == today + timedelta(days=10)


# ─── intervals ───────────────────────────────────────────────────────────────
def test_intervals_resample_whole_clusters():
    vals = [float(c) for c in range(10) for _ in range(100)]
    clustered, independent = bk.mean_interval(vals, [int(v) for v in vals]), bk.mean_interval(vals, list(range(1000)))
    assert clustered["value"] == independent["value"] == 4.5 and clustered["clusters"] == 10
    assert clustered["hi"] - clustered["lo"] > 3 * (independent["hi"] - independent["lo"])   # 10 days are 10 samples
    a, cl = [1.0] * 50 + [3.0] * 50, [i % 10 for i in range(100)]
    d = bk.diff_interval(a, cl, [x - 1.5 for x in a], cl)
    assert d["value"] == pytest.approx(1.5) and d["lo"] > 0
    same = bk.diff_interval(a, cl, list(a), cl)
    assert same["lo"] <= 0 <= same["hi"]
    assert bk.mean_interval([], []) is None and bk.diff_interval([1.0], [1], [], []) is None


def test_rank_correlation():
    x = list(range(100))
    r = bk.rank_corr(x, [v * 2.0 for v in x], x)
    assert r["value"] == pytest.approx(1.0) and r["lo"] > 0.99
    g = random.Random(3)
    z = bk.rank_corr(x, [g.random() for _ in x], x)
    assert z["lo"] < 0 < z["hi"]
    assert bk.rank_corr([1, 2], [1, 2], [1, 2]) is None
    assert list(bk._ranks([3, 1, 3])) == [1.5, 0.0, 1.5]                                  # ties share their ranks


# ─── factor rank ─────────────────────────────────────────────────────────────
D0 = date(2026, 9, 1)
DAYS = [D0 + timedelta(days=i) for i in range(10)]
CLOSES = {d: {"A": 100.0 if d == D0 else 110.0, "B": 100.0 if d == D0 else 90.0} for d in DAYS}   # A +10%, B -10% on day 1
DV = {(s, d): 1e15 for s in "AB" for d in DAYS}                                        # deep: the fee and a 1 bp spread


def test_legs():
    q = {"A": 5, "B": 1, "C": 3, "D": 5}
    assert bk.legs(q, "long_short") == [("A", 0.25), ("D", 0.25), ("B", -0.5)]
    assert bk.legs(q, "long") == [("A", 0.5), ("D", 0.5)]
    assert bk.legs(q, "universe") == [(s, 0.25) for s in "ABCD"]
    assert bk.legs({"A": 5}, "long_short") == []


FILLS = {("A", D0 + D): 100.0, ("B", D0 + D): 100.0, ("A", D0 + 8 * D): 110.0, ("B", D0 + 8 * D): 90.0}   # 01:00 prices
LS = [("A", 0.5), ("B", -0.5)]


def test_a_slice_trades_at_01_utc_after_its_ranking_is_marked_daily_and_sells_a_week_later():
    cost = bk.sim.side_cost(1e15)
    n = 1000 / (1 + cost)                         # $7,000 / 7: half long A, half short B, both net of the entry cost
    eq = dict(bk.factor_equity({D0 + D: LS}, CLOSES, FILLS, DV, {}, 7000.0, DAYS[-1]))
    assert min(eq) == D0 + D                                                    # day 0's ranking trades on day 1
    assert eq[D0 + D] == round(6000 + 1.1 * n, 2)                               # long +10%, short +10% by day 1's close
    assert eq[D0 + 8 * D] == round(6000 + 1.1 * n * (1 - cost), 2)              # sold at 01:00 a week later
    assert eq[DAYS[-1]] == eq[D0 + 8 * D]                                       # nothing open after that


def test_the_fill_is_the_first_hourly_close_after_the_ranking_else_the_ranked_close():
    cost = bk.sim.side_cost(1e15)
    n = 1000 / (1 + cost)
    late = dict(bk.factor_equity({D0 + D: [("A", 1.0)]}, CLOSES, {("A", D0 + D): 105.0}, DV, {}, 7000.0, DAYS[-1]))
    assert late[D0 + D] == round(6000 + n * 110 / 105, 2)                       # bought at 01:00, after part of the move
    fallback = dict(bk.factor_equity({D0 + D: [("A", 1.0)]}, CLOSES, {}, DV, {}, 7000.0, DAYS[-1]))
    assert fallback[D0 + D] == round(6000 + n * 1.1, 2)                         # no 1h candle: the ranked day's close


def test_funding_charges_the_long_and_pays_the_short_from_the_trade_day():
    cost = bk.sim.side_cost(1e15)
    n = 1000 / (1 + cost)
    base = dict(bk.factor_equity({D0 + D: LS}, CLOSES, FILLS, DV, {}, 7000.0, DAYS[-1]))
    on_a = dict(bk.factor_equity({D0 + D: LS}, CLOSES, FILLS, DV, {("A", D0 + 2 * D): 0.001}, 7000.0, DAYS[-1]))
    on_b = dict(bk.factor_equity({D0 + D: LS}, CLOSES, FILLS, DV, {("B", D0 + 2 * D): 0.001}, 7000.0, DAYS[-1]))
    assert on_a[D0 + 2 * D] == pytest.approx(base[D0 + 2 * D] - 0.001 * n / 2, abs=0.011)
    assert on_b[D0 + 2 * D] == pytest.approx(base[D0 + 2 * D] + 0.001 * n / 2, abs=0.011)
    day_one = dict(bk.factor_equity({D0 + D: [("A", 1.0)]}, CLOSES, FILLS, DV, {("A", D0 + D): 0.5}, 7000.0, DAYS[-1]))
    assert day_one[D0 + D] == round(6000 + 1.1 * n - 0.5 * n, 2)               # held from 01:00: the day's funding is owed


def test_weeks_and_their_verdict():
    vals = [100.0] * 6 + [101.0] + [101.0] * 6 + [102.01]
    curve = [(D0 + timedelta(days=i), v) for i, v in enumerate(vals)]
    rets = bk.weekly(curve, 100.0)
    assert rets == pytest.approx([0.01, 0.01]) and bk.week_stats(rets)["mean_pct"] == 1.0
    assert bk.week_stats([])["weeks"] == 0
    v = bk.factor_verdict(bk.week_stats(rets), D0, date(2026, 9, 15), "Making")
    assert v["status"] == "too_early" and v["first_read"] == D0 + timedelta(days=42)
    up = bk.factor_verdict({"weeks": 8, "mean_pct": 0.5, "t": 2.4, "up_pct": 75}, D0, date(2026, 11, 1), "Making")
    assert up["status"] == "making" and "+0.50% a week over 8 weeks" in up["words"]


# ─── the dossier report ──────────────────────────────────────────────────────
def world(n_each: int) -> list[dict]:
    """n_each clean calls that made 10% and n_each flagged ones that lost 30%, three calls a coin."""
    out = []
    for i in range(2 * n_each):
        red = i % 2 == 1
        out.append(call(i, coin=f"solana:C{i // 3}", at=T + i * timedelta(minutes=5), net=-30.0 if red else 10.0,
                        score=60 if red else 90, red=("Thin liquidity: $2K",) if red else (),
                        statuses={"liquidity": "bad" if red else "good", "x": "unknown"}))
    return out


def test_the_dossier_report_waits_for_its_sample():
    r = bk.dossier_report(world(100), {"liquidity": "Liquidity"}, T + 2 * D)
    assert r["verdict"]["status"] == "too_early" and r["population"]["scored"] == 200
    assert "the first read needs 500" in r["verdict"]["words"]


def test_the_dossier_report_reads_both_questions_set_in_advance(monkeypatch):
    r = bk.dossier_report(world(300), {"liquidity": "Liquidity"}, T + 2 * D)
    assert r["verdict"]["status"] == "helps" and r["verdict"]["words"].startswith("Calls without a red flag did +40.0 points")
    assert (r["red"]["none"]["mean_pct"], r["red"]["any"]["mean_pct"]) == (10.0, -30.0) and r["red"]["diff"]["lo"] > 0
    assert r["score"]["rho"]["value"] > 0.8
    assert [(b["label"], b["n"]) for b in r["score"]["bands"]] == [("under 70", 300), ("70 to 79", 0), ("80 to 84", 0),
                                                                    ("85 or more", 300)]
    (flag,) = r["flags"]
    assert (flag["label"], flag["n"], flag["mean_pct"], flag["others_mean_pct"]) == ("Liquidity", 300, -30.0, 10.0)
    line = next(ln for ln in r["lines"] if ln["key"] == "liquidity")
    assert line["by"]["good"]["mean_pct"] == 10.0 and line["by"]["bad"]["n"] == 300 and line["by"]["warn"]["n"] == 0
    assert bk.dossier_report(world(300), {"liquidity": None}, T + 2 * D)["flags"][0]["label"] == "liquidity"   # never blank
    flipped = [dict(c, net_return_pct=-c["net_return_pct"]) for c in world(300)]
    assert bk.dossier_report(flipped, {}, T + 2 * D)["verdict"]["status"] == "hurts"


def test_the_deep_end_books_are_measured_against_every_call():
    calls = world(10) + [call(100, coin="solana:F1", kind="fomo", at=T + 3 * D)]
    unpriced = {"call": [T - D, T + H, T + 2 * H], "fomo": [T + 3 * D]}           # never priced: in no book
    books = {b["key"]: b for b in bk.dex_books(calls, T + 5 * D, unpriced)}
    assert set(books) == {"calls", "clean", "score", "take_profit", "score_tp", "fomo", "bot"}
    assert (books["calls"]["never_priced"], books["fomo"]["never_priced"], books["bot"]["never_priced"]) == (2, 1, 0)
    assert "vs_baseline" not in books["calls"] and books["clean"]["vs_baseline"]["value"] > 0
    assert books["clean"]["skipped"]["no_dossier"] == 0 and books["fomo"]["taken"] == 1
    assert books["calls"]["taken"] == 7                       # 20 calls on 7 coins, three a coin within the day held


# ─── on Postgres (ALBATROSS_TEST_DSN: a migrated database, never production) ─
DSN = os.environ.get("ALBATROSS_TEST_DSN")
db_only = pytest.mark.skipif(not DSN, reason="set ALBATROSS_TEST_DSN to a migrated Postgres")
MIGRATION = Path(__file__).parents[1] / "db" / "migrations" / "036_paper_books.sql"
MINT = "BooksTestMint111111111111111111111111111111"


class _Pool:
    """db.pool() stand-in: every acquire() hands out the test's connection (inside its transaction)."""

    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False
        return _Ctx()

    def __getattr__(self, name):
        return getattr(self.conn, name)


async def _ledger_call(conn, at: datetime, scope: str, evidence: dict) -> int:
    """A DEX call logged at `at`, chained like ledger.append (which always stamps now)."""
    from albatross import ledger
    prev = await conn.fetchval("SELECT hash FROM trade_calls ORDER BY id DESC LIMIT 1") or ledger.GENESIS
    row = {k: None for k in ledger.FIELDS} | {
        "logged_at": at, "source_kind": "caller", "source_id": "books-test", "scope": scope, "market": "dex",
        "direction": "long", "tf": "1d", "entry_type": "market", "ref_price": 1.0, "targets": [], "horizon_until": at + D,
        "status": "proposed",
        "evidence": evidence}
    return await conn.fetchval(
        f"""INSERT INTO trade_calls ({", ".join(ledger.FIELDS)}, prev_hash, hash)
            VALUES ({", ".join(f"${i + 1}" for i in range(len(ledger.FIELDS) + 2))}) RETURNING id""",
        *[orjson.dumps(row[k]).decode() if k in ("evidence", "critique") and row[k] is not None else row[k]
          for k in ledger.FIELDS], prev, ledger._digest(prev, row))


def _in_tx(scenario, monkeypatch):
    import asyncpg

    from albatross import db

    async def go():
        conn = await asyncpg.connect(DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(MIGRATION.read_text())         # a no-op where 036 is applied
            pool = _Pool(conn)

            async def fake_pool():
                return pool
            monkeypatch.setattr(db, "pool", fake_pool)
            await scenario(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


@db_only
def test_take_profit_is_written_for_a_graded_call_and_its_resolved_rows_stay(monkeypatch):
    from albatross.backtest import ideas

    async def scenario(conn):
        at = datetime.now(UTC).replace(microsecond=0) - timedelta(days=3)
        ev = {"kind": "call", "liquidity_usd": 1e12, "liquidity_src": "dexscreener", "price": {"dex": "raydium", "labels": []}}
        cid = await _ledger_call(conn, at, f"solana:{MINT}", ev)
        await conn.execute("INSERT INTO micro_state (chain, token, symbol, candles_at) VALUES ('solana', $1, 'BKTEST', now())",
                           MINT)
        for m, close in ((10, 2.2), (300, 0.4)):
            await conn.execute(
                """INSERT INTO micro_candles_1m (chain, token, ts, open, high, low, close, source)
                   VALUES ('solana', $1, $2, $3, $3, $3, $3, 'birdeye')""",
                MINT, at.replace(second=0) + timedelta(minutes=m), close)
        for policy in ("horizon", "stop_target"):              # graded before take_profit existed
            await conn.execute(
                """INSERT INTO trade_call_outcomes (call_id, policy, status, net_return_pct, graded_at)
                   VALUES ($1, $2, 'graded', -61.0, now())""", cid, policy)
        # an open call, past its 5m and 1h checkpoints and short of 6h: nothing new to grade but its missing take_profit
        mint2 = MINT.replace("111", "222", 1)
        at2 = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=3)
        cid2 = await _ledger_call(conn, at2, f"solana:{mint2}", ev)
        await conn.execute("INSERT INTO micro_state (chain, token, symbol, candles_at) VALUES ('solana', $1, 'BKTEST2', now())",
                           mint2)
        await conn.execute("""INSERT INTO micro_candles_1m (chain, token, ts, open, high, low, close, source)
                              VALUES ('solana', $1, $2, 2.5, 2.5, 2.5, 2.5, 'birdeye')""",
                           mint2, at2.replace(second=0) + timedelta(minutes=20))
        for policy in ("horizon", "stop_target"):
            await conn.execute(
                """INSERT INTO trade_call_outcomes (call_id, policy, status, ret_5m_pct, ret_1h_pct, graded_at)
                   VALUES ($1, $2, 'open', 0.0, 150.0, now())""", cid2, policy)
        rows = [r for r in await ideas._dex(False, datetime.now(UTC)) if r[0] in (cid, cid2)]
        assert sorted((r[0], r[1]) for r in rows if r[0] == cid) == [(cid, "take_profit")]   # only the missing policy
        o = dict(zip(ideas.COLS, next(r for r in rows if r[0] == cid)))
        assert (o["status"], o["exit_reason"]) == ("graded", "target") and o["net_return_pct"] > 100
        assert o["exit_at"] == at.replace(second=0) + timedelta(minutes=11)
        o2 = {r[1]: dict(zip(ideas.COLS, r)) for r in rows if r[0] == cid2}
        assert o2["take_profit"]["exit_reason"] == "target" and o2["horizon"]["status"] == "open"
        # given up on: its candles stopped at minute 30, four days ago. The 24 h can't be priced; the 2x sale stands.
        mint3 = MINT.replace("111", "333", 1)
        at3 = datetime.now(UTC).replace(microsecond=0) - timedelta(days=4)
        cid3 = await _ledger_call(conn, at3, f"solana:{mint3}", ev)
        await conn.execute("INSERT INTO micro_state (chain, token, symbol, candles_error) VALUES ('solana', $1, 'BKTEST3', '429')",
                           mint3)
        for m, close in ((10, 2.2), (30, 1.5)):
            await conn.execute("""INSERT INTO micro_candles_1m (chain, token, ts, open, high, low, close, source)
                                  VALUES ('solana', $1, $2, $3, $3, $3, $3, 'birdeye')""",
                               mint3, at3.replace(second=0) + timedelta(minutes=m), close)
        o3 = {r[1]: dict(zip(ideas.COLS, r)) for r in await ideas._dex(False, datetime.now(UTC)) if r[0] == cid3}
        assert (o3["horizon"]["status"], o3["horizon"]["exit_reason"]) == ("ungradeable", "no_candles")
        # the last mark stays: sold at the last covered minute (the 1.5 close at minute 30, covered to minute 31)
        sim = ideas.sim
        last = sim.dex_round_trip_pct(1.0, 1.5, 1e12, sim.exit_liquidity(None, 1e12, 1.0, 1.5), sim.dex_fee("raydium", []),
                                      sim.GAS_USD["solana"])
        assert o3["horizon"]["net_return_pct"] == pytest.approx(last, abs=1e-4) and o3["horizon"]["exit_price"] == 1.5
        assert o3["horizon"]["exit_at"] == at3.replace(second=0) + timedelta(minutes=31)
        assert (o3["take_profit"]["status"], o3["take_profit"]["exit_reason"]) == ("graded", "target")
    _in_tx(scenario, monkeypatch)


@db_only
def test_the_job_stores_both_reports_and_the_api_reads_them_back(monkeypatch):
    from albatross.api import books as api

    async def scenario(conn):
        out = await bk.run()
        assert out["rows"] == len(bk.BOOKS) + 1
        got = await api.books()
        assert [b["key"] for b in got["books"]["books"]] == [b.key for b in bk.BOOKS]
        assert got["books"]["computed_at"] is not None and got["books"]["rules"]["score_min"] == bk.SCORE_MIN
        assert set(got["dossier"]) >= {"population", "red", "score", "flags", "lines", "verdict"}
    _in_tx(scenario, monkeypatch)


def test_the_api_answers_before_the_first_run(monkeypatch):
    import asyncpg

    from albatross import db
    from albatross.api import books as api

    class P:
        async def fetch(self, sql, *a):
            raise asyncpg.UndefinedTableError("relation \"paper_reports\" does not exist")

    async def pool():
        return P()
    monkeypatch.setattr(db, "pool", pool)
    assert asyncio.run(api.books()) == {"books": None, "dossier": None}
