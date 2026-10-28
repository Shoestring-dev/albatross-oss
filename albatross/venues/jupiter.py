"""Jupiter Price API v3: USD price of Solana mints, from the last swap across all venues, filtered by Jupiter's own
outlier heuristics. Mints it cannot price reliably are left out of the response (not returned as null).
Up to 50 ids per request (developers.jup.ag/docs/price). Measured 2026-09-29: each entry also carries liquidity
(all pools), createdAt and launchpad; Cache-Control max-age=5.

The key is paid and may be shared with another program, such as a trading bot (e.g. 100 requests per 10 s).
Albatross stays at or below 20 per 10 s: the bucket below allows at most 5 + 10 = 15 in any 10-second window.
On top of that we yield: when the rate-limit headers show more than half the window used (the bot is busy) we
pause until it frees, and a 429 is never retried. Headers (developers.jup.ag/docs/portal/rate-limits):
x-ratelimit-remaining (signed) and x-ratelimit-reset (unix seconds at which one slot frees).
Keyless (local testing only): lite-api.jup.ag, which Jupiter is phasing out in favour of keyless api.jup.ag at
0.5 RPS; measured there: current + remaining = 5 per 10-second window. Our keyless bucket: 1 + 4 per 10 s.
"""

import time
from datetime import UTC, datetime, timedelta

import httpx

from albatross.config import settings
from albatross.http import WeightLimiter, client

MAX_IDS = 50
# Read per call: a key saved on the Setup page applies without a restart.
_limiters = {True: WeightLimiter(per_minute=60, burst=5), False: WeightLimiter(per_minute=24, burst=1)}
_paused_until = 0.0


def keyed() -> bool:
    return bool(settings.jupiter_api_key)


def host() -> str:
    return "https://api.jup.ag" if keyed() else "https://lite-api.jup.ag"


def limiter() -> WeightLimiter:
    return _limiters[keyed()]


class Busy(Exception):
    """Jupiter's window is (nearly) used up; skip it this round."""


def _note_limits(r: httpx.Response) -> None:
    """Pause until the window frees when the organisation has used more than half of it (keyed: the bot has
    priority) or all of it (keyless)."""
    global _paused_until
    h = r.headers
    remaining, current, reset = h.get("x-ratelimit-remaining"), h.get("x-ratelimit-current"), h.get("x-ratelimit-reset")
    floor = (int(remaining) + int(current or 0)) / 2 if keyed() and remaining is not None else 1
    if r.status_code == 429 or (remaining is not None and int(remaining) < floor):
        _paused_until = float(reset) if reset else time.time() + 10


async def prices(mints: list[str]) -> tuple[dict[str, dict], datetime]:
    """{mint: {usdPrice, liquidity, createdAt, launchpad, blockId, ...}} for up to 50 mints."""
    if time.time() < _paused_until:
        raise Busy
    await limiter().acquire()
    headers = {"x-api-key": settings.jupiter_api_key} if keyed() else {}
    r = await client().get(host() + "/price/v3", params={"ids": ",".join(mints[:MAX_IDS])}, headers=headers)
    _note_limits(r)
    if r.status_code == 429:
        raise Busy
    r.raise_for_status()
    age = float(r.headers.get("age") or 0)
    return r.json() or {}, datetime.now(UTC) - timedelta(seconds=age)
