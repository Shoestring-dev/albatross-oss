"""Events as context for the LLM layers (Strategist, reports): what is scheduled around each asset and for the
market as a whole. Read-only; plain dicts sized for a prompt."""

from datetime import UTC, datetime, timedelta

from albatross import db

AHEAD = timedelta(days=7)
BACK = timedelta(days=2)


def _row(r) -> dict:
    d = {"when": r["starts_at"].strftime("%Y-%m-%d %H:%M UTC" if r["precision"] == "exact" else "%Y-%m-%d"),
         "precision": r["precision"], "kind": r["kind"], "title": r["title"], "importance": r["importance"],
         "status": r["status"]}
    if r["probability"] is not None:
        d["probability"] = round(r["probability"], 2)
    return d


async def upcoming(symbols: list[str], now: datetime | None = None) -> tuple[dict[str, list[dict]], list[dict]]:
    """(symbol -> its events from 2 days ago to 7 days ahead, market-wide macro events of importance >= 4)."""
    now = now or datetime.now(UTC)
    pool = await db.pool()
    if not await pool.fetchval("SELECT to_regclass('public.events') IS NOT NULL"):
        return {}, []
    rows = await pool.fetch(
        """SELECT kind, title, starts_at, precision, importance, status, probability, assets FROM events
           WHERE starts_at BETWEEN $1 AND $2 AND status <> 'cancelled'
             AND (assets && $3::text[] OR (kind = 'macro' AND importance >= 4))
           ORDER BY starts_at""", now - BACK, now + AHEAD, symbols)
    per: dict[str, list[dict]] = {}
    market = []
    for r in rows:
        if r["kind"] == "macro":
            market.append(_row(r))
        for a in r["assets"]:
            if a in symbols:
                per.setdefault(a, []).append(_row(r))
    return {k: v[:6] for k, v in per.items()}, market[:8]
