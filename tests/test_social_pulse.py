from datetime import UTC, datetime, timedelta

from albatross.agents import social_pulse as sp

NOW = datetime(2026, 9, 30, 5, 0, tzinfo=UTC)


def status_url(when: datetime, handle: str = "trader") -> str:
    """An x.com URL whose snowflake id encodes `when`."""
    ms = int(when.timestamp() * 1000) - sp.TWITTER_EPOCH_MS
    return f"https://x.com/{handle}/status/{ms << 22}"


def test_post_time_round_trips_and_rejects_fakes():
    t = NOW - timedelta(minutes=30)
    assert abs((sp.post_time(status_url(t)) - t).total_seconds()) < 1
    assert sp.post_time("https://x.com/someone/status/000") is None
    assert not sp.fresh_url(status_url(NOW - timedelta(hours=7)), NOW)
    assert sp.fresh_url(status_url(NOW - timedelta(hours=1)), NOW)


def test_budget_paces_the_day_and_stops_when_spent():
    weekly = 38.0
    daily = weekly / 7
    assert sp.coins_affordable(0.0, 0.0, weekly) == sp.MAX_COINS       # nothing spent: catch up, capped
    steady = sp.coins_affordable(daily, 6 * daily, weekly)
    assert 2 <= steady <= 4                                              # on pace: the pulse plus a few coins
    assert sp.call_cost(steady) <= daily / 24 + 1e-9
    assert sp.coins_affordable(daily * 1.2, 6 * daily, weekly) == steady  # over pace but under 1.5x: keep going
    assert sp.coins_affordable(1.5 * daily - 0.05, 6 * daily, weekly) == -1   # a hot day: stop
    assert sp.coins_affordable(1.0, weekly - 0.05, weekly) == -1         # the week is spent: stop


def test_a_week_of_runs_stays_inside_the_allowance():
    weekly, log_ = 38.0, []
    for hour in range(24 * 21):
        d = sum(c for h, c in log_ if hour - h < 24)
        w = sum(c for h, c in log_ if hour - h < 24 * 7)
        n = sp.coins_affordable(d, w, weekly)
        if n >= 0:
            log_.append((hour, sp.call_cost(n)))
    for hour in range(24 * 7, 24 * 21):
        assert sum(c for h, c in log_ if 0 <= hour - h < 24 * 7) <= weekly + 1e-9
        assert sum(c for h, c in log_ if 0 <= hour - h < 24) <= 1.5 * weekly / 7 + 1e-9
    per_day = sum(c for h, c in log_ if h >= 24 * 14) / 7
    assert 0.8 * weekly / 7 <= per_day <= weekly / 7 + 1e-9                # uses most of it, evenly


def test_priority_reads_due_and_live_coins_first():
    idle = dict(moving=False, trending=False, trade=False, news=False)
    assert sp.priority(1, 0.5, **{**idle, "moving": True}) == 0          # never twice within the hour
    assert sp.priority(1, 2, **idle) < 1                                  # Tier-1 not yet due
    assert sp.priority(1, 4, **idle) >= 1                                 # due after 4 h
    assert sp.priority(2, 30, **idle) == 0                                # Tier-2 only when something happens
    assert sp.priority(2, 30, **{**idle, "trending": True}) >= 1
    assert sp.priority(1, None, **idle) > sp.priority(1, 5, **idle)       # never read beats read long ago
    assert sp.priority(1, 2, **{**idle, "moving": True}) > sp.priority(1, 2, **idle)


def test_searches_are_spelled_out_with_windows_and_limits():
    text = sp.searches([{"symbol": "BTC", "hours": 2}, {"symbol": "HYPE", "hours": 4}])
    assert text.count("query") == 4                       # the pulse, the memecoin trenches, two coins
    assert f"limit {sp.PULSE_POSTS}" in text and "mode Top" in text
    assert f"limit {sp.TRENCHES_POSTS}  (memecoin trenches)" in text
    assert '3. query "$BTC min_faves:2 -filter:replies within_time:2h", mode Latest' in text
    assert '4. query "$HYPE' in text and 'scope "HYPE"' in text and "within_time:4h" in text


def _read(scope, url):
    return sp.ScopeRead(scope=scope, summary="s", posts_reviewed=5, sentiment_score=1, post_volume="normal",
                        crowding="none", narratives=[], sample_posts=[sp.Post(author="a", text="t", url=url)])


def test_grounding_drops_uncited_and_unasked_reads():
    good, stale = status_url(NOW - timedelta(minutes=20)), status_url(NOW - timedelta(hours=9))
    pulse = sp.Pulse(
        market_score=1, market_mood="m",
        headlines=[sp.Headline(text="ok", kind="flow", url=good), sp.Headline(text="old", kind="risk", url=stale)],
        trending=[sp.Trending(ticker="ENA", why="unlock", url="https://x.com/a/status/000")],
        reads=[_read("BTC", good), _read("ETH", stale), _read("DOGE", good)])
    reads, heads, trend = sp.grounded(pulse, {"BTC", "ETH"}, NOW)
    assert [r.scope for r in reads] == ["BTC"]            # ETH's only post is stale; DOGE wasn't asked for
    assert [h.text for h in heads] == ["ok"]
    assert trend == []


def test_grok_is_limited_to_x_search():
    args = sp.GROK_ARGS
    assert args[args.index("--tools") + 1] == "x_search"
    denied = args[args.index("--disallowed-tools") + 1].split(",")
    for tool in ("run_terminal_command", "read_file", "write", "web_search", "web_fetch", "spawn_subagent", "image_gen"):
        assert tool in denied
    assert "--no-subagents" in args and "--disable-web-search" in args
