"""The events calendar's LLM extractors (albatross/events/news_extract.py, oracle_calendar.py): their gates, and a
run of each with the model stubbed.

Fixtures (tests/fixtures/events/): news_items.json is 20 real news_items rows (pulled 2026-09-30); news_response.json
is a hand-written stand-in for the model's answer to them, with some deliberate mistakes; oracle_response.json is a
synthetic OpenRouter answer in the shape llm.openrouter.research reads, its search results as url_citation
annotations (its events are made up; converted from the Claude Code transcript the calendar used until 2026-10-01).
"""
import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from albatross import db
from albatross.events import news_extract as nx
from albatross.events import oracle_calendar as oc
from albatross.events import store
from albatross.llm import openrouter

FIX = Path(__file__).parent / "fixtures" / "events"
NOW = datetime(2026, 9, 30, 1, 0, tzinfo=UTC)
NAMES = {"ETH": "Ethereum", "NEAR": "NEAR Protocol", "W": "Wormhole", "BTC": "Bitcoin"}
ITEM = {"id": 1, "source": "cointelegraph", "url": "https://cointelegraph.com/news/glamsterdam-sepolia",
        "title": "Ethereum schedules Glamsterdam upgrade on Sepolia for Oct. 6",
        "summary": "Glamsterdam will bring proposer-builder separation, while its mainnet date remains unset.",
        "published_at": datetime(2026, 9, 29, 10, 46, tzinfo=UTC)}


def ev(**kw) -> nx.NewsEvent:
    base = dict(item=0, kind="upgrade", subject="Ethereum Glamsterdam", title="Glamsterdam on Sepolia",
                date_text="Oct. 6", date="2026-10-06", assets=["ETH"], importance=3, status="scheduled",
                quote="Ethereum schedules Glamsterdam upgrade on Sepolia for Oct. 6")
    return nx.NewsEvent(**(base | kw))


def why(e: nx.NewsEvent, item: dict = ITEM, now: datetime = NOW) -> str | None:
    return nx.gate(e, item, NAMES, now)[1]


# ─── news gate ──────────────────────────────────────────────────────────────
def test_quote_must_be_in_the_item_verbatim_up_to_spacing_case_entities_and_curly_quotes():
    assert why(ev()) is None
    assert why(ev(quote=' "ethereum SCHEDULES Glamsterdam upgrade\non Sepolia for Oct. 6." ')) is None
    item = ITEM | {"title": "Russia lets exchanges apply October 5 &#8211; but Bitcoin isn’t cleared"}
    assert why(ev(subject="Russia", quote="Russia lets exchanges apply October 5 - but Bitcoin isn't cleared",
                  date_text="October 5", date="2026-10-05"), item) is None
    assert why(ev(quote="Ethereum will activate Glamsterdam on Sepolia on Oct. 6")) == "quote not in the item"
    assert why(ev(quote="Oct. 6")) == "quote too short"


def test_date_words_must_be_inside_the_quote():
    r = why(ev(quote="Glamsterdam will bring proposer-builder separation"))
    assert r == "date words 'Oct. 6' not in the quote"
    assert why(ev(date_text="")) == "date words '' not in the quote"


def test_the_models_day_must_be_what_the_words_mean_counted_from_publication():
    item = ITEM | {"title": "Bitget resumes withdrawals", "published_at": datetime(2026, 9, 28, 11, 5, tzinfo=UTC),
                   "summary": "Ether withdrawals are scheduled to return Tuesday and USDt on Wednesday."}
    q = "Ether withdrawals are scheduled to return Tuesday and USDt on Wednesday."
    assert why(ev(subject="Bitget", quote=q, date_text="Tuesday", date="2026-09-29"), item) is None
    assert why(ev(subject="Bitget", quote=q, date_text="on Wednesday", date="2026-09-30"), item) is None
    assert why(ev(subject="Bitget", quote=q, date_text="Tuesday", date="2026-10-06"), item) == \
        "2026-10-06 is not what 'Tuesday' means (2026-09-22, 2026-09-29)"
    assert why(ev(date="Oct 6")) == "date 'Oct 6' is not YYYY-MM-DD"
    item = ITEM | {"title": "Project X ships its mainnet soon, the team says"}
    assert why(ev(subject="Project X", quote="Project X ships its mainnet soon", date_text="soon"), item) == \
        "date words 'soon' not understood"


def test_stored_date_and_precision_come_from_the_words_not_the_model():
    item = ITEM | {"title": "Monad plans its token generation event in October"}
    p, r = nx.gate(ev(subject="Monad", kind="tge", quote="Monad plans its token generation event in October",
                      date_text="in October", date="2026-10-15"), item, NAMES, NOW)
    assert r is None and (p.start, p.precision) == (date(2026, 10, 1), "month")


def test_window_keeps_two_days_back_and_half_a_year_ahead():
    item = ITEM | {"title": "Glamsterdam went live on Sepolia on Sept. 28 without issues"}
    e = ev(quote="Glamsterdam went live on Sepolia on Sept. 28", date_text="Sept. 28", date="2026-09-28",
           status="happened")
    assert why(e, item) is None
    assert why(e, item, datetime(2026, 10, 1, 1, tzinfo=UTC)) == "2026-09-28 is past"
    item = ITEM | {"title": "Ethereum targets Glamsterdam on mainnet for June 2027"}
    assert why(ev(quote="Ethereum targets Glamsterdam on mainnet for June 2027", date_text="June 2027",
                  date="2027-06-01"), item) == "2027-06-01 is beyond 180 days"


def test_subject_must_be_named_in_the_item_a_tracked_ticker_also_by_its_coin_name():
    assert why(ev(subject="Solana Alpenglow")) == "subject 'Solana Alpenglow' not named in the item"
    item = ITEM | {"title": "Coinbase will list Wormhole on Oct. 6"}
    assert why(ev(kind="listing", subject="W", venue="coinbase", quote="Coinbase will list Wormhole on Oct. 6"),
               item) is None
    assert why(ev(status="confirmed")) == "status 'confirmed'"
    assert why(ev(kind="macro", subject="Glamsterdam")) == "macro events come from their own feed"


def test_the_source_is_always_the_items_own_url():
    assert "url" not in nx.NewsEvent.model_fields               # the model is never asked for one
    events, reasons = nx.extract(nx.NewsEvents(events=[ev(), ev(item=3)]), [ITEM], NAMES, NOW)
    assert reasons == ["#3 Ethereum Glamsterdam: no item 3"]
    (e,) = events
    assert [(s.name, s.url) for s in e.sources] == [("Cointelegraph", ITEM["url"])]
    assert e.sources[0].excerpt == "Ethereum schedules Glamsterdam upgrade on Sepolia for Oct. 6"
    assert (e.starts_at, e.precision, e.assets) == (datetime(2026, 10, 6, tzinfo=UTC), "day", ["ETH"])


# ─── Oracle gate ────────────────────────────────────────────────────────────
def web(**kw) -> oc.WebEvent:
    base = dict(kind="unlock", subject="ARB", title="Arbitrum unlocks ARB", date_text="Oct 16, 2026",
                date="2026-10-16", status="scheduled", quote="cliff on Oct 16, 2026",
                url="https://tokenomist.ai/arbitrum")
    return oc.WebEvent(**(base | kw))


def test_oracle_keeps_only_urls_its_search_returned_and_dates_inside_30_days():
    evidence = {"https://tokenomist.ai/arbitrum", "https://www.theblock.co/post/1/sui"}
    cal = oc.Calendar(events=[
        web(), web(url="https://tokenomist.ai/arbitrum/"),                 # trailing slash: same page
        web(url="https://made-up.example/arb"),
        web(date_text="Nov. 1, 2026", date="2026-11-01", url="https://www.theblock.co/post/1/sui"),
        web(date_text="Oct 16, 2026", date="2026-10-17"),
        web(date_text="week 42", date="2026-10-14", precision="week"),     # words dates.read() can't read
        web(kind="macro", subject="CPI", title="US CPI report"),
    ])
    events, reasons = oc.extract(cal, evidence, NAMES, NOW)
    assert [(e.starts_at.date(), e.precision) for e in events] == [
        (date(2026, 10, 16), "day"), (date(2026, 10, 16), "day"), (date(2026, 10, 12), "week")]
    assert reasons == ["ARB: url not in the search results (https://made-up.example/arb)",
                       "ARB: 2026-11-01 is beyond 30 days",
                       "ARB: 2026-10-17 is not what 'Oct 16, 2026' means (2026-10-16)",
                       "CPI: macro events come from their own feed"]


# ─── runs with the model stubbed ────────────────────────────────────────────
class FakePool:
    """Answers the queries the extractors make; records every write."""

    def __init__(self, news: list[dict]):
        self.news = news
        self.assets = json.loads((FIX / "assets.json").read_text())
        self.writes: list[tuple] = []

    async def fetch(self, sql, *args):
        if "FROM news_items" in sql:
            return sorted(self.news, key=lambda r: r["published_at"], reverse=True)
        if "JOIN categories" in sql:
            return [{"symbol": a["symbol"], "sector": a["sector"]} for a in self.assets]
        if "FROM assets" in sql:
            return [{"symbol": a["symbol"], "name": a["name"]} for a in self.assets]
        raise AssertionError(sql)

    async def fetchval(self, sql, *args):
        if "count(*) FROM agent_runs" in sql:
            return 0
        if "sum(cost_usd)" in sql:
            return 0.0
        if "INSERT INTO agent_runs" in sql:
            self.writes.append(("agent_runs", args))
            return 4242
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.writes.append((sql, args))

    async def executemany(self, sql, rows):
        self.writes.append((sql, list(rows)))


@pytest.fixture
def world(monkeypatch):
    news = json.loads((FIX / "news_items.json").read_text())
    for r in news:
        r["published_at"] = datetime.fromisoformat(r["published_at"])
    pool = FakePool(news)

    async def get_pool():
        return pool

    upserted: list[tuple[str, list[store.Event]]] = []

    async def upsert(events, collector):
        upserted.append((collector, events))
        return len(events)

    monkeypatch.setattr(db, "pool", get_pool)
    monkeypatch.setattr(store, "upsert", upsert)
    return pool, upserted


def stub_model(monkeypatch) -> list[dict]:
    """openrouter.structured answering from news_response.json, each event pointed at its item's index in the
    batch it was sent (the fixture names items by title)."""
    answer = json.loads((FIX / "news_response.json").read_text())["events"]
    seen: list[dict] = []

    async def structured(agent, schema, system, user, **kw):
        payload = json.loads(user)
        seen.append(payload)
        titles = [it["title"] for it in payload["items"]]
        events = [{k: v for k, v in e.items() if k != "for"} | {"item": titles.index(e["for"])}
                  for e in answer if e["for"] in titles]
        return schema.model_validate({"events": events}), 777

    monkeypatch.setattr(openrouter, "structured", structured)
    return seen


def keys(events: list[store.Event]) -> list[str]:
    return sorted(store.make_key(e.kind, e.subject, e.starts_at, e.venue) for e in events)


def test_news_run_over_real_items(world, monkeypatch):
    pool, upserted = world
    seen = stub_model(monkeypatch)
    assert asyncio.run(nx.run(now=NOW)) == 7
    (payload,) = seen
    assert len(payload["items"]) == 15                         # the 5 items without date words never went out
    assert payload["today"] == "2026-09-30 (Wednesday)" and "NEAR" in payload["tracked"]
    assert payload["items"][0]["published"] == "2026-09-29T20:36Z (Tuesday)"   # newest first
    ((collector, events),) = upserted
    assert collector == "events_news"
    assert keys(events) == [
        "listing:nrr:nyse-arca:2026-09-29",
        "other:bitget:2026-09-29",
        "other:bitget:2026-09-30",
        "other:california-memecoin-law:2027-01-01",
        "other:okx-us:2026-09-30",
        "other:russia-exchange-licensing:2026-10-05",
        "upgrade:ethereum-glamsterdam:2026-10-06",
    ]
    by = {store.make_key(e.kind, e.subject, e.starts_at, e.venue): e for e in events}
    nrr = by["listing:nrr:nyse-arca:2026-09-29"]
    assert nrr.assets == ["NEAR"] and nrr.status == "happened"
    assert nrr.sources[0].url == "https://decrypt.co/379612/bitwise-launches-first-spot-near-etf"
    assert by["other:bitget:2026-09-30"].assets == []           # USDT isn't a tracked coin
    dropped = next(a for sql, a in pool.writes if "SET error" in str(sql))
    assert dropped[0] == 777 and dropped[1].startswith("dropped 7/14: ")
    for reason in ["PCE: macro events come from their own feed", "BGB: subject 'BGB' not named in the item",
                   "SEC and CFTC: 2026-10-03 is not what 'after Friday' means (2026-09-25, 2026-10-02)",
                   "BitMine: quote not in the item", "Ethereum Glamsterdam: date words 'Oct. 6' not in the quote",
                   "Alkanes: 2026-09-20 is not what 'Sept. 20' means (2027-09-20)",
                   "Russia exchange licensing: quote not in the item"]:
        assert reason in dropped[1]
    (marked,) = [a for sql, a in pool.writes if "events_run_id" in str(sql)]
    assert [(run_id, sorted(ids)) for run_id, ids in marked] == [(777, sorted(r["id"] for r in pool.news[:15]))]


def test_news_dry_run_writes_nothing(world, monkeypatch, capsys):
    pool, upserted = world
    stub_model(monkeypatch)
    assert asyncio.run(nx.run(write=False, now=NOW)) == 7
    assert upserted == [] and not [w for w in pool.writes if "events_run_id" in str(w[0])]
    out = capsys.readouterr().out
    assert "15 items, 14 events proposed, 7 kept" in out and "KEEP upgrade:ethereum-glamsterdam:2026-10-06" in out


def test_news_run_with_nothing_dated_calls_no_model(world, monkeypatch):
    pool, upserted = world
    pool.news = pool.news[15:]                                  # the fixture's five without date words
    seen = stub_model(monkeypatch)
    assert asyncio.run(nx.run(now=NOW)) == 0 and seen == [] and upserted == []


def test_news_run_fails_loudly_when_every_batch_fails(world, monkeypatch):
    async def broken(*a, **kw):
        raise RuntimeError("events_news call failed: HTTP 500")

    monkeypatch.setattr(openrouter, "structured", broken)
    with pytest.raises(RuntimeError, match="all 1 events_news batches failed"):
        asyncio.run(nx.run(now=NOW))
    assert world[1] == []


def test_oracle_run_over_a_recorded_answer(world, monkeypatch):
    pool, upserted = world
    bodies = []

    async def post(body):
        bodies.append(body)
        return json.loads((FIX / "oracle_response.json").read_text())

    monkeypatch.setattr(openrouter.settings, "openrouter_api_key", "k")
    monkeypatch.setattr(openrouter, "_post", post)
    assert asyncio.run(oc.run(now=datetime(2026, 9, 30, 6, 20, tzinfo=UTC))) == 5
    (body,) = bodies
    assert body["model"] == openrouter.RESEARCH and body["tools"] == [openrouter.web_search(oc.SEARCHES)]
    prompt = body["messages"][1]["content"]
    assert "2026-09-28 to 2026-10-30" in prompt and "HYPE (Hyperliquid)" in prompt
    ((collector, events),) = upserted
    assert collector == "events_oracle"
    assert keys(events) == [
        "governance:arbitrum-dao-treasury-rebalance:2026-10-01",
        "listing:hype:upbit:2026-09-30",
        "tge:megaeth:2026-10-01",
        "unlock:arb:2026-10-16",
        "upgrade:ethereum-glamsterdam:2026-10-06",              # the same key the news run wrote
    ]
    by = {e.subject: e for e in events}
    assert (by["MegaETH"].precision, by["Arbitrum DAO treasury rebalance"].precision) == ("month", "quarter")
    assert by["MegaETH"].sources[0].name == "Oracle (web)"
    ((_, (run_id, error)),) = [w for w in pool.writes if "SET error" in str(w[0])]
    assert run_id == 4242 and error.startswith("dropped 3/8: ")
    assert "LIT: url not in the search results (https://www.coinbase.com/blog/lighter-lit-listing)" in error
    assert "SUI: 2026-11-01 is beyond 30 days" in error
    assert "Monad: 2026-10-21 is not what 'October 20, 2026' means (2026-10-20)" in error


def test_oracle_respects_its_daily_call_cap(world, monkeypatch):
    pool, _ = world

    async def two(sql, *args):
        return 2

    monkeypatch.setattr(pool, "fetchval", two)
    monkeypatch.setattr(openrouter.settings, "openrouter_api_key", "k")
    with pytest.raises(openrouter.CallLimitExceeded, match="oracle_calendar: 2/2 calls today"):
        asyncio.run(oc.run())
