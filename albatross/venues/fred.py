"""FRED (St. Louis Fed), free key. Daily/weekly US macro series for the Macro analyst."""

from datetime import date

from albatross.config import settings
from albatross.http import WeightLimiter, client, raise_for_status, retrying

BASE = "https://api.stlouisfed.org/fred/series/observations"
limiter = WeightLimiter(per_minute=60, burst=5)     # FRED allows 120/min per key; stay at half

# series id -> (our metric name, what it is). WALCL/WTREGEN are $ millions, RRPONTSYD $ billions.
SERIES = {
    "DGS10": ("ust10y_pct", "10-year Treasury yield, %"),
    "DGS2": ("ust2y_pct", "2-year Treasury yield, %"),
    "T10YIE": ("breakeven10y_pct", "10-year breakeven inflation, %"),
    "DFF": ("fed_funds_pct", "effective fed funds rate, %"),
    "DTWEXBGS": ("dollar_index", "broad trade-weighted US dollar index"),
    "VIXCLS": ("vix", "CBOE VIX"),
    "SP500": ("spx", "S&P 500"),
    "NASDAQCOM": ("nasdaq", "Nasdaq Composite"),
    "WALCL": ("fed_assets_musd", "Fed total assets, $M (weekly, Wed)"),
    "WTREGEN": ("tga_musd", "Treasury General Account, $M (weekly, Wed)"),
    "RRPONTSYD": ("rrp_busd", "overnight reverse repo, $B"),
}


@retrying
async def observations(series_id: str, start: date) -> list[tuple[date, float]]:
    await limiter.acquire()
    r = await client().get(BASE, params={"series_id": series_id, "api_key": settings.fred_api_key,
                                         "file_type": "json", "observation_start": start.isoformat()})
    raise_for_status(r)                         # the key is in the URL: keep it out of the error text
    # FRED marks missing days (holidays) with "."
    return [(date.fromisoformat(o["date"]), float(o["value"]))
            for o in r.json().get("observations", []) if o["value"] not in (".", "")]


RELEASE_DATES = "https://api.stlouisfed.org/fred/releases/dates"


@retrying
async def _release_dates_page(start: date, end: date, offset: int) -> list[dict]:
    await limiter.acquire()
    r = await client().get(RELEASE_DATES, params={
        "api_key": settings.fred_api_key, "file_type": "json", "realtime_start": start.isoformat(),
        "realtime_end": end.isoformat(), "include_release_dates_with_no_data": "true",
        "order_by": "release_date", "sort_order": "asc", "limit": 1000, "offset": offset})
    raise_for_status(r)
    return r.json().get("release_dates", [])


async def release_dates(start: date, end: date) -> list[dict]:
    """Every release date of every FRED release in [start, end], scheduled ones included:
    [{'release_id': 10, 'release_name': 'Consumer Price Index', 'date': '2026-10-15'}, ...]"""
    out, offset = [], 0
    while True:
        page = await _release_dates_page(start, end, offset)
        out += page
        if len(page) < 1000:
            return out
        offset += 1000
