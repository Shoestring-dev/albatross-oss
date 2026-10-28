"""Controls set on the Output page (albatross/api/output.py): pause a job or a model agent, or scale its budget.

One row per key in agent_controls: a job name (as in ingest_health, checked by every tracked job run) or a model
agent (as in agent_runs, checked by every model call in albatross/llm). Reads are cached for CACHE_S, so the hot
paths cost nothing and a change takes effect within that time. No table yet means no controls.

The culling system's rungs (albatross/agents/cull.py, agent_status) act through the same questions, multiplied with
The owner's own settings: probation halves an agent's budget, paused cuts it to a tenth and runs it on a 10% sample where
the agent supports one (sampled()), retired stops it.
"""

import random
import time

import asyncpg

from albatross import db

CACHE_S = 30
RUNG_SCALE = {"probation": 0.5, "paused": 0.1}
RUNG_SAMPLE = {"paused": 0.1}
OFF_ENSEMBLE = {"probation", "paused", "retired"}      # their calls stop counting in the ensemble
_cache: dict = {"at": float("-inf"), "rows": {}}
_rung_cache: dict = {"at": float("-inf"), "rows": {}}


async def _rows() -> dict[str, dict]:
    if time.monotonic() - _cache["at"] > CACHE_S:
        pool = await db.pool()
        try:
            rows = await pool.fetch("SELECT key, paused, budget_scale FROM agent_controls")
        except asyncpg.UndefinedTableError:            # before migration 025
            rows = []
        _cache.update(at=time.monotonic(), rows={r["key"]: dict(r) for r in rows})
    return _cache["rows"]


async def _rungs() -> dict[str, str]:
    if time.monotonic() - _rung_cache["at"] > CACHE_S:
        pool = await db.pool()
        try:
            rows = await pool.fetch("SELECT agent, rung FROM agent_status")
        except asyncpg.UndefinedTableError:            # before migration 028
            rows = []
        _rung_cache.update(at=time.monotonic(), rows={r["agent"]: r["rung"] for r in rows})
    return _rung_cache["rows"]


async def rungs() -> dict[str, str]:
    """Every agent the culling system has placed (agents missing here are active)."""
    return await _rungs()


async def rung(key: str) -> str:
    """Where the culling system has this agent: active, watch, probation, paused or retired."""
    return (await _rungs()).get(key, "active")


async def paused(key: str) -> bool:
    return bool((await _rows()).get(key, {}).get("paused")) or await rung(key) == "retired"


async def scale(key: str) -> float:
    """The budget multiplier for this key (1.0 when unset): the owner's setting times the agent's rung."""
    return float((await _rows()).get(key, {}).get("budget_scale", 1.0)) * RUNG_SCALE.get(await rung(key), 1.0)


async def sampled(key: str, items: list, draw=random.random) -> list:
    """The share of this run's work a paused agent still does (all of it otherwise), so it keeps being graded and
    can earn its way back. Never empty when there was work."""
    rate = RUNG_SAMPLE.get(await rung(key), 1.0)
    if rate >= 1 or not items:
        return items
    return [x for x in items if draw() < rate] or [random.choice(items)]


def forget() -> None:
    """Drop the caches (after a change in this process)."""
    _cache["at"] = float("-inf")
    _rung_cache["at"] = float("-inf")
