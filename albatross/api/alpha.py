"""/api/alpha: the Alpha section. The review is static (albatross/alpha/review.py); Loss avoidance and Factor edge are
what the alpha_report job last stored. Read-only; nothing is computed here."""

import asyncpg
import orjson
from fastapi import APIRouter

from albatross import db
from albatross.alpha import review

router = APIRouter(prefix="/api")


@router.get("/alpha")
async def alpha():
    """{"review": {...}, "avoid" | "edge" | "live": {..., computed_at} | None}."""
    pool = await db.pool()
    try:
        rows = {r["key"]: r for r in await pool.fetch(
            "SELECT key, computed_at, report FROM paper_reports WHERE key IN ('alpha_avoid', 'alpha_edge', 'alpha_live')")}
    except asyncpg.UndefinedTableError:
        rows = {}

    def body(key: str) -> dict | None:
        r = rows.get(key)
        return None if r is None else {**orjson.loads(r["report"]), "computed_at": r["computed_at"]}
    return {"review": review.payload(), "avoid": body("alpha_avoid"), "edge": body("alpha_edge"), "live": body("alpha_live")}
