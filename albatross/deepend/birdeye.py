"""Birdeye Data API, shared by every Deep End module (Lite plan: 2.5M compute units a month, 15 rps).

Every call goes through get(): at most RPS requests a second (under the plan's 15), a compute-unit tally per UTC
day in de_api_usage, and a daily ceiling (DEEP_BIRDEYE_CU_PER_DAY, default 80,000 = the month's units / 31).
priority=True (a trader just opened a dossier) may run to 120% of the day. CU are the published per-endpoint
costs as best known; market.py measures and corrects them (owner B). Keys never leave this module.
"""

import asyncio
import logging
import time

from albatross import controls, db
from albatross.deepend.cfg import env_int, env_str
from albatross.http import client

log = logging.getLogger(__name__)

BASE = "https://public-api.birdeye.so"
CHAINS = {"solana": "solana", "base": "base", "bsc": "bsc", "ethereum": "ethereum"}     # ours -> x-chain
# Billed units, measured 2026-09-30 with /utils/v1/credits around each call. OHLCV bills by candles: v3 by candles
# RETURNED (<= 100: 12, <= 300: 25, <= 1000: 40, <= 2000: 75), so 12 holds for requests of <= 100 (count_limit=100);
# v1 by padded minutes (<= 100: 12, <= 300: 25, else 35), so 35 is its ceiling. An HTTP 400 bills nothing.
CU = {"/defi/v3/ohlcv": 12, "/defi/ohlcv": 35, "/defi/token_overview": 15, "/defi/token_security": 25,
      "/defi/v3/token/holder": 30, "/defi/token_trending": 25, "/defi/v2/tokens/new_listing": 20,
      "/defi/token_creation_info": 30, "/defi/v2/tokens/top_traders": 25, "/defi/v3/token/trade-data/single": 10,
      "/holder/v1/distribution": 30, "/trader/gainers-losers": 25, "/utils/v1/credits": 1}
DEFAULT_CU = 50
RPS = 10
PRIORITY_HEADROOM = 1.2


class BudgetExceeded(RuntimeError):
    pass


_lock = asyncio.Lock()
_last = 0.0


def daily_cu() -> int:
    return env_int("DEEP_BIRDEYE_CU_PER_DAY", 80_000)


async def used_today() -> int:
    pool = await db.pool()
    return int(await pool.fetchval(
        "SELECT coalesce(sum(units), 0) FROM de_api_usage WHERE provider = 'birdeye' AND day = (now() AT TIME ZONE 'UTC')::date") or 0)


async def _record(units: int) -> None:
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_api_usage (provider, day, calls, units) VALUES ('birdeye', (now() AT TIME ZONE 'UTC')::date, 1, $1)
           ON CONFLICT (provider, day) DO UPDATE SET calls = de_api_usage.calls + 1, units = de_api_usage.units + $1""", units)


async def get(path: str, params: dict | None = None, chain: str = "solana", *, priority: bool = False) -> dict | list | None:
    """-> the response's `data`, or None (no key, unsupported chain, or Birdeye said no). Raises BudgetExceeded."""
    key = env_str("BIRDEYE_API_KEY")
    if not key or chain not in CHAINS:
        return None
    cost = CU.get(path, DEFAULT_CU)
    limit = daily_cu() * await controls.scale("birdeye") * (PRIORITY_HEADROOM if priority else 1.0)
    if await used_today() + cost > limit:
        raise BudgetExceeded(f"birdeye: {await used_today():,} of {int(limit):,} units used today")
    global _last
    async with _lock:                                # RPS requests a second across the process
        wait = 1 / RPS - (time.monotonic() - _last)
        if wait > 0:
            await asyncio.sleep(wait)
        _last = time.monotonic()
    r = await client().get(BASE + path, params=params or {}, timeout=20,
                           headers={"X-API-KEY": key, "x-chain": CHAINS[chain], "accept": "application/json"})
    await _record(cost)
    if r.status_code != 200:
        log.warning("birdeye %s -> HTTP %s", path, r.status_code)
        return None
    body = r.json()
    return body.get("data") if isinstance(body, dict) and body.get("success", True) else None
