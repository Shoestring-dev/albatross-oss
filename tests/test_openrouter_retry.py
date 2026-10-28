import asyncio

import httpx
import pytest
from pydantic import BaseModel

from albatross import db
from albatross.config import settings
from albatross.llm import openrouter


class Answer(BaseModel):
    x: int


class Pool:
    def __init__(self):
        self.runs = []

    async def fetchval(self, sql, *args):
        if "INSERT INTO agent_runs" in sql:
            self.runs.append({"ok": args[6], "error": args[7]})
            return len(self.runs)
        return 0.0                                          # spent today


def call(monkeypatch, replies):
    pool, queue = Pool(), list(replies)

    async def fake_pool():
        return pool

    async def fake_post(body):
        r = queue.pop(0)
        if isinstance(r, Exception):
            raise r
        return {"model": body["model"], "usage": {}, "choices": [{"message": {"content": r}, "finish_reason": "stop"}]}

    async def spent(agent):
        return 0.0

    monkeypatch.setattr(db, "pool", fake_pool)
    monkeypatch.setattr(openrouter, "_post", fake_post)
    monkeypatch.setattr(openrouter, "spent_today", spent)
    monkeypatch.setattr(settings, "openrouter_api_key", "k")
    out = asyncio.run(openrouter.structured("t", Answer, "s", "u", daily_budget_usd=1))
    return out, pool.runs, queue


@pytest.mark.parametrize("first", [None, "", '{"x": 1', "not json", TimeoutError(), httpx.ConnectError("reset")])
def test_one_retry_recovers_transient_failures(monkeypatch, first):
    (parsed, run_id), runs, _ = call(monkeypatch, [first, '{"x": 7}'])
    assert parsed.x == 7 and run_id == 2
    assert [r["ok"] for r in runs] == [False, True]          # both attempts are on the record


def test_schema_mistakes_and_refusals_are_not_retried(monkeypatch):
    with pytest.raises(RuntimeError, match="validation error"):
        call(monkeypatch, ['{"y": 1}', '{"x": 7}'])
    refused = httpx.HTTPStatusError("402", request=httpx.Request("POST", "https://x"),
                                    response=httpx.Response(402, text='{"error": "credits"}'))
    with pytest.raises(RuntimeError, match="HTTP 402"):
        call(monkeypatch, [refused, '{"x": 7}'])


def test_two_failures_give_up(monkeypatch):
    with pytest.raises(RuntimeError, match="empty answer"):
        call(monkeypatch, [None, None])
