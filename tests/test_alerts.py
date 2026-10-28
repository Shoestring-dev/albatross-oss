"""Failure alerts: detection rules, the cadence map vs the scheduler, notification planning and the two
channels against a stub HTTP transport (nothing is sent anywhere)."""

import asyncio
import io
import json
import re
import tokenize
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from albatross.alerts import notify, rules

NOW = datetime(2026, 9, 30, 3, 0, tzinfo=UTC)
MIN = timedelta(minutes=1)


def job(name, last_ok_ago, errs=0, err=None, last_run_ago=None):
    return {"job": name, "last_ok": NOW - last_ok_ago if last_ok_ago is not None else None,
            "last_run": NOW - (last_run_ago if last_run_ago is not None else MIN),
            "consecutive_errors": errs, "last_error": err}


def keys(conds):
    return {c.key: c.severity for c in conds}


# ─── jobs ───────────────────────────────────────────────────────────────────
def test_healthy_jobs_raise_nothing():
    assert rules.jobs([job("grader", 3 * MIN), job("strategist", 50 * MIN), job("universe", timedelta(days=6))], NOW) == []


def test_more_than_n_consecutive_failures_is_critical():
    (c,) = rules.jobs([job("grader", 22 * MIN, errs=4, err="RuntimeError: boom")], NOW)
    assert (c.key, c.severity) == ("job_failing:grader", "critical")
    assert "×4" in c.title and "boom" in c.detail
    assert c.since == NOW - 17 * MIN          # the first run after the last success (5-min job)


def test_a_few_quick_failures_are_not_an_alert_yet():
    assert rules.jobs([job("grader", 12 * MIN, errs=2)], NOW) == []


def test_one_failure_that_lasts_30_min_is_a_warning():
    # hourly job: last ok 95 min ago, so its next run (35 min ago) failed and nothing has fixed it since
    (c,) = rules.jobs([job("strategist", 95 * MIN, errs=1, last_run_ago=35 * MIN)], NOW)
    assert (c.key, c.severity) == ("job_failing:strategist", "warning")


def test_a_failing_job_is_not_also_reported_stale():
    assert keys(rules.jobs([job("derivs_hl", 40 * MIN, errs=30)], NOW)) == {"job_failing:derivs_hl": "critical"}


def test_stale_after_three_cadences_with_a_floor():
    (c,) = rules.jobs([job("strategist", 3 * 60 * MIN + MIN)], NOW)
    assert (c.key, c.severity, c.title, c.since) == (
        "job_stale:strategist", "warning", "strategist hasn't succeeded", NOW - 181 * MIN)
    assert rules.jobs([job("derivs_hl", 10 * MIN)], NOW) == []            # 3 x 1 min, but the floor is 15
    assert keys(rules.jobs([job("derivs_hl", 16 * MIN)], NOW)) == {"job_stale:derivs_hl": "critical"}


def test_a_paused_or_retired_job_is_never_stale():
    """2026-10-02: the retired Jev analyst's job skips every run as 'paused', so its last success only ages."""
    stale = [job("jev_analyst", 5 * 60 * MIN)]
    assert keys(rules.jobs(stale, NOW)) == {"job_stale:jev_analyst": "warning"}
    assert rules.jobs(stale, NOW, {"jev_analyst"}) == []


def test_manual_jobs_are_ignored():
    assert rules.jobs([job("replay", timedelta(days=30), errs=9), job("regrade_ideas", None, errs=1)], NOW) == []


def test_every_scheduled_job_has_a_cadence_and_nothing_else_does():
    src = (Path(__file__).parents[1] / "albatross" / "__main__.py").read_text()
    # an X job is scheduled behind its lane guard: add(xsearch.only_in("api", jobs["social_pulse"]), "cron", ...)
    scheduled = set(re.findall(r'add(?:_job)?\((?:xsearch\.only_in\("\w+", )?jobs\["(\w+)"\]\)?,\s*"cron"', src))
    from albatross.deepend import registry          # the Deep End schedules its jobs in loops over the registry
    scheduled |= {name for name, _ in registry.SCHEDULE + registry.HOST_SCHEDULE}
    assert scheduled == set(rules.CADENCE)
    assert all(isinstance(v, timedelta) for v in rules.CADENCE.values())


# ─── LLM agents ─────────────────────────────────────────────────────────────
def agent(name, runs, failures=0, refused=0, last_402_ago=None, first_402_ago=None, top_error=None):
    return {"agent": name, "runs": runs, "failures": failures, "refused": refused, "top_error": top_error,
            "last_402": NOW - last_402_ago if last_402_ago is not None else None,
            "first_402": NOW - (first_402_ago or last_402_ago) if last_402_ago is not None else None}


def test_any_402_is_one_loud_alert_even_when_intermittent():
    # this afternoon's pattern: most calls still succeed, some are refused for lack of credit
    hour = [agent("ltf_analyst", 35, refused=19, last_402_ago=2 * MIN, first_402_ago=50 * MIN),
            agent("flow_analyst", 7, refused=6, last_402_ago=23 * MIN, first_402_ago=41 * MIN), agent("news_scout", 5)]
    (c,) = rules.llm(hour, NOW)
    assert (c.key, c.severity, c.since) == ("openrouter_402", "critical", NOW - 50 * MIN)
    assert c.title == "OpenRouter refusing calls: out of credit (HTTP 402)"
    assert c.detail.startswith("25 calls refused in the last hour: ltf_analyst ×19, flow_analyst ×6; last at 02:58 UTC.")


def test_no_402_no_alert():
    assert rules.llm([agent("strategist", 3), agent("news_scout", 12, failures=1)], NOW) == []


def test_high_failure_rate_needs_enough_runs_and_ignores_402s():
    hour = [agent("ltf_analyst", 4, failures=2, top_error="ValidationError: x"),
            agent("news_scout", 2, failures=2, top_error="no JSON object in output"),
            agent("htf_analyst", 10, failures=4, top_error="x"),
            agent("flow_analyst", 7, failures=0, refused=6, last_402_ago=MIN)]
    assert keys(rules.llm(hour, NOW)) == {"llm_failures:ltf_analyst": "warning", "openrouter_402": "critical"}


# ─── candles ────────────────────────────────────────────────────────────────
def test_candles():
    fresh, old = NOW - 4 * MIN, NOW - 17 * MIN
    assert rules.candles({"BTC": fresh, "ETH": fresh}, NOW) == []
    (c,) = rules.candles({"BTC": fresh, "ETH": old}, NOW)
    assert (c.severity, c.title, c.since) == ("warning", "1 Tier-1 asset without new candles", old + 5 * MIN)
    (c,) = rules.candles({"BTC": old, "ETH": None}, NOW)
    assert c.severity == "critical" and "ETH never" in c.detail


# ─── Call feed ─────────────────────────────────────────────────────────────
def hours(counts: dict[int, int]) -> dict[datetime, int]:
    """{hours before NOW's hour: rows} -> hourly buckets."""
    base = NOW.replace(minute=0)
    return {base - timedelta(hours=h): n for h, n in counts.items()}


def test_busy_feed_silent_for_two_hours():
    # 40 rows/h at this time of day on each of the previous 7 days, nothing for the last 3 h
    hist = hours({d * 24 + h: 40 for d in range(1, 8) for h in range(4)} | {3: 40})
    (c,) = rules.call_feed(NOW - 150 * MIN, hist, NOW)
    assert (c.key, c.title, c.since) == ("call_feed_silent", "Call feed silent", NOW - 150 * MIN)


def test_silence_at_a_normally_quiet_hour_is_fine():
    hist = hours({d * 24 + h: 40 for d in range(1, 8) for h in range(4, 12)} | {3: 40})
    assert rules.call_feed(NOW - 150 * MIN, hist, NOW) == []


def test_recent_rows_or_short_silence_are_fine():
    hist = hours({h: 40 for h in range(30)})
    assert rules.call_feed(NOW - 30 * MIN, hist, NOW) == []


def test_first_day_falls_back_to_the_overall_rate():
    hist = hours({h: 30 for h in range(3, 10)})             # 7 h of history, then silence
    assert keys(rules.call_feed(NOW - 170 * MIN, hist, NOW)) == {"call_feed_silent": "warning"}
    assert rules.call_feed(None, {}, NOW) == []


# ─── ledger, spend ──────────────────────────────────────────────────────────
def test_ledger_and_spend():
    assert rules.ledger(15, None) == []
    assert keys(rules.ledger(3, 4)) == {"ledger_broken": "critical"}
    assert rules.spend(11.99, 12.0) == []
    (c,) = rules.spend(12.5, 12.0)
    assert c.key == "spend_high" and "$12.50" in c.title


# ─── notification planning ──────────────────────────────────────────────────
def alert(key, first_ago, resolved_ago=None, severity="critical", since_ago=None):
    return {"key": key, "severity": severity, "title": f"{key} broke", "detail": "details here",
            "first_seen": NOW - first_ago, "since": NOW - (since_ago or first_ago),
            "resolved_at": NOW - resolved_ago if resolved_ago is not None else None}


def test_plan_announces_new_alerts_once():
    a = alert("grader", 5 * MIN)
    assert notify.plan({"grader": a}, {}, {}, NOW) == [("grader", "open", a)]
    assert notify.plan({"grader": a}, {}, {"grader": {"state": "open", "sent_at": NOW - 5 * MIN}}, NOW) == []


def test_plan_holds_messages_to_one_per_hour():
    r = alert("grader", 50 * MIN, resolved_ago=10 * MIN)
    told = {"grader": {"state": "open", "sent_at": NOW - 50 * MIN}}
    assert notify.plan({}, {"grader": r}, told, NOW) == []                        # resolved, but < 1 h
    told["grader"]["sent_at"] = NOW - 61 * MIN
    assert notify.plan({}, {"grader": r}, told, NOW) == [("grader", "resolved", r)]


def test_plan_stays_quiet_about_flaps_nobody_heard_of():
    assert notify.plan({}, {"grader": alert("grader", 9 * MIN, resolved_ago=4 * MIN)}, {}, NOW) == []


def test_plan_reannounces_a_recurrence_after_the_hour():
    a = alert("grader", 5 * MIN)
    assert notify.plan({"grader": a}, {}, {"grader": {"state": "resolved", "sent_at": NOW - 2 * 60 * MIN}}, NOW) == [
        ("grader", "open", a)]


def test_message_text():
    # the problem began 40 min ago even though the alert only opened 12 min ago
    assert notify.message("open", alert("openrouter_402", 12 * MIN, since_ago=40 * MIN), NOW) == (
        "🔴 openrouter_402 broke · for 40m\nhttp://localhost:8787/#/health")
    assert notify.message("open", alert("call_feed_silent", 5 * MIN, severity="warning", since_ago=150 * MIN), NOW) == (
        "🟠 call_feed_silent broke · for 2h 30m\nhttp://localhost:8787/#/health")
    assert notify.message("resolved", alert("grader", 95 * MIN, resolved_ago=5 * MIN), NOW) == (
        "✅ Resolved: grader broke · lasted 1h 30m\nhttp://localhost:8787/#/health")


# ─── Telegram (stub transport: nothing is sent) ─────────────────────────────
@pytest.fixture
def captured(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(500 if b"fail" in req.url.path.encode() else 200, json={"ok": True})

    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(notify, "client", lambda: stub)
    return seen


def test_channels_are_off_by_default():
    assert asyncio.run(notify.channels()) == []     # no token: no channels, and no DB read


def test_telegram_payload(captured):
    asyncio.run(notify.Telegram("123:SECRET", "123456789").send("🔴 grader failing ×5 · for 36m\nhttps://x"))
    (req,) = captured
    assert (req.method, str(req.url)) == ("POST", "https://api.telegram.org/bot123:SECRET/sendMessage")
    assert json.loads(req.content) == {"chat_id": "123456789", "text": "🔴 grader failing ×5 · for 36m\nhttps://x",
                                       "disable_web_page_preview": True}


def test_send_survives_a_failing_channel_and_never_logs_the_token(captured, caplog):
    ok = asyncio.run(notify.send([notify.Telegram("123:fail-SECRET", "1"), notify.Telegram("456:OTHER", "1")],
                                 "open", alert("grader", 5 * MIN), NOW))
    assert ok and len(captured) == 2
    assert "HTTP 500" in caplog.text and "SECRET" not in caplog.text


# The alert bot must never share its token with a bot that long-polls for updates: any call
# that reads or changes the update stream (getUpdates, webhooks) breaks it. Only sendMessage is allowed.
BOT_API_METHOD = re.compile(r"api\.telegram\.org/bot[^/\s\"']*/(\w+)")
UPDATE_STREAM = {"getUpdates", "setWebhook", "deleteWebhook", "getWebhookInfo", "logOut"}


def test_only_the_alerts_bot_reads_updates_and_nothing_touches_webhooks():
    """Only albatross/alerts/bot.py may read the update stream (on Albatross's own bot); no code anywhere may
    set, delete or inspect a webhook or log a bot out; notify.py only sends."""
    root = Path(__file__).parents[1] / "albatross"
    for path in root.rglob("*.py"):
        tokens = [t for t in tokenize.generate_tokens(io.StringIO(path.read_text()).readline)
                  if t.type in (tokenize.NAME, tokenize.STRING)]             # code and strings, not comments
        words = set(re.findall(r"\w+", " ".join(t.string for t in tokens)))
        assert not (UPDATE_STREAM - {"getUpdates"}) & words, path
        if path.name != "bot.py" or path.parent.name != "alerts":
            assert "getUpdates" not in words, path
    assert BOT_API_METHOD.findall(notify.SEND_MESSAGE) == ["sendMessage"]


def test_alerts_bot_refuses_a_shared_bot(monkeypatch):
    from albatross.alerts import bot
    from albatross.config import settings
    monkeypatch.setattr(settings, "telegram_shared_bots", "@Other_Bot, second_bot")
    assert bot.shared_bots() == {"other_bot", "second_bot"}       # read when the bot starts: Setup can change it
    monkeypatch.setattr(settings, "telegram_shared_bots", "")
    assert bot.shared_bots() == set()


def test_alerts_bot_password_and_lockout():
    from albatross.alerts import bot
    stored = bot.hash_password("correct horse")
    assert stored.startswith("pbkdf2:") and "$" not in stored and "correct horse" not in stored
    assert bot.check_password("correct horse", stored) and bot.check_password(" correct horse\n", stored)
    assert not bot.check_password("correct hors", stored) and not bot.check_password("x", None)
    g = bot.Gate()
    for i in range(bot.MAX_TRIES):
        assert not g.locked(7, 100.0 + i)
        g.miss(7, 100.0 + i)
    assert g.locked(7, 110.0) and not g.locked(8, 110.0)
    assert not g.locked(7, 100.0 + bot.LOCKOUT_S + 10)              # an hour later it lets them try again
    d = bot.decide
    assert d("/start", False, False, False) == "welcome" and d("/start", True, False, False) == "already"
    assert d("/stop", True, False, False) == "stopped" and d("/stop", False, False, False) == "not_subscribed"
    assert d("pw", False, False, True) == "ok" and d("pw", False, False, False) == "wrong"
    assert d("pw", False, True, True) == "locked" and d("hello", True, False, False) == "already"


def test_the_reddit_scout_failing_to_search_is_an_alert():
    assert rules.reddit(12, 12)[0].key == "reddit_empty" and "12 of its last 12 reads failed" in rules.reddit(12, 12)[0].title
    assert rules.reddit(12, 9) == [] and rules.reddit(5, 5) == []          # mostly worked / too few reads


def test_a_typesafe_402_names_typesafe_and_where_to_top_it_up():
    hour = [{"agent": "jev_scout", "runs": 756, "refused": 756, "first_402": NOW - 50 * MIN, "last_402": NOW - 2 * MIN,
             "typesafe_402": True, "failures": 0, "top_error": None},
            {"agent": "ltf_analyst", "runs": 4, "refused": 2, "first_402": NOW - 10 * MIN, "last_402": NOW - 5 * MIN,
             "typesafe_402": False, "failures": 0, "top_error": None}]
    by = {c.key: c for c in rules.llm(hour, NOW)}
    assert by["typesafe_402"].title.startswith("TypeSafe (Jev) refusing calls") and "console.typesafe.ai" in by["typesafe_402"].detail
    assert "jev_scout ×756" in by["typesafe_402"].detail and "jev_scout" not in by["openrouter_402"].detail
    assert "openrouter.ai/settings/credits" in by["openrouter_402"].detail
