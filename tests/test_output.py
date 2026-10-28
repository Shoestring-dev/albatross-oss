"""The output tracker (albatross/tracker.py, albatross/api/output.py), the controls, and the map of who reads whom."""

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from albatross import controls, tracker
from albatross.agents import graph
from albatross.ingest import health


# ─── recursive loops: the map ─────────────────────────────────────────────────
def test_no_agent_reads_its_own_conclusions_except_the_known_loops():
    loops = graph.cycles()
    unknown = [c for c in loops if not graph.known(c)]
    assert unknown == [], f"a new recursive loop: {unknown} (fix it, or name it in graph.KNOWN_LOOPS)"


def test_cycle_detection_finds_a_planted_loop():
    planted = {"a": ["c"], "b": ["a"], "c": ["b"], "d": []}
    assert graph.cycles(planted) == [["a", "b", "c"]]
    assert graph.cycles({"a": [], "b": ["a"]}) == []


def test_every_agent_in_the_map_is_known_and_depth_counts_model_hops():
    nodes = set(graph.READS) | {s for v in graph.READS.values() for s in v}
    assert nodes <= set(graph.KIND) and nodes <= set(graph.LABEL)
    d = graph.depth()
    assert d["ltf_analyst"] == 0 and d["strategist"] == 1 and d["deep_fit"] == 1 and d["deep_take"] == 2


def test_the_oracle_leaves_itself_out_of_the_ensemble_it_reads():
    src = open("albatross/agents/oracle.py").read()
    assert src.count("ensemble.compute(") == src.count("exclude=SELF")


# ─── loop signs ──────────────────────────────────────────────────────────────
def test_agent_flags():
    base = {"calls": 20, "ok": 20, "failed": 0, "repeats": 0, "output_total": 30, "measured": True, "cost_usd": 1.0}
    assert tracker.agent_flags(base) == []
    kinds = lambda a: [f["kind"] for f in tracker.agent_flags(a)]
    assert kinds(base | {"repeats": 7}) == ["repeating"]                          # 7/20 = 35% resent
    assert kinds(base | {"output_total": 0}) == ["spinning"]
    assert kinds(base | {"output_total": 0, "measured": False}) == []             # nothing to count it by: no verdict
    assert kinds(base | {"output_total": 0, "cost_usd": 0.0, "pool_usd": 0.0}) == []   # free and idle: not spending
    assert kinds(base | {"failed": 8, "ok": 12}) == ["failing"]
    assert kinds({"calls": 3, "ok": 3, "repeats": 3, "measured": True, "cost_usd": 1}) == []   # too few to judge
    assert kinds(base | {"paused": True}) == ["paused"]
    # the culling system's rung shows beside the owner's own pause (2026-10-02: the Output page said "ok" for culled agents)
    assert [f["label"] for f in tracker.agent_flags(base | {"rung": "paused"})] == ["Paused by culling"]
    assert tracker.agent_flags(base | {"rung": "watch"}) == []                     # on watch changes nothing yet
    assert [f["label"] for f in tracker.job_flags({"runs": 0, "rows": 0, "rung": "retired"})] == ["Retired"]
    # the quant model is code inside the features jobs: its rung shows there
    assert [f["label"] for f in tracker.job_flags({"job": "features_htf", "runs": 0, "rows": 0, "quant_rung": "probation"})] \
        == ["Quant model: benched"]
    assert tracker.job_flags({"job": "grader", "runs": 0, "rows": 0, "quant_rung": "probation"}) == []


def test_job_flags():
    kinds = lambda j: [f["kind"] for f in tracker.job_flags(j)]
    assert kinds({"runs": 30, "ok": 30, "failed": 0, "overlap": 0, "rows": 500}) == []
    assert kinds({"runs": 30, "ok": 30, "failed": 0, "overlap": 4, "rows": 500}) == ["overlap"]
    assert kinds({"runs": 30, "ok": 30, "failed": 0, "overlap": 0, "rows": 0}) == ["idle"]
    assert kinds({"runs": 10, "ok": 6, "failed": 4, "overlap": 0, "rows": 9}) == ["failing"]


# ─── controls ────────────────────────────────────────────────────────────────
def test_a_paused_job_is_skipped_and_recorded(monkeypatch):
    recorded, ran = [], []

    async def rows():
        return {"deep_x_scan": {"paused": True, "budget_scale": 1.0}}

    async def record(name, started, seconds, status, *a, **k):
        recorded.append((name, status))

    class Pool:
        async def execute(self, *a):
            ran.append("health")

    async def pool():
        return Pool()

    monkeypatch.setattr(controls, "_rows", rows)
    monkeypatch.setattr(health, "record_run", record)
    monkeypatch.setattr(health.db, "pool", pool)

    @health.tracked("deep_x_scan")
    async def job():
        ran.append("job")
        return 5

    assert asyncio.run(job()) == 0
    assert ran == [] and recorded == [("deep_x_scan", "paused")]
    assert job.__name__ == "deep_x_scan"                   # APScheduler names the job after it


def test_a_job_returning_a_breakdown_records_rows_and_detail(monkeypatch):
    recorded = []

    async def record(name, started, seconds, status, rows=None, detail=None, error=None):
        recorded.append((name, status, rows, detail))

    class Pool:
        async def execute(self, *a):
            pass

    async def pool():
        return Pool()

    monkeypatch.setattr(health, "record_run", record)
    monkeypatch.setattr(health.db, "pool", pool)

    @health.tracked("deep_compose")
    async def job():
        return {"rows": 12, "changed": 3}

    assert asyncio.run(job()) == 12
    assert recorded == [("deep_compose", "ok", 12, {"rows": 12, "changed": 3})]


def test_budget_scale_and_pause_reach_the_model_calls(monkeypatch):
    from albatross.config import settings
    from albatross.llm import openrouter

    async def rows():
        return {"deep_take": {"paused": True, "budget_scale": 1.0}, "news_scout": {"paused": False, "budget_scale": 0.0}}

    monkeypatch.setattr(controls, "_rows", rows)
    monkeypatch.setattr(settings, "openrouter_api_key", "k")

    async def spent(agent):
        return 0.0

    monkeypatch.setattr(openrouter, "spent_today", spent)
    with pytest.raises(openrouter.BudgetExceeded, match="paused"):
        asyncio.run(openrouter.structured("deep_take", object, "s", "u", daily_budget_usd=1.0))
    with pytest.raises(openrouter.BudgetExceeded, match=r"\$0.00 of \$0.00"):          # scaled to nothing
        asyncio.run(openrouter.structured("news_scout", object, "s", "u", daily_budget_usd=1.0))


def test_the_controls_endpoint_needs_its_header_and_a_known_key(monkeypatch):
    from albatross.api import output
    writes = []

    class Pool:
        async def fetchrow(self, sql, *args):
            writes.append(args)
            return {"key": args[0], "paused": bool(args[1]), "budget_scale": args[2] or 1.0, "note": args[3],
                    "updated_at": datetime.now(UTC)}

    async def pool():
        return Pool()

    async def known():
        return {"deep_x_scan", "birdeye"}

    monkeypatch.setattr(output.db, "pool", pool)
    monkeypatch.setattr(output, "_known_keys", known)
    app = FastAPI()
    app.include_router(output.router)
    c = TestClient(app)
    assert c.post("/api/controls/deep_x_scan", json={"paused": True}).status_code == 403           # no header
    h = {"X-Albatross-Action": "1"}
    assert c.post("/api/controls/nope", json={"paused": True}, headers=h).status_code == 404
    assert c.post("/api/controls/birdeye", json={"budget_scale": 9}, headers=h).status_code == 422   # 0..5
    r = c.post("/api/controls/deep_x_scan", json={"paused": True}, headers=h)
    assert r.status_code == 200 and r.json()["paused"] is True and writes[-1][:2] == ("deep_x_scan", True)


def test_the_output_page_shows_the_culling_verdict_like_the_agents_page():
    """2026-10-02: the retired Jev analyst read "not graded yet" on Output and "harmful" on Agents."""
    from albatross.api import output as api
    harmful = {"metric": "direction", "value": -0.063, "band": 0.052, "n": 351.0,
               "what": "hit rate over random calls with its own long/short mix"}
    q = api.fair(harmful)
    assert q["verdict"] == "harmful" and q["summary"].startswith("Worse than a coin flip: -6.3 points ± 5.2")
    assert api.fair(harmful | {"n": 12.0}) is None                                  # too early: the old record stands
    assert api.fair(None) is None and api.fair({"metric": "unreviewed"}) is None

