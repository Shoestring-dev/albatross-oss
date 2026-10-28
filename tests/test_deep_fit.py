"""Culture fit (albatross/deepend/fit.py): input assembly, the evidence gate, triggers and budget, on outputs recorded
from deepend_stage and OpenRouter on 2026-09-30 (tests/fixtures/deep_fit)."""

import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from albatross import db
from albatross.config import settings
from albatross.deepend import chain as chain_intel
from albatross.deepend import fit, xintel
from albatross.llm import openrouter
from albatross.venues import dexscreener

FIX = Path(__file__).parent / "fixtures" / "deep_fit"
NOW = datetime(2026, 9, 30, 17, 30, tzinfo=UTC)
SI = ("solana", "9aqmJjCnnMQv42TXLk921ceUkN35nea2QP969n1caqjj")
CONTRACT = {"status", "as_of", "score", "label", "summary", "meme", "meta", "audience", "originality", "risks",
            "citations"}

@pytest.fixture(autouse=True)
def no_plan_use(monkeypatch):
    """The job paces on its use of the ChatGPT plan too (llm/codex.py); these tests' fake pools don't answer it."""
    from albatross.llm import codex

    async def none(agent):
        return 0.0
    monkeypatch.setattr(codex, "plan_usd_today", none)



def load(name: str):
    return json.loads((FIX / name).read_text())


def _dates(r: dict) -> dict:
    return {k: datetime.fromisoformat(v) if k in ("created_at", "first_seen", "first_call", "first_at", "ts") and v
            else v for k, v in r.items()}


def si_rows() -> dict:
    """The DB rows behind SI's inputs as asyncpg returns them (JSONB as text)."""
    rec = load("si_rows.json")
    return {"token": _dates(rec["token"]), "same_name": [_dates(r) for r in rec["same_name"]],
            "calls": [_dates(r) for r in rec["calls"]],
            "pulse": {"ts": datetime.fromisoformat(rec["pulse"]["ts"]), "narratives": json.dumps(rec["pulse"]["narratives"])}}


PROFILE = {"name": "Super Intelligence", "symbol": "SI", "description": "it's not AI, it's SI", "image": None,
           "links": {"x": "https://x.com/i/communities/1"}, "nsfw": False, "sources": ["dexscreener", "pump.fun"]}


class Pool:
    """Answers fit.py's queries from recorded rows; records agent_runs inserts."""

    def __init__(self, rec: dict | None = None, *, used: int = 0, latest: dict | None = None,
                 researched_at: datetime | None = None):
        self.rec, self.used, self.latest, self.researched_at = rec, used, latest, researched_at
        self.runs: list[tuple] = []

    async def fetchrow(self, sql, *args):
        if sql is fit.TOKEN_SQL:
            return self.rec and self.rec["token"]
        if sql is fit.PULSE_SQL:
            return self.rec and self.rec["pulse"]
        if sql is fit.LATEST_SQL:
            return self.latest
        raise AssertionError(sql)

    async def fetch(self, sql, *args):
        if sql is fit.SAME_NAME_SQL:
            return self.rec["same_name"]
        if sql is fit.CALLS_SQL:
            return self.rec["calls"]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "INSERT INTO agent_runs" in sql:
            self.runs.append(args)
            return len(self.runs)
        if "count(*) FROM agent_runs" in sql:
            return self.used
        if "max(researched_at)" in sql:
            return self.researched_at
        raise AssertionError(sql)


@pytest.fixture
def env(monkeypatch):
    """A fake database and OpenRouter: returns (pool, stored rows, posted bodies, replies queue)."""
    pool, stored, posted, replies = Pool(si_rows()), [], [], []

    async def fake_pool():
        return pool

    async def fake_upsert(table, cols, rows, conflict, update=None):
        stored.extend(dict(zip(cols, r)) | {"_table": table} for r in rows)
        return len(rows)

    async def fake_post(body):
        posted.append(body)
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return copy.deepcopy(r)

    async def spent(agent):
        return 0.0

    async def profile(chain, address, launchpad):
        return copy.deepcopy(PROFILE)

    async def no_image(url):
        return None

    monkeypatch.setattr(db, "pool", fake_pool)
    monkeypatch.setattr(db, "upsert", fake_upsert)
    monkeypatch.setattr(openrouter, "_post", fake_post)
    monkeypatch.setattr(openrouter, "spent_today", spent)
    monkeypatch.setattr(settings, "openrouter_api_key", "k")
    monkeypatch.setattr(fit, "profile", profile)
    monkeypatch.setattr(fit, "image_part", no_image)
    monkeypatch.setattr(fit, "_failed", {})
    return pool, stored, posted, replies


def reply_with_citations(cites: list[dict]) -> dict:
    """The recorded SI answer, with the model's citations swapped for these."""
    r = load("si_response.json")
    msg = r["choices"][0]["message"]
    report = json.loads(msg["content"].strip().removeprefix("```json").removesuffix("```"))
    msg["content"] = json.dumps(report | {"citations": cites})
    return r


# ─── inputs ──────────────────────────────────────────────────────────────────
def test_inputs_assemble_token_calls_copycats_and_meta(env):
    inp = asyncio.run(fit.inputs(*SI, NOW))
    t = inp["token"]
    assert (t["symbol"], t["name"], t["launchpad"], t["created"]) == ("SI", "Super Intelligence", "pump.fun",
                                                                      "2026-09-29 22:42")
    assert t["description"] == PROFILE["description"] and t["liquidity"] == "$225K" and t["mcap"] == "$4.08M"
    # 22 other contracts share the ticker or the name; 19 are older and 2 deeper (a different "Super Inu" and a
    # Robinhood "Shiba Inu"), so this one is neither the original nor the deepest.
    c = inp["copycats"]
    assert (c["same_ticker"], c["same_name"], c["older_contracts"], c["deeper_contracts"]) == (21, 10, 19, 2)
    assert not c["this_is_oldest"] and not c["this_is_deepest"]
    assert len(c["others"]) <= 8 and all("…" in o["address"] for o in c["others"])
    assert any(o["name"] == "Super Inu" and o["same"] == ["ticker"] for o in c["others"])     # the deepest is shown
    g = inp["group_calls"]
    assert (g["calls"], g["groups"], g["first_call"]) == (22, 2, "2026-09-29 22:52")
    assert inp["trenches_meta"]["mood"].startswith("Crypto X is mildly bullish") and inp["trenches_meta"]["age"] == "1h 18m"
    assert inp["x_read"] is None                                     # builder C's section_x is still a stub


def test_inputs_carry_a_fresh_x_read_and_drop_a_stale_pulse(env, monkeypatch):
    async def section_x(chain, address):
        return {"status": "fresh", "as_of": NOW, "mood_label": "bullish", "posts_h": 46, "summary": "CT loves SI",
                "notable": [{"author": "@a", "followers": 9, "text": "x" * 500, "stance": "bullish", "url": "u"}]}

    monkeypatch.setattr(xintel, "section_x", section_x)
    inp = asyncio.run(fit.inputs(*SI, NOW + timedelta(hours=13)))
    assert inp["x_read"]["summary"] == "CT loves SI" and len(inp["x_read"]["notable"][0]["text"]) == 240
    assert inp["trenches_meta"] is None                              # the pulse is 13 h old by then


def test_whatever_trenches_meta_the_pulse_gains_is_passed_through_capped(env):
    """Builder C adds a memecoin-meta search to the hourly pulse; its shape isn't fixed yet, so it goes through as
    text, bookkeeping keys left out."""
    pool = env[0]
    n = json.loads(pool.rec["pulse"]["narratives"]) | {"trenches": {"metas": ["ai lore", "horror"]}, "junk": "x" * 9000}
    pool.rec["pulse"] = {"ts": pool.rec["pulse"]["ts"], "narratives": json.dumps(n)}
    meta = asyncio.run(fit.inputs(*SI, NOW))["trenches_meta"]
    assert meta["trenches"].startswith('{"trenches":{"metas":["ai lore","horror"]}') and len(meta["trenches"]) == 4000
    assert "agent_run_id" not in meta["trenches"] and "coins_read" not in meta["trenches"]


def test_a_token_we_never_stored_is_named_by_its_profile_or_skipped(env, monkeypatch):
    pool = env[0]
    pool.rec = dict(si_rows(), token=None)
    inp = asyncio.run(fit.inputs(*SI, NOW))
    assert inp["token"]["symbol"] == "SI" and inp["token"]["created"] is None

    async def nameless(chain, address, launchpad):
        return dict(PROFILE, name=None, symbol=None)

    monkeypatch.setattr(fit, "profile", nameless)
    assert asyncio.run(fit.inputs(*SI, NOW)) is None


def test_profile_reads_dexscreener_pumpfun_and_jupiter(monkeypatch):
    """Saw's recorded DexScreener pair and pump.fun record; builder A's Jupiter intel when it exists."""
    async def tokens(chain, addresses):
        return load("saw_dexscreener.json"), NOW

    class Client:
        async def get(self, url, timeout=None):
            assert url == fit.PUMP_URL.format(saw)
            return httpx.Response(200, json=load("saw_pumpfun.json"), request=httpx.Request("GET", url))

    saw = "9twiuSdTVkwtAC9XQDG57dFRhF4iqPih461HJfMZKci9"
    monkeypatch.setattr(dexscreener, "tokens", tokens)
    monkeypatch.setattr(fit, "client", lambda: Client())
    p = asyncio.run(fit.profile("solana", saw, "pump.fun"))
    assert (p["name"], p["symbol"], p["description"], p["nsfw"]) == ("The Torture Chamber", "Saw", "memecoin", False)
    assert p["links"] == {"website": "https://security-blog-kohl.vercel.app/blog/saw-test/",
                          "x": "https://x.com/poster_907/status/2105251378752308872"}
    assert p["image"].startswith("https://cdn.dexscreener.com/cms/images/") and p["sources"] == ["dexscreener", "pump.fun"]

    async def jupiter_intel(chain, address):
        return {"name": "The Torture Chamber", "symbol": "Saw", "icon": "https://static.jup.ag/saw.png",
                "twitter": "https://x.com/sawonsol", "website": "https://saw.fun"}

    monkeypatch.setattr(chain_intel, "jupiter_intel", jupiter_intel, raising=False)
    p = asyncio.run(fit.profile("solana", saw, "pump.fun"))
    assert p["image"] == "https://static.jup.ag/saw.png" and p["links"]["x"] == "https://x.com/sawonsol"
    assert p["links"]["other"] == ["https://security-blog-kohl.vercel.app/blog/saw-test/",
                                   "https://x.com/poster_907/status/2105251378752308872"]
    assert p["sources"] == ["jupiter", "dexscreener", "pump.fun"]


def test_images_come_only_from_known_https_hosts():
    ok = ["https://cdn.dexscreener.com/cms/images/x?width=800", "https://ipfs.io/ipfs/Qm1",
          "https://storage.googleapis.com/extraction_images/a.png", "https://gateway.mypinata.cloud/ipfs/Qm"]
    bad = [None, "", "http://cdn.dexscreener.com/x", "https://169.254.169.254/latest/meta-data",
           "https://evil.example/cdn.dexscreener.com", "file:///etc/passwd", "https://cdn.dexscreener.com.evil.io/x"]
    assert all(fit.image_ok(u) for u in ok) and not any(fit.image_ok(u) for u in bad)


# ─── the evidence gate ───────────────────────────────────────────────────────
def test_gate_keeps_only_urls_the_search_returned():
    seen = fit.evidence(load("si_response.json"))
    assert len(seen) == 5
    cites = [fit.Citation(title="", url="https://news.example.com/ai-name-change-sparks-insider-domain-trading-claims"),
             fit.Citation(title="t", url="https://headlines.example.com/260929/p44?ref=feed.example.com&utm_source=x#top"),
             fit.Citation(title="dupe", url="http://news.example.com/ai-name-change-sparks-insider-domain-trading-claims/"),
             fit.Citation(title="from memory", url="https://www.whitehouse.gov/presidential-actions/si/"),
             fit.Citation(title="not a url", url="news.example.com")]
    kept, dropped = fit.gate(cites, seen)
    assert [c.url for c in kept] == [cites[0].url, cites[1].url]
    assert kept[0].title == "AI name change sparks insider domain trading claims"      # filled from the search
    assert dropped == [cites[2].url, cites[3].url, "news.example.com"]


def test_google_search_answers_parse_and_gate_too():
    """Gemini's own Google search: fenced JSON and grounding-redirect links, which the model cites as it saw them."""
    r = load("saw_native_response.json")
    rep = fit.parse(r["choices"][0]["message"]["content"])
    assert rep.score == 6 and rep.reference_status == "live" and rep.metas == ["ai", "horror", "tech"]
    kept, dropped = fit.gate(rep.citations, fit.evidence(r))
    assert len(kept) == 1 and not dropped and "grounding-api-redirect" in kept[0].url


@pytest.mark.parametrize("raw", [None, "", "no json here", '{"score": 7'])
def test_parse_refuses_what_is_not_a_report(raw):
    with pytest.raises(ValueError):
        fit.parse(raw)


# ─── research and storage ────────────────────────────────────────────────────
def test_research_stores_the_recorded_report(env):
    pool, stored, posted, replies = env
    replies.append(load("si_response.json"))
    row = asyncio.run(fit.research(*SI, ["2 groups"]))
    assert len(stored) == 1 and stored[0]["_table"] == "de_fit_reports"
    s = stored[0]
    assert (s["score"], s["label"], s["confidence"], s["model"], s["run_id"]) == (7, "good fit", "normal",
                                                                                  "google/gemini-3.8-flash", 1)
    rep = json.loads(s["report"])
    assert [c["url"] for c in json.loads(s["citations"])] == [
        "https://news.example.com/ai-name-change-sparks-insider-domain-trading-claims/",
        "https://www.headlines.example.com/260929/p44?ref=feed.example.com"]
    assert rep["dropped_citations"] == [] and rep["evidence_urls"] == 5 and rep["cost_usd"] == 0.01672
    assert rep["why"] == ["2 groups"] and rep["inputs"]["copycats"]["same_ticker"] == 21
    assert rep["reference_status"] == "live" and rep["virality"] and rep["metas"] == ["political", "ai", "news"]
    body = posted[0]
    assert body["tools"] == [fit.SEARCH] and body["model"] == fit.MODEL and "temperature" not in body
    assert "TOKEN AND CONTEXT" in body["messages"][1]["content"][0]["text"]
    run = pool.runs[0]
    assert run[0] == fit.AGENT and run[5] == pytest.approx(0.016718) and run[6] is True    # cost incl. search, ok
    assert row["confidence"] == "normal"


def test_a_report_without_surviving_citations_is_stored_as_low_confidence(env):
    pool, stored, posted, replies = env
    replies.append(reply_with_citations([{"title": "made up", "url": "https://example.com/si-coin"}]))
    asyncio.run(fit.research(*SI))
    s = stored[0]
    assert s["confidence"] == "low" and json.loads(s["citations"]) == []
    assert json.loads(s["report"])["dropped_citations"] == ["https://example.com/si-coin"]
    sec = fit.section({**s, "researched_at": NOW, "report": s["report"]}, NOW)
    assert sec["label"] == "good fit (unsourced)" and sec["citations"] == []


def test_one_retry_and_it_drops_the_image_after_a_400(env, monkeypatch):
    pool, stored, posted, replies = env

    async def image(url):
        return {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}

    monkeypatch.setattr(fit, "image_part", image)
    bad = httpx.HTTPStatusError("400", request=httpx.Request("POST", "https://x"),
                                response=httpx.Response(400, text='{"error": "image"}'))
    replies.extend([bad, load("si_response.json")])
    asyncio.run(fit.research(*SI))
    assert [p["type"] for p in posted[0]["messages"][1]["content"]] == ["text", "image_url"]
    assert [p["type"] for p in posted[1]["messages"][1]["content"]] == ["text"]
    assert [r[6] for r in pool.runs] == [False, True] and len(stored) == 1


def test_a_schema_slip_is_not_retried(env):
    pool, stored, posted, replies = env
    r = load("si_response.json")
    r["choices"][0]["message"]["content"] = '{"score": 7}'
    replies.extend([r, load("si_response.json")])
    with pytest.raises(RuntimeError, match="validation error"):
        asyncio.run(fit.research(*SI))
    assert len(posted) == 1 and not stored


# ─── budget ──────────────────────────────────────────────────────────────────
def test_no_call_past_the_daily_budget(env, monkeypatch):
    pool, stored, posted, replies = env

    async def spent(agent):
        return fit.DAILY_USD

    monkeypatch.setattr(openrouter, "spent_today", spent)
    with pytest.raises(openrouter.BudgetExceeded):
        asyncio.run(fit.research(*SI))
    assert not posted and not pool.runs
    # the dossier says why there is nothing, and research_now serves that instead of failing
    assert asyncio.run(fit.section_fit(*SI))["status"] == "budget"
    assert asyncio.run(fit.research_now(*SI))["status"] == "budget"


def test_the_job_keeps_an_even_pace_through_the_day(monkeypatch):
    monkeypatch.setattr(fit, "DAILY_USD", 3.0)
    six = NOW.replace(hour=6, minute=0)                  # a quarter of the day, plus an hour's slack: $0.875
    assert fit.paced(0.80, six) and not fit.paced(0.90, six)
    assert fit.paced(2.99, NOW.replace(hour=23, minute=30)) and not fit.paced(3.0, NOW.replace(hour=23, minute=30))
    assert fit.paced(0.12, NOW.replace(hour=0, minute=0)) and not fit.paced(0.13, NOW.replace(hour=0, minute=0))


def test_the_job_respects_pace_and_the_hourly_cap(env, monkeypatch):
    pool, stored, posted, replies = env
    done = []

    async def candidates():
        return [{"chain": "solana", "address": f"T{i}", "n_groups": 3, "liquidity_usd": 1e5, "tier": "hot",
                 "why": [], "researched_at": None} for i in range(5)]

    async def research(chain, address, why=None):
        done.append(address)
        return {"ok": True}

    monkeypatch.setattr(fit, "candidates", candidates)
    monkeypatch.setattr(fit, "research", research)
    pool.used = fit.PER_HOUR - 1
    assert asyncio.run(fit.run()) == 1 and done == ["T0"]          # one call left this hour
    pool.used = fit.PER_HOUR
    assert asyncio.run(fit.run()) == 0                             # no room: nothing more
    pool.used = 0
    assert asyncio.run(fit.run()) == fit.PER_RUN

    async def spent(agent):
        return fit.DAILY_USD                                       # ahead of any pace

    monkeypatch.setattr(openrouter, "spent_today", spent)
    assert asyncio.run(fit.run()) == 0


def test_failed_and_nameless_tokens_back_off_and_the_rest_still_run(env, monkeypatch):
    async def candidates():
        return [{"chain": "evm", "address": a, "n_groups": 2, "liquidity_usd": None, "tier": "warm", "why": [],
                 "researched_at": None} for a in ("BAD", "NAMELESS", "GOOD")]

    async def research(chain, address, why=None):
        if address == "BAD":
            raise RuntimeError("deep_fit call failed")
        return None if address == "NAMELESS" else {"ok": True}

    monkeypatch.setattr(fit, "candidates", candidates)
    monkeypatch.setattr(fit, "research", research)
    monkeypatch.setattr(fit, "PER_RUN", 3)
    assert asyncio.run(fit.run()) == 1 and set(fit._failed) == {("evm", "BAD"), ("evm", "NAMELESS")}
    assert [r["address"] for r in fit.due(asyncio.run(candidates()), datetime.now(UTC), 5)] == ["GOOD"]
    fit._failed.update({k: datetime.now(UTC) - fit.BACKOFF for k in fit._failed})
    assert len(fit.due(asyncio.run(candidates()), datetime.now(UTC), 5)) == 3          # tried again later


# ─── triggers ────────────────────────────────────────────────────────────────
def row(address, groups=0, liq=None, tier="warm", why=(), researched=None):
    return {"chain": "solana", "address": address, "n_groups": groups, "liquidity_usd": liq, "tier": tier,
            "why": list(why), "researched_at": researched}


def test_the_bar_and_the_order():
    rows = [
        row("quiet", groups=1, liq=24_000),                                   # under every bar
        row("fresh", groups=3, researched=NOW - timedelta(hours=23)),         # researched today
        row("stale", groups=5, liq=9e5, tier="hot", researched=NOW - timedelta(hours=25)),   # daily refresh
        row("deep", groups=1, liq=30_000),
        row("called", groups=2, liq=5_000),
        row("hot-called", groups=2, liq=1_000, tier="hot"),
        row("trending", groups=0, liq=2_000, why=["trending"]),
        row("most-called", groups=4, liq=100),
    ]
    got = fit.due(rows, NOW, 10)
    assert [r["address"] for r in got] == ["hot-called", "most-called", "called", "deep", "trending", "stale"]
    by = {r["address"]: r["because"] for r in got}
    assert by["deep"] == ["liquidity $30K"] and by["trending"] == ["trending"]
    assert by["stale"] == ["5 groups", "liquidity $900K", "refresh"]
    assert [r["address"] for r in fit.due(rows, NOW, 2)] == ["hot-called", "most-called"]
    assert fit.due(rows, NOW, 0) == [] and fit.due(rows, NOW, -3) == []


# ─── the section the dossier serves ─────────────────────────────────────────
def test_section_has_exactly_the_contract_shape(env):
    pool = env[0]
    assert set(fit.section(None, NOW)) == CONTRACT and fit.section(None, NOW)["status"] == "none"
    assert fit.section(None, NOW, budget_left=False)["status"] == "budget"
    rep = fit.parse(load("si_response.json")["choices"][0]["message"]["content"]).model_dump()
    stored = {"researched_at": NOW - timedelta(hours=2), "score": 7, "label": "good fit", "report": json.dumps(rep),
              "citations": json.dumps(rep["citations"]), "confidence": "normal"}
    fresh = fit.section(stored, NOW)
    assert set(fresh) == CONTRACT and fresh["status"] == "fresh" and fresh["as_of"] == stored["researched_at"]
    assert fresh["meme"] == rep["meme"] and fresh["risks"] == rep["risks"] and len(fresh["citations"]) == 2
    assert fit.section(stored, NOW + timedelta(hours=23))["status"] == "stale"
    pool.latest = stored
    assert asyncio.run(fit.section_fit(*SI)) == fresh | {"status": fit.section(stored, datetime.now(UTC))["status"]}


def test_on_demand_reuses_a_report_from_the_last_hour(env):
    pool, stored, posted, replies = env
    pool.researched_at = datetime.now(UTC) - timedelta(minutes=20)
    asyncio.run(fit.research_now(*SI))
    assert not posted
    pool.researched_at = datetime.now(UTC) - timedelta(hours=2)
    replies.append(load("si_response.json"))
    asyncio.run(fit.research_now(*SI))
    assert len(posted) == 1 and json.loads(stored[0]["report"])["why"] == ["on demand"]


def test_registry_entries():
    assert fit.JOBS == {"deep_fit_research": fit.run}
    assert fit.SCHEDULE == [("deep_fit_research", {"minute": "*/10", "second": 35})]
    assert fit.CADENCE == {"deep_fit_research": 600} and not fit.HOST_JOBS and not fit.HOST_SCHEDULE
    assert [fit.label(s) for s in (0, 2, 3, 5, 7, 9, 10)] == ["no fit", "no fit", "weak fit", "some fit", "good fit",
                                                              "strong fit", "strong fit"]
