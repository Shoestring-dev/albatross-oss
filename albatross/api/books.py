"""/api/books: the paper books and the dossier report (albatross/backtest/books.py) as the paper_books job last
stored them. Read-only; nothing is computed here."""

import asyncpg
import orjson
from fastapi import APIRouter

from albatross import db

router = APIRouter(prefix="/api")


@router.get("/books")
async def books():
    """{"books": {books, rules, computed_at} | None, "dossier": {..., computed_at} | None}: None before the first run."""
    pool = await db.pool()
    try:
        rows = {r["key"]: r for r in await pool.fetch("SELECT key, computed_at, report FROM paper_reports")}
    except asyncpg.UndefinedTableError:              # before migration 036
        rows = {}

    def body(key: str) -> dict | None:
        r = rows.get(key)
        return None if r is None else {**orjson.loads(r["report"]), "computed_at": r["computed_at"]}
    return {"books": body("books"), "dossier": body("dossier")}
