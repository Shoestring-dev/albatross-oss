"""The X Scout (albatross/deepend/xintel.py) and the trenches search in Social Pulse.

The fixtures in tests/fixtures/deep_x are Grok CLI calls recorded on the host on 2026-09-30 with the production
flags: a 3-token read at the default reasoning effort, the same 4 tokens at low effort (it dropped the post list) and
at medium (production), and two Social Pulse calls with the trenches search. The X handles, post text and the low bits
of each status id are synthetic; each id keeps its timestamp bits, so the gate's time checks see the recorded times.
The DB scenarios at the bottom need a migrated Postgres (ALBATROSS_TEST_DSN, never production) and run inside a
transaction that is rolled back.
"""

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from albatross.agents import social_pulse as sp
from albatross.deepend import xintel as x
from albatross.llm import cli

FIX = Path(__file__).parent / "fixtures" / "deep_x"
CONTRACT = Path(__file__).parents[1] / "docs" / "deepend" / "fixtures"
NOW = datetime(2026, 9, 30, 17, 0, tzinfo=UTC)
M, H = timedelta(minutes=1), timedelta(hours=1)


def recorded(name: str) -> dict:
    d = json.loads((FIX / name).read_text())
    d["now"] = datetime.fromisoformat(d["now"])
    for t in d.get("tokens", []):
        for k in ("created_at", "first_call", "last_read"):
            if t.get(k):
                t[k] = datetime.fromisoformat(t[k])
    return d


def parsed(d: dict, schema=x.Scout):
    raw, meta = cli._parse_grok(json.dumps(d["envelope"]))
    return schema.model_validate_json(raw), meta


def judged(d: dict, scout=None):
    scout = scout or parsed(d)[0]
    return x.judge(scout, d["tokens"], d["searches"], d["now"], d["now"] + timedelta(seconds=d["latency_s"]))


def url(when: datetime, handle: str = "trader") -> str:
    """An x.com status URL whose snowflake id encodes `when`."""
    return f"https://x.com/{handle}/status/{int(when.timestamp() * 1000) - sp.TWITTER_EPOCH_MS << 22}"


# ─── the searches ────────────────────────────────────────────────────────────
def test_handles_come_from_account_links_only():
    # The links DexScreener listed for tokens called on 2026-09-30.
    assert x.handle_from("https://x.com/proj_nullmask") == "proj_nullmask"
    assert x.handle_from("https://x.com/proj_athena?s=11") == "proj_athena"
    assert x.handle_from("https://www.twitter.com/proj_hotdog/") == "proj_hotdog"
    assert x.handle_from("@proj_saw") == "proj_saw"
    assert x.handle_from("https://twitter.com/poster_083/status/2105332447917309717") is None   # the meme's tweet
    assert x.handle_from("https://x.com/i/communities/2022311512524377116") is None
    assert x.handle_from("https://x.com/search?q=0xcaf66eb2c00d206d741a654face34768cf2a7777&src=typed_query") is None
    assert x.handle_from("https://t.me/proj_nullmask") is None and x.handle_from(None) is None


def test_the_talk_search_uses_a_cashtag_only_when_it_can_find_the_coin():
    majors = {"MASK", "ATH", "BTC"}
    assert x.talk_query({"symbol": "LONG", "name": "longnose"}, 2, majors) == "$LONG min_faves:3 within_time:2h"
    # 19 recent tokens were called $SI on 2026-09-30 (the "super intelligence" meta): the cashtag finds the others.
    assert x.talk_query({"symbol": "SI", "name": "Super Iguana", "shared_ticker": 18}, 6, majors) == \
        '"Super Iguana" min_faves:3 within_time:6h'
    assert x.talk_query({"symbol": "BI", "name": "Binance Inu", "shared_ticker": 1}, 3, majors).startswith("$BI ")
    # A major's ticker would find the major: the name (long enough to be distinctive) and the account stand in.
    assert x.talk_query({"symbol": "MASK", "name": "Nullmask", "handle": "proj_nullmask"}, 6, majors) == \
        '("Nullmask" OR @proj_nullmask) min_faves:3 within_time:6h'
    assert x.talk_query({"symbol": "ATH", "name": "Athena", "handle": "proj_athena"}, 3, majors) == \
        "@proj_athena min_faves:3 within_time:3h"
    assert x.talk_query({"symbol": "ATH", "name": "Athena"}, 3, majors) is None
    assert x.talk_query({"symbol": "招财猫", "name": "招财猫"}, 1, majors) == '"招财猫" min_faves:3 within_time:1h'
    assert x.talk_query({"symbol": None, "name": None}, 1, majors) is None


def test_the_window_covers_a_new_token_s_life_or_the_time_since_the_last_read():
    assert x.window_h({"created_at": NOW - 70 * M}, NOW) == 2
    assert x.window_h({"created_at": NOW - timedelta(days=3)}, NOW) == 6
    assert x.window_h({"first_call": NOW - 30 * M}, NOW) == 1
    assert x.window_h({"created_at": NOW - timedelta(days=3), "last_read": NOW - 50 * M}, NOW) == 1
    assert x.window_h({"last_read": NOW - timedelta(hours=5)}, NOW) == 3
    assert x.window_h({}, NOW) == 6


def test_searches_are_spelled_out_and_numbered_per_token():
    toks = [{"chain": "solana", "address": "CA1", "symbol": "LONG", "name": "longnose", "window_h": 2},
            {"chain": "base", "address": "0xca2", "symbol": "SI", "name": "Super Iguana", "handle": "iguana",
             "account_search": True, "window_h": 1, "shared_ticker": 4},
            {"chain": "bsc", "address": "0xca3", "symbol": "BI", "handle": "binu", "account_search": False,
             "window_h": 3}]
    searches = x.plan(toks, set())
    assert [(s["n"], s["token"], s["facet"]) for s in searches] == [
        (1, "T1", "contract"), (2, "T1", "talk"), (3, "T2", "contract"), (4, "T2", "talk"), (5, "T2", "account"),
        (6, "T3", "contract"), (7, "T3", "talk")]       # T3's account search isn't due; @binu stays in its talk search
    assert searches[0]["query"] == "CA1 within_time:2h" and searches[0]["mode"] == "Latest"
    assert searches[4]["query"] == "from:iguana" and searches[4]["window_h"] is None
    assert searches[6]["query"] == "($BI OR @binu) min_faves:3 within_time:3h" and searches[6]["mode"] == "Top"
    text = x.prompt(toks, searches, NOW)
    assert "Current time: 2026-09-30 17:00 UTC" in text
    assert "  1. query `CA1 within_time:2h`, mode Latest, limit 8" in text
    assert "  7. query `($BI OR @binu) min_faves:3 within_time:3h`, mode Top, limit 5" in text
    assert 'Token "T1": $LONG ("longnose") on solana, contract CA1, no known project account' in text
    assert "project account @iguana; 4 other recent tokens use this ticker" in text


def test_grok_is_limited_to_x_search_at_medium_effort():
    args = x.GROK_ARGS
    assert args[args.index("--tools") + 1] == "x_search"
    assert args[args.index("--disallowed-tools") + 1] == sp.DENY_TOOLS
    assert args[args.index("--system-prompt-override") + 1] == x.SYSTEM
    assert args[args.index("--reasoning-effort") + 1] == "medium"
    assert "--no-subagents" in args and "--disable-web-search" in args


# ─── recorded reads: parsing, the gate, the numbers ──────────────────────────
def test_a_recorded_four_token_read():
    d = recorded("scout_4_tokens_medium.json")
    scout, meta = parsed(d)
    assert meta["equiv_cost_usd"] == pytest.approx(0.324, abs=0.001)          # $0.08 a token
    rows = {r["symbol"]: r for r in judged(d, scout)}
    assert [r["status"] for r in rows.values()] == ["read"] * 4
    si, ath, bi, chamber = rows["si16z"], rows["ATH"], rows["BI"], rows["CHAMBER"]
    # si16z: five caller templates on the contract, the rest the project's own posts.
    assert (si["posts_seen"], si["unique_authors"], si["shill_share"]) == (11, 4, 1.0)
    assert si["account"]["handle"] == "@proj_si16z" and si["account"]["last_post"] is not None
    assert si["first_mention"] == datetime(2026, 9, 30, 16, 7, 10, 704000, tzinfo=UTC)   # its launch post
    # ATH: the contract search hit its limit of 8, so the rate is 8 over the ~30 minutes they span.
    assert sum(p["facet"] == "contract" for p in ath["posts"]) == 8 and ath["posts_h"] > 8
    assert ath["first_mention"] is None                   # unknown: its creation time was not given
    assert bi["account"] is None and bi["first_mention"] is not None
    assert all(len(r["notable"]) == 3 and r["red_flags"] for r in rows.values())
    assert chamber["kols"] == []                          # no well-known author, nothing with 100 likes
    for r in rows.values():
        assert all(x.parse_post(p["url"]) for p in r["posts"] + r["notable"] + r["red_flags"])
    assert si["searches"] == [s["query"] for s in d["searches"][:3]]


def test_the_gate_drops_fabricated_stale_and_misfiled_posts():
    d = recorded("scout_4_tokens_medium.json")
    base = judged(d)[0]["posts_seen"]
    scout, _ = parsed(d)
    t1 = scout.reads[0]
    t1.posts += [
        x.SeenPost(search=1, url="https://x.com/someone/status/000", stance="bullish", kind="organic"),
        x.SeenPost(search=1, url=url(d["now"] - timedelta(hours=9)), stance="bullish", kind="organic"),  # outside 2 h
        x.SeenPost(search=4, url=url(d["now"] - 5 * M, "misfiled"), stance="bullish", kind="organic"),   # T2's search
        x.SeenPost(search=1, url=url(d["now"] + timedelta(hours=1)), stance="bullish", kind="organic"),  # the future
        x.SeenPost(search=1, url=t1.posts[0].url.replace("x.com", "twitter.com"), stance="bullish", kind="shill"),
    ]
    t1.notable.append(x.NotablePost(author="@ghost", text="made up", url="https://x.com/ghost/status/123",
                                    stance="bullish"))
    t1.red_flags.append(x.RedFlag(kind="other", detail="made up", url="https://example.com/ghost"))
    t1.account = x.Account(handle="@impostor")            # an account no search returned and nobody asked about
    r = judged(d, scout)[0]
    assert r["posts_seen"] == base == 11                  # none of the additions survives (the dupe is the same id)
    assert "@ghost" not in [n["author"] for n in r["notable"]]
    assert all("example.com" not in f["url"] for f in r["red_flags"])
    assert r["account"] is None


def test_nothing_found_is_quiet_only_when_the_call_proves_the_searches_ran():
    d = recorded("scout_4_tokens_medium.json")
    scout, _ = parsed(d)
    scout.reads[2].posts, scout.reads[2].notable = [], []
    rows = judged(d, scout)
    assert rows[2]["status"] == "quiet" and rows[2]["posts_h"] == 0 and rows[2]["mood"] is None
    for r in scout.reads:
        r.posts = []
    assert {r["status"] for r in judged(d, scout)} == {"ungrounded"}
    skipped, _ = parsed(d)
    skipped.reads = skipped.reads[:1]                     # the model skipped T2..T4 altogether
    assert [r["status"] for r in judged(d, skipped)] == ["read", "ungrounded", "ungrounded", "ungrounded"]


def test_a_live_read_built_by_the_scout_s_own_selection():
    # 18:12 UTC: the four trending tokens `pick` chose on the staging data, their prompt from `plan`/`prompt`.
    d = recorded("scout_4_tokens_live.json")
    assert [t["picked_for"] for t in d["tokens"]] == ["trending"] * 4
    assert '("Effective Accelerationism" OR @proj_eacc)' in d["searches"][6]["query"]   # $e/acc isn't a cashtag
    scout, meta = parsed(d)
    assert meta["equiv_cost_usd"] == pytest.approx(0.296, abs=0.001)          # $0.074 a token
    rows = {r["symbol"]: r for r in judged(d, scout)}
    assert {r["status"] for r in rows.values()} == {"read"}
    assert rows["STONK"]["posts_h"] > 30 and x.attention(rows["STONK"]["posts_h"]) == "warm"   # saturated contract
    assert rows["e/acc"]["mood"] < 0 and "rug_warning" in [f["kind"] for f in rows["e/acc"]["red_flags"]]
    assert rows["ALLINU"]["account"] is None and rows["OTC"]["account"]["handle"] == "@proj_otc"


def test_a_read_without_its_post_list_fails_the_schema():
    # At low effort Grok answered with summaries, notables and flags but no post list: nothing to compute from.
    with pytest.raises(ValidationError):
        parsed(recorded("scout_4_tokens_low_no_posts.json"))


def test_the_default_effort_read_parses_and_first_mention_needs_the_token_s_whole_life():
    d = recorded("scout_3_tokens_high.json")
    scout, meta = parsed(d)
    assert meta["equiv_cost_usd"] == pytest.approx(0.206, abs=0.001)
    rows = {r["symbol"]: r for r in judged(d, scout)}
    assert {r["status"] for r in rows.values()} == {"read"}
    assert rows["MASK"]["first_mention"] is None           # a week old: a 6 h window can't see its first post
    assert rows["LONG"]["first_mention"] is not None and rows["LONG"]["account"] is None
    assert rows["HOTDOG"]["account"]["handle"] == "@proj_hotdog"


def test_posts_per_hour():
    searches = {1: {"facet": "contract", "limit": 8}, 2: {"facet": "talk", "limit": 5}}

    def post(minutes_ago, facet="contract", kind="organic"):
        return {"at": NOW - minutes_ago * M, "facet": facet, "kind": kind}
    # Under the limit the search showed the whole window: the hour's posts, the project's own aside.
    assert x.posts_per_hour([post(10), post(50), post(90), post(20, "talk"), post(5, kind="project")],
                            searches, NOW) == 3
    # At the limit: 8 posts in the last 20 minutes is a 24-an-hour pace.
    assert x.posts_per_hour([post(m) for m in (1, 3, 5, 8, 11, 14, 17, 20)], searches, NOW) == 24


# ─── what to read ────────────────────────────────────────────────────────────
def token(**kw) -> dict:
    return {"chain": "solana", "address": "CA", "liquidity": 50_000, "tier": "hot", "first_call": NOW - 90 * M,
            "last_read": NOW - 2 * x.RESCAN, "last_attempt": NOW - 2 * x.RESCAN, **kw}


def test_new_calls_first_and_never_dust():
    new = token(first_call=NOW - 10 * M, last_read=None, last_attempt=None)
    score, why = x.priority(new, NOW)
    assert why == "new call" and 3 < score <= 4
    assert x.priority({**new, "first_call": NOW - 2 * M}, NOW)[0] > score          # the newest first
    assert x.priority({**new, "liquidity": 9_000}, NOW) is None                     # dust
    assert x.priority({**new, "liquidity": None}, NOW) is None                      # not priced yet
    assert x.priority({**new, "last_attempt": NOW - 10 * M}, NOW) is None           # never twice within 20 min
    missed = {**new, "first_call": NOW - 40 * M}                                    # missed its half hour unread
    assert x.priority(missed, NOW) is None                                          # nothing happening: it waits
    assert x.priority({**missed, "vol_recent": 20_000, "vol_prev": 5_000}, NOW) == (2.0, "volume rising")


def test_hot_tokens_are_rescanned_only_while_calls_or_volume_rise():
    flat = token()
    assert x.priority(flat, NOW) is None                                            # hot, flat, read already
    both = token(calls_1h=3, calls_prev_1h=1, vol_recent=40_000, vol_prev=20_000, last_read=NOW - 35 * M,
                 last_attempt=NOW - 35 * M)
    assert x.priority(both, NOW)[1] == "calls and volume rising"                     # every 30 minutes
    assert x.priority({**both, "last_read": NOW - 25 * M, "last_attempt": NOW - 25 * M}, NOW) is None
    calls = {**both, "vol_recent": 1_000}
    assert x.priority(calls, NOW) is None                                           # one signal: every hour
    assert x.priority({**calls, "last_read": NOW - 65 * M}, NOW)[1] == "calls rising"
    assert x.priority({**flat, "calls_1h": 2, "calls_prev_1h": 3}, NOW) is None     # calls slowing
    # The bot holds dozens of tokens (31 on 2026-09-30): each gets a read every 4 hours, after everything else.
    held = {**flat, "held": True}
    assert x.priority(held, NOW) is None
    assert x.priority({**held, "last_read": NOW - 5 * H}, NOW)[1] == "bot holds it"
    assert x.priority({**held, "last_read": None}, NOW) == (1.0, "bot holds it") < x.priority(both, NOW)
    assert x.priority({**flat, "trending_at": NOW - 5 * M}, NOW)[1] == "trending"
    assert x.priority({**flat, "tier": "warm", "vol_recent": 50_000}, NOW) is None  # warm tokens wait


def test_an_opened_dossier_jumps_the_rescan_queue():
    opened = token(viewed_at=NOW - 3 * M, last_read=NOW - 40 * M, last_attempt=NOW - 40 * M)
    assert x.priority(opened, NOW) == (2.5, "opened")
    assert x.priority({**opened, "last_read": NOW - 25 * M, "last_attempt": NOW - 25 * M}, NOW) is None
    rising = token(calls_1h=4, calls_prev_1h=1, address="R")
    fresh = token(first_call=NOW - 5 * M, last_read=None, last_attempt=None, address="N")
    got = x.pick([rising, opened, {**opened, "liquidity": 100, "address": "D"}, fresh], NOW, 2)
    assert [(t["address"], t["picked_for"]) for t in got] == [("N", "new call"), ("CA", "opened")]


# ─── pool ────────────────────────────────────────────────────────────────────
def test_the_scout_never_takes_social_pulse_s_share_of_the_pool():
    assert x.allowance(15, 40, 38) == 2                   # today's settings leave the scout $2 a week
    assert x.allowance(15, 40, 25) == 15
    assert x.allowance(15, 40, 45) == 0


def test_run_budget_bursts_when_under_pace_and_stops_at_the_limits():
    weekly = 60.0
    daily, hourly = weekly / 7, weekly / 7 / 24
    assert x.pace_hours(weekly) == 1
    assert x.run_budget(0, 0, 0, weekly) == pytest.approx(3 * hourly)               # a burst of calls, fresh day
    assert x.affordable(x.run_budget(0, 0, 0, weekly)) == x.MAX_TOKENS
    assert x.run_budget(hourly, daily, 6 * daily, weekly) <= 0                       # on pace, hour spent
    assert x.run_budget(0, 1.5 * daily, 6 * daily, weekly) <= 0                      # a hot day: stop
    # A small week is kept over a longer window, so it still funds a read now and then.
    one = x.USD_PER_CALL + x.USD_PER_TOKEN
    assert x.pace_hours(2.0) == pytest.approx(one / (2.0 / 168))                     # ~8.4 hours
    assert x.affordable(x.run_budget(0, 0, 0, 2.0)) == 1
    assert x.affordable(x.run_budget(one, one, one, 2.0)) == 0                       # read in this window already
    assert x.pace_hours(0) == 168 and x.run_budget(0, 0, 0, 0) <= 0
    assert x.run_budget(0, 0.5, weekly - 0.01, weekly) < x.USD_PER_CALL              # the week is spent
    assert x.pool_room(0, 39.99, 40) < 0.011 and x.affordable(x.pool_room(0, 39.99, 40)) == 0
    assert x.affordable(10) == x.MAX_TOKENS and x.affordable(0.09) == 0 and x.affordable(0.101) == 1


@pytest.mark.parametrize("weekly,pool,social", [(15.0, 40.0, 25.0), (15.0, 40.0, 38.0), (60.0, 100.0, 38.0)])
def test_three_weeks_of_scans_and_pulses_stay_inside_the_pool(weekly, pool, social):
    own = x.allowance(weekly, pool, social)               # $15, today's $2, and a bigger pool's $60
    pace = x.pace_hours(own) * 60
    scout, pulse, week = [], [], 7 * 1440                 # (minute, dollars)

    def spent(log_, now, minutes):
        return sum(c for t, c in log_ if now - t < minutes)
    for minute in range(0, 21 * 1440, 5):
        if minute % 60 == 10:                             # Social Pulse at :10, paced to its own allowance
            coins = sp.coins_affordable(spent(pulse, minute, 1440), spent(pulse, minute, week), social)
            if coins >= 0:
                pulse.append((minute, sp.call_cost(coins)))
        if minute % 60 == 0 and minute >= week:
            assert spent(scout, minute, week) <= own + 1e-9
            assert spent(scout, minute, 1440) <= 1.5 * own / 7 + 1e-9
            assert spent(scout, minute, week) + spent(pulse, minute, week) <= pool + 1e-9
        usd = min(x.run_budget(spent(scout, minute, pace), spent(scout, minute, 1440), spent(scout, minute, week), own),
                  x.pool_room(spent(scout, minute, 1440) + spent(pulse, minute, 1440),
                              spent(scout, minute, week) + spent(pulse, minute, week), pool))
        if n := x.affordable(usd):                        # demand is always there
            scout.append((minute, x.USD_PER_CALL + n * x.USD_PER_TOKEN))
        scout = [e for e in scout if minute - e[0] < week]
        pulse = [e for e in pulse if minute - e[0] < week]
    per_day = sum(c for t, c in scout) / 7                # the last week
    assert 0.8 * own / 7 <= per_day <= own / 7 + 1e-9    # uses most of its share, evenly


# ─── sections ────────────────────────────────────────────────────────────────
def stored(read_at, mood=1, posts_h=12.0, **kw):
    note = {"author": "@a", "followers": 48000, "text": "t", "url": url(read_at - 5 * M, "a"), "stance": "bullish"}
    return {"read_at": read_at, "status": "read", "mood": mood, "posts_h": posts_h, "unique_authors": 9, "summary": "s",
            "why": "w", "notable": json.dumps([note]), "kols": json.dumps([{"author": "@a", "likes": 150}]),
            "account": None, "red_flags": "[]", "shill_share": 0.4, "first_mention": None, **kw}


def test_section_x_matches_the_contract():
    fixture = json.loads((CONTRACT / "token.json").read_text())["x"]
    rows = [stored(NOW - 10 * M, mood=2, account=json.dumps({"handle": "@p", "followers": None, "created": None,
                                                             "name_changes": None, "last_post": None})),
            stored(NOW - 3 * H), stored(NOW - 30 * H, first_mention=NOW - 31 * H)]
    s = x.shape(rows, NOW, out_of_budget=False)
    assert set(fixture) <= set(s) | {"ask", "below_floor", "floor_usd"}     # those three: section_x adds them
    assert set(fixture["notable"][0]) == set(s["notable"][0]) and set(fixture["history"][0]) == set(s["history"][0])
    assert set(fixture["account"]) <= set(s["account"])
    assert (s["status"], s["mood"], s["mood_label"], s["kols"]) == ("fresh", 2, "bullish", ["@a"])
    assert [h["ts"] for h in s["history"]] == [NOW - 3 * H, NOW - 10 * M]     # the last day, oldest first
    assert s["first_mention"] == NOW - 31 * H and s["notable"][0]["at"] is not None
    assert x.shape(rows[1:], NOW, False)["status"] == "stale"
    assert x.shape(rows[1:], NOW, True)["status"] == "budget"
    assert x.shape([], NOW, False) == {"status": "none", **x.EMPTY}
    assert x.shape([], NOW, True)["status"] == "budget"


def test_labels_match_the_ui_fixtures():
    for row in json.loads((CONTRACT / "feed.json").read_text())["rows"]:
        if row["x_heat"]:
            assert x.attention(row["x_heat"]["posts_h"]) == row["x_heat"]["label"]
    assert [x.attention(v) for v in (0, 150, None)] == ["silent", "hot", None]
    assert [x.mood_label(m) for m in (-5, -3, -1, 0, 1, 2, 3, 4, None)] == [
        "very bearish", "bearish", "leaning bearish", "mixed", "leaning bullish", "bullish", "bullish", "euphoric",
        None]
    n = {"author": "@notable_trader", "followers": 48000, "text": "Saw is the chart of the week"}
    assert x.marker_label(n) == "@notable_trader (48k followers): 'Saw is the chart of the week'"
    assert x.marker_label({**n, "followers": None, "text": "a\n" + "b" * 100}).startswith("@notable_trader: 'a bbb")
    assert x.marker_label({**n, "followers": None, "text": "b" * 100}).endswith("…'")


def test_registry_runs_the_scout_on_the_host_every_five_minutes():
    from albatross.deepend import registry
    assert registry.HOST_JOBS[x.AGENT] is x.scan and registry.CADENCE[x.AGENT] == 300
    assert (x.AGENT, {"minute": "*/5", "second": 30}) in registry.HOST_SCHEDULE
    assert x.AGENT not in registry.JOBS                   # no Grok CLI in the container


# ─── Social Pulse: the trenches search ───────────────────────────────────────
def test_the_trenches_search_is_spelled_out_second():
    text = sp.searches([{"symbol": "BTC", "hours": 2}])
    assert text.count("query") == 3
    assert f"2. query '{sp.TRENCHES_QUERY}', mode Top, limit {sp.TRENCHES_POSTS}  (memecoin trenches)" in text
    assert '3. query "$BTC min_faves:2 -filter:replies within_time:2h"' in text
    assert "url:" not in sp.TRENCHES_QUERY                # x_search returned unrelated posts for url: operators
    assert sp.call_cost(0) == pytest.approx(sp.USD_PER_CALL + sp.USD_PER_POST * (sp.PULSE_POSTS + sp.TRENCHES_POSTS))


def test_a_recorded_trenches_read_keeps_only_cited_tickers():
    d = json.loads((FIX / "pulse_trenches.json").read_text())
    raw, meta = cli._parse_grok(json.dumps(d["envelope"]))
    pulse = sp.Pulse.model_validate_json(raw)
    now = datetime.fromisoformat(d["now"]) + timedelta(seconds=d["latency_s"])
    meme, metas = sp.trenches(pulse, now)
    assert [t.ticker for t in meme] == ["SI", "ZCOIN"] and metas[0].tickers == ["SI"]
    assert meta["equiv_cost_usd"] == pytest.approx(0.110, abs=0.001)
    pulse.trenches.append(sp.Trending(ticker="FAKE", why="x", url="https://x.com/a/status/000"))
    pulse.memecoin_narratives.append(sp.MemeNarrative(narrative="old", url=url(now - timedelta(hours=9))))
    assert [t.ticker for t in sp.trenches(pulse, now)[0]] == ["SI", "ZCOIN"] and len(sp.trenches(pulse, now)[1]) == 1
    empty = json.loads((FIX / "pulse_trenches_keywords.json").read_text())      # the first query found no coins
    assert sp.Pulse.model_validate_json(cli._parse_grok(json.dumps(empty["envelope"]))[0]).trenches == []


def test_the_market_row_keeps_its_fields_and_adds_the_trenches():
    pulse = sp.Pulse(market_score=1, market_mood="m", reads=[],
                     trenches=[sp.Trending(ticker="SI", why="w", url=url(NOW - 5 * M))],
                     memecoin_narratives=[sp.MemeNarrative(narrative="AI", tickers=["SI"], url=url(NOW - 5 * M))])
    meta = sp.market_narratives(pulse, [], [], {"BTC"}, 7, NOW)
    assert {"market_mood", "headlines", "trending", "coins_read", "agent_run_id"} <= set(meta)   # /api/social's
    assert meta["trenches"][0]["ticker"] == "SI" and meta["memecoin_narratives"][0]["narrative"] == "AI"


# ─── DB scenarios (ALBATROSS_TEST_DSN: a migrated Postgres, never production) ─
DSN = os.environ.get("ALBATROSS_TEST_DSN")
db_only = pytest.mark.skipif(not DSN, reason="set ALBATROSS_TEST_DSN to a migrated Postgres")
CA, CB = "XsCoutTestMint1111111111111111111111111111", "XsCoutTestMint2222222222222222222222222222"


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


def _in_tx(scenario, monkeypatch):
    import asyncpg

    from albatross import db

    async def go():
        conn = await asyncpg.connect(DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            pool = _Pool(conn)

            async def fake_pool():
                return pool
            monkeypatch.setattr(db, "pool", fake_pool)
            monkeypatch.setattr(x, "_exhausted", (0.0, False))
            await scenario(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


@db_only
def test_a_scan_end_to_end_with_the_recorded_call(monkeypatch):
    """scan() on the staging database with the recorded 4-token Grok output in place of the CLI: picks, prompt,
    agent_runs, the gate, de_x_reads, and what the dossier, the feed and the chart then read."""
    d = recorded("scout_4_tokens_medium.json")

    async def scenario(conn):
        now = datetime.now(UTC)
        toks = [{**t, "tier": "hot", "why": ["new call"], "first_call": now - 5 * M, "calls_1h": 1, "calls_24h": 1,
                 "held": False, "viewed_at": None, "trending_at": None, "discovered_at": None} for t in d["tokens"]]

        async def hot(tier=None):
            return [dict(t) for t in toks]

        async def socials(chain, addresses):
            return {}

        async def grok(agent, schema, text, **kw):
            assert agent == x.AGENT and kw["extra_args"] == x.GROK_ARGS
            for t in toks:
                assert t["address"] in text
            run_id = await conn.fetchval(
                "INSERT INTO agent_runs (agent, model, ok, output) VALUES ($1, 'grok:default', TRUE, $2) RETURNING id",
                x.AGENT, json.dumps({"subscription_equiv_cost_usd": 0.324}))
            return parsed(d)[0], run_id
        for t in toks:                                    # liquidity for the dust rule
            await conn.execute(
                """INSERT INTO micro_state (chain, token, symbol, name, liquidity_usd, created_at)
                   VALUES ($1, $2, $3, $4, 50000, $5) ON CONFLICT (chain, token) DO UPDATE SET liquidity_usd = 50000""",
                t["chain"], t["address"], t["symbol"], t["name"], t.get("created_at"))
        monkeypatch.setattr(x.watch, "tokens", hot)
        monkeypatch.setattr(x, "_dexscreener_handles", socials)
        monkeypatch.setattr(x.cli, "grok_structured", grok)
        monkeypatch.setattr(x.xsearch, "x_lane", lambda: "cli")         # the host worker's lane
        monkeypatch.setattr(x, "weekly_usd", lambda: 1000.0)
        monkeypatch.setattr(x, "pool_usd", lambda: 1100.0)
        judge = x.judge                                   # as of the recording: its posts are hours old now
        monkeypatch.setattr(x, "judge", lambda s, t, q, a, b: judge(s, t, q, d["now"], d["now"] + 4 * M))
        assert await x.scan() == 4
        rows = await conn.fetch("SELECT * FROM de_x_reads WHERE address = ANY($1) ORDER BY symbol",
                                [t["address"] for t in toks])
        assert [r["status"] for r in rows] == ["read"] * 4 and all(r["run_id"] for r in rows)
        assert await x.scan() == 0                        # never twice within 20 minutes
        bi = toks[2]
        s = await x.section_x(bi["chain"], bi["address"])
        assert s["status"] == "fresh" and s["summary"] and len(s["notable"]) == 3 and s["history"]
        h = await x.heat([(t["chain"], t["address"]) for t in toks] + [("solana", CA)])
        assert len(h) == 4 and h[(bi["chain"], bi["address"])]["label"] == x.attention(s["posts_h"])
        m = await x.markers(bi["chain"], bi["address"], d["now"] - 6 * H, now + H)
        assert sum(bool(k["detail"]["text"]) for k in m if k["kind"] == "x") == 3 and all(k["price"] is None for k in m)
        assert m == sorted(m, key=lambda k: k["t"])
        assert (await x.section_x("solana", CB))["status"] == "none"
    _in_tx(scenario, monkeypatch)


@db_only
def test_the_budget_counts_both_agents(monkeypatch):
    async def scenario(conn):
        before = await x.spend()
        for agent, usd in ((sp.AGENT, 1.5), (x.AGENT, 0.25), ("oracle_watch", 9.0)):
            await conn.execute("INSERT INTO agent_runs (agent, model, ok, output) VALUES ($1, 'm', TRUE, $2)",
                               agent, json.dumps({"subscription_equiv_cost_usd": usd}))
        after = await x.spend()
        assert after["own_w"] - before["own_w"] == pytest.approx(0.25)
        assert after["pool_7d"] - before["pool_7d"] == pytest.approx(1.75)          # the Oracle is another pool
    _in_tx(scenario, monkeypatch)


# ─── the chart's X markers, and "Search X now" (2026-10-06) ──────────────────
def sid(at: datetime) -> int:
    """A status id whose snowflake time is `at`, as parse_post decodes it."""
    return ((int(at.timestamp() * 1000) - 1288834974657) << 22) + 7


def test_the_chart_gets_every_verified_post_once_with_its_text_when_notable_and_each_x_red_flag():
    a, b, old = NOW - 50 * M, NOW - 30 * M, NOW - 3 * H
    url = lambda h, at: f"https://x.com/{h}/status/{sid(at)}"           # noqa: E731
    rows = [
        {"posts": [{"url": url("alpha", a), "stance": "bullish", "kind": "organic", "likes": 3, "facet": "contract"},
                   {"url": url("early", old), "stance": "bearish", "kind": "shill", "likes": 0, "facet": "talk"}],
         "notable": [], "red_flags": []},
        {"posts": [{"url": url("alpha", a), "stance": "bullish", "kind": "organic", "likes": 40, "facet": "contract"},
                   {"url": url("beta", b), "stance": "bearish", "kind": "bot", "likes": None, "facet": "contract"}],
         "notable": [{"author": "@alpha", "followers": 48000, "text": "chart of the week", "url": url("alpha", a),
                      "stance": "bullish"}],
         "red_flags": [{"kind": "paid_raid", "detail": "20 accounts posted the same text", "url": url("beta", b)}]},
    ]
    m = x.post_markers(rows, NOW - 2 * H, NOW)
    assert [(k["kind"], k["t"]) for k in m] == [("x", a), ("x", b), ("xflag", b)]      # the early post is off the chart
    alpha, beta, flag = m
    assert alpha["detail"]["likes"] == 40 and alpha["detail"]["text"] == "chart of the week"     # the newest read's view
    assert alpha["detail"]["followers"] == 48000 and alpha["label"] == "@alpha (48k followers): 'chart of the week'"
    assert alpha["detail"]["url"] == url("alpha", a) and alpha["id"] == str(sid(a)) and alpha["price"] is None
    assert beta["label"] == "@beta" and beta["detail"]["text"] is None and beta["detail"]["type"] == "bot"
    assert flag["label"] == "Paid raid" and flag["detail"]["detail"].startswith("20 accounts") and flag["detail"]["author"] == "@beta"


class AskPool:
    """de_x_asks as asks() reads and closes it."""
    def __init__(self, waiting):
        self.waiting, self.closed = waiting, []

    async def execute(self, sql, *a):
        if "status = 'expired'" in sql:
            return
        assert "UPDATE de_x_asks a SET done_at" in sql, sql
        self.closed += [((c, ad), a[2]) for c, ad in zip(a[0], a[1], strict=True)]

    async def fetch(self, sql, *a):
        assert "FROM de_x_asks WHERE done_at IS NULL" in sql, sql
        return self.waiting


def test_an_asked_read_waits_its_turn_counts_a_scan_since_and_never_passes_the_week(monkeypatch):
    t0 = datetime.now(UTC) - 2 * M
    calls = []

    def setup(waiting, room=1.0):
        pool = AskPool(waiting)

        async def get_pool():
            return pool

        async def ask_room():
            return room

        async def ask_tokens(keys):
            return [{"chain": c, "address": a, "symbol": "TOK", "name": "Token", "created_at": t0 - H, "picked_for": "asked"}
                    for c, a in keys]

        async def nothing(*a, **k):
            return None

        async def no_majors():
            return set()

        async def grok(agent, schema, prompt, **kw):
            calls.append((agent, prompt))
            return x.Scout(reads=[]), 7
        monkeypatch.setattr(x.db, "pool", get_pool)
        monkeypatch.setattr(x, "ask_room", ask_room)
        monkeypatch.setattr(x, "ask_tokens", ask_tokens)
        async def attach(picks, now):
            for t in picks:
                t.update(window_h=2, account_search=False)
        monkeypatch.setattr(x, "attach", attach)
        monkeypatch.setattr(x, "majors", no_majors)
        monkeypatch.setattr(x.cli, "grok_structured", grok)
        monkeypatch.setattr(x.xsearch, "x_lane", lambda: "cli")         # the host worker's lane
        monkeypatch.setattr(x, "store", nothing)
        monkeypatch.setattr(x, "judge", lambda s, picks, q, a, b: [{**t, "status": st} for t, st in zip(picks, ("read", "quiet"))])
        return pool

    w = lambda addr, read_at=None, st=None: {"chain": "solana", "address": addr, "asked_at": t0, "read_at": read_at, "read_status": st}  # noqa: E731

    async def held():
        async with x._grok:                              # the scan is reading: the ask waits for the next minute
            return await x.asks()
    pool = setup([w("A")])
    assert asyncio.run(held()) == 0 and not calls and not pool.closed

    pool = setup([w("A", t0 + M, "quiet"), w("B")], room=0.0)
    assert asyncio.run(x.asks()) == 0 and not calls
    assert pool.closed == [(("solana", "A"), "quiet"), (("solana", "B"), "refused")]   # A was read since; no room for B

    pool = setup([w("A"), w("B")])
    assert asyncio.run(x.asks()) == 2 and len(calls) == 1 and calls[0][0] == x.AGENT     # one call, the scout's spend
    assert "`A within_time:2h`" in calls[0][1] and "`B within_time:2h`" in calls[0][1]      # both contracts searched
    assert pool.closed == [(("solana", "A"), "read"), (("solana", "B"), "quiet")]


def test_a_posts_own_text_shows_when_no_read_picked_it_as_notable():
    at = NOW - 20 * M
    url = f"https://x.com/gamma/status/{sid(at)}"
    rows = [{"posts": [{"url": url, "stance": "bullish", "kind": "organic", "likes": 2, "facet": "talk",
                        "text": "bonded and still building"}], "notable": [], "red_flags": []}]
    (m,) = x.post_markers(rows, NOW - H, NOW)
    assert m["detail"]["text"] == "bonded and still building" and m["label"] == "@gamma: 'bonded and still building'"
    assert x.SeenPost(search=1, url=url, stance="neutral", kind="organic").text == ""      # older answers parse
