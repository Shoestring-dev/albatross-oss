"""The output tracker: what every job and model agent actually produced, what it cost, how it has graded, and the
signs of a loop (the System > Output page, albatross/api/output.py; loop alerts, albatross/alerts/rules.py).

Real output is what an agent leaves behind that something else uses: signals, ideas, X reads, fit reports, scored
candidates, reports, scored headlines. An agent whose calls leave nothing behind is spinning. Loop signs:
  repeating   the same input sent again after it already succeeded (agent_runs.input_hash): work redone for nothing
  spinning    spending with no output to show for it
  failing     most calls fail
  overlap     a job still running when its next run is due (job_runs 'overlap'): it can't keep up
  idle        a job that runs but never writes anything (often waiting on a key, e.g. CryptoPanic)
The map of who reads whom (albatross/agents/graph.py) covers the other kind of loop: an agent reading its own
conclusions back as someone else's evidence.
"""

from datetime import UTC, datetime, timedelta

import asyncpg

from albatross import controls, db
from albatross.agents import graph
from albatross.llm import codex

# Loop thresholds (per window). Small samples never flag: MIN_CALLS calls first.
MIN_CALLS = 5
REPEAT_SHARE = 0.30          # of successful calls resending an input that already succeeded in the window
FAIL_SHARE = 0.30
OVERLAPS = 3                 # skipped runs in the window
IDLE_RUNS = 10               # ok runs that all wrote nothing

# Per agent: what counts as its real output, as (label, SQL -> rows of (agent, n)); $1 = since.
OUTPUT_SQL = {
    "signals": """SELECT a.agent, count(*) FROM signals s JOIN agent_runs a ON a.id = s.agent_run_id
                  WHERE a.started_at > $1 GROUP BY 1""",
    "ideas": """SELECT a.agent, count(*) FROM trade_calls t JOIN agent_runs a ON a.id = t.agent_run_id
                WHERE a.started_at > $1 AND t.market <> 'dex' GROUP BY 1""",
    "coin reads": """SELECT 'social_pulse', count(*) FROM social_snapshots
                     WHERE source = 'grok_x' AND scope <> 'market' AND ts > $1""",
    "X reads": "SELECT 'deep_x_scan', count(*) FROM de_x_reads WHERE read_at > $1",
    "fit reports": "SELECT 'deep_fit', count(*) FROM de_fit_reports WHERE researched_at > $1",
    "Reddit reads": "SELECT 'deep_reddit_scan', count(*) FROM de_reddit_reads WHERE read_at > $1 AND status IN ('read', 'none')",
    "candidates": "SELECT 'jev_scout', count(*) FROM jev_scores WHERE scored_at > $1",
    "reports": "SELECT 'reports', count(*) FROM reports WHERE kind IN ('brief', 'daily') AND created_at > $1",
    "memos": "SELECT 'ceo', count(*) FROM reports WHERE kind = 'ceo' AND created_at > $1",
    "headlines scored": "SELECT 'news_scout', count(*) FROM news_items WHERE classification IS NOT NULL AND fetched_at > $1",
    "takes": "SELECT 'deep_take', count(*) FROM de_dossier WHERE take_at > $1",
    "critiques": """SELECT 'strategist_critic', count(*) FROM trade_calls
                    WHERE critique IS NOT NULL AND logged_at > $1 AND source_kind = 'strategist'""",
    "second opinions": """SELECT 'jev_critic', count(*) FROM trade_calls
                          WHERE evidence ? 'jev_critic' AND logged_at > $1""",
    "calendar events": """SELECT CASE WHEN s->>'collector' = 'events_oracle' THEN 'oracle_calendar' ELSE 'events_news' END,
                                 count(*)
                          FROM events, jsonb_array_elements(sources) s
                          WHERE (s->>'seen_at')::timestamptz > $1 AND s->>'collector' IN ('events_news', 'events_oracle')
                          GROUP BY 1""",
}


async def _outputs(since: datetime) -> dict[str, dict[str, int]]:
    pool = await db.pool()
    out: dict[str, dict[str, int]] = {}
    for label, sql in OUTPUT_SQL.items():
        try:
            for agent, n in await pool.fetch(sql, since):
                if n:
                    out.setdefault(agent, {})[label] = n
        except asyncpg.UndefinedTableError:
            continue
    return out


async def _controls() -> dict[str, dict]:
    pool = await db.pool()
    try:
        return {r["key"]: dict(r) for r in await pool.fetch("SELECT * FROM agent_controls")}
    except asyncpg.UndefinedTableError:
        return {}


# The culling system's rungs that change what an agent does (albatross/agents/cull.py), shown beside the owner's own controls.
CULLED = {
    "probation": ("Benched", "On the culling ladder (Agents page): out of the blended call, at half its budget where it has one."),
    "paused": ("Paused by culling", "On the culling ladder (Agents page): a tenth of its budget, on a 10% sample."),
    "retired": ("Retired", "Retired on the Agents page: its runs are skipped until it is reinstated there."),
}
# The quant model is code inside the features jobs, not an agent with calls of its own: its rung shows on them.
QUANT_JOBS = ("features_ltf", "features_htf", "features_daily")


def culled(rung: str | None, who: str | None = None) -> list[dict]:
    if rung not in CULLED:
        return []
    label, detail = CULLED[rung]
    return [{"kind": "paused", "label": f"{who}: {label.lower()}" if who else label,
             "detail": f"{who}, which this job computes. {detail} It still runs and is graded." if who else detail}]


def agent_flags(a: dict) -> list[dict]:
    """Loop signs for one agent's window (pure; tests/test_output.py)."""
    out = culled(a.get("rung"))
    calls, ok = a.get("calls", 0), a.get("ok", 0)
    if a.get("paused"):
        out.append({"kind": "paused", "label": "Paused", "detail": "Paused on this page: its model calls are skipped."})
    served = calls - a.get("lane_failed", 0)          # a failed try on the ChatGPT plan is retried on OpenRouter
    if served >= MIN_CALLS and a.get("failed", 0) / served >= FAIL_SHARE:
        out.append({"kind": "failing", "label": "Failing", "detail": f"{a['failed']} of {served} calls failed."})
    if ok >= MIN_CALLS and a.get("repeats", 0) / ok >= REPEAT_SHARE:
        out.append({"kind": "repeating", "label": "Repeating inputs",
                    "detail": f"{a['repeats']} of {ok} successful calls resent an input that had already succeeded."})
    if ok >= MIN_CALLS and a.get("measured") and not a.get("output_total") and (a.get("cost_usd") or a.get("pool_usd")):
        out.append({"kind": "spinning", "label": "Spending without output",
                    "detail": f"{ok} successful calls and nothing stored from them."})
    return out


def job_flags(j: dict) -> list[dict]:
    out = culled(j.get("rung")) or (culled(j.get("quant_rung"), "Quant model") if j.get("job") in QUANT_JOBS else [])
    if j.get("paused"):
        out.append({"kind": "paused", "label": "Paused", "detail": "Paused on this page: its runs are skipped."})
    runs = j.get("runs", 0)
    if runs >= MIN_CALLS and j.get("failed", 0) / runs >= FAIL_SHARE:
        out.append({"kind": "failing", "label": "Failing", "detail": f"{j['failed']} of {runs} runs failed."})
    if j.get("overlap", 0) >= OVERLAPS:
        out.append({"kind": "overlap", "label": "Can't keep up",
                    "detail": f"{j['overlap']} runs skipped because the last one was still going."})
    if j.get("ok", 0) >= IDLE_RUNS and not j.get("rows"):
        out.append({"kind": "idle", "label": "Idle", "detail": f"{j['ok']} runs, nothing written."})
    return out


async def agent_stats(hours: float) -> list[dict]:
    """Every model agent's calls, cost, repeats and real output over the last `hours`."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT agent, count(*) AS calls, count(*) FILTER (WHERE ok) AS ok,
                  count(*) FILTER (WHERE NOT ok AND model NOT LIKE 'codex:%' AND model NOT LIKE 'claude:%') AS failed,
                  count(*) FILTER (WHERE NOT ok AND (model LIKE 'codex:%' OR model LIKE 'claude:%')) AS lane_failed,
                  coalesce(sum(cost_usd), 0) AS cost_usd,
                  coalesce(sum((output->>'subscription_equiv_cost_usd')::float), 0)
                    + coalesce(sum(credits), 0) * $2 AS pool_usd,
                  coalesce(sum(credits), 0) AS credits,
                  max(started_at) AS last_call, avg(latency_ms)::int AS avg_ms
           FROM agent_runs WHERE started_at > $1 GROUP BY agent""", since, codex.CREDIT_USD)
    repeats = {r["agent"]: r["n"] for r in await pool.fetch(
        """SELECT agent, sum(n - 1) AS n FROM (
             SELECT agent, input_hash, count(*) AS n FROM agent_runs
             WHERE started_at > $1 AND ok AND input_hash IS NOT NULL GROUP BY 1, 2 HAVING count(*) > 1) x
           GROUP BY agent""", since)}
    hourly: dict[str, list] = {}
    bucket = "1 hour" if hours <= 48 else "6 hours"
    for r in await pool.fetch(
            f"""SELECT agent, time_bucket(interval '{bucket}', started_at) AS h, count(*) AS calls,
                       count(*) FILTER (WHERE ok) AS ok
                FROM agent_runs WHERE started_at > $1 GROUP BY 1, 2 ORDER BY 2""", since):
        hourly.setdefault(r["agent"], []).append({"t": r["h"], "calls": r["calls"], "ok": r["ok"]})
    outputs, ctl, rungs = await _outputs(since), await _controls(), await controls.rungs()
    measured = {agent for sql in OUTPUT_SQL.values() for agent in _agents_in(sql)}
    out = []
    for r in rows:
        a = dict(r)
        name = a["agent"]
        o = outputs.get(name, {})
        a.update(label=graph.LABEL.get(name, name), repeats=int(repeats.get(name) or 0), outputs=o,
                 output_total=sum(o.values()), measured=name in measured or bool(o), hourly=hourly.get(name, []),
                 paused=bool(ctl.get(name, {}).get("paused")), budget_scale=float(ctl.get(name, {}).get("budget_scale", 1.0)),
                 rung=rungs.get(name))
        spend = a["cost_usd"] or a["pool_usd"]
        a["cost_per_output"] = round(spend / a["output_total"], 6) if a["output_total"] and spend else None
        a["flags"] = agent_flags(a)
        out.append(a)
    return sorted(out, key=lambda a: (-len([f for f in a["flags"] if f["kind"] != "paused"]), -(a["cost_usd"] + a["pool_usd"]), a["agent"]))


def _agents_in(sql: str) -> list[str]:
    """Agents an output query names (the literal ones) plus every agent that writes signals or ideas."""
    if "JOIN agent_runs" in sql:
        return ["ltf_analyst", "htf_analyst", "flow_analyst", "macro_analyst", "strategist", "oracle", "jev_analyst",
                "social_pulse"]
    if "calendar" in sql or "collector" in sql:
        return ["events_news", "oracle_calendar"]
    return [sql.split("'")[1]] if "'" in sql else []


async def job_stats(hours: float) -> list[dict]:
    """Every tracked job's runs, rows, overlaps and failures over the last `hours` (job_runs)."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    pool = await db.pool()
    try:
        rows = await pool.fetch(
            """SELECT job, count(*) FILTER (WHERE status <> 'overlap') AS runs,
                      count(*) FILTER (WHERE status = 'ok') AS ok, count(*) FILTER (WHERE status = 'failed') AS failed,
                      count(*) FILTER (WHERE status IN ('paused', 'skipped')) AS paused_runs,
                      count(*) FILTER (WHERE status = 'overlap') AS overlap,
                      coalesce(sum(rows), 0) AS rows, count(*) FILTER (WHERE status = 'ok' AND coalesce(rows, 0) = 0) AS idle,
                      avg(seconds) FILTER (WHERE status IN ('ok', 'failed')) AS avg_s,
                      max(seconds) FILTER (WHERE status IN ('ok', 'failed')) AS max_s, max(started_at) AS last_run
               FROM job_runs WHERE started_at > $1 GROUP BY job""", since)
    except asyncpg.UndefinedTableError:
        return []
    bucket = "1 hour" if hours <= 48 else "6 hours"
    hourly: dict[str, list] = {}
    for r in await pool.fetch(
            f"""SELECT job, time_bucket(interval '{bucket}', started_at) AS h, coalesce(sum(rows), 0) AS rows,
                       count(*) FILTER (WHERE status <> 'overlap') AS runs
                FROM job_runs WHERE started_at > $1 GROUP BY 1, 2 ORDER BY 2""", since):
        hourly.setdefault(r["job"], []).append({"t": r["h"], "rows": r["rows"], "runs": r["runs"]})
    ctl, rungs = await _controls(), await controls.rungs()
    out = []
    for r in rows:
        j = dict(r)
        j.update(hourly=hourly.get(j["job"], []), paused=bool(ctl.get(j["job"], {}).get("paused")), rung=rungs.get(j["job"]),
                 quant_rung=rungs.get("quant") if j["job"] in QUANT_JOBS else None)
        j["avg_s"] = round(j["avg_s"], 2) if j["avg_s"] is not None else None
        j["max_s"] = round(j["max_s"], 2) if j["max_s"] is not None else None
        j["flags"] = job_flags(j)
        out.append(j)
    return sorted(out, key=lambda j: (-len([f for f in j["flags"] if f["kind"] not in ("paused", "idle")]), j["job"]))


async def quality() -> dict[str, dict]:
    """How each producer has graded against what actually happened, where outcomes exist."""
    from albatross.strategy import ensemble
    pool = await db.pool()
    out: dict[str, dict] = {}
    by: dict[str, list] = {}
    for (src, _tf), w in (await ensemble.source_weights()).items():
        if w.get("n"):
            by.setdefault(src, []).append(w)
    for src, ws in by.items():
        n = sum(w["n"] for w in ws)
        edge = sum(w["edge"] * w["n"] for w in ws) / n
        hit = sum(w["hit"] * w["n"] for w in ws) / n
        out[src] = {"graded": n, "hit_rate": round(hit, 3), "edge": round(edge, 3),
                    "summary": f"{hit:.0%} right on direction, {edge:+.1%} against always-long/always-short "
                               f"({n:,} graded, 30 d)"}
    r = await pool.fetchrow(
        """SELECT count(*) AS n, avg((o.exit_reason = 'target')::int) AS won, avg(o.r_multiple) AS r
           FROM trade_call_outcomes o JOIN trade_calls c ON c.id = o.call_id
           WHERE c.source_kind = 'strategist' AND o.policy = 'stop_target' AND o.status = 'graded'
             AND o.graded_at > now() - interval '30 days'""")
    if r and r["n"]:
        out["strategist_ideas"] = {"graded": r["n"], "won": round(float(r["won"] or 0), 3), "avg_r": round(float(r["r"] or 0), 2),
                                   "summary": f"ideas: {float(r['won'] or 0):.0%} hit target, average {float(r['r'] or 0):+.2f}R "
                                              f"({r['n']:,} graded, 30 d)"}
    try:
        r = await pool.fetchrow(
            """SELECT count(*) AS n, avg(p_win) AS p, avg((exit_reason = 'target')::int) AS won, avg(r_multiple) AS r,
                      count(p_cal) AS n_cal, avg(p_cal) AS p_cal,
                      avg((exit_reason = 'target')::int) FILTER (WHERE p_cal IS NOT NULL) AS won_cal
               FROM jev_scores WHERE outcome = 'graded' AND graded_at > now() - interval '7 days'""")
        if r and r["n"]:
            said, won = float(r["p"]), float(r["won"] or 0)
            out["jev_scout"] = {"graded": r["n"], "said": round(said, 3), "won": round(won, 3),
                                "avg_r": round(float(r["r"] or 0), 2),
                                "summary": f"said {said:.0%} on average, {won:.0%} reached the target ({r['n']:,} graded, 7 d)"}
            if r["n_cal"]:                          # candidates scored with the corrected odds (jev_calibration)
                cal, won_cal = float(r["p_cal"]), float(r["won_cal"] or 0)
                out["jev_scout"] |= {"cal": round(cal, 3), "won_cal": round(won_cal, 3), "graded_cal": r["n_cal"]}
                out["jev_scout"]["summary"] += f"; corrected, it says {cal:.0%} and {won_cal:.0%} won ({r['n_cal']:,})"
    except asyncpg.UndefinedTableError:
        pass
    return out
