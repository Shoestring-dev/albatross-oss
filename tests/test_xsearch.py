"""X search on the xAI API (albatross/llm/xai.py) and the lane choice between it and the Grok CLI
(albatross/llm/xsearch.py): the request, the response as docs.x.ai shows it, the caps, the evidence gate on API answers,
which process runs the X jobs, the Health page and alerts when no lane is on, and the Setup status route.

There is no real xAI key here: every HTTP call goes to a stub transport and every query to a stub pool. The response
bodies follow the shapes in docs.x.ai (developers/tools/x-search, tools/citations, cost-tracking, the Responses API
reference), read 2026-10-27.
"""

import asyncio
import inspect
import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from albatross import __main__ as main
from albatross import db
from albatross.agents import graph
from albatross.agents import social_pulse as sp
from albatross.alerts import monitor, rules
from albatross.api import agents as ag
from albatross.api import xsetup
from albatross.config import settings
from albatross.deepend import xintel as x
from albatross.llm import cli, xai, xsearch

KEY = "xai-TESTKEYnotreal0000000000abcd"
NOW = datetime.now(UTC)
M, H = timedelta(minutes=1), timedelta(hours=1)
FIX = Path(__file__).parent / "fixtures" / "deep_x"


def status(when: datetime, handle: str = "trader", low: int = 7) -> str:
    """An x.com status URL whose snowflake id encodes `when`."""
    return f"https://x.com/{handle}/status/{((int(when.timestamp() * 1000) - sp.TWITTER_EPOCH_MS) << 22) + low}"


def body(answer: dict | str, citations: list[str], *, ticks: int | None = 2_871_250_000, posts: int = 44,
         status_: str = "completed") -> dict:
    """A /v1/responses body after X searches, shaped like the docs' examples: the searches as custom_tool_call items,
    the answer as the message's output_text, every returned URL in citations, the cost in ticks."""
    text = answer if isinstance(answer, str) else json.dumps(answer)
    usage = {"input_tokens": 18_204, "input_tokens_details": {"cached_tokens": 12_032}, "output_tokens": 1_406,
             "output_tokens_details": {"reasoning_tokens": 2_210}, "total_tokens": 21_820,
             "num_sources_used": 0, "num_server_side_tools_used": 3,
             "server_side_tool_usage_details": {"web_search_calls": 0, "x_search_calls": 3, "x_posts_fetched": posts,
                                                "x_users_fetched": 0, "code_interpreter_calls": 0,
                                                "file_search_calls": 0, "mcp_calls": 0, "document_search_calls": 0,
                                                "image_generation_calls": 0}}
    if ticks is not None:
        usage["cost_in_usd_ticks"] = ticks
    return {
        "id": "5808284d-ae14-9981-9289-73515f67ebda", "object": "response", "created_at": 1_793_000_000,
        "completed_at": 1_793_000_042, "model": "grok-4.3", "status": status_, "store": False,
        "output": [
            {"type": "custom_tool_call", "id": "ctc_1", "call_id": "xs_01", "name": "x_keyword_search",
             "input": json.dumps({"query": "$BTC min_faves:2 -filter:replies within_time:2h", "mode": "Latest",
                                  "limit": 8}), "status": "completed"},
            {"type": "custom_tool_call", "id": "ctc_2", "call_id": "xs_02", "name": "x_keyword_search",
             "input": json.dumps({"query": "(crypto OR bitcoin) min_faves:50 within_time:1h", "mode": "Top"}),
             "status": "completed"},
            {"type": "message", "id": "msg_5808284d", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": text, "logprobs": [], "annotations": []}]},
        ],
        "reasoning": {"effort": "low", "summary": "detailed"},
        "text": {"format": {"type": "json_schema"}},
        "usage": usage,
        "citations": citations,
    }


class Pool:
    """agent_runs as xai.py and xsearch.py read and write it."""

    def __init__(self, today=0, week=0, spent=0.0, seen=None):
        self.today, self.week, self.spent, self.seen, self.rows, self.updates = today, week, spent, seen, [], []

    async def execute(self, sql, *a):
        assert sql.startswith("UPDATE agent_runs SET ok = FALSE"), sql
        self.updates.append(a)

    async def fetchval(self, sql, *a):
        if "INSERT INTO agent_runs" in sql:
            self.rows.append(dict(zip(("agent", "model", "latency_ms", "input_tokens", "output_tokens", "cost_usd",
                                       "ok", "error", "input_hash", "output"), a, strict=True)))
            return len(self.rows)
        if "xai:%" in sql:
            return self.spent
        if "grok:%" in sql:
            return self.seen
        if "interval '7 days'" in sql:
            return self.week
        if "date_trunc('day'" in sql:
            return self.today
        raise AssertionError(sql)


@pytest.fixture
def api(monkeypatch):
    """The API lane with a key, a stub pool and a stub transport: (pool, requests seen, set the answer)."""
    pool, seen = Pool(), []
    answer = {"r": (200, {})}

    async def get_pool():
        return pool

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        code, payload = answer["r"]
        return httpx.Response(code, text=payload) if isinstance(payload, str) else httpx.Response(code, json=payload)
    stub = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(db, "pool", get_pool)
    monkeypatch.setattr(xai, "client", lambda: stub)
    monkeypatch.setattr(settings, "xai_api_key", KEY)
    monkeypatch.setattr(settings, "xai_model", None)
    monkeypatch.setattr(settings, "xai_daily_usd", 3.0)
    monkeypatch.setattr(settings, "x_search_via", "auto")

    def respond(code, payload):
        answer["r"] = (code, payload)
    return pool, seen, respond


def pulse_answer(posts: list[str]) -> dict:
    read = {"scope": "BTC", "summary": "ETF flows lead the talk", "why_it_matters": "", "notable": False,
            "posts_reviewed": 8, "sentiment_score": 2, "post_volume": "normal", "crowding": "none",
            "narratives": ["ETF inflows"], "sample_posts": [{"author": "@a", "text": "t", "url": u} for u in posts]}
    return {"market_score": 1, "market_mood": "calm", "headlines": [
        {"text": h, "kind": "flow", "coins": ["BTC"], "url": u} for h, u in zip(("real", "made up"), posts)],
        "trending": [], "trenches": [], "memecoin_narratives": [], "reads": [read]}


def call(schema=sp.Pulse, **kw):
    return asyncio.run(xai.x_structured("social_pulse", schema, "Run these searches.", system=sp.SYSTEM,
                                        max_calls_per_day=36, **kw))


# ─── the request ─────────────────────────────────────────────────────────────
def test_the_request_is_the_responses_api_with_x_search_and_the_schema(api):
    pool, seen, respond = api
    real = status(NOW - 20 * M)
    respond(200, body(pulse_answer([real]), [real]))
    call(from_date=date(2026, 10, 26), effort="medium")
    (req,) = seen
    assert (req.method, str(req.url)) == ("POST", "https://api.x.ai/v1/responses")
    assert req.headers["authorization"] == f"Bearer {KEY}"
    b = json.loads(req.content)
    assert b["model"] == "grok-4.3" and b["tools"] == [{"type": "x_search", "from_date": "2026-10-26"}]
    assert b["input"] == [{"role": "system", "content": sp.SYSTEM}, {"role": "user", "content": "Run these searches."}]
    fmt = b["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["name"] == "Pulse" and fmt["strict"] is True
    assert fmt["schema"] == sp.Pulse.model_json_schema()
    assert b["reasoning"] == {"effort": "medium"} and b["max_turns"] == xai.MAX_TURNS
    assert b["include"] == ["no_inline_citations"] and b["store"] is False


def test_the_model_comes_from_settings_when_set(api, monkeypatch):
    pool, seen, respond = api
    monkeypatch.setattr(settings, "xai_model", "grok-4.7")
    real = status(NOW - 20 * M)
    respond(200, body(pulse_answer([real]), [real]))
    call()
    assert json.loads(seen[0].content)["model"] == "grok-4.7" and pool.rows[0]["model"] == "xai:grok-4.7"
    assert "from_date" not in json.loads(seen[0].content)["tools"][0]          # no date range unless asked


# ─── the response ────────────────────────────────────────────────────────────
def test_the_answer_its_tokens_and_its_real_cost_go_to_agent_runs(api):
    pool, seen, respond = api
    real = status(NOW - 20 * M)
    respond(200, body(pulse_answer([real]), [real, "https://x.ai/news"]))
    pulse, run_id = call()
    assert isinstance(pulse, sp.Pulse) and pulse.reads[0].sample_posts[0].url == real and run_id == 1
    (row,) = pool.rows
    assert row["agent"] == "social_pulse" and row["model"] == "xai:grok-4.3" and row["ok"] is True
    assert row["input_tokens"] == 18_204 and row["output_tokens"] == 1_406 + 2_210       # reasoning is billed output
    assert row["cost_usd"] == pytest.approx(0.287125)                                  # 2,871,250,000 ticks
    out = json.loads(row["output"])
    assert out["x_posts_fetched"] == 44 and out["x_search_calls"] == 3 and out["cost_reported"] is True
    assert out["uncited"] == [] and out["result"]["market_mood"] == "calm" and out["raw_tail"] is None
    assert "subscription_equiv_cost_usd" not in out           # the API's spend is cost_usd only (xsearch.spend_sql)


def test_a_cost_the_response_leaves_out_is_estimated_high():
    u = xai.usage(body("{}", [], ticks=None, posts=100))
    assert u["cost_reported"] is False
    assert u["cost_usd"] == pytest.approx(100 * 0.005 + 18_204 / 1e6 * 2 + 3_616 / 1e6 * 6)
    assert xai.usage({"usage": {"cost_in_nano_usd": 37_756_000}})["cost_usd"] == pytest.approx(0.037756)


def test_citations_come_from_the_list_and_the_annotations():
    b = body("{}", ["https://x.com/i/status/1975607901571199086", "https://x.ai/news",
                    "https://twitter.com/someone/status/1234567890123456789?s=20",
                    "https://x.com/i/web/status/1975607901571199999", "https://x.com/someone"])
    b["output"][-1]["content"][0]["annotations"] = [
        {"type": "url_citation", "url": "https://x.com/xai/status/1991284813727474073", "start_index": 0,
         "end_index": 9, "title": "1"}]
    assert xai.cited(b) == {1975607901571199086, 1234567890123456789, 1991284813727474073, 1975607901571199999}


# ─── the evidence gate on API answers ────────────────────────────────────────
def test_a_post_the_search_never_returned_is_dropped_by_social_pulse_s_gate(api):
    """Grok can skip a search and write a plausible post: recent, real-shaped, so the snowflake check alone passes it.
    Only the posts in the response's citations reach the gate."""
    pool, seen, respond = api
    real, fake = status(NOW - 20 * M, "desk"), status(NOW - 15 * M, "ghost", low=99)
    assert sp.fresh_url(fake, NOW)                            # shaped right and recent: the gate alone would keep it
    respond(200, body(pulse_answer([real, fake]), [real.replace("/desk/", "/i/")]))
    pulse, _ = call()
    reads, heads, _ = sp.grounded(pulse, {"BTC"}, datetime.now(UTC))
    assert [h.text for h in heads] == ["real"]
    assert [p.url for p in reads[0].sample_posts] == [real]
    assert json.loads(pool.rows[0]["output"])["uncited"] == [fake, fake]                # the headline and the sample


def test_the_x_scout_s_gate_works_on_an_api_answer():
    """The recorded 4-token X Scout answer, as the API would return it, with one token's posts missing from the
    citations: that token is ungrounded; the other three are read exactly as on the CLI."""
    d = json.loads((FIX / "scout_4_tokens_medium.json").read_text())
    now = datetime.fromisoformat(d["now"])
    for t in d["tokens"]:
        for k in ("created_at", "first_call", "last_read"):
            if t.get(k):
                t[k] = datetime.fromisoformat(t[k])
    raw, _ = cli._parse_grok(json.dumps(d["envelope"]))
    scout = x.Scout.model_validate_json(raw)
    urls = [p.url for r in scout.reads for p in r.posts] + [n.url for r in scout.reads for n in r.notable]
    t2 = {p.url for r in scout.reads if r.token == "T2" for p in r.posts}
    returned = [u for u in urls if u not in t2]
    via_api = x.Scout.model_validate_json(raw)
    blanked = xai.uncited(via_api, xai.cited({"citations": returned}))
    assert set(blanked) >= t2 and not set(blanked) & set(returned)
    finished = now + timedelta(seconds=d["latency_s"])
    on_cli = {r["symbol"]: r for r in x.judge(scout, d["tokens"], d["searches"], now, finished)}
    on_api = {r["symbol"]: r for r in x.judge(via_api, d["tokens"], d["searches"], now, finished)}
    t2_symbol = d["tokens"][1]["symbol"]
    assert on_cli[t2_symbol]["status"] == "read" and on_api[t2_symbol]["status"] == "ungrounded"
    for sym, r in on_cli.items():
        if sym != t2_symbol:
            assert on_api[sym]["status"] == r["status"] and on_api[sym]["posts"] == r["posts"]


def test_no_citations_at_all_means_nothing_is_grounded(api):
    pool, seen, respond = api
    real = status(NOW - 20 * M)
    respond(200, body(pulse_answer([real]), []))
    pulse, _ = call()
    assert sp.grounded(pulse, {"BTC"}, datetime.now(UTC)) == ([], [], [])


def test_social_pulse_runs_end_to_end_on_the_api_lane(api, monkeypatch):
    """run() with the key set: the call goes to the API with yesterday as the search's first day, a grounded answer is
    stored, and an answer whose posts the search never returned is discarded twice and stores nothing."""
    pool, seen, respond = api
    written = []

    async def pick(limit, now):
        return []

    async def spent(hours):
        return 0.0

    async def upsert(table, cols, rows, keys):
        written.append((table, rows))
        return len(rows)

    async def signals(reads, now, run_id):
        return None
    monkeypatch.setattr(sp, "pick", pick)
    monkeypatch.setattr(sp, "spent", spent)
    monkeypatch.setattr(sp.db, "upsert", upsert)
    monkeypatch.setattr(sp, "_as_signals", signals)
    assert xsearch.x_lane() == "api"
    real = status(datetime.now(UTC) - 20 * M)
    respond(200, body(pulse_answer([real]), [real]))
    assert asyncio.run(sp.run()) == 1
    (table, rows), = written
    assert table == "social_snapshots" and rows[0][1] == sp.MARKET and rows[0][2] == "grok_x"
    assert json.loads(rows[0][5])["headlines"][0]["url"] == real
    tool = json.loads(seen[0].content)["tools"][0]
    assert tool["from_date"] == (datetime.now(UTC) - timedelta(days=1)).date().isoformat()
    fake = status(datetime.now(UTC) - 10 * M, "ghost", low=99)
    respond(200, body(pulse_answer([fake]), [real]))
    with pytest.raises(RuntimeError, match="two attempts not grounded"):
        asyncio.run(sp.run())
    assert len(written) == 1 and len(pool.updates) == 2 and "ungrounded" in pool.updates[0][1]


# ─── the caps ────────────────────────────────────────────────────────────────
def test_the_caps_refuse_before_any_request(api, monkeypatch):
    pool, seen, respond = api
    pool.today = 36
    with pytest.raises(cli.CallLimitExceeded, match="36/36 calls today"):
        call()
    pool.today, pool.week = 3, 100
    with pytest.raises(cli.CallLimitExceeded, match="100/100 calls in the last 7 days"):
        call(max_calls_per_7d=100)
    pool.week, pool.spent = 0, 3.0
    with pytest.raises(cli.CallLimitExceeded, match=r"cost \$3.00 today, the daily cap is \$3.00"):
        call()
    pool.spent = 0.0
    monkeypatch.setattr(settings, "xai_api_key", None)
    with pytest.raises(cli.CallLimitExceeded, match="no xAI API key"):
        call()
    monkeypatch.setattr(settings, "xai_api_key", KEY)

    async def paused(agent):
        return True
    monkeypatch.setattr(xai.controls, "paused", paused)
    with pytest.raises(cli.CallLimitExceeded, match="paused"):
        call()
    assert seen == [] and pool.rows == []
    assert xai.CallLimitExceeded is cli.CallLimitExceeded            # the type the callers already catch


def test_the_daily_cap_counts_every_xai_run_and_only_those(api):
    pool, seen, respond = api
    pool.spent = 2.99
    real = status(NOW - 20 * M)
    respond(200, body(pulse_answer([real]), [real]))
    call()                                                    # under the cap: one more call may run
    assert len(seen) == 1
    sql = inspect.getsource(xai.spent_today)
    assert "model LIKE 'xai:%'" in sql and "agent =" not in sql


# ─── failures, and the key never in a log ────────────────────────────────────
def test_a_rejected_call_is_recorded_without_the_key(api, caplog):
    pool, seen, respond = api
    respond(401, {"error": f"Incorrect API key provided: {KEY}. Get one at console.x.ai"})
    with caplog.at_level(logging.DEBUG), pytest.raises(RuntimeError) as e:
        call()
    (row,) = pool.rows
    assert row["ok"] is False and row["error"].startswith("HTTP 401: Incorrect API key provided: [key]")
    assert KEY not in row["error"] and KEY not in str(e.value) and KEY not in caplog.text
    assert "TESTKEY" not in row["output"]


def test_an_incomplete_or_unparseable_answer_fails_with_its_cost_kept(api):
    pool, seen, respond = api
    respond(200, body('{"market_score": 1, "market_mo', [], status_="incomplete"))
    with pytest.raises(RuntimeError, match="response incomplete"):
        call()
    assert pool.rows[-1]["cost_usd"] == pytest.approx(0.287125) and pool.rows[-1]["ok"] is False
    respond(200, body("not json at all", []))
    with pytest.raises(RuntimeError, match="ValidationError"):
        call()
    assert json.loads(pool.rows[-1]["output"])["raw_tail"] == "not json at all"


def test_a_network_failure_is_a_runtime_error(api, monkeypatch):
    pool, seen, respond = api

    def down(request):
        raise httpx.ConnectError("connection refused", request=request)
    monkeypatch.setattr(xai, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(down)))
    with pytest.raises(RuntimeError, match="ConnectError"):
        call()
    assert pool.rows[0]["cost_usd"] is None and KEY not in pool.rows[0]["error"]


# ─── the Setup page's Test button ────────────────────────────────────────────
def check(monkeypatch, code, payload=None, exc=None):
    seen = []

    def handler(request):
        seen.append(request)
        if exc:
            raise exc("down", request=request)
        return httpx.Response(code, json=payload)
    monkeypatch.setattr(xai, "client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    return asyncio.run(xai.check_key(KEY)), seen


def test_check_key(monkeypatch):
    record = {"redacted_api_key": "xai-...abcd", "name": "Albatross box", "acls": ["api-key:model:*",
              "api-key:endpoint:*"], "team_blocked": False, "api_key_blocked": False, "api_key_disabled": False}
    (ok, msg), seen = check(monkeypatch, 200, record)
    assert (ok, msg) == (True, "Key works (name: Albatross box).")
    assert (seen[0].method, str(seen[0].url)) == ("GET", "https://api.x.ai/v1/api-key")
    assert seen[0].headers["authorization"] == f"Bearer {KEY}"
    assert check(monkeypatch, 200, {**record, "name": ""})[0] == (True, "Key works.")
    assert check(monkeypatch, 401, {"error": "bad key"})[0] == (False, "xAI rejected this key.")
    assert check(monkeypatch, 200, {**record, "api_key_disabled": True})[0][0] is False
    assert check(monkeypatch, 200, {**record, "team_blocked": True})[0][0] is False
    assert check(monkeypatch, 429)[0][0] is False and "Try again" in check(monkeypatch, 503)[0][1]
    (ok, msg), _ = check(monkeypatch, 0, exc=httpx.ConnectError)
    assert not ok and msg.startswith("Couldn't reach api.x.ai")
    assert asyncio.run(xai.check_key("  ")) == (False, "Paste a key first.")


# ─── the lane ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("via,key,grok,lane", [
    ("api", True, True, "api"), ("api", False, True, "off"), ("api", True, False, "api"),
    ("cli", True, True, "cli"), ("cli", False, True, "cli"), ("cli", True, False, "off"),
    ("auto", True, True, "api"), ("auto", True, False, "api"), ("auto", False, True, "cli"),
    ("auto", False, False, "off"),
    (" API ", True, False, "api"), ("nonsense", False, True, "cli"), ("", False, False, "off"),
])
def test_the_lane_for_each_setting(monkeypatch, via, key, grok, lane):
    monkeypatch.setattr(settings, "x_search_via", via)
    monkeypatch.setattr(settings, "xai_api_key", KEY if key else "  ")
    monkeypatch.setattr(xsearch, "grok_installed", lambda: grok)
    assert xsearch.x_lane() == lane


def test_the_grok_binary_is_the_one_cli_runs(monkeypatch, tmp_path):
    exe = tmp_path / "grok"
    monkeypatch.setattr(cli, "GROK", str(exe))
    assert not xsearch.grok_installed()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert xsearch.grok_installed()


def lane(monkeypatch, name):
    monkeypatch.setattr(xsearch, "x_lane", lambda: name)


def test_the_dispatcher_calls_the_chosen_lane(monkeypatch):
    calls = []

    async def api_call(agent, schema, prompt, **kw):
        calls.append(("api", kw))
        return "answer", 1

    async def cli_call(agent, schema, prompt, **kw):
        calls.append(("cli", kw))
        return "answer", 2
    monkeypatch.setattr(xai, "x_structured", api_call)
    monkeypatch.setattr(cli, "grok_structured", cli_call)
    kw = dict(system=sp.SYSTEM, max_calls_per_day=36, timeout=300, grok_args=sp.GROK_ARGS, from_date=date(2026, 10, 26))
    lane(monkeypatch, "api")
    assert asyncio.run(xsearch.structured("social_pulse", sp.Pulse, "p", **kw)) == ("answer", 1)
    assert calls[-1][1]["system"] == sp.SYSTEM and calls[-1][1]["from_date"] == date(2026, 10, 26)
    assert calls[-1][1]["effort"] == xai.EFFORT and "extra_args" not in calls[-1][1]
    lane(monkeypatch, "cli")
    assert asyncio.run(xsearch.structured("social_pulse", sp.Pulse, "p", **kw)) == ("answer", 2)
    assert calls[-1][1]["extra_args"] == sp.GROK_ARGS and "system" not in calls[-1][1]
    lane(monkeypatch, "off")
    with pytest.raises(cli.CallLimitExceeded, match="X search is off"):
        asyncio.run(xsearch.structured("social_pulse", sp.Pulse, "p", **kw))
    assert len(calls) == 2


# ─── which process runs the X jobs ───────────────────────────────────────────
def test_a_job_runs_only_in_the_process_that_owns_its_lane(monkeypatch):
    ran = []

    async def social_pulse():
        ran.append(1)
        return 5
    in_ingest, on_host = xsearch.only_in("api", social_pulse), xsearch.only_in("cli", social_pulse)
    assert in_ingest.__name__ == on_host.__name__ == "social_pulse"          # APScheduler's name for it
    lane(monkeypatch, "cli")
    assert asyncio.run(in_ingest()) == 0 and asyncio.run(on_host()) == 5 and ran == [1]
    lane(monkeypatch, "api")
    assert asyncio.run(in_ingest()) == 5 and asyncio.run(on_host()) == 0 and ran == [1, 1]
    lane(monkeypatch, "off")
    assert asyncio.run(in_ingest()) == 0 and asyncio.run(on_host()) == 0 and ran == [1, 1]


def test_both_processes_schedule_the_x_jobs_behind_their_lane():
    run, host = inspect.getsource(main.run), inspect.getsource(main.host)
    assert 'add(xsearch.only_in("api", jobs["social_pulse"]), "cron", minute=10)' in run
    assert 'xsearch.only_in("api", jobs[name])' in run and "deep.HOST_SCHEDULE" in run
    assert 'sched.add_job(xsearch.only_in("cli", jobs["social_pulse"]), "cron", minute=10)' in host
    assert 'xsearch.only_in("cli", jobs[name])' in host
    assert xsearch.X_JOBS == {sp.AGENT, *x.HOST_JOBS} and set(xsearch.X_AGENTS) == {sp.AGENT, x.AGENT}
    assert xsearch.X_JOBS <= set(main.jobs) and all(j in rules.CADENCE for j in xsearch.X_JOBS)


def test_a_skipped_run_leaves_no_trace_in_job_health(monkeypatch):
    """The guard sits outside tracked(): the process that doesn't own the lane writes no ingest_health or job_runs row,
    so a dead host worker still shows as late while the CLI is the lane."""
    async def no_pool():
        raise AssertionError("the skipped run touched the database")
    monkeypatch.setattr(db, "pool", no_pool)
    lane(monkeypatch, "cli")
    assert asyncio.run(xsearch.only_in("api", main.jobs["social_pulse"])()) == 0


# ─── pacing in the lane's dollars ────────────────────────────────────────────
def test_on_the_api_each_agent_keeps_its_share_of_the_daily_cap(monkeypatch):
    monkeypatch.setattr(settings, "xai_daily_usd", 3.0)
    monkeypatch.setattr(settings, "social_weekly_usd", 25.0)
    monkeypatch.setattr(xsearch, "pool_week", lambda: 40.0)
    monkeypatch.setattr(x, "env_float", lambda name, default: default)        # DEEP_X_WEEKLY_USD unset: $15
    lane(monkeypatch, "cli")
    assert (x.social_usd(), x.weekly_usd(), x.pool_usd()) == (25.0, 15.0, 40.0)
    assert "subscription_equiv_cost_usd" in xsearch.spend_sql()
    lane(monkeypatch, "api")
    assert x.pool_usd() == pytest.approx(21.0)                                  # $3 a day
    assert x.social_usd() / 7 == pytest.approx(1.875) and x.weekly_usd() / 7 == pytest.approx(1.125)
    assert xsearch.spend_sql() == "cost_usd"
    # at the API's pace Social Pulse still reads the pulse, and coins as the day's share allows
    assert sp.coins_affordable(0.0, 0.0, x.social_usd()) >= 0
    assert sp.coins_affordable(1.5 * 1.875, 6 * 1.875, x.social_usd()) == -1


# ─── off: no alerts, nothing late on Health ──────────────────────────────────
def test_the_lane_as_the_containers_see_it(monkeypatch):
    now = NOW
    for via in ("auto", "cli"):
        monkeypatch.setattr(settings, "x_search_via", via)
        assert xsearch.seen_lane("off", now - 2 * H, now) == "cli"             # the host worker is at work
        assert xsearch.seen_lane("off", now - 8 * 24 * H, now) == "off"        # gone for over a week
        assert xsearch.seen_lane("off", None, now) == "off"
    monkeypatch.setattr(settings, "x_search_via", "api")
    assert xsearch.seen_lane("off", now - 2 * H, now) == "off"                 # the API chosen, with no key
    assert xsearch.seen_lane("api", None, now) == "api" and xsearch.seen_lane("cli", None, now) == "cli"


class HealthPool:
    """What the alerts job and /api/health read, with the X jobs long stale."""

    def __init__(self, seen=None):
        self.seen = seen
        self.health = [{"job": j, "last_run": NOW - 5 * H, "last_ok": NOW - 5 * H, "consecutive_errors": 0,
                        "last_error": None, "last_rows": 0} for j in sorted(xsearch.X_JOBS)]

    async def fetch(self, sql, *a):
        return self.health if "FROM ingest_health" in sql else []

    async def fetchval(self, sql, *a):
        return self.seen if "grok:%" in sql else 0

    async def fetchrow(self, sql, *a):
        return None


def use(monkeypatch, pool):
    async def get_pool():
        return pool
    monkeypatch.setattr(db, "pool", get_pool)


def test_no_alerts_for_the_x_jobs_while_no_lane_is_on(monkeypatch):
    async def ledger():
        return 0, None

    async def no_agents(hours):
        return []
    monkeypatch.setattr(monitor, "_ledger_state", ledger)
    monkeypatch.setattr(monitor.tracker, "agent_stats", no_agents)
    monkeypatch.setattr(settings, "x_search_via", "auto")
    lane(monkeypatch, "off")
    use(monkeypatch, HealthPool(seen=None))
    assert asyncio.run(monitor.evaluate(NOW)) == []
    use(monkeypatch, HealthPool(seen=NOW - 6 * H))                 # the host worker ran Grok, then stopped
    stale = {c.key for c in asyncio.run(monitor.evaluate(NOW))}
    assert {"job_stale:social_pulse", "job_stale:deep_x_scan", "job_stale:deep_x_ask"} <= stale
    lane(monkeypatch, "api")
    assert "job_stale:social_pulse" in {c.key for c in asyncio.run(monitor.evaluate(NOW))}


def test_health_shows_the_x_jobs_as_paused_while_no_lane_is_on(monkeypatch):
    from albatross.api import app as api_app

    async def counts():
        return {}

    async def no_verdict():
        return None
    monkeypatch.setattr(api_app, "_health_counts", counts)
    monkeypatch.setattr(api_app, "_fomo_verdict", no_verdict)
    api_app._cache.clear()
    monkeypatch.setattr(settings, "x_search_via", "auto")
    lane(monkeypatch, "off")
    use(monkeypatch, HealthPool(seen=None))
    assert {j["state"] for j in asyncio.run(api_app.health())["jobs"]} == {"paused"}
    lane(monkeypatch, "api")
    assert {j["state"] for j in asyncio.run(api_app.health())["jobs"]} == {"late"}


def test_the_openrouter_spend_alert_leaves_out_the_xai_api():
    assert "model NOT LIKE 'xai:%'" in inspect.getsource(monitor.evaluate)


# ─── the Setup status route ──────────────────────────────────────────────────
def client_for(monkeypatch, pool):
    use(monkeypatch, pool)
    app = FastAPI()
    app.include_router(xsetup.router)
    return TestClient(app, base_url="http://localhost:8787")


def test_the_status_route(monkeypatch):
    monkeypatch.setattr(settings, "site_password_hash", None)
    monkeypatch.setattr(settings, "xai_daily_usd", 3.0)
    monkeypatch.setattr(settings, "xai_model", None)
    seen = datetime(2026, 10, 27, 9, 10, tzinfo=UTC)
    monkeypatch.setattr(settings, "x_search_via", "auto")
    monkeypatch.setattr(settings, "xai_api_key", KEY)
    r = client_for(monkeypatch, Pool(spent=0.41234, seen=seen)).get("/api/setup/x")
    assert r.status_code == 200 and KEY not in r.text
    assert r.json() == {"lane": "api", "via": "auto",
                        "api": {"key_set": True, "spent_today_usd": 0.4123, "cap_usd": 3.0, "model": "grok-4.3"},
                        "cli": {"host_worker_seen_at": "2026-10-27T09:10:00+00:00"}}
    monkeypatch.setattr(settings, "xai_api_key", None)
    monkeypatch.setattr(xsearch, "grok_installed", lambda: False)             # the dashboard container
    recent = datetime.now(UTC) - H
    d = client_for(monkeypatch, Pool(seen=recent)).get("/api/setup/x").json()
    assert d["lane"] == "cli" and d["api"]["key_set"] is False and d["cli"]["host_worker_seen_at"] == recent.isoformat()
    d = client_for(monkeypatch, Pool(seen=None)).get("/api/setup/x").json()
    assert d["lane"] == "off" and d["cli"]["host_worker_seen_at"] is None


def test_the_status_route_is_behind_the_setup_guard(monkeypatch):
    monkeypatch.setattr(settings, "site_password_hash", None)
    c = client_for(monkeypatch, Pool())
    c.base_url = "http://evil.example"
    assert c.get("/api/setup/x").status_code == 403


def test_the_agents_page_names_the_api_model():
    assert ag.model_of("xai:grok-4.3", graph.MODEL) == {"group": "api", "name": "Grok 4.3 (xAI API)",
                                                         "slug": "xai:grok-4.3"}
    assert ag.model_of("grok:default", graph.MODEL)["name"] == "Grok (CLI)"
