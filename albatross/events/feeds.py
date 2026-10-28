"""What the structured event feeds (macro, listings, polymarket) share: tracked tiers, notice text, write-or-print."""

import html
import re

from albatross import db
from albatross.events import store
from albatross.events.store import Event


async def tiers() -> dict[str, int]:
    """Tracked symbol -> tier (1..3), for `assets` and importance."""
    pool = await db.pool()
    return {r["symbol"]: r["tier"] for r in await pool.fetch(
        "SELECT symbol, min(tier) AS tier FROM assets WHERE active GROUP BY symbol")}


def plain_text(page: str) -> str:
    """An HTML page or notice body as one line of text (scripts and styles dropped)."""
    page = re.sub(r"<(script|style)\b.*?</\1>", " ", page, flags=re.S | re.I)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", page))).strip()


def line(e: Event) -> str:
    p = f" p={e.probability:.2f}" if e.probability is not None else ""
    return (f"{e.starts_at:%Y-%m-%d %H:%M} {e.precision:<5} {e.kind:<9} i{e.importance} {e.status:<9}{p} "
            f"{e.title}  {e.assets or ''}  [{store.make_key(e.kind, e.subject, e.starts_at, e.venue)}]")


async def write(events: list[Event], collector: str, dry_run: bool) -> int:
    """Upsert, or with dry_run print what would be written. Returns the new-row count (dry run: all of them)."""
    if dry_run:
        for e in sorted(events, key=lambda e: e.starts_at):
            print(line(e))
        return len(events)
    return await store.upsert(events, collector=collector)
