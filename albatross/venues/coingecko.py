"""CoinGecko: market caps, symbol -> id mapping, and category membership."""

from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from albatross.config import settings
from albatross.http import WeightLimiter, client, retryable

# Keyless public API throttles hard (and punishes bursts); the free Demo key allows 30/min. Read per call: a key
# saved on the Setup page applies without a restart.
_limiters = {True: WeightLimiter(per_minute=25, burst=1), False: WeightLimiter(per_minute=4, burst=1)}


def _pro() -> bool:
    return bool(settings.coingecko_api_key) and settings.coingecko_plan == "pro"


def base() -> str:
    return "https://pro-api.coingecko.com/api/v3" if _pro() else "https://api.coingecko.com/api/v3"


def _headers() -> dict:
    if not settings.coingecko_api_key:
        return {}
    name = "x-cg-pro-api-key" if _pro() else "x-cg-demo-api-key"
    return {name: settings.coingecko_api_key}


@retry(
    retry=retry_if_exception(retryable),
    wait=wait_exponential(multiplier=15, min=15, max=120),  # keyless 429s need a long cool-down
    stop=stop_after_attempt(8),
    reraise=True,
)
async def _get(path: str, params: dict):
    await _limiters[bool(settings.coingecko_api_key)].acquire()
    r = await client().get(base() + path, params=params, headers=_headers())
    r.raise_for_status()
    return r.json()


async def markets(category: str | None = None, pages: int = 1) -> list[dict]:
    """Coins sorted by market cap, 250 per page."""
    out: list[dict] = []
    for page in range(1, pages + 1):
        params = {"vs_currency": "usd", "order": "market_cap_desc", "per_page": 250, "page": page}
        if category:
            params["category"] = category
        batch = await _get("/coins/markets", params)
        out.extend(batch)
        if len(batch) < 250:
            break
    return out
