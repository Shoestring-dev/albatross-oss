"""/api/onchain/mvrv: the MVRV Z-score history for BTC or ETH (albatross/ingest/onchain.py), weekly points plus
the latest day, for the cycle-gauge chart on those coins' pages. Read-only."""

from fastapi import APIRouter, HTTPException

from albatross import db

router = APIRouter(prefix="/api")
# Where past cycles turned, per asset, drawn as reference lines (from our own series, 2026-09-30).
MARKS = {"btc": [{"v": 0, "label": "cycle bottoms (2015, 2018, 2022)", "side": "left"},
                 {"v": 7, "label": "2013 and 2017 tops", "side": "right"}],
         "eth": [{"v": 0, "label": "cycle bottoms (2018, 2022)", "side": "left"},
                 {"v": 3, "label": "2017 and 2021 peaks", "side": "right"}]}
SETTLE_DAYS = 180           # the expanding deviation needs history before a Z means anything (2010's first months spike)


@router.get("/onchain/mvrv")
async def mvrv(asset: str = "btc"):
    asset = asset.lower()
    if asset not in MARKS:
        raise HTTPException(404, "MVRV is tracked for btc and eth")
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT ts, value FROM metric_series WHERE source = 'coinmetrics' AND metric = $1 ORDER BY ts""",
        f"{asset}_mvrv_z")
    rows = rows[SETTLE_DAYS:] if len(rows) > SETTLE_DAYS else rows
    pts = [(r["ts"], r["value"]) for i, r in enumerate(rows) if i % 7 == 0 or i == len(rows) - 1]
    return {"asset": asset, "points": [[t.date().isoformat(), round(v, 3)] for t, v in pts], "marks": MARKS[asset],
            "latest": {"date": rows[-1]["ts"].date().isoformat(), "z": rows[-1]["value"]} if rows else None}
