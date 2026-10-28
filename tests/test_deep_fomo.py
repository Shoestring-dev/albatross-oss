"""FOMO (albatross/deepend/fomo.py) on payloads in the shape fomoapi.io sent on 2026-10-01 19:45 UTC, with every
handle, display name, wallet, id, avatar and thesis made up (tests/fixtures/deep_fomo): the 7d board (7 of its 150
traders), 13 messages of the live socket (the welcome, a heartbeat, buys with an exact, an ambiguous and no matched
fill, a position add, sells with and without a fill, a thesis on BSC, a Hyperliquid perp), and two holder lookups cut
to 3 holders, one live and one served stale (6,533 s old). No database: the pools are fakes."""

import asyncio
import inspect
import json
import math
import time
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedOK, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from albatross import db, ledger
from albatross.deepend import fomo
from albatross.micro import ledger_feed

FX = Path(__file__).parent / "fixtures" / "deep_fomo"
T = datetime(2026, 10, 1, 19, 48, tzinfo=UTC)
SI = "DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP"
CARDS = "CARDSccUMFKoPRZxt5vt3ksUbxEFEcnZ3H2pd3dKxYjp"
KEY = "test-KEY/with+chars"


def load(name: str):
    return json.loads((FX / name).read_text())


def messages() -> list[dict]:
    return load("ws_messages.json")


def alert(trader: str, kind: str | None = None) -> dict:
    return next(m["message"] for m in messages() if m["message"].get("trader") == trader
                and (kind is None or m["message"].get("alertType") == kind))


def event(m: dict, received: datetime = T) -> dict:
    return fomo.parse_event(m, received)


def board_filters(watch=frozenset(), whale=fomo.WHALE_USD, **ranks) -> fomo.Filters:
    """The 7d board's traders as the feed's filters; ranks= overrides a handle's 7d rank."""
    rows = [{"window": "7d", "rank": ranks.get(r["handle"], r["rank"]), "handle": r["handle"], "user_id": r["user_id"]}
            for r in fomo.parse_board(load("board_7d.json"))]
    return fomo.filters_from(rows, set(watch), whale)


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    """A key, fresh REST state and default limits for every test."""
    monkeypatch.setenv("FOMOAPI_KEY", KEY)
    monkeypatch.setattr(fomo, "_api", {"stopped": None, "cool_until": 0.0, "cool_s": fomo.COOL_MIN, "remaining": None})


# ─── chains, boards, events ──────────────────────────────────────────────────
def test_fomo_chains_map_to_ours_and_the_rest_are_skipped():
    assert fomo.our_chain("robinhood", 4663) == "robinhood"
    assert fomo.our_chain(None, 1399811149) == "solana"
    assert fomo.our_chain("eth", None) == "ethereum" and fomo.our_chain("bnb", None) == "bsc"
    assert fomo.our_chain("whatever", "8453") == "base"                   # the chain id wins, as text too
    assert fomo.our_chain("hyperliquid", 1337) is None                    # perps
    assert fomo.our_chain("arc", 5042) is None and fomo.our_chain("monad", None) is None


def test_the_board_gives_wallets_with_their_evidence():
    rows = fomo.parse_board(load("board_7d.json"))
    assert [r["rank"] for r in rows] == [1, 2, 4, 7, 14, 68, 108]
    top = rows[0]
    assert (top["handle"], top["user_id"], top["pnl_usd"], top["volume_usd"], top["trades"], top["followers"]) == (
        "SampleTallTrader", "8f83254e-6d69-54a3-811f-b005a7e9efe1", 739411.0, 1086874.0, 87, 44689)
    assert (top["solana_evidence"], top["evm_evidence"]) == ("onchain-holdings", "provider-claimed")
    assert top["created_at"] == datetime(2026, 9, 18, 16, 17, 32, 398000, tzinfo=UTC)
    b3n = rows[2]
    assert b3n["evm_wallet"] is None and b3n["evm_evidence"] is None      # no wallet, no evidence
    assert rows[3]["clan"]["name"] == "$" and rows[0]["clan"] is None
    shouty = {"traders": [{**load("board_7d.json")["traders"][0],
                           "wallets": {"evm": "0x249D0739040274DB91BA940A457A8401102ACBA8"}}]}
    assert fomo.parse_board(shouty)[0]["evm_wallet"] == "0x249d0739040274db91ba940a457a8401102acba8"
    # FOMO's ranks are kept; a repeated or missing one falls back to the board's order.
    assert [r["rank"] for r in fomo.parse_board({"traders": [{"handle": "a", "rank": 3}, {"handle": "b", "rank": 3},
                                                             {"handle": "c"}]})] == [1, 2, 3]
    assert fomo.parse_board(None) == [] and fomo.parse_board({"available": False}) == []


def test_events_parse_and_sizes_follow_fomos_meaning():
    buy = event(alert("hosample"))
    assert (buy["type"], buy["chain"], buy["token_address"], buy["symbol"], buy["handle"]) == (
        "buy", "solana", "CbcyNo7m1amFWqEQm2m4PLv1UNvpcL3C1Ujm6AkzpKoU", "e/acc", "hosample")
    assert buy["event_id"] == "20755d55-6b9c-5fa9-80de-a6bb8e87b635"
    assert buy["ts"] == datetime.fromtimestamp(1790883155, UTC) and buy["received_at"] == T
    assert fomo.size(buy["raw"]) == 6008.08                              # the matched on-chain fill
    gage = alert("samplewtf")                                               # ambiguous fill: the position after it
    assert fomo.size(gage) == 6220.87 and event({**gage, "tokenAddress": gage["tokenAddress"].upper().replace(
        "0X", "0x")})["token_address"] == gage["tokenAddress"]          # EVM lowercase
    # usdValue on a sell is the position's realized PnL so far, never a size.
    sold = alert("sampletitan")
    assert (sold["usdValue"], fomo.size(sold)) == (37871.64, 4891.51)
    unmatched = alert("Samplepeanut")
    assert unmatched["usdValue"] == 1039.36 and fomo.size(unmatched) is None
    assert not fomo.whale(unmatched, 1_000)                              # a realized PnL never makes a whale
    assert fomo.whale(alert("sample999"), fomo.WHALE_USD)                   # a $595K position
    assert not fomo.whale(alert("sampl3ss"), fomo.WHALE_USD) and fomo.whale(alert("sampl3ss"), 25_000)
    assert event(alert("samplegains"))["chain"] is None               # a Hyperliquid perp
    thesis = event(alert("3SAMPLE"))
    assert (thesis["type"], thesis["chain"], fomo.size(thesis["raw"])) == ("thesis", "bsc", None)
    assert event({"type": "heartbeat", "ts": 1}) is None and event({"type": "alert"}) is None
    pushed = event({"type": "alert", "alertType": "buy", "trader": "x", "ts": 1790883155000})
    assert pushed["event_id"].startswith("h:")                           # no id: a hash of what identifies it


def test_what_the_feed_keeps_and_which_buys_are_calls():
    f = board_filters(watch={("base", "0x9e00fc92493451eba1c63dd3880d68b622037ba3")})
    why = {m: fomo.why_kept(event(alert(m)), f) for m in
           ("macsample", "sampletitan", "sample999", "hosample", "sampl3ss", "3SAMPLE", "Samplepeanut")}
    assert why == {"macsample": "tracked", "sampletitan": "tracked", "sample999": "tracked", "hosample": None,
                   "sampl3ss": None, "3SAMPLE": "thesis", "Samplepeanut": "watch"}
    assert fomo.why_kept(event(alert("sampl3ss")), board_filters(whale=25_000)) == "whale"
    # Calls: buys by anyone on the 7d or 30d board (ranks 1-150, banded when graded), matched by FOMO's user id,
    # else by handle (any case). Only a top-50 trader's call brings a new token into the price pipeline.
    assert fomo.call_ranks(event(alert("sample999")), f) == {"7d": 108}       # rank 108: a call, not a top trader's
    assert not fomo.top(fomo.call_ranks(event(alert("sample999")), f))
    only_24h = fomo.filters_from([{"window": "24h", "rank": 1, "handle": "sample999", "user_id": None}], set(),
                                 fomo.WHALE_USD)
    assert fomo.call_ranks(event(alert("sample999")), only_24h) is None      # a 24h or all-time board alone is no call
    assert fomo.call_ranks(event(alert("sample999")), board_filters(**{"sample999": 12})) == {"7d": 12}
    assert fomo.call_ranks(event(alert("macsample")), f) is None         # a sell
    top_buy = {**alert("hosample"), "userId": "8f83254e-6d69-54a3-811f-b005a7e9efe1", "trader": "renamed"}
    assert fomo.call_ranks(event(top_buy), f) == {"7d": 1}
    by_handle = {**alert("hosample"), "userId": None, "trader": "SAMPLETALLTRADER"}
    assert fomo.call_ranks(event(by_handle), f) == {"7d": 1}
    assert not fomo.top({"24h": 1, "all": 3}) and fomo.top({"30d": 50}) and not fomo.top({"7d": 51})
    # One call per position: FOMO's tradeId, else the trader and token that day.
    e = event(alert("sample999"))
    assert fomo.position(e) == "d9d0f3d3-a04f-4f28-97e5-99e65b08b545"
    e["raw"] = {**e["raw"], "tradeId": None}
    assert fomo.position(e) == f"813c287b-a2da-5333-b90f-1e6fc6eba6a1:solana:{CARDS}:2026-10-01"


# ─── the reader ──────────────────────────────────────────────────────────────
def test_the_reader_counts_everything_and_buffers_what_it_keeps():
    r = fomo.Reader()
    r.filters = board_filters(watch={("base", "0x9e00fc92493451eba1c63dd3880d68b622037ba3")})
    for m in messages():
        r.on_message(json.dumps(m["message"]), datetime.fromisoformat(m["received_at"]))
    r.on_message(json.dumps(alert("hosample")), T)                         # replayed after a reconnect
    r.on_message(b"not json", T)
    r.on_message(json.dumps([alert("macsample"), {"type": "subscribed"}]), T)   # a batch
    assert r.welcome == {"realtime": True, "delay_s": 0}
    assert [e["handle"] for e in r.buffer] == ["sample999", "sample999", "sampletitan", "macsample", "Samplepeanut",
                                               "3SAMPLE"]
    rep = r.report()
    assert (rep["seen"], rep["types"]) == (11, {"buy": 5, "perp": 1, "sell": 4, "thesis": 1})
    assert rep["kept"] == {"thesis": 1, "tracked": 4, "watch": 1}
    assert (rep["other_chain"], rep["replayed"], rep["heartbeats"], rep["buffered"]) == (1, 2, 1, 6)
    assert rep["tracked"] == 7 and r.report()["seen"] == 0                # the counters restart


class Socket:
    """A fake connection: hands out its messages, then ends with `end`."""

    def __init__(self, msgs=(), end=None):
        self.msgs, self.end = [json.dumps(m) for m in msgs], end or OSError("connection reset")

    async def recv(self):
        if self.msgs:
            return self.msgs.pop(0)
        raise self.end

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def scripted(sessions: list, urls: list):
    def connect(url, **kw):
        urls.append(url)
        s = sessions.pop(0)
        if isinstance(s, BaseException):
            raise s
        return s
    return connect


def test_the_reader_reconnects_with_backoff_and_writes_what_it_read(monkeypatch):
    written, urls, waits = [], [], []

    async def store(rows):
        written.extend(rows)
        return {"events": len(rows), "calls": 0, "tokens": 0}

    async def sleep(s):
        waits.append(s)
        if len(waits) == 4:
            raise asyncio.CancelledError

    monkeypatch.setattr(fomo, "store_events", store)
    bye = ConnectionClosedOK(Close(1000, "bye"), Close(1000, "bye"), True)
    live = [alert("macsample"), alert("sample999"), {"type": "heartbeat"}, alert("hosample")]
    r = fomo.Reader(scripted([OSError(f"cannot reach wss://api.fomoapi.io/ws/alerts?key={KEY}"),
                              Socket(live, bye), Socket(),
                              InvalidStatus(Response(401, "Unauthorized", Headers()))], urls), sleep)
    r.filters = board_filters()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(r.run())
    assert waits == [2.0, 4.0, 8.0, fomo.REFUSED_S]                       # doubling; a refused key waits 15 min
    assert all(u == "wss://api.fomoapi.io/ws/alerts?key=test-KEY%2Fwith%2Bchars" for u in urls)
    assert [e["handle"] for e in written] == ["macsample", "sample999"]      # flushed when the socket closed
    assert KEY not in (r.error or "") and "***" not in r.error           # the last error, the 401
    assert r.since is None and r.report()["disconnects"] == 4
    assert fomo.Reader._safe(OSError(f"x?key={KEY}")) == "OSError: x?key=***"


def test_a_long_connection_resets_the_backoff(monkeypatch):
    waits = []

    async def sleep(s):
        waits.append(s)
        if len(waits) == 3:
            raise asyncio.CancelledError

    async def store(rows):
        return {"events": len(rows), "calls": 0, "tokens": 0}

    monkeypatch.setattr(fomo, "store_events", store)
    monkeypatch.setattr(fomo, "STABLE_S", 0.0)
    r = fomo.Reader(scripted([OSError("down"), Socket(), Socket()], []), sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(r.run())
    assert waits == [2.0, 2.0, 2.0]


def test_a_pause_on_the_output_page_stops_a_busy_reader(monkeypatch):
    written, paused = [], []

    async def store(rows):
        written.extend(rows)
        return {"events": len(rows), "calls": 0, "tokens": 0}

    async def is_paused(key):
        paused.append(key)
        return len(paused) > 1                           # running at the start, paused once connected

    monkeypatch.setattr(fomo, "store_events", store)
    monkeypatch.setattr(fomo.controls, "paused", is_paused)
    monkeypatch.setattr(fomo.controls, "CACHE_S", 0)
    r = fomo.Reader(scripted([Socket([alert("macsample")] + [{"type": "heartbeat"}] * 50)], []))
    r.filters = board_filters()
    asyncio.run(r.run())                                 # returns: no reconnect, no exception
    assert paused == ["deep_fomo_feed", "deep_fomo_feed"] and [e["handle"] for e in written] == ["macsample"]


def test_a_silent_socket_is_dropped(monkeypatch):
    waits = []

    class Silent(Socket):
        async def recv(self):
            await asyncio.sleep(1)

    async def sleep(s):
        waits.append(s)
        raise asyncio.CancelledError

    monkeypatch.setattr(fomo, "FLUSH_S", 0.01)
    monkeypatch.setattr(fomo, "IDLE_S", 0.0)
    r = fomo.Reader(scripted([Silent()], []), sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(r.run())
    assert waits == [2.0] and r.error.startswith("Idle")


def test_backoff():
    assert [fomo.backoff(a) for a in range(10)] == [2, 4, 8, 16, 32, 64, 128, 256, 300, 300]
    assert fomo.backoff(0, 429) == 60 and fomo.backoff(7, 429) == 256
    assert fomo.backoff(0, 401) == fomo.backoff(5, 403) == fomo.REFUSED_S


def test_a_write_failure_keeps_the_rows_for_the_next_flush(monkeypatch):
    calls = []

    async def store(rows):
        calls.append(len(rows))
        if len(calls) == 1:
            raise OSError("database down")
        return {"events": len(rows), "calls": 1, "tokens": 1}

    monkeypatch.setattr(fomo, "store_events", store)
    r = fomo.Reader()
    r.filters = board_filters(**{"sample999": 3})
    r.on_message(json.dumps(alert("sample999")), T)
    asyncio.run(r.flush(force=True))
    assert len(r.buffer) == 1
    asyncio.run(r.flush(force=True))
    rep = r.report()
    assert calls == [1, 1] and r.buffer == [] and (rep["rows"], rep["calls"], rep["write_errors"]) == (1, 1, 1)


def test_the_keeper_starts_one_reader(monkeypatch):
    started = []

    async def filters(prev=None):
        return board_filters()

    async def forever(self):
        started.append(1)
        await asyncio.Event().wait()

    monkeypatch.setattr(fomo, "load_filters", filters)
    monkeypatch.setattr(fomo.Reader, "run", forever)
    monkeypatch.setattr(fomo, "_reader", fomo.Reader())

    async def twice():
        a = await fomo.feed()
        await asyncio.sleep(0)
        b = await fomo.feed()
        fomo._reader.task.cancel()
        return a, b
    a, b = asyncio.run(twice())
    assert (a["started"], b["started"], started) == (True, False, [1])
    assert a["rows"] == 0 and a["tracked"] == 7
    monkeypatch.delenv("FOMOAPI_KEY")
    assert asyncio.run(fomo.feed()) == {"rows": 0, "reader": "no FOMOAPI_KEY"}


# ─── writing events and calls ────────────────────────────────────────────────
class Conn:
    """Answers store_events' and store_holders' statements; records them."""

    def __init__(self, existing=(), known=()):
        self.existing, self.known, self.log = set(existing), list(known), []

    async def fetch(self, sql, *args):
        self.log.append((sql, args))
        if sql is fomo.INSERT_EVENTS_SQL:
            return [{"event_id": e} for e in args[0] if e not in self.existing]
        if sql is fomo.INSERT_TOKENS_SQL:
            return [{"address": a} for a in args[1]]
        if sql is fomo.INSERT_CALLS_SQL:
            return [{"src_id": i} for i, _ in enumerate(args[0], 1)]
        if "FROM de_fomo_traders" in sql:
            return [{"handle": h} for h in self.known if h.lower() in args[0]]
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.log.append((sql, args))

    async def executemany(self, sql, rows):
        self.log.append((sql, list(rows)))

    @asynccontextmanager
    async def transaction(self):
        yield


class Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


def use(monkeypatch, conn: Conn) -> Conn:
    async def pool():
        return Pool(conn)
    monkeypatch.setattr(db, "pool", pool)
    return conn


def test_a_top_traders_first_buy_of_a_position_becomes_one_call(monkeypatch):
    conn = use(monkeypatch, Conn(existing={"2aae41c3-2f4b-519c-af9e-b44faa8067c5"}))
    f = board_filters(**{"sample999": 9})
    rows = []
    for m in (alert("sample999"), next(x["message"] for x in messages() if x["message"].get("eventId") ==
                                    "2aae41c3-2f4b-519c-af9e-b44faa8067c5"), alert("macsample")):
        e = event(m)
        e["call"] = fomo.call_ranks(e, f)
        rows.append(e)
    out = asyncio.run(fomo.store_events(rows))
    assert out == {"events": 2, "calls": 1, "tokens": 1}                  # the add was stored before: no second call
    sqls = [s for s, _ in conn.log]
    assert sqls[0] is fomo.INSERT_EVENTS_SQL and "pg_advisory_xact_lock(hashtext('micro_tokens'))" in sqls[1]
    tokens = dict(conn.log)[fomo.INSERT_TOKENS_SQL]
    assert tokens == ([ "solana"], [CARDS], ["CARDS"], [datetime.fromtimestamp(1790883515, UTC)])
    calls = dict(conn.log)[fomo.INSERT_CALLS_SQL]
    assert calls[1] == ["d9d0f3d3-a04f-4f28-97e5-99e65b08b545"] and calls[7] == ["sample999"]
    assert (calls[9], calls[10], calls[11]) == ([9], [None], [594660.07])  # ranks then, and the position's size
    raw = dict(conn.log)[fomo.INSERT_EVENTS_SQL][10]
    assert orjson.loads(raw[0])["tradeId"] == "d9d0f3d3-a04f-4f28-97e5-99e65b08b545"
    # Adds in one batch: still one call, the earliest buy's.
    conn = use(monkeypatch, Conn())
    adds = [event(m) for m in messages() if m["message"].get("trader") == "sample999" for m in [m["message"]]]
    for e in adds:
        e["call"] = {"7d": 9}
    assert asyncio.run(fomo.store_events(list(reversed(adds)))) == {"events": 2, "calls": 1, "tokens": 1}
    assert dict(conn.log)[fomo.INSERT_CALLS_SQL][0] == ["9f3378e6-dad6-5baa-af9c-ac357b81d39e"]


def test_a_ranked_but_not_top_traders_buy_is_a_call_that_brings_no_new_token(monkeypatch):
    conn = use(monkeypatch, Conn())
    e = event(alert("sample999"))
    e["call"] = fomo.call_ranks(e, board_filters())                      # rank 108 on the 7d board
    assert asyncio.run(fomo.store_events([e])) == {"events": 1, "calls": 1, "tokens": 0}
    assert fomo.INSERT_TOKENS_SQL not in dict(conn.log)                   # priced only if we price the token anyway
    assert dict(conn.log)[fomo.INSERT_CALLS_SQL][9] == [108]


def test_fomo_buys_never_become_group_calls():
    """A FOMO buy goes to de_fomo_calls and micro_tokens only: micro_calls (n_calls, n_groups) never sees it."""
    sqls = [v for k, v in vars(fomo).items() if k.endswith("_SQL")]
    assert len(sqls) > 10 and not any("micro_calls" in s for s in sqls)
    assert "INTO micro_calls" not in inspect.getsource(fomo)
    assert "FROM de_fomo_calls" in ledger_feed.PENDING_SQL


# ─── the ledger (albatross/micro/ledger_feed.py) ─────────────────────────────
def fomo_pending(**kw) -> dict:
    """A de_fomo_calls row as micro_ledger's PENDING_SQL returns it, priced by our pipeline (the staging run's
    CARDS)."""
    r = {"kind": "fomo", "src_id": 1, "at": T, "received_at": T + timedelta(seconds=9), "chain": "solana",
         "address": CARDS, "intent": "buy", "who": "sample999", "group_id": None, "group_name": "FOMO", "strategy": None,
         "size_usd": 594660.07, "bot_price": None, "first_checked": None, "first_seen": T, "trade_chain": "solana",
         "symbol": "CARDS", "created_at": T - timedelta(days=60),
         "pool": "HnhpJPJgBG2KwniMTNW8cVBHvk1hFog3RC3kjnyc23tD",
         "dex": "raydium", "labels": ["CLMM"], "price_usd": 0.2068453818737712, "price_ts": T + timedelta(seconds=10),
         "sources": ["jupiter", "dexscreener"], "flags": [], "liquidity_usd": 3396706.89,
         "liquidity_src": "dexscreener", "liquidity_ts": T + timedelta(seconds=10)}
    return r | kw


def test_a_fomo_buy_is_logged_like_a_group_call_at_our_price():
    now = T + timedelta(seconds=10)
    price, detail = ledger_feed.price_check(fomo_pending(), now)
    assert (price, detail) == (0.2068453818737712, "ok")
    assert ledger_feed.verdict(fomo_pending(), price, now) == ("logged", None)
    assert ledger_feed.verdict(fomo_pending(received_at=T + timedelta(minutes=11)), price,
                               T + timedelta(minutes=11)) == ("unpriced", "late")      # a replay after an outage
    assert ledger_feed.verdict(fomo_pending(first_checked=T + timedelta(minutes=1)), None,
                               T + timedelta(minutes=11)) == ("unpriced", "no_price")
    row = ledger_feed.ledger_row(fomo_pending(), price, now)
    assert (row["source_kind"], row["source_id"], row["scope"], row["market"], row["direction"], row["tf"]) == (
        "fomo", "sample999", f"solana:{CARDS}", "dex", "long", "1d")
    assert row["thesis"] == "Bought on FOMO by ranked trader @sample999" and row["ref_price"] == price
    ev = row["evidence"]
    assert (ev["kind"], ev["src_id"], ev["call_type"], ev["group_name"], ev["size_usd"]) == (
        "fomo", 1, "fomo_buy", "FOMO", 594660.07)
    assert (ev["price"]["dex"], ev["liquidity_usd"]) == ("raydium", 3396706.89)   # what the DEX grader reads
    stored = row | {"logged_at": now, "evidence": orjson.loads(orjson.dumps(ev))}
    assert ledger._digest(ledger.GENESIS, row | {"logged_at": now}) == ledger._digest(ledger.GENESIS, stored)
    assert ledger_feed.SOURCE_KIND == {"call": "caller", "trade": "bot", "fomo": "fomo"}


# ─── REST: metering, pacing, 402 and 429 ─────────────────────────────────────
def test_pacing_spreads_the_day_and_never_passes_the_month():
    oct1, nov1 = date(2026, 10, 1), date(2026, 11, 1)
    assert fomo.days_left(oct1) == 31 and fomo.days_left(date(2026, 2, 28)) == 1
    assert fomo.daily_budget(8_000, 240_000, 0, oct1) == pytest.approx(240_000 / 31)   # a 31-day month binds
    assert fomo.daily_budget(8_000, 240_000, 0, nov1) == 8_000
    assert fomo.daily_budget(8_000, 240_000, 236_000, date(2026, 10, 31)) == 4_000
    assert fomo.daily_budget(8_000, 240_000, 250_000, oct1) == 0
    midnight = datetime(2026, 10, 1, tzinfo=UTC)
    assert fomo.pace(8_000, midnight) == pytest.approx(1_000 + 7_000 / 24)
    assert fomo.pace(8_000, midnight + timedelta(hours=12)) == pytest.approx(1_000 + 3_500 + 7_000 / 24)
    assert fomo.pace(8_000, midnight + timedelta(hours=23, minutes=30)) == 8_000
    assert fomo.pace(500, midnight) == 500                                # a day smaller than the boards
    fresh_day = {"today": 0, "month": 0}
    assert fomo.room("board", 250, fresh_day, 8_000, 0, 240_000, midnight)          # boards don't wait for the pace
    assert fomo.room("holders", 250, fresh_day, 8_000, 1_000, 240_000, midnight)    # the first hour's share
    assert not fomo.room("holders", 250, {"today": 100, "month": 100}, 8_000, 1_000, 240_000, midnight)
    assert fomo.room("holders", 250, {"today": 1_000, "month": 1_000}, 8_000, 0, 240_000, midnight + timedelta(hours=1))
    assert not fomo.room("board", 250, {"today": 7_900, "month": 7_900}, 8_000, 0, 240_000, midnight)
    assert not fomo.room("board", 250, {"today": 0, "month": 239_900}, 8_000, 0, 240_000, midnight)


class Answer:
    def __init__(self, status: int, body=None, cost: str | None = "250", left: str | None = "246000"):
        self.status_code, self.body = status, body
        self.headers = {k: v for k, v in (("x-credits-cost", cost), ("x-credits-remaining", left)) if v is not None}

    def json(self):
        return self.body


class Http:
    def __init__(self, answers: list):
        self.answers, self.asked = list(answers), []

    async def get(self, url, params=None, timeout=None, headers=None):
        self.asked.append((url, params, headers))
        return self.answers.pop(0)


class UsagePool:
    """de_api_usage and today's boards, as get() reads them; records the credits written."""

    def __init__(self, today=0, month=0, boards_done=0):
        self.today, self.month, self.boards_done, self.recorded = today, month, boards_done, []

    async def fetchrow(self, sql, *args):
        assert sql is fomo.USAGE_SQL
        return {"today": self.today, "month": self.month, "before": self.month - self.today}

    async def fetchval(self, sql, *args):
        assert "de_fomo_leaderboard" in sql
        return self.boards_done

    async def execute(self, sql, *args):
        assert "de_api_usage" in sql and "'fomoapi'" in sql
        self.recorded.append(args[0])
        self.today += args[0]
        self.month += args[0]


def rest(monkeypatch, answers: list, **usage) -> tuple[Http, UsagePool]:
    http, pool = Http(answers), UsagePool(**usage)

    async def get_pool():
        return pool
    monkeypatch.setattr(fomo, "client", lambda: http)
    monkeypatch.setattr(db, "pool", get_pool)
    # The whole day's budget, whatever the clock says: at 01:00 UTC the real pace refused the second lookup.
    # pace() has its own test.
    monkeypatch.setattr(fomo, "pace", lambda daily, now: daily)
    return http, pool


def test_every_call_is_metered_and_a_402_stops_rest_calls_until_tomorrow(monkeypatch):
    board = load("board_7d.json")
    http, pool = rest(monkeypatch, [Answer(200, board), Answer(200, {"holders": []}, cost=None), Answer(402, {
        "error": "credits_exhausted"}, cost=None)])
    assert asyncio.run(fomo.get("/v2/leaderboard/7d", {"limit": 150}, kind="board")) == board
    url, params, headers = http.asked[0]
    assert (url, params, headers["authorization"]) == ("https://api.fomoapi.io/v2/leaderboard/7d", {"limit": 150},
                                                       f"Bearer {KEY}")
    assert asyncio.run(fomo.get(f"/token/{SI}/holders", kind="holders")) == {"holders": []}
    with pytest.raises(fomo.Exhausted):
        asyncio.run(fomo.get(f"/token/{SI}/holders", kind="holders"))
    assert pool.recorded == [250, 250, 0]               # the header's cost, the published cost, nothing for a 402
    assert fomo._api["remaining"] == 246000
    with pytest.raises(fomo.Exhausted):                 # the rest of the day: refused without a call
        asyncio.run(fomo.get("/v2/leaderboard/24h", kind="board"))
    assert len(http.asked) == 3
    fomo._api["stopped"] = date(2026, 1, 1)             # a new day
    http.answers.append(Answer(200, board))
    assert asyncio.run(fomo.get("/v2/leaderboard/24h", kind="board")) == board


def test_a_429_backs_off_and_doubles(monkeypatch):
    http, pool = rest(monkeypatch, [Answer(429, cost=None), Answer(429, cost=None), Answer(200, {"ok": 1})])
    assert asyncio.run(fomo.get("/v2/leaderboard/7d", kind="board")) is None
    assert fomo._api["cool_s"] == 2 * fomo.COOL_MIN
    assert asyncio.run(fomo.get("/v2/leaderboard/7d", kind="board")) is None     # cooling off: no call made
    assert len(http.asked) == 1
    fomo._api["cool_until"] = 0.0                        # the minute has passed
    assert asyncio.run(fomo.get("/v2/leaderboard/7d", kind="board")) is None
    assert fomo._api["cool_s"] == 4 * fomo.COOL_MIN
    fomo._api["cool_until"] = 0.0
    assert asyncio.run(fomo.get("/v2/leaderboard/7d", kind="board")) == {"ok": 1}
    assert fomo._api["cool_s"] == fomo.COOL_MIN and pool.recorded == [0, 0, 250]


def test_our_budget_refuses_before_calling(monkeypatch):
    http, pool = rest(monkeypatch, [], month=239_900, today=900)
    with pytest.raises(fomo.BudgetExceeded):
        asyncio.run(fomo.get("/v2/leaderboard/7d", kind="board"))
    http, pool = rest(monkeypatch, [], month=7_800, today=7_800)           # the day spent
    with pytest.raises(fomo.BudgetExceeded):
        asyncio.run(fomo.get(f"/token/{SI}/holders", kind="holders"))
    assert http.asked == []
    http, pool = rest(monkeypatch, [Answer(401, {"error": "invalid key"}, cost=None)])
    with pytest.raises(RuntimeError, match="401"):
        asyncio.run(fomo.get("/v2/leaderboard/7d", kind="board"))
    monkeypatch.delenv("FOMOAPI_KEY")
    assert asyncio.run(fomo.get("/v2/leaderboard/7d", kind="board")) is None


def test_the_board_job_fetches_each_window_once_a_day(monkeypatch):
    asked, stored = [], []

    class BoardPool:
        async def fetch(self, sql, *args):
            return [{"window": "24h"}]                   # stored earlier today

        async def execute(self, sql, *args):
            stored.append(("best", args))

    async def get(path, params=None, *, kind):
        asked.append(path)
        if path.endswith("/30d"):
            raise fomo.BudgetExceeded("spent")
        return load("board_7d.json")

    async def store(window, rows, day, now):
        stored.append((window, len(rows)))
        return len(rows)

    async def pool():
        return BoardPool()
    monkeypatch.setattr(db, "pool", pool)
    monkeypatch.setattr(fomo, "get", get)
    monkeypatch.setattr(fomo, "store_board", store)
    assert asyncio.run(fomo.leaderboard()) == 7
    assert asked == ["/v2/leaderboard/7d", "/v2/leaderboard/30d"]            # 24h done; stopped at the budget
    assert stored[0] == ("7d", 7) and stored[1][0] == "best"


# ─── holders ─────────────────────────────────────────────────────────────────
def test_holder_lookups_parse_and_a_stale_answer_is_dated_by_its_age():
    live = fomo.parse_holders(load("holders_live.json"), T)
    assert live["stale"] is False and live["snapshot_at"] == T
    goat = live["holders"][0]
    assert (goat["handle"], goat["value_usd"], goat["pnl_usd"], goat["cost_basis_usd"], goat["is_dev"]) == (
        "SampleGoat_", 1671221.8, 1618071.07, 137691.24, False)
    assert goat["evm_wallet"] == "0x7e2a1041f79884790206f20f70859623bc4c66e7" and goat["thesis"].startswith("Just don")
    assert (goat["solana_evidence"], goat["evm_evidence"]) == ("onchain-holdings", "onchain-holdings")
    stale = fomo.parse_holders(load("holders_stale.json"), T)
    assert stale["stale"] is True and stale["snapshot_at"] == T - timedelta(seconds=6533)
    assert stale["holders"][0]["clan"]["name"] == "Clan Orchid"
    assert fomo.parse_holders({"available": False, "holders": []}, T) is None
    twice = {"holders": [{"handle": "A", "valueUsd": 1}, {"handle": "a", "valueUsd": 2}, {"valueUsd": 3}]}
    assert [h["value_usd"] for h in fomo.parse_holders(twice, T)["holders"]] == [1.0]


def test_store_holders_keeps_the_snapshot_the_check_and_the_wallets(monkeypatch):
    conn = use(monkeypatch, Conn(known=["QX_SAMPLE_AT_HQ"]))
    body = load("holders_stale.json")
    body["holders"][1]["handle"] = "qx_sample_at_hq"                       # a known trader, other case
    got = fomo.parse_holders(body, T)
    assert asyncio.run(fomo.store_holders("solana", SI, got, T)) == 3
    log = dict((s, a) for s, a in conn.log)
    snap = next(a for s, a in conn.log if "INSERT INTO de_fomo_holders" in s)
    assert [r[0] for r in snap] == [T - timedelta(seconds=6533)] * 3
    assert [r[3] for r in snap] == ["unisample", "QX_SAMPLE_AT_HQ", "jexample"]
    traders = next(a for s, a in conn.log if s is fomo.HOLDER_TRADER_SQL)
    assert traders[0][4] == "0x5296a45f0f203974db49f2dcbf3179690d38af32" and orjson.loads(traders[0][7])["name"] == \
        "Clan Orchid"
    check = next(a for s, a in conn.log if "de_fomo_holder_checks" in s)
    assert check[:6] == ("solana", SI, T, T - timedelta(seconds=6533), 3, "stale")
    assert orjson.loads(check[6])["unisample"]["is_dev"] is False
    assert asyncio.run(fomo.store_holders("solana", SI, None, T)) == 0
    assert next(a for s, a in reversed(conn.log) if "de_fomo_holder_checks" in s)[4:6] == (0, "unavailable")
    assert log                                                              # every statement went through the fake


def test_which_hot_tokens_get_a_holder_lookup():
    now = T
    assert fomo.holders_due({}, now)
    assert not fomo.holders_due({"checked_at": now - timedelta(minutes=20), "holders": 5}, now)
    assert fomo.holders_due({"checked_at": now - timedelta(minutes=31), "holders": 5}, now)
    nobody = {"checked_at": now - timedelta(minutes=45), "holders": 0}
    assert not fomo.holders_due(nobody, now)                               # nobody held it: 2 h
    assert fomo.holders_due(nobody | {"last_event": now - timedelta(minutes=5)}, now)   # unless FOMO traded it since
    cands = [{"address": "quiet", "checked_at": now - timedelta(hours=3)},
             {"address": "fresh", "checked_at": None},
             {"address": "opened", "why": ["opened"], "checked_at": now - timedelta(hours=1)},
             {"address": "active", "events_24h": 4, "checked_at": now - timedelta(minutes=40)}]
    order = sorted(cands, key=lambda t: fomo.holders_order(t, now), reverse=True)
    assert [t["address"] for t in order] == ["active", "opened", "fresh", "quiet"]


# ─── the dossier's section ───────────────────────────────────────────────────
def section_inputs():
    """SI as the staging run saw it: macsample' sell from the socket, a thesis, the stale holder lookup."""
    sell = alert("macsample")
    events = [{"ts": T - timedelta(minutes=1), "type": "thesis", "handle": "Csample69", "user_id": "u-c",
               "text": "chill", "usd_value": None, "fill_usd": None, "position_usd": None, "realized_usd": None},
              {"ts": fomo._ts(sell["ts"]), "type": "sell", "handle": "macsample", "user_id": sell["userId"],
               "text": sell["text"], "usd_value": sell["usdValue"], "fill_usd": sell["tradeUsd"], "position_usd": None,
               "realized_usd": sell["realizedPnlUsd"]}]
    got = fomo.parse_holders(load("holders_stale.json"), T)
    keep = ("is_dev", "pnl_usd", "cost_basis_usd", "thesis")
    details = {h["handle"].lower(): {k: h[k] for k in keep} for h in got["holders"]}
    check = {"checked_at": T, "snapshot_at": got["snapshot_at"], "holders": 3, "status": "stale",
             "details": orjson.dumps(details).decode()}
    holders = [{"handle": h["handle"], "amount": h["amount"], "value_usd": h["value_usd"]} for h in got["holders"]]
    traders = [{"handle": "macsample", "user_id": sell["userId"], "display_name": "777", "pnl_30d": 1_200_000.0,
                "ranks": '{"7d": 14}'},
               {"handle": "QX_SAMPLE_AT_HQ", "user_id": None, "display_name": "QX", "pnl_30d": None,
                "ranks": '{"7d": 11, "30d": 60}'}]
    return events, check, holders, traders


def test_the_section_names_the_traders_in_it():
    s = fomo.shape(*section_inputs(), T)
    assert [(t["handle"], t["rank"], t["top"], t["action"]) for t in s["traders"]] == [
        ("QX_SAMPLE_AT_HQ", 11, True, "holds"), ("macsample", 14, True, "sell"), ("unisample", None, False, "holds"),
        ("jexample", None, False, "holds")]
    gl, mac = s["traders"][:2]
    assert (gl["board"], gl["usd"], gl["holding_usd"], gl["position_pnl_usd"], gl["is_dev"]) == (
        "7d", 491520.5, 491520.5, 358032.14, False)
    assert gl["at"] == T - timedelta(seconds=6533)                          # the stale snapshot's own time
    assert (mac["usd"], mac["realized_usd"], mac["pnl_30d"], mac["holding_usd"]) == (15554.86, 580386.88, 1_200_000.0,
                                                                                     None)
    assert mac["at"] == datetime.fromtimestamp(1790883935, UTC)
    assert (s["n_traders"], s["n_ranked"], s["n_top"], s["n_holders"]) == (4, 2, 2, 3)
    assert (s["buys"], s["sells"], s["buyers"], s["sellers"]) == (0, 1, 0, 1)
    assert [(t["handle"], t["source"]) for t in s["theses"]] == [
        ("Csample69", "feed"), ("unisample", "holding"), ("QX_SAMPLE_AT_HQ", "holding"), ("jexample", "holding")]
    assert s["theses"][0]["at"] == T - timedelta(minutes=1) and s["theses"][1]["at"] is None
    # dated by its data (the newest event, or the snapshot when newer), never by when we looked
    assert (s["as_of"], s["holders_as_of"], s["holders_stale"], s["window_h"]) == (
        T - timedelta(minutes=1), T - timedelta(seconds=6533), True, 168)
    assert fomo.shape([], None, [], [], T) is None
    json.dumps(s, default=str)


def test_a_trim_is_not_an_exit_and_only_the_7d_and_30d_boards_rank():
    events, check, holders, traders = section_inputs()
    snap = check["snapshot_at"]
    sold = events[-1]                                                  # macsample' sell
    hold = {"handle": "macsample", "amount": 1.0, "value_usd": 61_000.0}
    after = fomo.shape(events, check | {"snapshot_at": sold["ts"] + timedelta(minutes=5)}, [*holders, hold],
                       traders, T)
    assert next(t for t in after["traders"] if t["handle"] == "macsample")["action"] == "trim"   # still holds after
    before = fomo.shape(events, check, [*holders, hold], traders, T)  # an older list: a $15.5K sale of $61K is a trim
    assert next(t for t in before["traders"] if t["handle"] == "macsample")["action"] == "trim"
    gone = fomo.shape(events, check, holders, traders, T)             # not holding: it left
    assert next(t for t in gone["traders"] if t["handle"] == "macsample")["action"] == "sell"
    ranked = [t | {"ranks": '{"24h": 1, "all": 2, "30d": 140}'} if t["handle"] == "macsample" else t for t in traders]
    mac = next(t for t in fomo.shape(events, check, holders, ranked, T)["traders"] if t["handle"] == "macsample")
    assert (mac["rank"], mac["board"], mac["top"]) == (140, "30d", False)   # a 24h or all-time #1 is not a top trader
    assert snap < sold["ts"]


def test_one_message_the_database_refuses_never_stalls_the_feed(monkeypatch):
    import asyncpg

    written = []
    r = fomo.Reader()
    good = [{"event_id": f"e{i}", "ts": T + timedelta(seconds=i), "text": "ok"} for i in range(3)]
    bad = {"event_id": "bad", "ts": T, "text": "ok", "bad": True}
    r.buffer = [*good, bad]

    async def single_refused(rows):                 # the batch and the bad row alone are refused, the rest land
        if any(e.get("bad") for e in rows):
            raise asyncpg.exceptions.UntranslatableCharacterError("unsupported Unicode escape sequence")
        written.extend(e["event_id"] for e in rows)
        return {"events": len(rows), "calls": 0, "tokens": 0}

    monkeypatch.setattr(fomo, "store_events", single_refused)
    asyncio.run(r.flush(force=True))
    assert sorted(written) == ["e0", "e1", "e2"] and r.buffer == [] and r.tally["bad_rows"] == 1
    m = {"type": "alert", "alertType": "buy", "eventId": "x1", "trader": {"oops": 1}, "userId": 42,
         "text": "gm\x00 frens", "token": "SYM\x00", "tokenAddress": "So11111111111111111111111111111111111111112",
         "chain": "solana", "nested": {"a": ["b\x00"]}}
    e = fomo.parse_event(m, T)
    assert e["handle"] is None and e["user_id"] == "42" and e["text"] == "gm frens" and e["symbol"] == "SYM"
    assert e["raw"]["nested"] == {"a": ["b"]} and "\\u0000" not in fomo._json(e["raw"])
    assert fomo.Filters().trader(42, {"x": 1}) is None


def test_section_reads_the_tables(monkeypatch):
    events, check, holders, traders = section_inputs()

    class SectionPool:
        async def fetch(self, sql, *args):
            if sql is fomo.EVENTS_SQL:
                assert args == ("solana", SI, fomo.RECENT)
                return events
            if sql is fomo.HOLDERS_SQL:
                return holders
            if sql is fomo.TRADERS_SQL:
                assert set(args[0]) == {"csample69", "macsample", "unisample", "qx_sample_at_hq", "jexample"}
                return traders
            raise AssertionError(sql)

        async def fetchrow(self, sql, *args):
            return check

    async def pool():
        return SectionPool()
    monkeypatch.setattr(db, "pool", pool)
    s = asyncio.run(fomo.section("solana", SI))
    assert s["n_top"] == 2 and s["traders"][1]["handle"] == "macsample"


# ─── registry ────────────────────────────────────────────────────────────────
def test_registry_entries():
    from albatross.deepend import registry
    assert fomo.JOBS == {"deep_fomo_leaderboard": fomo.leaderboard, "deep_fomo_feed": fomo.feed,
                         "deep_fomo_holders": fomo.holders}
    assert set(fomo.JOBS) == {n for n, _ in fomo.SCHEDULE} == set(fomo.CADENCE)
    assert fomo.CADENCE == {"deep_fomo_leaderboard": 3_600, "deep_fomo_feed": 60, "deep_fomo_holders": 300}
    assert registry.JOBS["deep_fomo_feed"] is fomo.feed and ("deep_fomo_feed", {"second": 20}) in registry.SCHEDULE
    assert not fomo.HOST_JOBS and not fomo.HOST_SCHEDULE
    assert math.isclose(fomo.BOARD_CREDITS, 1_000)


def test_alerts_and_holder_snapshots_are_kept_thirty_days(monkeypatch):
    seen = []

    class P:
        async def execute(self, sql, *args):
            seen.append((sql, args))
            return "DELETE 12" if "de_fomo_events" in sql else "DELETE 3"

    async def pool():
        return P()
    monkeypatch.setattr(db, "pool", pool)
    assert asyncio.run(fomo.prune(T)) == {"events": 12, "holders": 3}
    assert all(args == (T - fomo.RETAIN, fomo.PRUNE_BATCH) for _, args in seen[:2]) and len(seen) == 3
    assert "de_fomo_holder_demand" in seen[2][0] and seen[2][1] == ((T - fomo.RETAIN).date(),)
    assert fomo._deleted(None) == 0 and fomo._deleted("DELETE") == 0



def test_rest_answers_are_cleaned_and_a_bad_board_or_holder_answer_is_never_bought_twice(monkeypatch):
    s = "gm\ud83d frens\x00"                                              # a lone surrogate (stdlib json lets it in)
    assert fomo.clean({"a": [s]}) == {"a": ["gm frens"]} and orjson.dumps(fomo.clean({"a": s}))
    rows = fomo.parse_board({"traders": [{"handle": {"x": 1}}, {"handle": 42, "userId": ["u"], "displayName": "D\x00"}]})
    assert [(r["handle"], r["user_id"], r["display_name"]) for r in rows] == [("42", None, "D")]
    assert fomo._ts(1e20) is None and fomo._ts("not a time") is None
    asked, failed = [], []

    async def get(path, params=None, *, kind):
        asked.append(path)
        return {"traders": [{"handle": "a", "rank": 1}]}

    async def bad_store(window, rows, day, now):
        raise TypeError("Object of type str is not JSON serializable")

    class P:
        async def fetch(self, sql, *a):
            return []

        async def execute(self, sql, *a):
            return "DELETE 0"

    async def pool():
        return P()
    monkeypatch.setattr(db, "pool", pool)
    monkeypatch.setattr(fomo, "get", get)
    monkeypatch.setattr(fomo, "store_board", bad_store)
    monkeypatch.setattr(fomo, "_boards_failed", set())
    assert asyncio.run(fomo.leaderboard(("7d",))) == 0
    assert asyncio.run(fomo.leaderboard(("7d",))) == 0 and asked == ["/v2/leaderboard/7d"]   # not bought again today

    async def holders_body(path, params=None, *, kind):
        return {"holders": [{"handle": "h", "valueUsd": 1}]}

    async def refused(*a):
        raise ValueError("invalid input for query argument $8")          # asyncpg's client-side DataError is one

    async def mark(chain, address, at):
        failed.append(address)
    monkeypatch.setattr(fomo, "get", holders_body)
    monkeypatch.setattr(fomo, "store_holders", refused)
    monkeypatch.setattr(fomo, "mark_failed", mark)
    assert asyncio.run(fomo.lookup_holders("solana", "TOKEN")) == 0 and failed == ["TOKEN"]


def test_a_message_that_breaks_the_parser_is_skipped_never_fatal(monkeypatch):
    r = fomo.Reader()

    def boom(m, now):
        raise OSError("year is out of range")
    monkeypatch.setattr(fomo, "parse_event", boom)
    r.on_message(json.dumps({"type": "alert", "alertType": "buy", "ts": 1e20}), T)   # no exception escapes
    assert r.tally["bad_messages"] == 1 and r.report()["bad_messages"] == 1


def test_the_holder_job_records_what_was_due_and_what_got_a_lookup(monkeypatch):
    calls = []

    class P:
        async def execute(self, sql, *args):
            calls.append((sql, args))

    async def pool():
        return P()
    monkeypatch.setattr(db, "pool", pool)
    due = [{"chain": "solana", "address": "A"}, {"chain": "solana", "address": "B"}]
    asyncio.run(fomo.demand(due, [], T))
    asyncio.run(fomo.demand([], due[:1], T))
    asyncio.run(fomo.demand([], [], T))                                  # nothing: no write
    assert len(calls) == 2 and calls[0][1][2:] == (["solana", "solana"], ["A", "B"], [False, False])
    assert calls[1][1][4] == [True] and "coalesce(de_fomo_holder_demand.looked_at" in calls[1][0]
    assert fomo.wallets({"evm": {"x": 1}, "solana": None})["evm_wallet"] is None   # a reshaped field is dropped


def test_a_429_cool_off_stops_the_holder_run_and_its_tokens_are_never_counted_served(monkeypatch):
    """Correctness review, 2026-10-01: during a cool-off no call goes out, so those tokens were not served. The call
    that got the 429 did go out on our budget, and counts."""
    hot = [{"chain": "solana", "address": a} for a in "ABC"]
    asked, recorded = [], []

    async def tokens(kind):
        return hot

    class P:
        async def fetch(self, sql, *a):
            return []

    async def pool():
        return P()

    async def lookup(chain, address):
        asked.append(address)
        if address == "B":                                                # B's call answers 429: the cool-off starts
            fomo._api["cool_until"] = time.monotonic() + 60
            return None
        return 2

    async def demand(due, looked, now):
        recorded.append(([t["address"] for t in due], [t["address"] for t in looked]))
    monkeypatch.setattr(fomo, "env_str", lambda k, d=None: "key")
    monkeypatch.setattr(fomo.watch, "tokens", tokens)
    monkeypatch.setattr(db, "pool", pool)
    monkeypatch.setattr(fomo, "holders_due", lambda t, now: True)
    monkeypatch.setattr(fomo, "holders_order", lambda t, now: "CBA".index(t["address"]))   # A first, then B, C
    monkeypatch.setattr(fomo, "lookup_holders", lookup)
    monkeypatch.setattr(fomo, "demand", demand)
    monkeypatch.setattr(fomo, "_api", dict(fomo._api, cool_until=0.0))
    assert asyncio.run(fomo.holders(limit=3)) == 2
    assert asked == ["A", "B"] and recorded == [(["A", "B", "C"], []), ([], ["A", "B"])]
    asked.clear(), recorded.clear()
    assert asyncio.run(fomo.holders(limit=3)) == 0                       # still cooling off: nothing goes out
    assert asked == [] and recorded == [(["A", "B", "C"], []), ([], [])]



def test_a_dropped_row_fails_the_feed_run_so_it_is_seen(monkeypatch):
    from collections import Counter
    monkeypatch.setattr(fomo._reader, "tally", Counter({"bad_rows": 2, "seen": 5}))
    monkeypatch.setattr(fomo._reader, "ensure", lambda: False)

    async def filters(prev=None):
        return fomo.Filters()
    monkeypatch.setattr(fomo, "load_filters", filters)
    with pytest.raises(RuntimeError, match="dropped 2 event"):
        asyncio.run(fomo.feed())
    assert KeyError not in fomo.DATA_ERRORS and KeyError in fomo.UNSTORABLE      # a bug in the feed fails loudly
