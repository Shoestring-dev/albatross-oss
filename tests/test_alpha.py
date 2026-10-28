"""The Alpha section (albatross/alpha): the rebuilt checklist's flags, the weekly IC on fixed days, and the review's
own consistency. Nothing here may switch anything off: the section only measures."""

from datetime import date, timedelta

import pandas as pd

from albatross.alpha import avoid, edge, review
from albatross.lab import factor


def test_the_rebuilt_checklist_flags_young_coins_and_unpaid_listings_and_drops_lp_and_insiders():
    f = {"n_red_flags": 2.0, "age_h": 30.0, "check:lp": "bad", "check:insiders": "bad", "check:dexpaid": "good"}
    assert avoid.rebuilt_flags(f) == []                                       # LP and insiders are information now
    assert avoid.rebuilt_flags({**f, "age_h": 0.5}) == ["young"]
    assert avoid.rebuilt_flags({**f, "check:dexpaid": "warn"}) == ["unpaid"]
    assert avoid.rebuilt_flags({**f, "age_h": 3.0, "check:dexpaid": "warn"}) == ["young"]     # unpaid counts after 24 h
    assert avoid.rebuilt_flags({**f, "check:dev": "bad"}) == ["dev"]
    assert avoid.rebuilt_flags({"n_red_flags": None, "age_h": 0.5}) is None   # no dossier: nothing to judge


def test_the_fixed_rules_are_recorded_only_after_they_were_written_down():
    assert avoid.FORWARD_FROM.date() == date(2026, 10, 10)
    c = {"source_kind": "caller", "f": {"age_h": 48.0, "check:dev": "good", "check:liquidity": "good"}}
    assert avoid.seasoned(c) and not avoid.seasoned({**c, "f": {**c["f"], "check:dev": "bad"}})
    assert not avoid.seasoned({**c, "source_kind": "bot"})


def test_the_weekly_ic_uses_fixed_days_and_the_next_seven_closes():
    days = pd.date_range("2026-06-01", periods=60, freq="D")                 # 2026-06-01 is a Monday
    syms = [f"C{i}" for i in range(20)]
    growth = {s: 1 + 0.001 * i for i, s in enumerate(syms)}                  # higher number, higher return
    px = pd.DataFrame({s: [growth[s] ** k for k in range(len(days))] for s in syms}, index=days)
    ranks = pd.DataFrame([{"day": d.date(), "symbol": s, "score": float(i), "quintile": 1 + i // 4, "live": False}
                          for d in days for i, s in enumerate(syms)])
    rows = edge.weekly(ranks, px)
    assert rows and all((r["day"] - factor.EPOCH).days % 7 == 0 for r in rows)
    assert all(abs(r["ic"] - 1.0) < 1e-9 and r["spread"] > 0 for r in rows)  # a perfect ranking
    assert rows[-1]["day"] + timedelta(days=7) <= days[-1].date()


def test_the_review_is_complete_and_switches_nothing_off():
    p = review.payload()
    assert [b["rank"] for b in p["bets"]] == [1, 2, 3, 4, 5]
    for c in p["components"]:
        assert c["lane"] in ("memecoins", "majors", "measuring") and c["finding"]
        assert c["verdict"] in ("keep", "adjust", "abandon", "fixed")
        assert c["status"] in ("running", "on trial", "proposed", "done", "paper only")
        assert c.get("page") in (None, "avoid", "edge")
    # Nothing removed yet: an abandon verdict is a proposal and the component still runs
    assert all(c["status"] == "running" for c in p["components"] if c["verdict"] == "abandon" and not c.get("page"))


def test_every_live_call_gets_a_verdict_from_its_own_dossier():
    from datetime import UTC, datetime

    from albatross.alpha import live
    assert live.verdict({"n_red_flags": None}) == ("unread", [])
    assert live.verdict({"n_red_flags": 0.0, "age_h": 30.0}) == ("pass", [])
    assert live.verdict({"n_red_flags": 1.0, "age_h": 0.2, "check:dev": "bad"}) == ("skip", ["dev", "young"])
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    base = {"source_kind": "caller", "scope": "solana:abc", "symbol": "ABC", "status": None, "net_return_pct": None,
            "exit_at": None, "exit_reason": None, "tp_status": None, "tp_net": None, "tp_exit_at": None, "tp_reason": None}
    calls = [{**base, "logged_at": now - timedelta(hours=h), "f": {"n_red_flags": 0.0, "age_h": 30.0}} for h in (60, 30, 2)]
    rows = live.feed(calls, now)
    assert [r["at"] for r in rows] == [now - timedelta(hours=2), now - timedelta(hours=30)]     # 48 hours, newest first
    assert rows[0]["verdict"] == "pass" and rows[0]["state"] == "open" and rows[0]["net"] is None
    assert rows[1]["state"] == "settling"                                  # its 24 hours are up, not graded yet
    again = [{**calls[2], "logged_at": now - timedelta(hours=2) + timedelta(minutes=m), "source_id": f"G{m}"} for m in (0, 3, 9)]
    rows = live.feed(again, now)                                           # called again 3 min later: one row
    assert [r["calls"] for r in rows] == [1, 2] and rows[1]["who"] == ["G0", "G3"]


def test_the_feeds_sums_count_every_call_as_the_test_tile_does():
    """Alpha › Live: the feed's summary under "The test" with group calls must equal the hero tile to the decimal, so a
    row that merges repeat calls carries each call's own result, and the sums count calls, not rows."""
    from datetime import UTC, datetime

    from albatross.alpha import live
    now = datetime(2026, 10, 10, 20, tzinfo=UTC)
    def call(minutes, net, flags, kind="caller", who="G"):
        f = {"n_red_flags": 0.0, "age_h": 2.0 if flags else 30.0}
        return {"source_kind": kind, "source_id": who, "scope": f"solana:{minutes // 10}", "symbol": "X", "ref_price": 1.0,
                "logged_at": now - timedelta(minutes=minutes), "f": f, "status": "graded" if minutes > 1440 else None,
                "net_return_pct": net, "exit_at": None, "exit_reason": None, "tp_status": None, "tp_net": None,
                "tp_exit_at": None, "tp_reason": None}
    calls = sorted([call(100, -10.0, False), call(102, -30.0, False, who="H"),     # one coin twice in 5 min: one row
                    call(300, 5.0, False), call(500, -60.0, True), call(501, -80.0, True, who="H"),
                    call(700, None, True), call(800, -20.0, True, kind="bot")], key=lambda c: c["logged_at"])
    rows = live.feed(calls, now)
    tile = live.live_test(calls, now)["caller"]
    for verdict in ("pass", "skip"):
        each = [net for r in rows if r["test"] and r["kind"] == "caller" and r["verdict"] == verdict
                for net, _ in r["each"] if net is not None]
        assert len(each) == tile[verdict]["so_far"]["n"]
        assert round(sum(each) / len(each), 2) == tile[verdict]["so_far"]["mean"]
    assert len(rows) == 5 and sum(r["calls"] for r in rows) == 7


def test_a_repeat_call_with_another_verdict_keeps_its_own_row():
    from datetime import UTC, datetime

    from albatross.alpha import live
    now = datetime(2026, 10, 10, 20, tzinfo=UTC)
    base = {"source_kind": "caller", "source_id": "G", "scope": "solana:a", "symbol": "A", "ref_price": 1.0, "status": None,
            "net_return_pct": -5.0, "exit_at": None, "exit_reason": None, "tp_status": None, "tp_net": None,
            "tp_exit_at": None, "tp_reason": None}
    calls = [{**base, "logged_at": now - timedelta(minutes=10), "f": {"n_red_flags": None}},              # no dossier yet
             {**base, "logged_at": now - timedelta(minutes=8), "f": {"n_red_flags": 0.0, "age_h": 2.0}}]  # then a skip
    assert [r["verdict"] for r in live.feed(calls, now)] == ["skip", "unread"]
