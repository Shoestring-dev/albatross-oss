"""GeckoTerminal public API (keyless): 1m OHLCV per pool.

Limits, checked 2026-09-29: 10 calls/min per IP on the keyless tier (geckoterminal.com/dex-api: paid plans go "from
10 calls/min to 250 calls/min"; a separate measurement found the same). In practice it is ~5:
on 2026-09-30 8/min drew a 429 about once a minute (every candle run stalled exactly one 20 s retry wait), and a
sustained 6/min (the candle job plus the hourly reconciliation) one every 2 minutes, so we use 5. A 429
carries Retry-After: 0, so on one the limiter grants nothing for HOLD_S and the call fails at once: the candle job ends
its round, reconciliation compares with the candles it has. It is never retried inside the call, where the wait held
a job past its minute. Keyless data refreshes every 60 s (docs.coingecko.com,
pool OHLCV by address). One call returns up to 1000 1m candles, so a pool fetched at least every ~16 h never loses a
minute; fetching more often only buys latency. `token=<address>` returns candles priced in that token whichever side
of the pool it sits on, a second guard against quote-side prices. Only minutes with trades are returned.
"""

import logging

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from albatross.http import WeightLimiter, client

log = logging.getLogger(__name__)

BASE = "https://api.geckoterminal.com/api/v2"
NETWORK = {"solana": "solana", "robinhood": "robinhood", "ethereum": "eth", "base": "base", "bsc": "bsc"}
limiter = WeightLimiter(per_minute=5, burst=1)
HOLD_S = 30                 # after a 429 nobody here calls for this long: the per-IP window needs a real cool-down
TIMEOUT_S = 10              # a 1000-candle answer takes ~0.4 s


def _transient(e: BaseException) -> bool:
    """A server error or a dropped connection is retried once, briefly; a 429 never is (the limiter holds instead)."""
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code >= 500
    return isinstance(e, httpx.TransportError)


@retry(retry=retry_if_exception(_transient), wait=wait_exponential(multiplier=2, min=2, max=10),
       stop=stop_after_attempt(2), reraise=True)
async def _get(path: str, params: dict) -> dict:
    await limiter.acquire()
    r = await client().get(BASE + path, params=params, headers={"Accept": "application/json"}, timeout=TIMEOUT_S)
    if r.status_code == 429:
        limiter.hold(HOLD_S)
        log.warning("GeckoTerminal 429 (keyless, per IP): no calls for %d s", HOLD_S)
    r.raise_for_status()
    return r.json()


async def ohlcv_1m(chain: str, pool: str, token: str, limit: int = 1000) -> list[list[float]]:
    """[[unix_ts, open, high, low, close, volume_usd], ...] newest first, USD price of `token`."""
    data = await _get(f"/networks/{NETWORK[chain]}/pools/{pool}/ohlcv/minute",
                      {"aggregate": 1, "limit": limit, "currency": "usd", "token": token})
    return data["data"]["attributes"]["ohlcv_list"]
