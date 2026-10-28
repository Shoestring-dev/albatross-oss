"""Polymarket Gamma API (public, read-only market data; we never trade)."""

from albatross.http import WeightLimiter, client, retrying

GAMMA = "https://gamma-api.polymarket.com"
limiter = WeightLimiter(per_minute=60, burst=5)
PAGE = 100                                          # Gamma's cap per call


@retrying
async def _events_page(tag_slug: str, offset: int) -> list[dict]:
    await limiter.acquire()
    r = await client().get(f"{GAMMA}/events", params={"tag_slug": tag_slug, "closed": "false", "limit": PAGE,
                                                      "offset": offset})
    r.raise_for_status()
    return r.json()


async def open_events(tag_slug: str) -> list[dict]:
    """Every open event under a tag, each with its markets (closed ones included)."""
    out, offset = [], 0
    while True:
        page = await _events_page(tag_slug, offset)
        out += page
        if len(page) < PAGE:
            return out
        offset += PAGE
