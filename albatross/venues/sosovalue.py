"""SoSoValue (free key): daily US spot-ETF flows for BTC and ETH."""

from datetime import date

from albatross.config import settings
from albatross.http import WeightLimiter, client, retrying

BASE = "https://api.sosovalue.xyz/openapi/v2/etf/historicalInflowChart"
limiter = WeightLimiter(per_minute=10, burst=2)
ETFS = {"us-btc-spot": "btc_etf", "us-eth-spot": "eth_etf"}


@retrying
async def flows(etf_type: str) -> list[dict]:
    """-> [{date, net_inflow_usd, net_assets_usd, value_traded_usd, cum_inflow_usd}], ~300 days, newest first.
    Their edge blocks default library user agents (403); the shared client sends albatross/x.y."""
    await limiter.acquire()
    r = await client().post(BASE, json={"type": etf_type}, headers={"x-soso-api-key": settings.sosovalue_api_key})
    r.raise_for_status()
    body = r.json()
    if body.get("code") != 0:
        raise RuntimeError(f"sosovalue {etf_type}: {body.get('msg')}")
    return [{"date": date.fromisoformat(d["date"]), "net_inflow_usd": d.get("totalNetInflow"),
             "net_assets_usd": d.get("totalNetAssets"), "value_traded_usd": d.get("totalValueTraded"),
             "cum_inflow_usd": d.get("cumNetInflow")} for d in body.get("data") or []]
