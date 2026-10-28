"""Upbit notices (public, no key). Trade notices carry new listings, delistings and investment warnings."""

from albatross.http import WeightLimiter, client, retrying

BASE = "https://api-manager.upbit.com/api/v1/announcements"
limiter = WeightLimiter(per_minute=30, burst=5)


@retrying
async def _get(url: str, params: dict | None = None) -> dict:
    await limiter.acquire()
    r = await client().get(url, params=params)
    r.raise_for_status()
    body = r.json()
    if not body.get("success"):
        raise RuntimeError(f"upbit {url}: {body.get('error')}")
    return body["data"]


async def notices(category: str = "trade", per_page: int = 20) -> list[dict]:
    """Newest notices: [{'id', 'uuid', 'title', 'listed_at', 'first_listed_at', ...}]; listed_at moves on each edit."""
    data = await _get(BASE, {"os": "web", "page": 1, "per_page": per_page, "category": category})
    return data["notices"]


async def notice(notice_id: int) -> dict:
    """One notice with its body (markdown with HTML tables)."""
    return await _get(f"{BASE}/{notice_id}")
