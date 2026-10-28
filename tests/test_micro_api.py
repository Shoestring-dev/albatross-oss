from datetime import UTC, datetime, timedelta

from albatross.api.micro import call_price

T = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def test_call_price_prefers_the_last_trade_then_our_quote_then_the_ledger():
    base = {"called_at": T, "pre_close": None, "quote_price": None, "ledger_price": None, "ledger_at": None,
            "post_open": None, "post_ts": None}
    assert call_price({**base, "pre_close": 1.0, "quote_price": 2.0, "ledger_price": 3.0}) == (1.0, "pool", 0.0)
    assert call_price({**base, "quote_price": 2.0, "ledger_price": 3.0}) == (2.0, "quote", 0.0)
    p, how, after = call_price({**base, "ledger_price": 3.0, "ledger_at": T + timedelta(minutes=4)})
    assert (p, how, round(after)) == (3.0, "ledger", 4)
    p, how, after = call_price({**base, "post_open": 4.0, "post_ts": T + timedelta(minutes=2)})
    assert (p, how, round(after)) == (4.0, "after", 2)
    assert call_price(base) == (None, None, None)


def test_a_page_gets_the_stale_value_at_once_while_one_refresh_runs_and_what_is_stored_waits_for_fresh():
    import asyncio
    import time

    from albatross.api import micro
    builds = []

    async def build():
        builds.append(1)
        await asyncio.sleep(0)
        return f"new{len(builds)}"

    async def go():
        micro._cache["t"] = (time.monotonic() - 120, "old")      # past its minute, inside STALE_OK
        a = await micro._cached("t", 60, build)
        b = await micro._cached("t", 60, build)                  # its refresh is still running: no second one
        await asyncio.gather(*micro._background)
        c = await micro._cached("t", 60, build)
        micro._cache["t"] = (time.monotonic() - 120, "old")
        d = await micro._cached("t", 60, build, stale_ok=False)  # deep_compose stores it: waits for a fresh one
        micro._cache["t"] = (time.monotonic() - 900, "old")      # too old even for a page
        e = await micro._cached("t", 60, build)
        return a, b, c, d, e
    try:
        assert asyncio.run(go()) == ("old", "old", "new1", "new2", "new3")
    finally:
        micro._cache.pop("t", None)
        micro._locks.pop("t", None)
