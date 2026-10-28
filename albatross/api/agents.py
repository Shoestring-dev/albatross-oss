"""Agents page (static/agents.js): who reads whom, drawn from albatross/agents/graph.py, with each agent's model,
last-24-hour output and cost (tracker, via the Output report), how it has graded, and where the culling system has it
(albatross/agents/cull.py): its rung, the live evidence a review judges, and its review history.

Grades come from the culling system's evidence, measured against fair baselines (random calls with the agent's own
long/short mix; trades in R after costs). Agents it has nothing to judge by fall back to the Track record's words.
POST /api/agents/{agent}/rung is the owner's retire / reinstate, behind the same header as the Output page's controls."""

import asyncio
import time
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from albatross import db
from albatross.agents import cull, graph
from albatross.strategy import ensemble
from albatross.api import intel as intel_api
from albatross.api import output as output_api

router = APIRouter(prefix="/api")

CACHE_S = 60
_cache: dict[str, tuple[float, dict]] = {}
_lock = asyncio.Lock()
STEPS = ["Reads raw data", "One step removed", "Two steps removed", "Three steps removed", "Four steps removed"]
LANES = [{"id": "majors", "name": "Majors", "sub": "bias calls and trade ideas on the liquid coins"},
         {"id": "deep", "name": "The Deep End", "sub": "memecoin dossiers for the trenches"}]
EDGE_KIND = {"proven": "edge", "worse": "behind", "none": "none", "early": "early"}
CRITIC_MIN = 60             # graded ideas before a critic is judged


def gpt_name(slug: str) -> str:
    """gpt-6-luna -> GPT-6 Luna, gpt-6.1-sol -> GPT-6.1 Sol."""
    parts = slug.split("-")
    return " ".join([f"GPT-{parts[1]}" if len(parts) > 1 else "GPT", *(p.title() for p in parts[2:])])


def model_of(slug: str | None, kind: str) -> dict:
    """agent_runs.model -> {"group": api | jev | cli | code | idle, "name": what to show}."""
    if kind == graph.RULES:
        return {"group": "code", "name": "Code"}
    if not slug:
        return {"group": "idle", "name": "No calls this week"}
    s = slug.lower()
    if s.startswith("typesafe/"):
        return {"group": "jev", "name": "Jev", "slug": slug}
    if s.startswith("codex:"):                          # The ChatGPT plan (llm/codex.py)
        return {"group": "cli", "name": f"{gpt_name(s.partition(':')[2])} (ChatGPT plan)", "slug": slug}
    if s.split("/")[-1].startswith("gpt-"):
        return {"group": "api", "name": gpt_name(s.split("/")[-1]), "slug": slug}
    if s.startswith("xai:"):                            # X search on the xAI API (llm/xai.py): grok-4.3
        words = [w.title() if w.isalpha() else w for w in s.partition(":")[2].split("-")]
        return {"group": "api", "name": f"{' '.join(words)} (xAI API)", "slug": slug}
    if ":" in s and "/" not in s:                       # a subscription CLI: grok:default, claude:opus
        cli, _, model = s.partition(":")
        name = cli.title() + ("" if model in ("", "default") else f" {model.title()}")
        return {"group": "cli", "name": f"{name} (CLI)", "slug": slug}
    known = {"deepseek": "DeepSeek", "gemini": "Gemini", "gpt": "GPT", "claude": "Claude", "grok": "Grok", "qwen": "Qwen"}
    words = [known.get(w, w.upper() if w[:1] == "v" and w[1:2].isdigit() else w.title())
             for w in s.split("/")[-1].split("-")]
    return {"group": "api", "name": " ".join(words), "slug": slug}


def _pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _direction(edge: dict | None) -> tuple[str, str] | None:
    """The Track record's verdict on a source's direction calls -> (kind, words), headline timeframe first."""
    if not edge:
        return None
    t = next((x for x in edge["tfs"] if x["tf"] == edge["headline_tf"]), None)
    kind = EDGE_KIND.get(edge["status"], "early")
    if t is None or kind == "early":
        return "early", f"Too early to tell ({t['n']:,} graded on {t['tf']})" if t else "Too early to tell"
    lead = "ahead of" if t["edge_pts"] > 0 else "behind"
    return kind, f"{_pct(t['hit_rate'])} right on {t['tf']}, {abs(t['edge_pts']):.1f} points {lead} a one-way caller ({t['n']:,} graded)"


def grade(node: str, edge: dict | None, quality: dict, record: dict | None, critics: dict) -> dict:
    """{"kind": edge | none | behind | early | input, "text": one line} for a node, from the graded records."""
    if node == "factor_rank" and record:
        bt, live = record.get("backtest") or {}, record.get("live") or {}
        if bt.get("weeks"):
            kind = "edge" if (bt.get("t") or 0) >= 2 and (bt.get("mean_spread_pct") or 0) > 0 else "none"
            text = (f"Top fifth beat the bottom by {bt['mean_spread_pct']:.2f}% a week, {bt['won_pct']}% of "
                    f"{bt['weeks']} weeks (backtest)")
            text += f"; {live['weeks']} live weeks so far" if live.get("weeks") else "; live tracking has just started"
            return {"kind": kind, "text": text}
    if node == "jev_scout" and (q := quality.get("jev_scout")):
        return {"kind": "behind" if q["avg_r"] < 0 else "none",
                "text": f"Says {_pct(q['said'])}, {_pct(q['won'])} hit; {q['avg_r']:+.2f}R a trade after costs "
                        f"({q['graded']:,} graded, 7 d)"}
    if node in critics:
        return critics[node]
    direction = _direction(edge)
    if node == "strategist" and (q := quality.get("strategist_ideas")):
        ideas = f"ideas {q['avg_r']:+.2f}R average, {_pct(q['won'])} hit target ({q['graded']:,} graded, 30 d)"
        if direction:
            return {"kind": direction[0], "text": f"Calls {direction[1][0].lower()}{direction[1][1:]}; {ideas}"}
        return {"kind": "none" if q["avg_r"] <= 0 else "early", "text": ideas[0].upper() + ideas[1:]}
    if direction:
        return {"kind": direction[0], "text": direction[1]}
    if graph.LANE.get(node) == "deep":
        return {"kind": "input", "text": "Not graded yet"}
    if node == "reports":
        return {"kind": "input", "text": "Writes up the others; not graded"}
    return {"kind": "input", "text": "Feeds others; not graded on its own"}


def critic_grades(rows: list[dict]) -> dict:
    """rows: graded Strategist ideas as {status, r, jev_veto}. -> grades for the two critics."""
    out = {}
    vetoed = [r["r"] for r in rows if r["status"] == "vetoed"]
    kept = [r["r"] for r in rows if r["status"] != "vetoed"]
    if len(vetoed) < CRITIC_MIN // 2:
        out["strategist_critic"] = {"kind": "early", "text": f"Too few vetoed ideas graded to judge ({len(vetoed)})"}
    else:
        diff = sum(kept) / len(kept) - sum(vetoed) / len(vetoed)
        out["strategist_critic"] = {"kind": "edge" if diff >= 0.2 else "none",
                                    "text": f"Ideas it vetoed did {abs(diff):.2f}R {'worse' if diff > 0 else 'better'} "
                                            f"than the ones it kept ({len(vetoed)} vetoed)"}
    pv = sorted((r for r in rows if r["jev_veto"] is not None), key=lambda r: r["jev_veto"])
    k = len(pv) // 3
    if k:
        low = sum(r["r"] for r in pv[:k]) / k
        high = sum(r["r"] for r in pv[-k:]) / k
        text = (f"Ideas it most wanted vetoed averaged {high:+.2f}R, the ones it liked {low:+.2f}R "
                f"({len(pv)} graded)")
        kind = "early" if len(pv) < CRITIC_MIN else ("edge" if low - high >= 0.2 else "none")
        out["jev_critic"] = {"kind": kind, "text": text}
    else:
        out["jev_critic"] = {"kind": "early", "text": "No graded ideas yet"}
    return out


FAIR_KIND = {"earns": "edge", "no_skill": "none", "harmful": "behind", "too_early": "early"}


def fair_grade(ev: dict | None) -> dict | None:
    """The culling system's evidence as a card's grade, or None when there is nothing to judge by."""
    v = cull.judge(ev)
    if v == "unreviewed":
        return None
    return {"kind": FAIR_KIND[v], "text": cull.words(ev, v)[:1].upper() + cull.words(ev, v)[1:]}


def assemble(out: dict, intel: dict | None, models: dict[str, str], critics: dict, now: datetime,
             evidence: dict | None = None, status: dict | None = None, history: dict | None = None) -> dict:
    """The page's payload from the Output report (24 h), the Track record's verdicts, this week's models, and the
    culling system's evidence, status and history."""
    evidence, status, history = evidence or {}, status or {}, history or {}
    dep = graph.depth()
    nodes_ids = sorted(dep, key=lambda n: (dep[n], graph.ORDER.index(n) if n in graph.ORDER else len(graph.ORDER), n))
    agents = {a["agent"]: a for a in out.get("agents", [])}
    edges_by_src = {e["source"]: e for e in (intel or {}).get("edge", [])}
    quality = {}
    for a in out.get("agents", []):
        if a.get("quality"):
            quality["strategist_ideas" if a["agent"] == "strategist" else a["agent"]] = a["quality"]
    readers: dict[str, list[str]] = {n: [] for n in nodes_ids}
    for src, dst in graph.edges():
        readers[src].append(dst)
    deepest = max(dep.values(), default=0)
    nodes = []
    for n in nodes_ids:
        kind = graph.KIND.get(n, graph.MODEL)
        a = agents.get(n) or {}
        nodes.append({
            "id": n, "label": graph.LABEL.get(n, n), "role": graph.ROLE.get(n, ""), "lane": graph.LANE.get(n, "majors"),
            "col": dep[n] + 1, "kind": kind, "model": model_of(models.get(n), kind),
            "calls": a.get("calls"), "outputs": a.get("outputs") or {}, "output_total": a.get("output_total"),
            "cost_usd": a.get("cost_usd"), "pool_usd": a.get("pool_usd"), "failed": a.get("failed"),
            "flags": [f for f in a.get("flags", []) if f["kind"] != "idle"],
            "paused": a.get("paused", False), "budget_scale": a.get("budget_scale", 1.0),
            "grade": fair_grade(evidence.get(n)) or grade(n, edges_by_src.get(n), quality, (intel or {}).get("record"), critics),
            **_ladder(n, status.get(n), evidence.get(n), history.get(n, [])),
            "reads": graph.READS.get(n, []), "attention": graph.ATTENTION.get(n, []), "read_by": sorted(readers[n]),
            "lands": graph.LANDS.get(n, []), "self": graph.SELF.get(n)})
    for d, (name, where, view) in graph.DEST.items():
        lane = "deep" if d == "feed" else "majors"
        nodes.append({"id": d, "label": name, "role": where, "lane": lane, "col": deepest + 2, "kind": "dest", "view": view,
                      "reads": sorted(n for n, lands in graph.LANDS.items() if d in lands)})
    edges = [{"from": s, "to": t, "kind": "reads"} for s, t in graph.edges()]
    edges += [{"from": s, "to": t, "kind": "attention"} for t, srcs in graph.ATTENTION.items() for s in srcs
              if (s, t) not in graph.edges()]
    edges += [{"from": n, "to": d, "kind": "lands"} for n, lands in graph.LANDS.items() for d in lands]
    s = out.get("summary", {})
    g = out.get("graph", {})
    rungs = [n.get("rung") for n in nodes if n.get("rung")]
    return {"as_of": now, "hours": out.get("hours", 24),
            "summary": {"agents": len(nodes_ids), "calls": s.get("calls"), "outputs": s.get("outputs"),
                        "spend_usd": s.get("spend_usd"), "pool_usd": s.get("pool_usd"),
                        "loops": sum(1 for loop in g.get("loops", []) if not loop.get("known")),
                        "attention_loops": len(g.get("attention_loops", [])),
                        "rungs": {r: rungs.count(r) for r in cull.RUNGS if r != "active" and rungs.count(r)},
                        "next_review": cull.next_review(now),
                        # timeframes every ensemble source has been culled from: nothing is called there
                        "uncovered": ensemble.uncovered({n["id"]: n["rung"] for n in nodes if n.get("rung")}, graph.LABEL)},
            "columns": ["Raw data"] + STEPS[:deepest + 1] + ["Lands in"], "lanes": LANES,
            "raw": {k: [{"name": a, "detail": b} for a, b in v] for k, v in graph.RAW.items()},
            "nodes": nodes, "edges": edges}


def _ladder(node: str, st: dict | None, ev: dict | None, hist: list[dict]) -> dict:
    """Where the culling system has an agent, and what the next review would say on today's evidence."""
    if node in cull.EXEMPT or graph.KIND.get(node) is None:
        return {}
    v = cull.judge(ev)
    st = st or {}
    return {"rung": st.get("rung", "active"), "rung_since": st.get("since"), "rung_reason": st.get("reason"),
            "rung_set_by": st.get("set_by"), "streak": st.get("streak", 0),
            "review": {"verdict": v, "text": cull.words(ev, v), "metric": (ev or {}).get("metric", "unreviewed"),
                       "value": (ev or {}).get("value"), "band": (ev or {}).get("band"), "n": (ev or {}).get("n")},
            "history": hist}


async def _ladder_state() -> tuple[dict, dict]:
    """agent_status rows, and each agent's last 6 reviews (newest first). Empty before migration 028."""
    pool = await db.pool()
    if not await pool.fetchval("SELECT to_regclass('public.agent_status') IS NOT NULL"):
        return {}, {}
    status = {r["agent"]: dict(r) for r in await pool.fetch("SELECT * FROM agent_status")}
    history: dict[str, list[dict]] = {}
    for r in await pool.fetch(
            """SELECT * FROM (SELECT agent, reviewed_at, verdict, value, band, n, metric, counted, rung_before, rung_after,
                                     detail->>'reason' AS reason,
                                     row_number() OVER (PARTITION BY agent ORDER BY reviewed_at DESC) AS k
                              FROM agent_reviews) x WHERE k <= 6 ORDER BY agent, reviewed_at DESC"""):
        history.setdefault(r["agent"], []).append({k: r[k] for k in r.keys() if k != "k"})
    return status, history


async def _models() -> dict[str, str]:
    """Each agent's most-used model over the last 7 days."""
    pool = await db.pool()
    best: dict[str, tuple[int, str]] = {}
    for r in await pool.fetch(
            """SELECT agent, model, count(*) AS n FROM agent_runs
               WHERE started_at > now() - interval '7 days' AND model IS NOT NULL GROUP BY 1, 2"""):
        if r["n"] > best.get(r["agent"], (0, ""))[0]:
            best[r["agent"]] = (r["n"], r["model"])
    return {a: m for a, (_, m) in best.items()}


async def _critics() -> dict:
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT c.status, o.r_multiple AS r, (c.evidence->'jev_critic'->>'veto')::float AS jev_veto
           FROM trade_calls c JOIN trade_call_outcomes o ON o.call_id = c.id
           WHERE c.source_kind = 'strategist' AND o.policy = 'stop_target' AND o.status = 'graded'
             AND o.r_multiple IS NOT NULL""")
    return critic_grades([dict(r) for r in rows])


@router.get("/agents")
async def agents():
    async with _lock:
        hit = _cache.get("agents")
        if hit and time.monotonic() - hit[0] < CACHE_S:
            return hit[1]
        # The map and the 24-hour figures must load; a failing grade source leaves those grades unknown.
        out, intel, models, critics, evidence, ladder = await asyncio.gather(
            output_api.output("24h"), intel_api.intel(), _models(), _critics(), cull.evidence(), _ladder_state(),
            return_exceptions=True)
        if isinstance(out, BaseException):
            raise out
        intel, models, critics, evidence, ladder = (None if isinstance(x, BaseException) else x
                                                    for x in (intel, models, critics, evidence, ladder))
        status, history = ladder or ({}, {})
        body = assemble(out, intel, models or {}, critics or {}, datetime.now(UTC), evidence, status, history)
        _cache["agents"] = (time.monotonic(), body)
        return body


class Rung(BaseModel):
    rung: str = Field(pattern="^(active|watch|probation|paused|retired)$")
    note: str | None = Field(default=None, max_length=300)


@router.post("/agents/{agent}/rung")
async def set_rung(agent: str, body: Rung, request: Request):
    """The owner retires an agent, or reinstates one. Needs the X-Albatross-Action header, like the Output page's controls."""
    if request.headers.get("x-albatross-action") != "1":
        raise HTTPException(403, "missing the X-Albatross-Action header")
    if agent not in graph.KIND or agent in cull.EXEMPT:
        raise HTTPException(404, f"no agent called {agent} on the ladder")
    done = await cull.set_rung(agent, body.rung, body.note)
    _cache.clear()
    return done
