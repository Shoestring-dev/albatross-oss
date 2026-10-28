"""The Deep End contract (docs/SPEC-DEEP-END.md section 4): the fixtures the UI builds against carry every
top-level key the API promises, and the registry loads every owner module."""

import json
from pathlib import Path

FIX = Path(__file__).parents[1] / "docs" / "deepend" / "fixtures"
TOKEN_KEYS = {"token", "market", "call", "calls", "security", "holders", "traders", "x", "fit", "news", "github", "fomo",
              "reddit", "wallets", "checklist", "flag_checks", "score", "freshness"}
ROW_KEYS = {"chain", "address", "symbol", "name", "logo", "age_s", "price", "mcap", "liquidity", "change_h1",
            "since_call_pct", "peak_x", "n_calls", "n_groups", "calls_24h", "first_call_at", "x_heat", "score",
            "red_flags", "flag_checks", "discovered_via", "spark"}
STATUSES = {"good", "warn", "bad", "unknown"}


def test_token_fixture_matches_the_contract():
    d = json.loads((FIX / "token.json").read_text())
    assert TOKEN_KEYS <= set(d)
    assert all(c["status"] in STATUSES and c["source"] and isinstance(c["scored"], bool) for c in d["checklist"])
    s = d["score"]
    assert s["good"] + s["warn"] + s["bad"] + s["unknown"] == sum(c["scored"] for c in d["checklist"])


def test_feed_and_candles_fixtures_match_the_contract():
    feed = json.loads((FIX / "feed.json").read_text())
    assert feed["rows"] and all(ROW_KEYS <= set(r) for r in feed["rows"])
    c = json.loads((FIX / "candles.json").read_text())
    assert {"source", "candles", "markers", "until"} <= set(c)
    assert all(len(x) == 6 for x in c["candles"]) and all(m["kind"] in {"call", "bot", "x"} for m in c["markers"])


def test_registry_loads_every_owner_module():
    from albatross.deepend import registry
    for name in registry.MODULES:
        m = __import__(f"albatross.deepend.{name}", fromlist=["x"])
        for attr in ("JOBS", "SCHEDULE", "HOST_JOBS", "HOST_SCHEDULE", "CADENCE"):
            assert hasattr(m, attr), (name, attr)
    assert set(registry.CADENCE) >= set(registry.JOBS) | set(registry.HOST_JOBS)


def test_trending_majors_stay_warm():
    from datetime import UTC, datetime, timedelta
    from albatross.deepend import watch
    now = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    trending = {"trending_at": now - timedelta(minutes=5), "discovered_at": now - timedelta(hours=3)}
    assert watch.heat({**trending, "discovered_mcap": 2.2e6}, now) == ("hot", ["trending"])
    assert watch.heat({**trending, "discovered_mcap": 336e6}, now)[0] == "warm"          # BONK-sized: not the trenches
    assert watch.heat({**trending, "discovered_mcap": None}, now) == ("hot", ["trending"])


def test_an_old_bot_bag_is_warm_a_fresh_buy_is_hot():
    from datetime import UTC, datetime, timedelta
    from albatross.deepend import watch
    now = datetime(2026, 9, 30, 18, 0, tzinfo=UTC)
    assert watch.heat({"held": True, "last_entry": now - timedelta(minutes=30)}, now) == ("hot", ["bot just bought it"])
    assert watch.heat({"held": True, "last_entry": now - timedelta(days=2)}, now)[0] == "warm"
