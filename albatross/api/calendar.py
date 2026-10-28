"""/api/calendar: the events calendar (docs/SPEC-EVENTS.md). Read-only, cached 60 s.

  GET /api/calendar?days_back=2&days_ahead=30&kind=&asset=&min_importance=1
      -> {"events": [...], "counts": {kind: n}} sorted by starts_at. The window runs from midnight UTC `days_back`
         days ago to the end of the day `days_ahead` days on; an event counts while any of its period is inside
         (a 'Q4' TGE stays on the calendar through December). `kind` takes a comma list; `counts` ignores it, so a
         view can show every kind's size next to the filtered list.
  GET /api/calendar/{id} -> one event.
"""

import time

import orjson
from fastapi import APIRouter, HTTPException, Query

from albatross import db

router = APIRouter(prefix="/api")

COLS = ("id, key, kind, title, subject, venue, starts_at, precision, ends_at, assets, chains, importance, status, "
        "probability, summary, sources, first_seen")
# How long an event lasts when it has no ends_at: its precision's period.
END = """coalesce(ends_at, starts_at + CASE precision WHEN 'day' THEN interval '1 day'
         WHEN 'week' THEN interval '7 days' WHEN 'month' THEN interval '1 month'
         WHEN 'quarter' THEN interval '3 months' ELSE interval '0' END)"""
TTL = 60
_cache: dict[str, tuple[float, object]] = {}


async def _cached(key: str, fn):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < TTL:
        return hit[1]
    val = await fn()
    _cache[key] = (time.monotonic(), val)
    return val


def _event(r) -> dict:
    return dict(r) | {"sources": orjson.loads(r["sources"])}


def shape(rows: list[dict], kinds: set[str]) -> dict:
    """Window rows (already in starts_at order) -> the response: counts by kind over all of them, events filtered."""
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return {"events": [r for r in rows if not kinds or r["kind"] in kinds], "counts": counts}


@router.get("/calendar")
async def calendar(days_back: int = Query(2, ge=0, le=365), days_ahead: int = Query(30, ge=0, le=730),
                   kind: str = "", asset: str = "", min_importance: int = Query(1, ge=1, le=5)):
    kinds = {k.strip().lower() for k in kind.split(",") if k.strip()}
    asset = asset.strip().upper()

    async def load():
        pool = await db.pool()
        rows = await pool.fetch(
            f"""SELECT {COLS} FROM events
                WHERE starts_at < date_trunc('day', now()) + make_interval(days => $2 + 1)
                  AND (starts_at >= date_trunc('day', now()) - make_interval(days => $1)
                       OR {END} > date_trunc('day', now()) - make_interval(days => $1))
                  AND importance >= $3 AND ($4 = '' OR $4 = ANY(assets))
                ORDER BY starts_at, importance DESC, id""", days_back, days_ahead, min_importance, asset)
        return shape([_event(r) for r in rows], kinds)

    return await _cached(f"cal:{days_back}:{days_ahead}:{','.join(sorted(kinds))}:{asset}:{min_importance}", load)


@router.get("/calendar/{event_id}")
async def calendar_event(event_id: int):
    async def load():
        pool = await db.pool()
        return await pool.fetchrow(f"SELECT {COLS} FROM events WHERE id = $1", event_id)

    r = await _cached(f"event:{event_id}", load)
    if r is None:
        raise HTTPException(404, "no such event")
    return _event(r)
