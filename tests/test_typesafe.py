"""TypeSafe's Jev API (albatross/llm/typesafe.py): once it answers that the organisation has no credits, the rest of
the calls stop for NO_CREDIT_WAIT instead of each asking again (the scout asks once per coin, ~63 every 5 minutes)."""
import asyncio

import httpx
import pytest

from albatross.llm import typesafe


class Pool:
    def __init__(self):
        self.rows = []

    async def fetchval(self, sql, *a):
        self.rows.append(a)
        return len(self.rows)


class Client:
    def __init__(self, status, body):
        self.calls, self.status, self.body = 0, status, body

    async def post(self, url, json, timeout, headers):
        self.calls += 1
        return httpx.Response(self.status, json=self.body, request=httpx.Request("POST", url))


@pytest.fixture
def jev(monkeypatch):
    pool = Pool()

    async def get_pool():
        return pool

    async def nothing_spent(agent):
        return 0.0
    monkeypatch.setattr(typesafe.settings, "typesafe_api_key", "k")
    monkeypatch.setattr(typesafe.db, "pool", get_pool)
    monkeypatch.setattr(typesafe, "spent_today", nothing_spent)
    monkeypatch.setattr(typesafe, "_no_credit_until", 0.0)

    def use(client):
        monkeypatch.setattr(typesafe, "client", lambda: client)
        return pool
    return use


def ask():
    return asyncio.run(typesafe.decide("jev_scout", {"s": 1}, {"q": typesafe.noul("i", "y", "n")}, daily_budget_usd=8.0))


def test_no_credit_stops_the_calls_that_follow_without_asking_again(jev):
    c = Client(402, {"detail": {"error_type": "billing_error", "message": "Your organization has no available TypeSafe API credits."}})
    pool = jev(c)
    with pytest.raises(typesafe.OutOfCredit, match="out of credit"):
        ask()
    for _ in range(5):                                   # the rest of the run: refused here, not by TypeSafe
        with pytest.raises(typesafe.OutOfCredit, match="next try in 15 min"):
            ask()
    assert c.calls == 1 and len(pool.rows) == 1          # one call, one agent_runs row
    assert issubclass(typesafe.OutOfCredit, typesafe.BudgetExceeded)     # callers already stop on a spent budget


def test_answers_come_back_when_there_is_credit(jev):
    c = Client(200, {"answers": {"q": {"noul": 0.6}}, "usage": {"input_tokens": 100}, "model": "jev-1.13.0"})
    jev(c)
    answers, run_id = ask()
    assert answers == {"q": {"noul": 0.6}} and run_id == 1 and c.calls == 1
