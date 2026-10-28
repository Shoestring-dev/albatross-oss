"""The Setup page's X search status: which lane runs X search (Social Pulse and the X Scout) and what each lane shows
(albatross/llm/xsearch.py). Behind read_guard, like the rest of Setup; it never returns the key, only whether one is
set."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends

from albatross.api.guard import read_guard
from albatross.config import settings
from albatross.llm import xai, xsearch

router = APIRouter()


@router.get("/api/setup/x", dependencies=[Depends(read_guard)])
async def x_status() -> dict:
    """lane: the lane in force ("api" | "cli" | "off"); the CLI counts as on while the host worker has made a Grok call
    in the last week. via: settings.x_search_via as set. api: the key, today's spend against the daily cap (UTC day)
    and the model. cli: when the host worker last made a Grok call."""
    seen = await xsearch.host_seen_at()
    return {
        "lane": xsearch.seen_lane(xsearch.x_lane(), seen, datetime.now(UTC)),
        "via": settings.x_search_via,
        "api": {"key_set": bool((settings.xai_api_key or "").strip()),
                "spent_today_usd": round(await xai.spent_today(), 4), "cap_usd": settings.xai_daily_usd,
                "model": xai.model()},
        "cli": {"host_worker_seen_at": seen.isoformat() if seen else None},
    }
