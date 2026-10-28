"""The CEO memo on Home: its actions, each with a button that asks first (redesign step 3, 2026-10-06).

  GET  /api/memo/latest                      the latest CEO memo: its actions, what was done about each, the next review
  POST /api/memo/{id}/act   {idx, action, scale?}
       pause      the agent goes to the ladder's paused rung (a 10% sample at a tenth of its budget, still graded)
       retire     the agent goes to the retired rung
       reinstate  the agent goes back to active
       budget     the agent's budget scale is set (agent_controls.budget_scale, as on the Output page)
  POST /api/memo/{id}/undo  {idx}           puts back what was there before, within 24 hours
Both POSTs need the X-Albatross-Action header, like the Output page's controls. Every act is recorded in memo_acts
with the state before it (the ladder row with its streak, or the budget with its note), so Home can show "acted on"
and offer the undo. An act that would change nothing, or the wrong thing (a pause would start a retired agent again),
is refused; acting again within the undo window keeps the first state to go back to; an undo is refused once the
agent has been moved since (by the weekly review, the Agents page or another memo).
Rule-based agents (quant, deep_compose) get no buttons (`rules`): their runs never read a budget or a sample, so a
pause or a budget would change only a label or their weight in the blended call, and a retire stops a scheduled job
outright (deep_compose is the job that builds every dossier). Home says so and links to the Agents page.

Memos written before 2026-10-06 keep their actions only as markdown ("- Pause: Quant model. Its recorded ..."): they
are read back from it, the names matched to ids through the agents' labels and the books' names.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

import orjson
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from albatross import controls, db
from albatross.agents import cull, graph

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

UNDO_FOR = timedelta(hours=24)
LINE = re.compile(r"^- (\w+): (.+?)\. (.+)$", re.M)
# which buttons each kind of recommendation gets: the acts it may take
ACTS = {"pause": ("pause",), "cull": ("retire",), "resume": ("reinstate",), "budget": ("budget",)}
# acts that would change nothing, or the wrong thing: (action, the agent's rung now) -> why not
REFUSE = {
    ("pause", "paused"): "it is already paused",
    ("pause", "retired"): "it is retired, and a pause would start it again. Reinstate it on the Agents page if you want that",
    ("retire", "retired"): "it is already retired",
    ("reinstate", "active"): "it is already active",
}


def parse_markdown(md: str) -> list[dict]:
    """The actions of a memo stored only as markdown: [{kind, name, why}] in the memo's order."""
    part = md.split("## Recommended", 1)
    if len(part) < 2:
        return []
    body = part[1].split("\n_", 1)[0]                   # the italic notes after the list are not actions
    return [{"kind": k.lower(), "name": n.strip(), "why": w.strip()} for k, n, w in LINE.findall(body)]


async def _ids_by_name() -> dict[str, str]:
    """Display name -> id, for agents (graph.LABEL) and books (book:<key>), to read old memos back."""
    names = {label: key for key, label in graph.LABEL.items()}
    names["No red flag"] = "book:clean"             # its name in the memos before 2026-10-06
    pool = await db.pool()
    row = await pool.fetchrow("SELECT report FROM paper_reports WHERE key = 'books'")
    for b in (orjson.loads(row["report"]).get("books") or []) if row else []:
        names.setdefault(b["name"], f"book:{b['key']}")
    return names


def on_ladder(target: str | None) -> bool:
    return bool(target) and target in graph.KIND and target not in cull.EXEMPT


def rules(target: str | None) -> bool:
    """A rule-based agent: its runs never read a budget or a sample."""
    return graph.KIND.get(target or "") == graph.RULES


def acts_for(kind: str, target: str | None) -> list[str]:
    return list(ACTS.get(kind, ())) if on_ladder(target) and not rules(target) else []


async def _memo(report_id: int | None = None) -> dict | None:
    pool = await db.pool()
    row = await pool.fetchrow(
        f"""SELECT id, title, body_md, meta, created_at FROM reports WHERE kind = 'ceo'
            {"AND id = $1" if report_id is not None else ""} ORDER BY created_at DESC LIMIT 1""",
        *([report_id] if report_id is not None else []))
    if not row:
        return None
    meta = orjson.loads(row["meta"]) if isinstance(row["meta"], (str, bytes)) else row["meta"]
    if meta and meta.get("actions") is not None:
        actions = [dict(a) for a in meta["actions"]]
    else:
        ids = await _ids_by_name()
        actions = [{**a, "target": ids.get(a["name"]), "plain": ""} for a in parse_markdown(row["body_md"])]
    for i, a in enumerate(actions):
        a["idx"] = i
        a["acts"] = acts_for(a["kind"], a.get("target"))
        a["rules"] = rules(a.get("target"))
    return {"id": row["id"], "title": row["title"], "created_at": row["created_at"], "body_md": row["body_md"],
            "actions": actions}


@router.get("/memo/latest")
async def latest():
    memo = await _memo()
    now = datetime.now(UTC)
    if memo is None:
        return {"memo": None, "next_review": cull.next_review(now)}
    pool = await db.pool()
    acted = {r["idx"]: dict(r) for r in await pool.fetch(
        "SELECT idx, action, after, before, acted_at, undone_at FROM memo_acts WHERE report_id = $1", memo["id"])}
    rungs = await controls.rungs()
    for a in memo["actions"]:
        r = acted.get(a["idx"])
        a["acted"] = None if r is None else {
            "action": r["action"], "at": r["acted_at"], "undone_at": r["undone_at"],
            "after": _json(r["after"]), "can_undo": r["undone_at"] is None and now - r["acted_at"] < UNDO_FOR}
        a["rung"] = rungs.get(a["target"], "active") if on_ladder(a.get("target")) else None
    # what was done from earlier memos and can still be undone (a memo is written every 12 hours, the undo lasts 24)
    recent = [{**dict(r), "after": _json(r["after"]), "name": graph.LABEL.get(r["target"], r["target"])}
              for r in await pool.fetch(
                  """SELECT a.report_id, a.idx, a.target, a.action, a.after, a.acted_at, r.title
                     FROM memo_acts a JOIN reports r ON r.id = a.report_id
                     WHERE a.report_id <> $1 AND a.undone_at IS NULL AND a.acted_at > now() - $2::interval
                     ORDER BY a.acted_at DESC""", memo["id"], UNDO_FOR)]
    return {"memo": memo, "recent": recent, "next_review": cull.next_review(now)}


def _json(v):
    return orjson.loads(v) if isinstance(v, (str, bytes)) else v


class Act(BaseModel):
    idx: int = Field(ge=0, le=9)
    action: Literal["pause", "retire", "reinstate", "budget"]
    scale: float | None = Field(default=None, ge=0.0, le=1.0)


class Undo(BaseModel):
    idx: int = Field(ge=0, le=9)


def _check_header(request: Request) -> None:
    """A dependency, so it answers 403 before the body is even read."""
    if request.headers.get("x-albatross-action") != "1":
        raise HTTPException(403, "missing the X-Albatross-Action header")


async def _state(agent: str) -> dict:
    """The agent's ladder row and budget as the tables hold them now (never the 30 s controls cache): what an act
    changes and its undo puts back."""
    pool = await db.pool()
    s = await pool.fetchrow("SELECT rung, streak, since, reason, set_by FROM agent_status WHERE agent = $1", agent)
    c = await pool.fetchrow("SELECT budget_scale, note FROM agent_controls WHERE key = $1", agent)
    return {"rung": s["rung"] if s else "active", "status": {**dict(s), "since": s["since"].isoformat()} if s else None,
            "scale": float(c["budget_scale"]) if c else 1.0, "note": c["note"] if c else None}


async def _restore_rung(agent: str, before: dict, rung_now: str, note: str) -> None:
    """The ladder row as it was before the act, streak, since and reason included (removed when there was none),
    logged in agent_reviews like a change from the Agents page."""
    s = before.get("status")
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        if s is None:
            await conn.execute("DELETE FROM agent_status WHERE agent = $1", agent)
        else:
            await conn.execute(
                """INSERT INTO agent_status (agent, rung, streak, since, reason, set_by, updated_at)
                   VALUES ($1, $2, $3, $4, $5, $6, now())
                   ON CONFLICT (agent) DO UPDATE SET rung = $2, streak = $3, since = $4, reason = $5, set_by = $6,
                     updated_at = now()""",
                agent, s["rung"], s["streak"], datetime.fromisoformat(s["since"]), s["reason"], s["set_by"])
        await conn.execute(
            """INSERT INTO agent_reviews (reviewed_at, agent, metric, verdict, counted, rung_before, rung_after, detail)
               VALUES (now(), $1, 'manual', 'manual', false, $2, $3, $4)""",
            agent, rung_now, before["rung"], orjson.dumps({"reason": note}).decode())
    controls.forget()


async def _set_scale(key: str, scale: float, note: str) -> None:
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO agent_controls (key, paused, budget_scale, note, updated_at) VALUES ($1, false, $2, $3, now())
           ON CONFLICT (key) DO UPDATE SET budget_scale = $2, note = $3, updated_at = now()""", key, scale, note)
    controls.forget()


def _forget_pages() -> None:
    """The Agents and Output pages cache their builds; the next one shows the change."""
    from albatross.api import agents as agents_api, output as output_api
    agents_api._cache.clear()
    output_api._cache.clear()


@router.post("/memo/{report_id}/act", dependencies=[Depends(_check_header)])
async def act(report_id: int, body: Act):
    memo = await _memo(report_id)
    if memo is None or body.idx >= len(memo["actions"]):
        raise HTTPException(404, "no such memo action")
    a = memo["actions"][body.idx]
    if body.action not in a["acts"]:
        raise HTTPException(409, f"this action can't {body.action}")
    agent, note = a["target"], f"from the CEO memo of {memo['title']}"
    cur = await _state(agent)
    if body.action == "budget":
        if body.scale is None:
            raise HTTPException(422, "a budget needs a scale")
        if cur["rung"] == "retired":
            raise HTTPException(409, "it is retired, so a budget would change nothing")
        before, after = {"scale": cur["scale"], "note": cur["note"]}, {"scale": body.scale}
        await _set_scale(agent, body.scale, note)
    else:
        rung = {"pause": "paused", "retire": "retired", "reinstate": "active"}[body.action]
        if why := REFUSE.get((body.action, cur["rung"])):
            raise HTTPException(409, why)
        before, after = {"rung": cur["rung"], "status": cur["status"]}, {"rung": rung}
        await cull.set_rung(agent, rung, note)
    pool = await db.pool()
    # acting again while the first act can still be undone keeps the state from before the first one
    await pool.execute(
        """INSERT INTO memo_acts (report_id, idx, target, action, after, before) VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
           ON CONFLICT (report_id, idx) DO UPDATE SET target = $3, action = $4, after = $5::jsonb,
             before = CASE WHEN memo_acts.undone_at IS NULL AND memo_acts.acted_at > now() - $7::interval
                           THEN memo_acts.before ELSE EXCLUDED.before END,
             acted_at = now(), undone_at = NULL""",
        report_id, body.idx, agent, body.action, orjson.dumps(after).decode(), orjson.dumps(before).decode(), UNDO_FOR)
    _forget_pages()
    log.info("memo %s action %s: %s %s %s", report_id, body.idx, body.action, agent, after)
    return {"ok": True, "target": agent, "action": body.action, "before": before, "after": after}


@router.post("/memo/{report_id}/undo", dependencies=[Depends(_check_header)])
async def undo(report_id: int, body: Undo):
    pool = await db.pool()
    r = await pool.fetchrow("SELECT * FROM memo_acts WHERE report_id = $1 AND idx = $2", report_id, body.idx)
    if r is None or r["undone_at"] is not None:
        raise HTTPException(404, "nothing to undo")
    if datetime.now(UTC) - r["acted_at"] > UNDO_FOR:
        raise HTTPException(409, "too late to undo: change it on the Agents page")
    before, after = _json(r["before"]), _json(r["after"])
    note = f"undone from Home (CEO memo {report_id}, action {body.idx + 1})"
    cur = await _state(r["target"])
    if "scale" in before:
        if abs(cur["scale"] - float(after["scale"])) > 1e-6:
            raise HTTPException(409, f"its budget has changed since (it is {round(cur['scale'] * 100)}% now): change it on the Agents page")
        await pool.execute("UPDATE agent_controls SET budget_scale = $2, note = $3, updated_at = now() WHERE key = $1",
                           r["target"], float(before["scale"]), before.get("note"))
        controls.forget()
    else:
        if cur["rung"] != after["rung"]:
            raise HTTPException(409, f"it has moved since (it is {cur['rung']} now): change it on the Agents page")
        if "status" in before:
            await _restore_rung(r["target"], before, cur["rung"], note)
        else:                                   # recorded before 2026-10-06's fuller record: the rung alone
            await cull.set_rung(r["target"], before["rung"], note)
    await pool.execute("UPDATE memo_acts SET undone_at = now() WHERE report_id = $1 AND idx = $2", report_id, body.idx)
    _forget_pages()
    return {"ok": True, "target": r["target"], "restored": before}
