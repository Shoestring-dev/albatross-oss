"""The Alpha section (review of 2026-10-09): the review itself, and the suggestions running beside what they would
replace. Job alpha_report (hourly) stores Loss avoidance and Factor edge in paper_reports; the review is static."""

import asyncio
import logging
from datetime import UTC, datetime

import orjson

from albatross import db
from albatross.alpha import avoid, edge, live
from albatross.lab import meme

log = logging.getLogger(__name__)


async def run() -> dict:
    """alpha_report -> {"rows": reports written}."""
    now = datetime.now(UTC)
    calls = await meme.load()
    rows = []
    if calls:
        rows.append(("alpha_avoid", now, orjson.dumps(await asyncio.to_thread(avoid.report, calls, now),
                                                      default=str).decode()))
    rows.append(("alpha_edge", now, orjson.dumps(await edge.report(now), default=str).decode()))
    await db.upsert("paper_reports", ["key", "computed_at", "report"], rows, ["key"])
    log.info("alpha_report: %d reports", len(rows))
    return {"rows": len(rows)}


async def run_live() -> dict:
    """alpha_live (every 5 minutes, after the grader): every new call's verdict and how it is doing, and the factor
    lists marked now."""
    now = datetime.now(UTC)
    calls = await meme.load()
    body = await live.report(calls, now)
    await db.upsert("paper_reports", ["key", "computed_at", "report"],
                    [("alpha_live", now, orjson.dumps(body, default=str).decode())], ["key"])
    return {"rows": len(body["feed"])}
