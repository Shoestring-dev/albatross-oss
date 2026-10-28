"""The culling system: agents that don't hold their weight lose influence and budget step by step, and earn them back
with evidence (docs/PLAN-LEARNING-LOOP.md section 7).

Every Thursday at 01:00 UTC (job agent_review) each agent is judged on its last 30 days:
  direction   the analysts, Social Pulse, the Jev analyst, the quant model, the Oracle: skill over the fair baseline
              (random calls with its own long/short mix, grading/fair.py), pooled over its timeframes
  trades      the Strategist's ideas and the Jev scout's top-tenth candidates: mean R after costs per day, with the
              interval across days (same-day trades move together, so a day is the sample)
  critics     the Strategist's critic and Jev's: what their vetoes avoided, once MIN_CRITIC ideas are graded
  unreviewed  feeders, writers and the Deep End: nothing to judge them by yet; their cost is still shown
Verdicts: earns (the 95% interval is above 0), no skill (it includes 0), harmful (it is below 0), too early (fewer
than 30 effective samples, 7 days of trades, or 60 graded ideas).

The ladder: one weak counted review puts an agent on watch, two in a row on probation (out of the ensemble, half
budget), four on paused (a tenth of the budget and a 10% sample where the agent supports one, so it is still graded
and can earn its way back). Only the owner retires an agent. Any review that earns returns it to active. A review counts
toward the ladder only if it comes at least 6 days after the last counted one. Effects: controls.py and the ensemble.
One Telegram message for each review that moves anyone.
"""

import logging
import math
from collections import Counter
from datetime import UTC, datetime, timedelta

import orjson

from albatross import controls, db, tracker
from albatross.agents import graph
from albatross.config import settings
from albatross.grading import fair

log = logging.getLogger(__name__)

RUNGS = ["active", "watch", "probation", "paused", "retired"]
DIRECTION = ["quant", "ltf_analyst", "htf_analyst", "flow_analyst", "macro_analyst", "social_pulse", "jev_analyst",
             "oracle", "ensemble"]
EXEMPT = {"ensemble", "factor_rank"}       # the aggregator, and the base edge (judged by its own record)
MIN = {"direction": 30, "trades": 7, "critic": 60}
COUNT_GAP = timedelta(days=6)
WATCH, PROBATION, PAUSED = 1, 2, 4         # counted weak reviews in a row
REVIEW_DAY, REVIEW_HOUR = 3, 1             # Thursday, 01:00 UTC


def verdict(value: float | None, band: float | None, n: float | None, minimum: float) -> str:
    if value is None or band is None or n is None or n < minimum:
        return "too_early"
    if value - band > 0:
        return "earns"
    if value + band < 0:
        return "harmful"
    return "no_skill"


def step(rung: str, streak: int, v: str, counted: bool) -> tuple[str, int]:
    """One review -> (rung, streak). Earning returns an agent to active at once; weak reviews move it down only when
    they count; it never moves down past what its streak says, and a retired agent stays retired (the owner reinstates)."""
    if rung == "retired":
        return rung, streak
    if v == "earns":
        return "active", 0
    if not counted or v not in ("no_skill", "harmful"):
        return rung, streak
    streak += 1
    target = "paused" if streak >= PAUSED else "probation" if streak >= PROBATION else "watch"
    return (target if RUNGS.index(target) > RUNGS.index(rung) else rung), streak


def next_review(now: datetime) -> datetime:
    days = (REVIEW_DAY - now.weekday()) % 7
    at = (now + timedelta(days=days)).replace(hour=REVIEW_HOUR, minute=0, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=7)


def _mean_band(xs: list[float]) -> tuple[float | None, float | None]:
    n = len(xs)
    if n < 2:
        return (xs[0] if xs else None), None
    m = sum(xs) / n
    return m, 1.96 * math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1) / n)


def _diff(a: list[float], b: list[float]) -> tuple[float | None, float | None]:
    """Mean of a minus mean of b, and the 95% half-width."""
    if len(a) < 2 or len(b) < 2:
        return None, None
    (ma, _), (mb, _) = _mean_band(a), _mean_band(b)
    va = sum((x - ma) ** 2 for x in a) / (len(a) - 1)
    vb = sum((x - mb) ** 2 for x in b) / (len(b) - 1)
    return ma - mb, 1.96 * math.sqrt(va / len(a) + vb / len(b))


def critic_evidence(rows: list[dict]) -> dict[str, dict]:
    """rows: the Strategist's graded ideas as {status, r, jev_veto}. A critic is worth having if the ideas it would
    stop did worse than the ones it lets through."""
    kept = [r["r"] for r in rows if r["status"] != "vetoed"]
    vetoed = [r["r"] for r in rows if r["status"] == "vetoed"]
    value, band = _diff(kept, vetoed)
    out = {"strategist_critic": {"metric": "critic", "value": value, "band": band, "n": len(rows),
                                 "what": f"ideas it kept minus the {len(vetoed)} it vetoed"}}
    pv = sorted((r for r in rows if r["jev_veto"] is not None), key=lambda r: r["jev_veto"])
    k = len(pv) // 3
    value, band = _diff([r["r"] for r in pv[:k]], [r["r"] for r in pv[-k:]]) if k else (None, None)
    out["jev_critic"] = {"metric": "critic", "value": value, "band": band, "n": len(pv),
                         "what": "the third it least wanted vetoed minus the third it most did"}
    return out


async def evidence() -> dict[str, dict]:
    """agent -> {metric, value, band, n, what, detail}: the numbers a review judges, also shown on the Agents page."""
    pool = await db.pool()
    out: dict[str, dict] = {}
    by = await fair.by_source()
    for agent in DIRECTION:
        p = fair.pooled(by.get(agent, []))
        out[agent] = {"metric": "direction", "value": p and p["skill"], "band": p and p["band"], "n": p and p["n_eff"],
                      "what": "hit rate over random calls with its own long/short mix", "detail": p}
    ideas = [float(r["r"]) for r in await pool.fetch(
        """SELECT date_trunc('day', c.logged_at) AS d, avg(o.r_multiple) AS r FROM trade_calls c
           JOIN trade_call_outcomes o ON o.call_id = c.id AND o.policy = 'stop_target' AND o.status = 'graded'
           WHERE c.source_kind = 'strategist' AND c.status = 'proposed' AND o.r_multiple IS NOT NULL
             AND c.logged_at > now() - make_interval(days => $1) GROUP BY 1 ORDER BY 1""", fair.WINDOW_DAYS)]
    scout = [float(r["r"]) for r in await pool.fetch(
        """WITH g AS (SELECT date_trunc('day', scored_at) AS d, r_multiple,
                             ntile(10) OVER (PARTITION BY date_trunc('day', scored_at), tf ORDER BY p_win) AS dec
                      FROM jev_scores WHERE outcome = 'graded' AND r_multiple IS NOT NULL
                        AND scored_at > now() - make_interval(days => $1))
           SELECT d, avg(r_multiple) AS r FROM g WHERE dec = 10 GROUP BY 1 ORDER BY 1""", fair.WINDOW_DAYS)]
    for agent, days, what in (("strategist", ideas, "mean R a day of its ideas, after costs"),
                              ("jev_scout", scout, "mean R a day of its top-tenth candidates, after costs")):
        value, band = _mean_band(days)
        out[agent] = {"metric": "trades", "value": value, "band": band, "n": len(days), "what": what, "detail": None}
    out["strategist"]["detail"] = {"direction": fair.pooled(by.get("strategist", []))}   # its calls, for the record
    rows = [dict(r) for r in await pool.fetch(
        """SELECT c.status, o.r_multiple::float AS r, (c.evidence->'jev_critic'->>'veto')::float AS jev_veto
           FROM trade_calls c JOIN trade_call_outcomes o ON o.call_id = c.id
           WHERE c.source_kind = 'strategist' AND o.policy = 'stop_target' AND o.status = 'graded'
             AND o.r_multiple IS NOT NULL AND c.logged_at > now() - make_interval(days => $1)""", fair.WINDOW_DAYS)]
    out |= critic_evidence(rows)
    return out


def judge(ev: dict | None) -> str:
    if not ev or ev.get("metric") == "unreviewed":
        return "unreviewed"
    return verdict(ev["value"], ev["band"], ev["n"], MIN[ev["metric"]])


def words(ev: dict | None, v: str) -> str:
    """One line for a review: the verdict and the number behind it."""
    if v == "unreviewed":
        return "nothing to grade it by yet"
    unit = "points" if ev["metric"] == "direction" else "R"
    scale = 100 if ev["metric"] == "direction" else 1
    # what the verdict is against, named (the redesign's one vocabulary, 2026-10-06)
    vs = {"direction": "a coin flip", "critic": "its vetoes"}.get(ev["metric"], "breaking even")
    if not ev.get("n"):
        return "too early (nothing graded in the last 30 days)"
    count = (f"{ev['n']:,.0f} graded windows" if ev["metric"] == "direction" else
             f"{ev['n']} days" if ev["metric"] == "trades" else f"{ev['n']} graded ideas")
    if v == "too_early" or ev["value"] is None:
        return f"too early ({count})"
    num = f"{ev['value'] * scale:+.1f} {unit} ± {ev['band'] * scale:.1f}" if ev["band"] is not None else ""
    label = {"earns": f"better than {vs}", "no_skill": f"no better than {vs}", "harmful": f"worse than {vs}"}[v]
    return f"{label}: {num}, {ev['what']} ({count})"


async def review(now: datetime | None = None) -> dict:
    """The weekly review: judge every agent, move it on the ladder, record why, and say so once on Telegram."""
    now = now or datetime.now(UTC)
    pool = await db.pool()
    ev_all = await evidence()
    costs = {a["agent"]: a for a in await tracker.agent_stats(168)}
    status = {r["agent"]: dict(r) for r in await pool.fetch("SELECT * FROM agent_status")}
    reviews, moves = [], []
    for agent in sorted(a for a in graph.KIND if a not in EXEMPT):
        ev = ev_all.get(agent) or {"metric": "unreviewed", "value": None, "band": None, "n": None}
        v = judge(ev)
        st = status.get(agent) or {"rung": "active", "streak": 0, "counted_at": None}
        counted = v in ("earns", "no_skill", "harmful") and (st["counted_at"] is None or now - st["counted_at"] >= COUNT_GAP)
        rung, streak = step(st["rung"], st["streak"], v, counted)
        c = costs.get(agent, {})
        reason = words(ev, v)
        reviews.append({"agent": agent, "metric": ev["metric"], "verdict": v, "value": ev.get("value"),
                        "band": ev.get("band"), "n": ev.get("n"), "cost_usd": c.get("cost_usd"),
                        "pool_usd": c.get("pool_usd"), "counted": counted, "rung_before": st["rung"],
                        "rung_after": rung, "streak": streak, "reason": reason, "changed": rung != st["rung"]})
        if rung != st["rung"]:
            moves.append(reviews[-1])
    async with pool.acquire() as conn, conn.transaction():
        await conn.executemany(
            """INSERT INTO agent_reviews (reviewed_at, agent, metric, verdict, value, band, n, cost_usd, pool_usd,
                                          counted, rung_before, rung_after, detail)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)""",
            [(now, r["agent"], r["metric"], r["verdict"], r["value"], r["band"], r["n"], r["cost_usd"], r["pool_usd"],
              r["counted"], r["rung_before"], r["rung_after"],
              orjson.dumps({"reason": r["reason"], "streak": r["streak"]}).decode()) for r in reviews])
        await conn.executemany(
            """INSERT INTO agent_status (agent, rung, streak, since, reason, counted_at, set_by, updated_at)
               VALUES ($1::text, $2::text, $3::int, $4::timestamptz, $5::text,
                       CASE WHEN $6::boolean THEN $4::timestamptz END, 'review', $4::timestamptz)
               ON CONFLICT (agent) DO UPDATE SET
                 since = CASE WHEN agent_status.rung = EXCLUDED.rung THEN agent_status.since ELSE EXCLUDED.since END,
                 rung = EXCLUDED.rung, streak = EXCLUDED.streak, reason = EXCLUDED.reason,
                 counted_at = CASE WHEN $6::boolean THEN $4::timestamptz ELSE agent_status.counted_at END,
                 set_by = CASE WHEN agent_status.rung = EXCLUDED.rung THEN agent_status.set_by ELSE 'review' END,
                 updated_at = $4::timestamptz""",
            [(r["agent"], r["rung_after"], r["streak"], now, r["reason"], r["counted"]) for r in reviews])
    controls.forget()
    if moves:
        await notify(moves, now)
    log.info("agent_review: %d agents, %d moved: %s", len(reviews), len(moves),
             ", ".join(f"{m['agent']} {m['rung_before']}->{m['rung_after']}" for m in moves) or "none")
    return {"rows": len(reviews), "moved": [f"{m['agent']}: {m['rung_before']} -> {m['rung_after']}" for m in moves],
            "verdicts": dict(Counter(r["verdict"] for r in reviews))}


def message(moves: list[dict], now: datetime) -> str:
    base = settings.alert_dashboard_url.split("#")[0].rstrip("/")
    lines = [f"Albatross agent review · {now:%a %b} {now.day}"]
    for m in moves:
        up = RUNGS.index(m["rung_after"]) < RUNGS.index(m["rung_before"])
        lines.append(f"{'▲' if up else '▼'} {graph.LABEL.get(m['agent'], m['agent'])}: {m['rung_before']} → "
                     f"{m['rung_after']}. {m['reason'][:1].upper()}{m['reason'][1:]}.")
    nxt = next_review(now)
    lines.append(f"Next review {nxt:%a %b} {nxt.day} · {base}/#/agents")
    return "\n".join(lines)


async def notify(moves: list[dict], now: datetime) -> None:
    from albatross.alerts import notify as alerts
    text = message(moves, now)
    for ch in await alerts.channels():
        try:
            await ch.send(text)
        except Exception as e:  # noqa: BLE001 - the review stands; the message is a courtesy
            log.warning("agent_review: telegram send failed: %s", type(e).__name__)


async def set_rung(agent: str, rung: str, note: str | None = None) -> dict:
    """The owner's decision from the Agents page: retire an agent, or reinstate one (back to active, streak cleared)."""
    if rung not in RUNGS:
        raise ValueError(f"unknown rung {rung}")
    now = datetime.now(UTC)
    pool = await db.pool()
    before = await pool.fetchval("SELECT rung FROM agent_status WHERE agent = $1", agent) or "active"
    reason = note or ("retired by the owner" if rung == "retired" else "reinstated by the owner" if rung == "active" else f"set to {rung} by the owner")
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO agent_status (agent, rung, streak, since, reason, set_by, updated_at)
               VALUES ($1::text, $2::text, 0, $3::timestamptz, $4::text, 'owner', $3::timestamptz)
               ON CONFLICT (agent) DO UPDATE SET rung = $2::text, since = $3::timestamptz, reason = $4::text,
                 set_by = 'owner', updated_at = $3::timestamptz,
                 streak = CASE WHEN $2::text = 'active' THEN 0 ELSE agent_status.streak END""", agent, rung, now, reason)
        await conn.execute(
            """INSERT INTO agent_reviews (reviewed_at, agent, metric, verdict, counted, rung_before, rung_after, detail)
               VALUES ($1, $2, 'manual', 'manual', false, $3, $4, $5)""",
            now, agent, before, rung, orjson.dumps({"reason": reason}).decode())
    controls.forget()
    return {"agent": agent, "rung": rung, "before": before, "reason": reason}
