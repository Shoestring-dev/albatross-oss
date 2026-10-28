"""Dashboard endpoints on awkward rows: nullable columns must never turn a page into a 500 (Today went blank
once over one idea without a reward:risk). A stub pool stands in for Postgres; nothing touches a database."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from albatross import db
from albatross.api import app as api
from albatross.api import overview

NOW = datetime.now(UTC)
MIN = timedelta(minutes=1)


class StubPool:
    """Answers each query with the first canned result whose key occurs in its SQL."""

    def __init__(self, fetch=(), fetchrow=(), fetchval=()):
        self.routes = {"fetch": list(fetch), "fetchrow": list(fetchrow), "fetchval": list(fetchval)}

    def _answer(self, kind, sql, empty):
        return next((v for k, v in self.routes[kind] if k in sql), empty)

    async def fetch(self, sql, *args):
        return self._answer("fetch", sql, [])

    async def fetchrow(self, sql, *args):
        return self._answer("fetchrow", sql, None)

    async def fetchval(self, sql, *args):
        return self._answer("fetchval", sql, None)


@pytest.fixture
def stub(monkeypatch):
    def use(pool):
        async def get_pool():
            return pool
        monkeypatch.setattr(db, "pool", get_pool)
        api._cache.clear()
    yield use
    api._cache.clear()


def test_overview_survives_null_prices_and_a_call_without_reward_risk(stub, monkeypatch):
    async def no_macro():
        return {}
    monkeypatch.setattr(overview.macro, "snapshot", no_macro)
    stub(StubPool(
        fetch=[
            ("SELECT symbol FROM assets", [{"symbol": "BTC"}, {"symbol": "ETH"}]),
            ("WITH now_", [{"symbol": "BTC", "px": None, "px_24h": 80_000.0},      # newest snapshot has no mark
                           {"symbol": "ETH", "px": 2_600.0, "px_24h": None}]),
            ("FROM candles", [{"symbol": "ETH", "open": 2_500.0}]),                   # 24h-ago price from 5m candles
            ("FROM trade_calls c", [{"id": 7, "logged_at": NOW - MIN, "scope": "SOL", "direction": "short", "tf": "4h",
                                     "status": "proposed", "reward_risk": None, "source_id": "strategist",
                                     "verdict": None}]),
        ],
        fetchrow=[("count(*) AS total", {"total": 1, "vetoed": 0, "today": 1})],
        fetchval=[("to_regclass", False)],
    ))
    out = asyncio.run(overview.overview())
    assert out["kpis"]["btc"] == {"price": None, "chg_24h_pct": None}                # no live mark: no change, no crash
    assert out["kpis"]["eth"]["chg_24h_pct"] == pytest.approx(4.0)                  # rolling 24h, never the daily candle
    (idea,) = out["radar"]
    assert idea["title"] == "Short SOL · 4h" and idea["detail"] == "proposed"      # no "R:R None"
    assert out["report"] is None


def job(name, last_ok_ago, errs=0):
    return {"job": name, "last_ok": NOW - last_ok_ago if last_ok_ago is not None else None,
            "last_run": NOW - MIN, "consecutive_errors": errs, "last_error": "boom" if errs else None,
            "last_rows": 3}


def test_job_state_matches_the_alert_rule():
    assert api.job_state(job("grader", 4 * MIN), NOW) == "ok"
    assert api.job_state(job("grader", 16 * MIN), NOW) == "late"             # 5-min job: 3 cadences, 15-min floor
    assert api.job_state(job("derivs_hl", 14 * MIN), NOW) == "ok"            # 1-min job: never stricter than 15 min
    assert api.job_state(job("strategist", 2 * 60 * MIN), NOW) == "ok"
    assert api.job_state(job("strategist", 3 * 60 * MIN + MIN), NOW) == "late"
    assert api.job_state(job("grader", None), NOW) == "late"                 # scheduled but never succeeded
    assert api.job_state(job("grader", 4 * MIN, errs=1), NOW) == "failing"
    assert api.job_state(job("replay", 30 * 24 * 60 * MIN, errs=2), NOW) == "manual"   # one-offs aren't watched


def test_health_reports_state_and_cadence_per_job(stub):
    stub(StubPool(
        fetch=[("ingest_health", [job("grader", 4 * MIN), job("strategist", None), job("replay", MIN, errs=1)]),
               ("agent_runs", [{"agent": "strategist", "calls": 3, "failures": 1, "cost_usd": 0.0}])],
        fetchrow=[("count(*) FROM candles", {"assets": 0, "candles": 0, "deriv_snapshots": 0, "news": 0,
                                             "features": 0, "signals": 0, "collecting_since": None})],
    ))
    out = asyncio.run(api.health())
    jobs = {j["job"]: (j["state"], j["every_s"]) for j in out["jobs"]}
    assert jobs == {"grader": ("ok", 300.0), "strategist": ("late", 3600.0), "replay": ("manual", None)}
    assert out["counts"]["collecting_since"] is None and out["agent_spend_today"][0]["failures"] == 1


def test_alerts_without_an_alerts_table_or_a_first_check(stub):
    stub(StubPool(fetchval=[("to_regclass", False)]))
    assert asyncio.run(api.alerts()) == {"open": [], "resolved": [], "checked_at": None}
    stub(StubPool(fetchval=[("to_regclass", True), ("ingest_health", None)]))
    assert asyncio.run(api.alerts())["open"] == []
