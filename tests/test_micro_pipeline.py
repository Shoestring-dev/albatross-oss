"""The micro lane's candle job (albatross/micro/pipeline.py): a run fits its minute, so none is skipped."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from albatross.micro import pipeline


def _run(monkeypatch, backlog_s: float = 0.0, tokens: int = 20, call_s: float = 3.0):
    """One micro_candles run on a fake clock: the limiter grants a call every 12 s (5/min); a call's own time
    (response plus storing) is call_s."""
    clock, free = [0.0], [backlog_s]            # free: when the limiter next grants a call (reconciliation's backlog)
    now = datetime.now(UTC)
    rows = [{"chain": "solana", "token": f"T{i}", "pool": f"P{i}", "candles_at": None, "txns_h1": 5,
             "created_at": now - timedelta(hours=2), "first_seen": now - timedelta(hours=2), "last_entry": None,
             "open_position": False, "volume_h24_usd": 1e4} for i in range(tokens)]

    class Pool:
        async def fetch(self, sql, *a):
            return rows

    async def pool():
        return Pool()

    async def fetch(chain, token, pool_addr, limit):
        start = max(clock[0], free[0])
        free[0] = start + 12.0
        clock[0] = start + call_s
        return 60

    monkeypatch.setattr(pipeline.db, "pool", pool)
    monkeypatch.setattr(pipeline, "fetch_candles", fetch)
    monkeypatch.setattr(pipeline, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(pipeline.geckoterminal, "limiter", SimpleNamespace(wait_s=lambda: max(0.0, free[0] - clock[0])))
    return asyncio.run(pipeline.candles()), clock[0]


def test_a_candle_run_uses_its_minute_and_ends_before_the_next(monkeypatch):
    out, ended = _run(monkeypatch)
    assert out == {"rows": 5 * 60, "calls": 5, "due": 20, "slowest_s": pipeline.CALL_S, "rate_limited": False}
    assert out["calls"] == pipeline.CANDLE_CALLS_PER_MIN and ended <= pipeline.RUN_S < 60      # not ~2 a minute


def test_a_busy_process_means_fewer_calls_not_an_overrun(monkeypatch):
    out, ended = _run(monkeypatch, call_s=12.0)                   # a restart's catch-up: each call takes 12 s
    assert out["calls"] == 4 and out["slowest_s"] == 12.0 and ended <= pipeline.RUN_S


def test_a_backed_up_limiter_means_fewer_calls_not_an_overrun(monkeypatch):
    out, ended = _run(monkeypatch, backlog_s=30.0)                # reconciliation took the limiter's next 30 s
    assert 0 < out["calls"] < 5 and ended <= pipeline.RUN_S


def test_a_429_holds_every_call_and_is_never_retried_inside_the_call(monkeypatch):
    import httpx
    import pytest

    from albatross.http import WeightLimiter
    from albatross.venues import geckoterminal as gt
    sent = []

    def answer(request):
        sent.append(request.url.path)
        return httpx.Response(429, headers={"Retry-After": "0"})

    fake = httpx.AsyncClient(transport=httpx.MockTransport(answer))
    monkeypatch.setattr(gt, "client", lambda: fake)
    monkeypatch.setattr(gt, "limiter", WeightLimiter(per_minute=5, burst=1))
    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(gt.ohlcv_1m("solana", "P1", "T1", 100))
    assert len(sent) == 1                                          # no 20 s retry inside the job's minute
    assert gt.limiter.wait_s() >= gt.HOLD_S                         # everyone waits out the cool-down instead
