"""System > Output (albatross/tracker.py over HTTP): what every job and model agent produced, what it cost, how it
has graded, the loop signs, the map of who reads whom, and the controls (pause, budget scale).

Changing a control needs the X-Albatross-Action header: a page on another site can't send it with the owner's cookie
without a CORS preflight this API never answers, so the site's login alone can't be used against it."""

import asyncio
import time
from datetime import UTC, datetime, timedelta

import asyncpg
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from albatross import controls, db, tracker
from albatross.agents import cull, graph
from albatross.config import settings
from albatross.llm import codex

router = APIRouter(prefix="/api")

WINDOWS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}
CACHE_S = 300          # a fresh build takes ~2 s (the Agents page waited on it); 5 minutes is fresh enough
_cache: dict[str, tuple[float, dict]] = {}


def fair(ev: dict | None) -> dict | None:
    """The culling system's verdict on its fair baseline (agents/cull.py), as the Agents page shows it, when it has
    one; else None and the older record stands. (2026-10-02: the Output page said "not graded yet" for the retired
    Jev analyst while the Agents page said harmful.)"""
    v = cull.judge(ev)
    if v not in ("earns", "no_skill", "harmful"):
        return None
    w = cull.words(ev, v)
    return {"summary": w[:1].upper() + w[1:], "verdict": v}
_lock = asyncio.Lock()


async def plan(hours: float) -> dict | None:
    """The ChatGPT plan's weekly allowance and how the subscription lane is doing (llm/codex.py). The lane's state is
    read from the calls themselves, since this process can't see ingest's switch or its cool-off after a limit."""
    pool = await db.pool()
    try:
        lim = await codex.latest_limits()
    except asyncpg.UndefinedTableError:                # before migration 037
        return None
    now = datetime.now(UTC)
    gpt = list(codex.TWINS)
    r = await pool.fetchrow(
        """SELECT count(*) FILTER (WHERE model LIKE 'codex:%') AS on_plan,
                  count(*) FILTER (WHERE model LIKE 'codex:%' AND NOT ok) AS failed,
                  coalesce(sum(credits), 0)::float AS credits,
                  count(*) FILTER (WHERE model = ANY($2::text[]) AND agent NOT IN ('oracle', 'oracle_calendar')) AS api
           FROM agent_runs WHERE started_at > $1""", now - timedelta(hours=hours), gpt)
    recent = await pool.fetch(                         # the lane's state, from its last ten calls
        """SELECT model LIKE 'codex:%' AND ok AS on_plan, started_at FROM agent_runs
           WHERE (model LIKE 'codex:%' OR (model = ANY($1::text[]) AND agent NOT IN ('oracle', 'oracle_calendar')))
           ORDER BY started_at DESC LIMIT 10""", gpt)
    out = {"on_plan": r["on_plan"], "failed": r["failed"], "credits": round(r["credits"], 1),
           "credits_usd": round(r["credits"] * codex.CREDIT_USD, 2), "on_openrouter": r["api"],
           "reserve_pct": settings.codex_reserve_pct,
           "recent": {"n": len(recent), "on_plan": sum(1 for x in recent if x["on_plan"]),
                      "at": recent[0]["started_at"]} if recent else None}
    if lim:
        out |= {"used_percent": lim["used_percent"], "checked_at": lim["checked_at"], "resets_at": lim["resets_at"],
                "reading_age_min": round((now - lim["checked_at"]).total_seconds() / 60),
                "pace_line": round(codex.pace_line(lim["window_mins"], lim["resets_at"], now, settings.codex_reserve_pct,
                                                   settings.codex_burst_pct), 1)}
    return out


async def _build(hours: float) -> dict:
    agents, jobs, quality, evidence, chatgpt = await asyncio.gather(
        tracker.agent_stats(hours), tracker.job_stats(hours), tracker.quality(), cull.evidence(), plan(hours),
        return_exceptions=True)
    for x in (agents, jobs, quality):
        if isinstance(x, BaseException):
            raise x
    evidence = {} if isinstance(evidence, BaseException) else evidence
    chatgpt = None if isinstance(chatgpt, BaseException) else chatgpt
    by_agent = {a["agent"]: a for a in agents}
    g = graph.summary()
    for n in g["nodes"]:
        a = by_agent.get(n["id"])
        n["calls"] = a["calls"] if a else None
        n["output_total"] = a["output_total"] if a else None
    for a in agents:                                   # attach the graded record where one exists
        a["quality"] = fair(evidence.get(a["agent"])) or quality.get("strategist_ideas" if a["agent"] == "strategist" else a["agent"])
        a["depth"] = next((n["depth"] for n in g["nodes"] if n["id"] == a["agent"]), None)
    flags = [f | {"who": a["agent"], "label_who": a["label"]} for a in agents for f in a["flags"] if f["kind"] != "paused"]
    flags += [f | {"who": j["job"], "label_who": j["job"]} for j in jobs for f in j["flags"] if f["kind"] not in ("paused", "idle")]
    return {
        "hours": hours,
        "summary": {"calls": sum(a["calls"] for a in agents), "outputs": sum(a["output_total"] for a in agents),
                    "spend_usd": round(sum(a["cost_usd"] for a in agents), 2),
                    "pool_usd": round(sum(a["pool_usd"] for a in agents), 2),
                    "job_runs": sum(j["runs"] for j in jobs), "flags": len(flags),
                    "loops": sum(1 for loop in g["loops"] if not loop["known"]), "known_loops": len(g["loops"])},
        "flags": flags, "agents": agents, "jobs": jobs, "graph": g, "plan": chatgpt,
        "thresholds": {"min_calls": tracker.MIN_CALLS, "repeat_share": tracker.REPEAT_SHARE,
                       "fail_share": tracker.FAIL_SHARE, "overlaps": tracker.OVERLAPS},
    }


@router.get("/output")
async def output(window: str = "24h"):
    hours = WINDOWS.get(window, 24)
    async with _lock:
        hit = _cache.get(window)
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        body = await _build(hours)
        _cache[window] = (time.monotonic(), body)
        return body


class Control(BaseModel):
    paused: bool | None = None
    budget_scale: float | None = Field(default=None, ge=0, le=5)
    note: str | None = Field(default=None, max_length=300)


async def _known_keys() -> set[str]:
    pool = await db.pool()
    jobs = {r["job"] for r in await pool.fetch("SELECT job FROM ingest_health")}
    agents = {r["agent"] for r in await pool.fetch(
        "SELECT DISTINCT agent FROM agent_runs WHERE started_at > now() - interval '30 days'")}
    return jobs | agents | set(graph.KIND) | {"birdeye"}


@router.get("/controls")
async def list_controls():
    pool = await db.pool()
    return {"controls": [dict(r) for r in await pool.fetch("SELECT * FROM agent_controls ORDER BY key")]}


@router.post("/controls/{key}")
async def set_control(key: str, body: Control, request: Request):
    if request.headers.get("x-albatross-action") != "1":
        raise HTTPException(403, "missing the X-Albatross-Action header")
    if key not in await _known_keys():
        raise HTTPException(404, f"no job or agent called {key}")
    pool = await db.pool()
    row = await pool.fetchrow(
        """INSERT INTO agent_controls (key, paused, budget_scale, note, updated_at)
           VALUES ($1, coalesce($2, false), coalesce($3, 1.0), $4, now())
           ON CONFLICT (key) DO UPDATE SET paused = coalesce($2, agent_controls.paused),
             budget_scale = coalesce($3, agent_controls.budget_scale),
             note = coalesce($4, agent_controls.note), updated_at = now()
           RETURNING *""", key, body.paused, body.budget_scale, body.note)
    controls.forget()
    _cache.clear()                                   # the next /output shows the change
    return dict(row)
