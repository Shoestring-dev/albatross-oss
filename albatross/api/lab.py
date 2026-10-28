"""/api/lab: the strategy lab (albatross/lab/run.py) as its hourly job left it: what is on paper, the best tested, the
latest tested, and how many each day. Read-only; nothing is computed here."""

from datetime import timedelta

import asyncpg
import orjson
from fastapi import APIRouter

from albatross import db
from albatross.lab import run as lab

router = APIRouter(prefix="/api")

FAMILIES = ("meme", "factor")
COLS = """id, family, name, origin, parent_ids, idea, status, created_at, tested_at, discovery, holdout, forward,
          t_needed, score, paper_at, decided_at, reason"""
CHECKPOINT = {"meme": lab.MEME_CHECKPOINT, "factor": lab.FACTOR_CHECKPOINT}


def _row(r) -> dict:
    d = dict(r)
    for k in ("discovery", "holdout", "forward"):
        if isinstance(d[k], str):
            d[k] = orjson.loads(d[k])
    d["parent_ids"] = list(d["parent_ids"] or [])
    if d["paper_at"] is not None:
        d["checkpoint"] = d["paper_at"] + CHECKPOINT[d["family"]]
    return d


@router.get("/lab")
async def lab_state():
    """{"report": the last run's windows, baselines and bar | None, "families": {meme|factor: {counts, paper, champions,
    best, recent}}, "daily": tested and survived per day and family (14 days), "origins": the same per origin}."""
    pool = await db.pool()
    try:
        rep = await pool.fetchrow("SELECT computed_at, report FROM paper_reports WHERE key = 'lab'")
        fams = {}
        for f in FAMILIES:
            counts = {r["status"]: r["n"] for r in await pool.fetch(
                "SELECT status, count(*) AS n FROM lab_strategies WHERE family = $1 GROUP BY status", f)}
            fams[f] = {
                "counts": counts,
                "paper": [_row(r) for r in await pool.fetch(
                    f"SELECT {COLS} FROM lab_strategies WHERE family = $1 AND status = 'paper' ORDER BY paper_at, id", f)],
                "champions": [_row(r) for r in await pool.fetch(
                    f"SELECT {COLS} FROM lab_strategies WHERE family = $1 AND status = 'champion' ORDER BY decided_at", f)],
                "retired": [_row(r) for r in await pool.fetch(
                    f"""SELECT {COLS} FROM lab_strategies WHERE family = $1 AND status = 'retired'
                        ORDER BY decided_at DESC LIMIT 10""", f)],
                "best": [_row(r) for r in await pool.fetch(
                    f"""SELECT {COLS} FROM lab_strategies WHERE family = $1 AND score IS NOT NULL
                        ORDER BY score DESC, id LIMIT 15""", f)],
                "recent": [_row(r) for r in await pool.fetch(
                    f"""SELECT {COLS} FROM lab_strategies WHERE family = $1 AND tested_at IS NOT NULL
                        ORDER BY tested_at DESC, id DESC LIMIT 20""", f)],
            }
        daily = [dict(r) for r in await pool.fetch(
            """SELECT tested_at::date AS day, family, count(*) AS tested,
                      count(*) FILTER (WHERE status <> 'rejected') AS survived
               FROM lab_strategies WHERE tested_at > now() - $1::interval GROUP BY 1, 2 ORDER BY 1, 2""",
            timedelta(days=14))]
        origins = [dict(r) for r in await pool.fetch(
            """SELECT family, origin, count(*) FILTER (WHERE tested_at IS NOT NULL) AS tested,
                      count(*) FILTER (WHERE status NOT IN ('rejected', 'queued')) AS survived,
                      count(*) FILTER (WHERE status = 'queued') AS queued
               FROM lab_strategies GROUP BY 1, 2 ORDER BY 1, 2""")]
    except asyncpg.UndefinedTableError:              # before migration 042
        return {"report": None, "families": {f: None for f in FAMILIES}, "daily": [], "origins": []}
    report = None if rep is None else {**orjson.loads(rep["report"]), "computed_at": rep["computed_at"]}
    return {"report": report, "families": fams, "daily": daily, "origins": origins}
