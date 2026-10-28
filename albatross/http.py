import asyncio
import logging
import re
import time

import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

# httpx logs full URLs at INFO, and some APIs take keys as query params. Keep it quiet.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

_client: httpx.AsyncClient | None = None


def client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(20.0),
            headers={"User-Agent": "albatross/0.1"},
            follow_redirects=True,
        )
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# A key in a URL's query or a Telegram bot path, as it shows in an error: kept out of logs and the database.
SECRET_IN_URL = re.compile(r"((?<![\w-])(?:api[_-]?key|apikey|auth_token|access_token|token|key)=)[^&\s'\"<>]+|(/bot)\d+:[\w-]+",
                           re.IGNORECASE)


def redact(text: str) -> str:
    return SECRET_IN_URL.sub(lambda m: f"{m.group(1)}…" if m.group(1) else f"{m.group(2)}…", text)


def raise_for_status(r: httpx.Response) -> None:
    """httpx's raise_for_status, with an error text that leaves out the query string: for APIs that take the key there
    (Coinalyze, FRED). The response stays on the error, so retryable() still reads its status."""
    if r.is_error:
        raise httpx.HTTPStatusError(f"HTTP {r.status_code} from {r.url.host}{r.url.path}", request=r.request, response=r)


def retryable(e: BaseException) -> bool:
    if isinstance(e, httpx.HTTPStatusError):
        return e.response.status_code == 429 or e.response.status_code >= 500
    return isinstance(e, httpx.TransportError)


retrying = retry(
    retry=retry_if_exception(retryable),
    wait=wait_exponential(multiplier=1, min=2, max=60),
    stop=stop_after_attempt(5),
    reraise=True,
)


class WeightLimiter:
    """Token bucket measured in API 'weight' per minute."""

    def __init__(self, per_minute: float, burst: float | None = None):
        self.capacity = burst if burst is not None else per_minute
        self.tokens = self.capacity
        self.rate = per_minute / 60.0
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()

    def hold(self, seconds: float) -> None:
        """Grant nothing for `seconds` (a 429's cool-down): whoever acquires next waits it out."""
        self.updated = time.monotonic()
        self.tokens = -seconds * self.rate

    def wait_s(self, weight: float = 1.0) -> float:
        """Seconds until `weight` would be granted, if nobody else takes it first (a job's time budget uses it)."""
        tokens = min(self.capacity, self.tokens + (time.monotonic() - self.updated) * self.rate)
        return max(0.0, (weight - tokens) / self.rate)

    async def acquire(self, weight: float = 1.0) -> None:
        async with self.lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= weight:
                    self.tokens -= weight
                    return
                await asyncio.sleep((weight - self.tokens) / self.rate)
