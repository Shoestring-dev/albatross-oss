"""On-chain cycle gauge: MVRV and the MVRV Z-score for BTC and ETH, daily, into metric_series.

  MVRV      market cap / realized cap (realized cap = every coin valued at the price it last moved)
  Z-score   (market cap - realized cap) / standard deviation of market cap over all history UP TO that day

The standard deviation is expanding, as in the usual published charts: dividing old values by today's much
larger deviation would flatten every past cycle (the 2017 top would read 0.4 instead of ~9). Today's value is
the same either way. Checked 2026-09-30 against BGeometrics' published Z (1.09 on Sep 23; ours 1.01, the gap
is realized-cap method) and the known cycle marks (2017 top ~8.9, 2018 and 2022 bottoms ~-0.4). Source: Coin
Metrics community data, market cap and MVRV ratio; the whole history is re-pulled each run (~6k days)."""

import logging
import math
from datetime import UTC, date, datetime

from albatross import db
from albatross.venues import coinmetrics

log = logging.getLogger(__name__)

ASSETS = ("btc", "eth")
SOURCE = "coinmetrics"
COLS = ["source", "metric", "ts", "value", "meta"]
MIN_DAYS = 30               # no Z-score until there's a month of history to measure a deviation against


def mvrv_z(rows: list[dict]) -> list[tuple[date, float, float, float, float | None]]:
    """rows oldest first with CapMrktCurUSD and CapMVRVCur -> (day, market cap, realized cap, mvrv, z)."""
    out, n, s, ss = [], 0, 0.0, 0.0
    for r in rows:
        mc, mv = r.get("CapMrktCurUSD"), r.get("CapMVRVCur")
        if not mc or not mv:
            continue
        n, s, ss = n + 1, s + mc, ss + mc * mc
        sd = math.sqrt(max(ss / n - (s / n) ** 2, 0.0))
        rc = mc / mv
        out.append((r["day"], mc, rc, mv, (mc - rc) / sd if n >= MIN_DAYS and sd > 0 else None))
    return out


async def sync() -> int:
    rows = []
    for asset in ASSETS:
        series = mvrv_z(await coinmetrics.daily(asset, ["CapMrktCurUSD", "CapMVRVCur"]))
        for day, mc, rc, mv, z in series:
            t = datetime(day.year, day.month, day.day, tzinfo=UTC)
            rows += [(SOURCE, f"{asset}_market_cap_usd", t, mc, None), (SOURCE, f"{asset}_realized_cap_usd", t, rc, None),
                     (SOURCE, f"{asset}_mvrv", t, mv, None)]
            if z is not None:
                rows.append((SOURCE, f"{asset}_mvrv_z", t, z, None))
        if series:
            log.info("onchain: %s MVRV Z %.2f on %s (%d days)", asset, series[-1][4] or float("nan"), series[-1][0], len(series))
    await db.upsert("metric_series", COLS, rows, ["source", "metric", "ts"])
    return len(rows)
