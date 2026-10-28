"""Today page helpers: the report excerpt and how trade ideas are folded into the 24h radar."""

from datetime import UTC, datetime, timedelta

from albatross.api.overview import _excerpt, _idea_items

T = datetime(2026, 9, 29, 22, 43, 46, tzinfo=UTC)


def idea(scope, secs=0, direction="long", tf="1h", status="proposed", verdict="keep", source="strategist", rr=2.0):
    return {"id": hash(scope) % 1000, "logged_at": T - timedelta(seconds=secs), "scope": scope, "direction": direction,
            "tf": tf, "status": status, "reward_risk": rr, "source_id": source, "verdict": verdict}


def test_excerpt_drops_headings_and_list_markers_and_reads_snake_case():
    md = ("## Book\nRegimes: ETH trending_up, DOGE bounce_in_downtrend; ret_1 and vol_24h.\n\n## Ideas\n"
          "- Only **AERO** survived.\n1. See [x](https://x.com/a_b)")
    assert _excerpt(md) == "Regimes: ETH trending up, DOGE bounce in downtrend; ret 1 and vol 24h. Only AERO survived. See x"


def test_excerpt_cuts_on_a_word():
    out = _excerpt("word " * 100, n=22)
    assert out == "word word word word…"


def test_a_strategist_batch_is_one_radar_entry():
    rows = [idea("PENDLE", 0, status="vetoed", verdict="veto"), idea("POL", 1), idea("FET", 2, tf="4h"),
            idea("AAVE", 3), idea("BCH", 4, tf="4h"), idea("TAO", 5), idea("RUNE", 6), idea("AERO", 7200, tf="1d")]
    batch, single = _idea_items(rows)
    assert batch["n"] == 7 and batch["title"] == "7 new ideas, all long" and batch["tone"] == "bull"
    assert batch["detail"] == "PENDLE, POL, FET, AAVE, BCH +2 more · 1h, 4h · 1 vetoed by the critic"
    assert batch["at"] == T
    assert single["title"] == "Long AERO · 1d" and single["detail"] == "R:R 2.00 · critic: keep" and "n" not in single


def test_mixed_batches_say_so_and_other_sources_stay_separate():
    rows = [idea("BTC", 0, direction="short"), idea("ETH", 30), idea("SOL", 40, source="oracle")]
    batch, single = _idea_items(rows)
    assert batch["title"] == "2 new ideas, 1 long, 1 short" and batch["tone"] == "accent"
    assert batch["detail"] == "BTC, ETH · 1h"
    assert single["title"] == "Long SOL · 1h"


def test_a_lone_vetoed_idea_reads_plainly():
    (item,) = _idea_items([idea("MON", status="vetoed", verdict="veto", rr=None)])
    assert item["detail"] == "vetoed by the critic" and item["tone"] == "bull"
