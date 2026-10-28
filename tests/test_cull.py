"""The culling system (albatross/agents/cull.py): fair baselines, verdicts, the ladder, its effects, the weekly review."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest

from albatross import controls
from albatross.agents import cull
from albatross.grading import fair
from albatross.strategy import ensemble

NOW = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)


def test_the_fair_baseline_is_random_calls_with_the_agents_own_long_short_mix():
    # The Flow analyst on 1h, 2026-10-01: 75% long, 44.2% right, price rose in 43.7% of its windows. The hindsight
    # bar (always-short, picked after the fact) was 56.3%; random calls with its own mix would have hit 46.8%.
    f = fair.fair(1088, 0.442, 0.75, 0.437)
    assert round(f["matched"], 3) == 0.468 and round(f["hindsight"], 3) == 0.563 and round(f["skill"], 3) == -0.026
    assert f["always_long"] == 0.437 and round(f["n_eff"], 1) == 181.3
    both = fair.pooled([f | {"tf": "1h"}, fair.fair(209, 0.498, 0.77, 0.498) | {"tf": "4h"}])
    assert both["n"] == 1297 and -0.026 < both["skill"] < 0


def test_verdicts_need_enough_evidence_and_a_clear_interval():
    assert cull.verdict(0.05, 0.03, 100, 30) == "earns"
    assert cull.verdict(-0.05, 0.03, 100, 30) == "harmful"
    assert cull.verdict(-0.02, 0.03, 100, 30) == "no_skill"
    assert cull.verdict(0.30, 0.03, 10, 30) == "too_early" and cull.verdict(None, None, None, 30) == "too_early"


def test_the_ladder_moves_one_step_per_counted_weak_review_and_back_up_on_evidence():
    rung, streak = "active", 0
    path = []
    for v in ["no_skill", "harmful", "no_skill", "no_skill"]:
        rung, streak = cull.step(rung, streak, v, counted=True)
        path.append(rung)
    assert path == ["watch", "probation", "probation", "paused"] and streak == 4
    assert cull.step("paused", 4, "no_skill", counted=False) == ("paused", 4)       # an early review moves nothing
    assert cull.step("probation", 2, "too_early", counted=True) == ("probation", 2)
    assert cull.step("paused", 4, "earns", counted=False) == ("active", 0)          # evidence returns it at once
    assert cull.step("retired", 5, "earns", counted=True) == ("retired", 5)        # only the owner reinstates
    assert cull.step("probation", 0, "no_skill", counted=True) == ("probation", 1)  # never back up without earning


def test_a_critic_is_worth_having_if_what_it_stops_does_worse():
    rows = ([{"status": "proposed", "r": 0.4, "jev_veto": 0.05}, {"status": "proposed", "r": 0.6, "jev_veto": 0.1}] * 20
            + [{"status": "vetoed", "r": -0.9, "jev_veto": 0.5}, {"status": "vetoed", "r": -1.1, "jev_veto": 0.45}] * 20)
    ev = cull.critic_evidence(rows)
    assert ev["strategist_critic"]["value"] > 1 and ev["jev_critic"]["value"] > 1
    assert cull.verdict(**{k: ev["jev_critic"][k] for k in ("value", "band", "n")}, minimum=60) == "earns"


def test_rungs_act_through_the_same_controls_as_the_owners_settings(monkeypatch):
    async def rows():
        return {"flow_analyst": {"paused": False, "budget_scale": 2.0}}

    async def rungs():
        return {"flow_analyst": "probation", "ltf_analyst": "paused", "htf_analyst": "retired"}

    monkeypatch.setattr(controls, "_rows", rows)
    monkeypatch.setattr(controls, "_rungs", rungs)
    run = asyncio.run
    assert run(controls.scale("flow_analyst")) == 1.0                      # The owner's 2x times probation's half
    assert run(controls.scale("ltf_analyst")) == 0.1 and run(controls.paused("htf_analyst")) is True
    assert run(controls.paused("ltf_analyst")) is False                    # paused agents still run, on a sample
    coins = list(range(70))
    draws = iter([0.05, 0.5] * 35)
    assert run(controls.sampled("ltf_analyst", coins, draw=lambda: next(draws))) == list(range(0, 70, 2))
    assert run(controls.sampled("flow_analyst", coins)) == coins
    assert len(run(controls.sampled("ltf_analyst", [1, 2], draw=lambda: 0.99))) == 1   # never no work at all


def test_a_culled_source_counts_for_nothing_in_the_ensemble():
    w = {("flow_analyst", "1h"): {"weight": 0.1}, ("ltf_analyst", "15m"): {"weight": 0.41}, ("quant", "5m"): {"weight": 0.05}}
    out = ensemble.culled(w, {"flow_analyst": "probation", "ltf_analyst": "watch"})
    assert out[("flow_analyst", "1h")] == {"weight": 0.0, "culled": "probation"}
    assert out[("ltf_analyst", "15m")]["weight"] == 0.41 and out[("quant", "5m")]["weight"] == 0.05


def _fake_db(monkeypatch, status_rows):
    written = {}

    class Conn:
        async def executemany(self, sql, rows):
            written["reviews" if "agent_reviews" in sql else "status"] = list(rows)

        @asynccontextmanager
        async def transaction(self):
            yield

    class Pool:
        async def fetch(self, sql, *a):
            return status_rows

        @asynccontextmanager
        async def acquire(self):
            yield Conn()

    async def pool():
        return Pool()

    monkeypatch.setattr(cull.db, "pool", pool)
    return written


def test_the_weekly_review_moves_agents_and_says_so_once(monkeypatch):
    ev = {"flow_analyst": {"metric": "direction", "value": -0.026, "band": 0.072, "n": 181.0, "what": "x"},
          "quant": {"metric": "direction", "value": -0.09, "band": 0.02, "n": 2100.0, "what": "x"},
          "social_pulse": {"metric": "direction", "value": 0.015, "band": 0.12, "n": 10.0, "what": "x"},
          "ltf_analyst": {"metric": "direction", "value": 0.06, "band": 0.03, "n": 120.0, "what": "x"}}
    sent = []

    async def evidence():
        return ev

    async def stats(hours):
        return [{"agent": "flow_analyst", "cost_usd": 9.5, "pool_usd": 0.0}]

    async def notify(moves, now):
        sent.append(cull.message(moves, now))

    monkeypatch.setattr(cull, "evidence", evidence)
    monkeypatch.setattr(cull.tracker, "agent_stats", stats)
    monkeypatch.setattr(cull, "notify", notify)
    written = _fake_db(monkeypatch, [
        {"agent": "quant", "rung": "watch", "streak": 1, "counted_at": NOW - timedelta(days=7)},
        {"agent": "ltf_analyst", "rung": "probation", "streak": 2, "counted_at": NOW - timedelta(days=7)}])
    out = asyncio.run(cull.review(NOW))
    moved = dict(m.split(": ") for m in out["moved"])
    assert moved == {"flow_analyst": "active -> watch", "quant": "watch -> probation", "ltf_analyst": "probation -> active"}
    by = {r[1]: r for r in written["reviews"]}
    assert by["social_pulse"][3] == "too_early" and by["social_pulse"][11] == "active"     # too little evidence: no move
    assert by["news_scout"][3] == "unreviewed" and "ensemble" not in by and "factor_rank" not in by
    assert len(sent) == 1 and "▼ Flow analyst: active → watch" in sent[0] and "▲ LTF analyst: probation → active" in sent[0]
    assert "Next review Thu Oct 15" in sent[0] and "/#/agents" in sent[0]


def test_a_review_within_six_days_of_the_last_counted_one_moves_no_one_down(monkeypatch):
    async def evidence():
        return {"flow_analyst": {"metric": "direction", "value": -0.05, "band": 0.02, "n": 300.0, "what": "x"}}

    async def stats(hours):
        return []

    async def notify(moves, now):
        raise AssertionError("nothing moved, nothing to say")

    monkeypatch.setattr(cull, "evidence", evidence)
    monkeypatch.setattr(cull.tracker, "agent_stats", stats)
    monkeypatch.setattr(cull, "notify", notify)
    written = _fake_db(monkeypatch, [{"agent": "flow_analyst", "rung": "watch", "streak": 1,
                                      "counted_at": NOW - timedelta(days=2)}])
    assert asyncio.run(cull.review(NOW))["moved"] == []
    assert {r[1]: r[9] for r in written["reviews"]}["flow_analyst"] is False                # recorded, not counted


def test_only_the_owner_retires_and_the_endpoint_needs_its_header(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from albatross.api import agents as api
    done = []

    async def set_rung(agent, rung, note=None):
        done.append((agent, rung))
        return {"agent": agent, "rung": rung}

    with pytest.raises(ValueError):
        asyncio.run(cull.set_rung("flow_analyst", "gone"))
    monkeypatch.setattr(api.cull, "set_rung", set_rung)
    app = FastAPI()
    app.include_router(api.router)
    c = TestClient(app)
    assert c.post("/api/agents/flow_analyst/rung", json={"rung": "retired"}).status_code == 403
    h = {"X-Albatross-Action": "1"}
    assert c.post("/api/agents/ensemble/rung", json={"rung": "retired"}, headers=h).status_code == 404   # exempt
    assert c.post("/api/agents/flow_analyst/rung", json={"rung": "gone"}, headers=h).status_code == 422
    assert c.post("/api/agents/flow_analyst/rung", json={"rung": "retired"}, headers=h).status_code == 200
    assert done == [("flow_analyst", "retired")]


def test_an_agent_with_nothing_graded_is_too_early_not_a_crash():
    ev = {"metric": "direction", "value": None, "band": None, "n": None, "what": "x"}
    assert cull.judge(ev) == "too_early" and cull.words(ev, "too_early") == "too early (nothing graded in the last 30 days)"


def test_a_timeframe_every_ensemble_source_was_culled_from_is_uncovered():
    """2026-10-02: the quant model on probation and the LTF analyst paused left 5m and 15m with no source; the matrix
    and the coin page say so instead of showing the culled sources' last calls as stale."""
    from albatross.agents import graph
    from albatross.strategy import ensemble
    unc = ensemble.uncovered({"quant": "probation", "ltf_analyst": "paused"}, graph.LABEL)
    assert set(unc) == {"5m", "15m"}
    assert unc["5m"] == "the quant model is benched and the LTF analyst is paused"
    assert ensemble.uncovered({"quant": "watch", "ltf_analyst": "paused"}, graph.LABEL) == {}     # watch keeps its weight
    assert "1h" not in ensemble.uncovered({"quant": "probation", "ltf_analyst": "paused", "flow_analyst": "paused"})
