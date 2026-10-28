"""Coin Metrics Community API (free, no key): daily on-chain asset metrics. We use market cap and the MVRV ratio,
from which realized cap = market cap / MVRV (CapRealUSD itself isn't in the free tier). Data lags a day."""

from datetime import date, datetime

from albatross.http import WeightLimiter, client, retrying

BASE = "https://community-api.coinmetrics.io/v4/timeseries/asset-metrics"
limiter = WeightLimiter(per_minute=10, burst=2)     # community limit is 10 requests / 6 s; one call a day is plenty


@retrying
async def _page(url: str, params: dict | None) -> dict:
    await limiter.acquire()
    r = await client().get(url, params=params, headers={"User-Agent": "albatross/0.1"}, timeout=60)
    r.raise_for_status()
    return r.json()


async def daily(asset: str, metrics: list[str], start: date = date(2009, 1, 1)) -> list[dict]:
    """[{"day": date, metric: float, ...}] oldest first, every page."""
    params = {"assets": asset, "metrics": ",".join(metrics), "frequency": "1d", "page_size": 10000,
              "start_time": start.isoformat()}
    url, out = BASE, []
    while url:
        d = await _page(url, params)
        for r in d.get("data", []):
            row = {"day": datetime.fromisoformat(r["time"][:10]).date()}
            row |= {m: float(r[m]) for m in metrics if r.get(m) not in (None, "")}
            out.append(row)
        url, params = d.get("next_page_url"), None       # the next page URL carries every parameter
    return out
