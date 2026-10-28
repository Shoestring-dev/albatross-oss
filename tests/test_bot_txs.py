"""When each trading bot transaction happened (albatross/micro/bot_txs.py) and the chart's buy and sale markers
built from them (api/deep.py bot_markers). The fixtures are a synthetic buy and sale in the shape of Solana RPC
getTransaction (jsonParsed) results; every address, signature, amount and time in them is made up."""
import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from albatross.api import deep as api
from albatross.micro import bot_txs

FIX = Path(__file__).parent / "fixtures" / "bot_txs"
MINT = "4bNTReJV4ZF23zXZYD42XezymVZHkpjrXnKMd3N1pump"
BUY, SALE = (json.loads((FIX / f).read_text()) for f in ("bot_buy.json", "bot_sale.json"))


def test_the_bot_wallets_token_and_sol_changes_say_which_way_and_when():
    b, s = bot_txs.parse(BUY, MINT), bot_txs.parse(SALE, MINT)
    assert b["side"] == "buy" and round(b["token_delta"], 2) == 241724.23 and round(b["sol_delta"], 4) == -0.0503
    assert b["block_time"] == datetime(2026, 3, 14, 14, 20, 24, tzinfo=UTC)
    assert s["side"] == "sell" and round(s["token_delta"], 2) == -241724.23 and round(s["sol_delta"], 4) == 0.0430
    assert s["block_time"] == datetime(2026, 3, 14, 14, 35, 32, tzinfo=UTC)
    assert bot_txs.parse({**SALE, "meta": {**SALE["meta"], "err": {"InstructionError": [0, "x"]}}}, MINT)["side"] == "failed"
    assert bot_txs.parse(SALE, "SomeOtherMint")["side"] == "none"


T0 = datetime(2026, 3, 14, 14, 20, tzinfo=UTC)
TRADE = {"src_id": 7, "trader": "u-5a7e0c21", "strategy": "demotrail", "caller": "Sample Caller (@samplecaller)", "entry_at": T0,
         "entry_price": 1e-5, "size_usd": 5.0, "status": "closed", "closed_at": T0 + timedelta(minutes=15, seconds=20),
         "close_reason": "trail from 6.93x", "realized_x": 3.1, "real_pnl_usd": 10.5}


def tx(sig, mins, side, tok, sol):
    return {"trader": "u-5a7e0c21", "src_id": 7, "sig": sig * 70, "block_time": T0 + timedelta(minutes=mins), "side": side,
            "token_delta": tok, "sol_delta": sol}


def test_every_sale_on_chain_is_a_marker_with_its_share_and_the_last_one_is_the_close():
    txs = [tx("a", 0.4, "buy", 1000.0, -0.04), tx("b", 5, "sell", -250.0, 0.03), tx("c", 15.3, "sell", -750.0, 0.11)]
    m = api.bot_markers([TRADE], txs, T0 - timedelta(hours=1))
    assert [(k["kind"], k["detail"]["title"] if k["kind"] == "bot_sale" else "") for k in m] == [
        ("bot", ""), ("bot_sale", "Our bot sold 25%"), ("bot_sale", "Our bot sold")]          # no third, separate close
    buy, part, last = m
    assert buy["detail"]["sol"] == 0.04 and buy["detail"]["caller"] == "Sample Caller (@samplecaller)" and buy["detail"]["sig"] == "a" * 70
    assert part["detail"]["share"] == 0.25 and part["detail"]["sol"] == 0.03 and "reason" not in part["detail"]
    assert last["detail"]["reason"] == "trail from 6.93x" and last["detail"]["pnl_usd"] == 10.5 and last["detail"]["share"] == 0.75
    assert last["t"] == T0 + timedelta(minutes=15.3)


def test_without_timed_transactions_the_close_is_the_sale_and_a_late_close_shows_as_well():
    assert [k["kind"] for k in api.bot_markers([TRADE], [], T0)] == ["bot", "bot_sale"]
    late = {**TRADE, "closed_at": T0 + timedelta(hours=2), "close_reason": "tokens left the wallet (sold outside the bot)"}
    m = api.bot_markers([late], [tx("a", 0.4, "buy", 1000.0, -0.04), tx("b", 5, "sell", -1000.0, 0.05)], T0)
    assert [(k["kind"], k["detail"]["title"]) for k in m[1:]] == [("bot_sale", "Our bot sold the rest"), ("bot_sale", "Sold outside the bot")]


class Pool:
    def __init__(self, todo):
        self.todo, self.rows = todo, []

    async def fetch(self, sql, *a):
        return self.todo

    async def execute(self, sql, *a):
        self.rows.append(a)


class Resp:
    def __init__(self, status, body=None):
        self.status_code, self._body = status, body

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("bad", request=None, response=None)

    def json(self):
        return self._body


def test_the_job_times_what_it_can_and_stops_at_the_first_rate_limit(monkeypatch):
    todo = [{"trader": "u", "src_id": 7, "address": MINT, "sig": s} for s in ("buy", "sale", "gone", "more")]
    answers = {"buy": Resp(200, {"result": BUY}), "sale": Resp(200, {"result": SALE}), "gone": Resp(200, {"result": None}),
               "more": Resp(429)}
    pool = Pool(todo)

    async def get_pool():
        return pool

    class Client:
        async def post(self, url, timeout, json):
            return answers[json["params"][0]]

    async def no_wait(s):
        return None
    monkeypatch.setattr(bot_txs.db, "pool", get_pool)
    monkeypatch.setattr(bot_txs, "client", lambda: Client())
    monkeypatch.setattr(bot_txs.asyncio, "sleep", no_wait)
    assert asyncio.run(bot_txs.run()) == 2
    assert [(r[0], r[4], r[7]) for r in pool.rows] == [("buy", "buy", None), ("sale", "sell", None), ("gone", None, "not found")]


def test_a_buy_before_the_chart_sits_at_its_edge_while_the_position_lasts_into_it_and_sales_name_their_buy():
    start = T0 + timedelta(days=3)
    held = {**TRADE, "status": "open", "closed_at": None, "close_reason": None}
    (m,) = api.bot_markers([held], [], start)                                   # still held: shown at the edge
    assert m["kind"] == "bot" and m["t"] == start and m["detail"]["earlier"] and m["detail"]["bought_at"] == T0
    sold = {**TRADE, "closed_at": start + timedelta(hours=1), "close_reason": "manual close"}
    edge, sale = api.bot_markers([sold], [], start)                             # sold within the chart: both
    assert edge["detail"]["earlier"] and sale["detail"]["title"] == "Sold by hand"
    assert (sale["detail"]["bought_at"], sale["detail"]["caller"]) == (T0, "Sample Caller (@samplecaller)")
    assert api.bot_markers([{**TRADE, "closed_at": start - timedelta(hours=1)}], [], start) == []    # all over before it
