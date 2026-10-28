"""DefiLlama (keyless): total USD stablecoin supply, a proxy for crypto's dry powder."""

from datetime import UTC, datetime

from albatross.http import WeightLimiter, client, retrying

limiter = WeightLimiter(per_minute=20, burst=2)


@retrying
async def stablecoin_supply() -> list[tuple[datetime, float]]:
    await limiter.acquire()
    r = await client().get("https://stablecoins.llama.fi/stablecoincharts/all")
    r.raise_for_status()
    return [(datetime.fromtimestamp(int(p["date"]), UTC), p["totalCirculatingUSD"]["peggedUSD"])
            for p in r.json() if p.get("totalCirculatingUSD", {}).get("peggedUSD")]
