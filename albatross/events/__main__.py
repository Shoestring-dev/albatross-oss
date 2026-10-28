"""Dry run: run one structured event feed against the live sources and print the events it would write.

    python -m albatross.events macro|listings|polymarket

Nothing is written. It reads `assets` for tracked symbols (and event_feed_seen when it exists), so point it at a
database; the read-only role is enough. The real runs are the scheduled jobs events_macro / _listings / _polymarket.
"""

import asyncio
import logging
import sys

from albatross import db, http
from albatross.events import listings, macro, polymarket

FEEDS = {"macro": macro, "listings": listings, "polymarket": polymarket}


async def main(name: str) -> None:
    try:
        n = await FEEDS[name].run(dry_run=True)
        print(f"# {name}: {n} events (dry run, nothing written)")
    finally:
        await http.close()
        await db.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(sys.argv) < 2 or sys.argv[1] not in FEEDS:
        sys.exit(__doc__)
    asyncio.run(main(sys.argv[1]))
