"""Macro inputs into metric_series: FRED rates/dollar/liquidity/risk, SoSoValue ETF flows,
DefiLlama stablecoin supply. Re-pulls a trailing window each run (idempotent upserts), so a missed
run or a late FRED revision heals itself. First run backfills two years."""

import logging
from datetime import UTC, date, datetime, timedelta

from albatross import db
from albatross.config import settings
from albatross.venues import defillama, fred, sosovalue

log = logging.getLogger(__name__)

COLS = ["source", "metric", "ts", "value", "meta"]
KEY = ["source", "metric", "ts"]


def _ts(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=UTC)


async def _since(source: str) -> date:
    pool = await db.pool()
    last = await pool.fetchval("SELECT max(ts) FROM metric_series WHERE source = $1", source)
    # Two-year backfill first time; afterwards 45 days back, which covers weekly series and revisions.
    return (last.date() - timedelta(days=45)) if last else datetime.now(UTC).date() - timedelta(days=730)


async def sync() -> int:
    rows = []
    if settings.fred_api_key:
        start = await _since("fred")
        for sid, (metric, _) in fred.SERIES.items():
            rows += [("fred", metric, _ts(d), v, None) for d, v in await fred.observations(sid, start)]
    if settings.sosovalue_api_key:
        for etf, prefix in sosovalue.ETFS.items():
            for d in await sosovalue.flows(etf):
                t = _ts(d["date"])
                rows += [("sosovalue", f"{prefix}_net_inflow_usd", t, d["net_inflow_usd"], None),
                         ("sosovalue", f"{prefix}_net_assets_usd", t, d["net_assets_usd"], None)]
    start = await _since("defillama")
    rows += [("defillama", "stablecoin_supply_usd", t, v, None)
             for t, v in await defillama.stablecoin_supply() if t.date() >= start]
    rows = [r for r in rows if r[3] is not None]
    await db.upsert("metric_series", COLS, rows, KEY)
    return len(rows)


