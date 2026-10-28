"""The Reddit scout (albatross/deepend/reddit.py): thread links, resolving Google's grounding links, the evidence gate on
planted answers (in the shape Gemini answers through OpenRouter with Google's own search: fenced JSON citing grounding
links, often with no url_citation annotations), the control search, budget and pacing, picking, the prompt, the stored
row and the dossier section. The DB scenario at the bottom needs a migrated Postgres (ALBATROSS_TEST_DSN, never
production) and runs inside a transaction that is rolled back.
"""

import asyncio
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest

from albatross import db
from albatross.agents import graph
from albatross.config import settings
from albatross.deepend import reddit as r
from albatross.deepend import registry
from albatross.llm import openrouter

NOW = datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
D, H = timedelta(days=1), timedelta(hours=1)
CA = "DEW9dSN6QpWyNthphCpMmAbZP1Q4cEKR9xQXAri98WDP"
SI = {"kind": "token", "chain": "solana", "address": CA, "symbol": "SI", "name": "Super Inu", "n_groups": 2,
      "n_calls": 16, "shared_ticker": 18, "picked_for": "2 groups, 16 calls"}


def g(name: str) -> str:
    """A Google grounding link (the token is made up; where it leads is the test's `where`)."""
    return f"https://vertexaisearch.cloud.google.com/grounding-api-redirect/AUZIYQ{name}x9Kq"


CONTROL = "https://www.reddit.com/r/CryptoCurrency/comments/1wv0001/daily_crypto_discussion_october_01_2026/"
POD = "https://www.reddit.com/r/solana/comments/1nq7xk2/super_inu_si_just_flipped_50m_mcap/"
MOON = "https://www.reddit.com/r/CryptoMoonShots/comments/1nq9b01/super_inu_dev_wallet_sold_8_of_supply/"
TYPED = "https://www.reddit.com/r/memecoins/comments/1nqa0c3/is_super_inu_legit/"     # no grounding link leads here
SCAM = "https://www.reddit.com/r/CryptoScams/comments/1nzz111/fake_super_inu_airdrop/"
# Where Google's links lead: share tags and an old. spelling, a price page, and two that never answer.
WHERE = {g("Ctrl"): CONTROL + "?share_id=ab12", g("Pod"): POD + "?utm_source=share&utm_medium=web3x",
         g("Moon"): MOON.replace("www.", "old."), g("Bitget"): "https://www.bitget.com/price/super-inu?ref=gg",
         g("Slow"): None, g("Gone"): None}


def day(n: int) -> str:
    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


CTRL = g("Ctrl")


def answer(threads=(), flags=(), control=CTRL, mood=1, summary="Reddit is split on Super Inu.",
           why="A reported dev sale is the risk Reddit is talking about.", other=0) -> dict:
    return {"control": control, "threads": list(threads), "other_coin_threads": other, "mood": mood,
            "summary": summary, "red_flags": list(flags), "why_it_matters": why}


def planted_si() -> dict:
    """SI's read: two threads Google returned, one reddit URL the model typed itself, a link that leads to a price
    page, one the resolver gave up on, and a red flag citing a thread nothing returned."""
    return answer(threads=[
        {"url": g("Pod"), "title": "Super Inu ($SI) just flipped $50M mcap", "posted": day(1), "score": 212,
         "comments": 64, "stance": "bullish", "notable": True},
        {"url": g("Moon"), "title": "Super Inu dev wallet sold 8% of supply", "posted": day(2), "comments": 30,
         "stance": "bearish", "notable": True},
        {"url": TYPED, "title": "Is Super Inu legit?", "stance": "neutral"},
        {"url": g("Bitget"), "title": "Super Inu price", "stance": "neutral"},
        {"url": g("Slow"), "title": "SI to 1B", "stance": "bullish"}],
        flags=[{"kind": "scam", "detail": "A thread reports the dev wallet sold 8% of supply.", "url": g("Moon")},
               {"kind": "impersonation", "detail": "A fake airdrop uses the Super Inu name.", "url": SCAM}],
        other=1)


def response(body: dict | str, cost: float = 0.0048, annotations=()) -> dict:
    """OpenRouter's reply as Gemini with Google search gives it: the JSON fenced, the links in the text."""
    text = body if isinstance(body, str) else "```json\n" + json.dumps(body, indent=1) + "\n```"
    return {"model": "google/gemini-3.8-flash", "provider": "Google",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text,
                                                              "annotations": list(annotations)}}],
            "usage": {"prompt_tokens": 2900, "completion_tokens": 900, "cost": cost}}


class Pool:
    """db.pool() stand-in: answers the scout's queries, records every write."""

    def __init__(self, reads: int = 0, spent: float = 0.0, tokens=(), majors=(), latest=None, last=()):
        self.reads, self.spent, self.tokens, self.majors, self.latest = reads, spent, list(tokens), list(majors), latest
        self.last = list(last)
        self.runs: list[tuple] = []
        self.writes: list[tuple] = []

    async def fetchval(self, sql, *args):
        if "FROM de_reddit_reads" in sql:
            return self.reads
        if "sum(cost_usd)" in sql:
            return self.spent
        if "INSERT INTO agent_runs" in sql:
            self.runs.append(args)
            return 9000 + len(self.runs)
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.writes.append((sql, args))

    async def fetch(self, sql, *args):
        if sql is r.TOKENS_SQL:
            return self.tokens
        if sql is r.MAJORS_SQL:
            return self.majors
        if sql is r.LAST_SQL:
            return self.last
        raise AssertionError(sql)

    async def fetchrow(self, sql, *args):
        if sql in (r.TOKEN_SQL, r.MAJOR_SQL):
            if isinstance(self.latest, Exception):
                raise self.latest
            self.writes.append(("fetchrow", args))
            return self.latest
        raise AssertionError(sql)

    def stored(self) -> list[dict]:
        out = []
        for sql, args in self.writes:
            if sql is r.INSERT:
                row = dict(zip(r.COLS, args))
                for k in ("threads", "flags", "audit"):
                    row[k] = json.loads(row[k]) if row[k] else row[k]
                out.append(row)
        return out

    def run_updates(self) -> list[tuple]:
        return [args for sql, args in self.writes if isinstance(sql, str) and sql.startswith("UPDATE agent_runs")]


@pytest.fixture
def world(monkeypatch):
    """A fake database, OpenRouter answering from `replies` and Google's links resolving from WHERE.
    Returns (pool, posted bodies, replies, the links asked about)."""
    pool, posted, replies, asked = Pool(), [], [], []

    async def fake_pool():
        return pool

    async def fake_post(body):
        posted.append(body)
        rep = replies.pop(0)
        if isinstance(rep, Exception):
            raise rep
        return rep

    async def fake_location(link):
        asked.append(link)
        return WHERE.get(link)

    monkeypatch.setattr(db, "pool", fake_pool)
    monkeypatch.setattr(openrouter, "_post", fake_post)
    monkeypatch.setattr(r, "location", fake_location)
    monkeypatch.setattr(settings, "openrouter_api_key", "k")
    return pool, posted, replies, asked


def no_link_in(stored: dict) -> bool:
    return "vertexaisearch" not in json.dumps({k: v for k, v in stored.items() if k != "read_at"})


# ─── thread links ────────────────────────────────────────────────────────────
def test_every_spelling_of_one_thread_is_the_same_thread():
    for u in [POD, POD.replace("www.", "old."), POD.replace("https://www.", "https://"), POD.rstrip("/"),
              POD.replace("www.", "np.").replace("1nq7xk2", "1NQ7XK2"), POD + "?utm_source=share&utm_medium=web3x&share_id=x",
              POD.replace("www.", "m.") + "kq3x9z1/",                 # a comment's permalink: its thread
              POD + "#comments", POD.replace("https", "http")]:
        assert r.thread(u) == {"key": "1nq7xk2", "url": POD, "subreddit": "r/solana",
                               "slug": "super_inu_si_just_flipped_50m_mcap"}, u
    bare = {"key": "1nq7xk2", "url": "https://www.reddit.com/comments/1nq7xk2/", "subreddit": None, "slug": None}
    assert r.thread("https://redd.it/1nq7xk2") == bare and r.thread("https://www.reddit.com/comments/1nq7xk2") == bare
    assert r.thread("https://www.reddit.com/r/solana/comments/1nq7xk2/comment/kq3x9z1/?context=3")["url"] == \
        "https://www.reddit.com/r/solana/comments/1nq7xk2/"
    assert r.thread("https://www.reddit.com/user/sample_user/comments/1nq8aaa/my_si_bags/")["subreddit"] == "u/sample_user"
    assert r.thread("https://www.reddit.com/gallery/1nq7xk2")["url"] == "https://www.reddit.com/comments/1nq7xk2/"


@pytest.mark.parametrize("url", [
    "https://www.reddit.com/r/solana/", "https://www.reddit.com/r/solana/search/?q=SI", "https://www.reddit.com/user/x/",
    "https://www.reddit.com/r/solana/wiki/index", "https://i.redd.it/abc123.png", "https://v.redd.it/abc123",
    "https://www.reddit.com/r/solana/s/AbC12xYz9",                     # a share code: only Reddit knows the thread
    "https://notreddit.com/r/solana/comments/1nq7xk2/x/", "https://reddit.com.evil.io/r/solana/comments/1nq7xk2/x/",
    "https://reddit-mirror.example.edu/r/sample/comments/1t0r/abcdef/3fig",   # a mirror Claude saw
    "ftp://reddit.com/r/solana/comments/1nq7xk2/", "https://x.com/a/status/1975000000000000000", "not a url", "", None])
def test_anything_but_a_reddit_thread_is_not_a_thread(url):
    assert r.thread(url) is None


def test_only_the_main_reddit_hosts_count_where_a_google_link_leads():
    assert all(r.on_reddit(u) for u in (POD, POD.replace("www.", "old."), POD.replace("www.", "np."),
                                        POD.replace("https://www.", "https://"), "https://redd.it/1nq7xk2"))
    assert not any(r.on_reddit(u) for u in (POD.replace("www.", "m."), "https://www.bitget.com/x", None, "x"))
    assert r.grounding(g("Pod")) and not r.grounding(POD)
    assert not r.grounding(g("Pod").replace("https", "http")) and not r.grounding("https://evil.io/grounding-api-redirect/x")


# ─── resolving Google's links: never followed, never Reddit ──────────────────
def test_a_google_link_is_asked_once_and_never_followed(monkeypatch):
    seen = []

    async def handler(request):
        seen.append(str(request.url))
        if request.url.host != r.GROUNDING:
            raise AssertionError(f"requested {request.url}")             # Reddit is never requested
        if "Slow" in request.url.path:
            await asyncio.sleep(1)
        if "Gone" in request.url.path:
            return httpx.Response(404)
        return httpx.Response(302, headers={"Location": POD})

    mock = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
    monkeypatch.setattr(r, "client", lambda: mock)
    monkeypatch.setattr(r, "RESOLVE_S", 0.05)
    assert asyncio.run(r.location(g("Pod"))) == POD and seen == [g("Pod")]  # the 302 was read, not followed
    assert asyncio.run(r.location(g("Gone"))) is None                       # no redirect: nothing
    assert asyncio.run(r.location(g("Slow"))) is None                       # past RESOLVE_S: dropped
    assert asyncio.run(r.location(POD)) is None and asyncio.run(r.location("https://evil.io/grounding-api-redirect/x")) is None
    assert seen == [g("Pod"), g("Gone"), g("Slow")]                         # only Google's redirector, ever


def test_at_most_twelve_links_are_resolved_a_read(monkeypatch):
    asked = []

    async def fake_location(link):
        asked.append(link)
        return POD

    monkeypatch.setattr(r, "location", fake_location)
    links = [g(f"L{i:02d}") for i in range(14)]
    where = asyncio.run(r.resolve(links))
    assert asked == links[:12] and list(where) == links
    assert [where[u] for u in links[12:]] == [None, None]


def test_the_control_is_resolved_first_and_each_link_once():
    read = r.RedditRead.model_validate(planted_si())
    assert r.cited(read) == [g("Ctrl"), g("Pod"), g("Moon"), g("Bitget"), g("Slow")]   # plain text is never asked


# ─── the evidence gate ───────────────────────────────────────────────────────
def test_planted_answer_keeps_only_threads_google_returned(world):
    pool, posted, replies, asked = world
    replies.append(response(planted_si()))
    row = asyncio.run(r.read(SI | {"created_at": datetime.now(UTC) - 10 * D}))

    (body,) = posted
    assert body["model"] == r.MODEL == "google/gemini-3.8-flash" and body["tools"] == [r.SEARCH]
    assert r.SEARCH["parameters"]["engine"] == "native" and body["reasoning"] == {"effort": "low"}
    assert CA in body["messages"][1]["content"] and "JSON Schema" in body["messages"][0]["content"]
    assert asked == [g("Ctrl"), g("Pod"), g("Moon"), g("Bitget"), g("Slow")]

    (stored,) = pool.stored()
    assert stored["status"] == "read" and stored["agent_run_id"] == 9001 and stored["chain"] == "solana"
    assert [t["url"] for t in stored["threads"]] == [POD, MOON]                  # canonical, tags stripped
    assert stored["threads"][1] == {"subreddit": "r/CryptoMoonShots", "title": "Super Inu dev wallet sold 8% of supply",
                                    "url": MOON, "posted": day(2), "score": None, "comments": 30, "stance": "bearish",
                                    "notable": True}
    assert stored["flags"] == [{"kind": "scam", "detail": "A thread reports the dev wallet sold 8% of supply.", "url": MOON}]
    assert stored["citations_dropped"] == 4
    assert stored["audit"] == {
        "dropped": [TYPED,                                                       # typed, no link led there
                    "google link -> https://www.bitget.com/price/super-inu",    # led off Reddit
                    "google link: unresolved",                                   # the resolver gave up
                    SCAM],                                                       # the flag nothing returned
        "other_coin_threads": 1, "control": "https://www.reddit.com/r/CryptoCurrency/comments/1wv0001/"
                                            "daily_crypto_discussion_october_01_2026/",
        "links": 5, "unresolved": 1, "reddit_threads": 3}
    assert stored["mood"] == 1 and stored["activity"] == "few" and stored["summary"] == "Reddit is split on Super Inu."
    assert row["why"] == stored["why"] == "A reported dev sale is the risk Reddit is talking about."
    assert no_link_in(stored)                                                    # never a grounding link
    ((run_id, error, ok),) = pool.run_updates()
    assert run_id == 9001 and ok is True and error.startswith(f"dropped 4/7 citations: {TYPED}; google link -> ")
    (run,) = pool.runs
    assert run[0] == r.AGENT and run[1] == "google/gemini-3.8-flash" and run[5] == 0.0048
    assert "vertexaisearch" not in run[-1] and "[google link]" in run[-1]       # the audit log masks them too


def test_a_reddit_url_the_model_typed_counts_only_when_a_google_link_led_there():
    where = {g("Pod"): POD + "?utm_source=x"}
    read = r.RedditRead.model_validate(answer(threads=[
        {"url": POD.replace("www.", "old.").rstrip("/"), "title": "Super Inu ($SI) just flipped $50M mcap", "stance": "bullish"},
        {"url": TYPED, "title": "Is Super Inu legit?", "stance": "neutral"}], control=None))
    got = r.judge(read, where, SI, NOW)
    assert [t["url"] for t in got["threads"]] == [POD] and got["audit"]["dropped"] == [TYPED]


def test_no_discussion_found_needs_a_control_that_reached_reddit(world):
    pool, _, replies, _ = world
    replies.append(response(answer(summary="Nothing on Reddit mentions this coin.")))
    asyncio.run(r.read(SI))
    (stored,) = pool.stored()
    assert (stored["status"], stored["summary"], stored["activity"], stored["mood"]) == ("none", r.NONE_FOUND, "none", None)
    assert stored["audit"]["control"].startswith("https://www.reddit.com/r/CryptoCurrency/") and pool.run_updates() == []


@pytest.mark.parametrize("control", [None, g("Bitget"), g("Gone"), CONTROL])     # no link, off Reddit, dead, typed
def test_without_a_control_that_reached_reddit_the_read_is_blind(world, control):
    pool, _, replies, _ = world
    replies.append(response(answer(control=control, summary="No threads.")))
    asyncio.run(r.read(SI))
    (stored,) = pool.stored()
    assert (stored["status"], stored["summary"], stored["activity"], stored["mood"]) == ("blind", None, None, None)
    ((_, error, ok),) = pool.run_updates()
    assert ok is False and error.startswith("blind: none of Google's links led to Reddit (the control: ")


def test_a_recorded_btc_read(world, monkeypatch):
    """The read of 2026-10-01 (tests/fixtures/deep_reddit/btc_read.json; Google's tokens, thread ids and slugs synthetic):
    three r/CryptoCurrency daily threads, each proven by where its grounding link led."""
    rec = json.loads((Path(__file__).parent / "fixtures" / "deep_reddit" / "btc_read.json").read_text())
    at = datetime(2026, 10, 1, 20, 2, tzinfo=UTC)
    read = r.parse(rec["response"]["choices"][0]["message"]["content"])
    got = r.judge(read, rec["where"], rec["subject"], at)
    want = ["https://www.reddit.com/r/CryptoCurrency/comments/1wtv0a1/daily_discussion_thread/",
            "https://www.reddit.com/r/CryptoCurrency/comments/1wt0b02/daily_discussion_thread/",
            "https://www.reddit.com/r/CryptoCurrency/comments/1ws0c03/daily_discussion_thread/"]
    assert (got["status"], got["mood"], got["activity"], got["citations_dropped"]) == ("read", 0, "few", 0)
    assert [t["url"] for t in got["threads"]] == want and {t["subreddit"] for t in got["threads"]} == {"r/CryptoCurrency"}
    assert got["audit"]["control"] == want[0] and got["why"].startswith("Retail sentiment remains cautious")

    pool, _, replies, asked = world
    monkeypatch.setattr(r, "location", lambda link: _resolved(rec["where"], asked, link))
    replies.append(rec["response"])
    asyncio.run(r.read({**rec["subject"], "chain": None, "address": None, "picked_for": "news impact 3"}))
    (stored,) = pool.stored()
    assert stored["status"] == "read" and stored["chain"] is None and stored["symbol"] == "BTC"
    assert sorted(t["url"] for t in stored["threads"]) == sorted(want) and no_link_in(stored)
    assert len(asked) == 3                                # the control's link is a thread's too: asked once


async def _resolved(where: dict, asked: list, link: str):
    asked.append(link)
    return where.get(link)


def test_a_subreddit_front_page_proves_reach_but_neither_a_thread_nor_an_absence():
    """Recorded 2026-10-01 (BTC): Google returned r/Bitcoin's and r/BitcoinMarkets' front pages, the model gave them the
    pinned daily threads' titles and counts, and left the control empty. Reddit was reached; no thread was proven, and
    "no discussion found" would be an unverified negative: the read is 'unverified', stored but never served."""
    front = {g("Btc"): "https://www.reddit.com/r/Bitcoin/", g("Bm"): "https://www.reddit.com/r/BitcoinMarkets/"}
    read = r.RedditRead.model_validate(answer(control=None, threads=[
        {"url": g("Btc"), "title": "Daily Discussion, October 01, 2026", "posted": "2026-10-01", "score": 29,
         "comments": 18, "stance": "bullish", "notable": True},
        {"url": g("Bm"), "title": "[Daily Discussion] - Monday, September 28, 2026", "posted": "2026-09-28",
         "score": 1, "stance": "neutral"}]))
    got = r.judge(read, front, {"kind": "major", "symbol": "BTC"}, NOW)
    assert (got["status"], got["summary"], got["threads"]) == ("unverified", None, [])
    assert got["audit"]["dropped"] == ["google link -> https://www.reddit.com/r/Bitcoin/",
                                       "google link -> https://www.reddit.com/r/BitcoinMarkets/"]


def test_invented_threads_are_unverified_never_no_discussion_and_fail_the_run(world):
    pool, _, replies, _ = world
    replies.append(response(answer(threads=[{"url": TYPED, "title": "t", "stance": "bullish"},
                                            {"url": g("Gone"), "title": "t", "stance": "bullish"}], mood=4,
                                   summary="Reddit is euphoric.", why="Strong retail interest is building.")))
    asyncio.run(r.read(SI))
    (stored,) = pool.stored()
    assert (stored["status"], stored["summary"], stored["mood"], stored["why"], stored["threads"]) == \
        ("unverified", None, None, "", [])
    ((_, error, ok),) = pool.run_updates()
    assert ok is False and error == f"dropped 2/2 citations: {TYPED}; google link: unresolved"


def test_annotations_alone_are_no_evidence(world):
    """A plain url_citation is text like any other: only where Google's own links lead counts."""
    pool, _, replies, _ = world
    ann = [{"type": "url_citation", "url_citation": {"url": TYPED, "title": "reddit.com"}}]
    replies.append(response(answer(threads=[{"url": TYPED, "title": "Is Super Inu legit?", "stance": "neutral"}]),
                            annotations=ann))
    asyncio.run(r.read(SI))
    (stored,) = pool.stored()
    assert stored["status"] == "unverified" and stored["audit"]["dropped"] == [TYPED]


# ─── the judgement ───────────────────────────────────────────────────────────
def _read(threads=(), flags=(), mood=2, why="Reddit noticed the launch.") -> r.RedditRead:
    return r.RedditRead(threads=[r.Thread(stance="neutral", **t) for t in threads], control=g("Ctrl"),
                        red_flags=[r.Flag(**f) for f in flags], mood=mood, summary="  Some   talk. ", why_it_matters=why)


def _world(*pids: str, slug: str = "si_thread") -> tuple[list[str], dict]:
    links = [g(p) for p in pids]
    return links, {g("Ctrl"): CONTROL} | {u: f"https://www.reddit.com/r/solana/comments/{p}/{slug}/"
                                          for u, p in zip(links, pids)}


def test_activity_counts_verified_threads_of_the_last_72_hours():
    assert [r.level(n) for n in (0, 1, 3, 4, 9)] == ["none", "few", "few", "active", "active"]
    links, where = _world("1aaaaa1", "1aaaaa2", "1aaaaa3", "1aaaaa4", "1aaaaa5")
    ages = [0, 1, 3, None, 9]                             # days before NOW; 9 days old is listed, not counted
    threads = [{"url": u, "title": "si thread", "posted": (NOW - a * D).date().isoformat() if a is not None else None}
               for u, a in zip(links, ages)]
    got = r.judge(_read(threads), where, SI, NOW)
    assert got["activity"] == "active" and len(got["threads"]) == 5 and got["summary"] == "Some talk."
    assert r.judge(_read(threads[3:]), where, SI, NOW)["activity"] == "few"   # the undated counts, the old doesn't
    future = r.judge(_read([{"url": links[0], "title": "si", "posted": "2026-12-25"}]), where, SI, NOW)
    assert future["threads"][0]["posted"] is None         # a date in the future is unknown, not trusted


def test_a_thread_from_before_the_token_existed_is_another_coin():
    (old, new), where = _world("1bbbbb1", "1bbbbb2")
    got = r.judge(_read([{"url": old, "title": "si thread", "posted": (NOW - 30 * D).date().isoformat()},
                         {"url": new, "title": "si thread", "posted": (NOW - D).date().isoformat()}],
                        [{"kind": "rug", "detail": "rugged", "url": old}]), where, SI | {"created_at": NOW - 3 * D}, NOW)
    assert [t["url"] for t in got["threads"]] == [where[new]] and got["flags"] == []
    assert got["audit"]["other_coin_threads"] == 1 and got["citations_dropped"] == 0


def test_titles_follow_the_threads_own_slug_and_advice_is_never_stored():
    (link,), where = _world("1ccccc1", slug="dev_sold_half_the_supply")
    got = r.judge(_read([{"url": link, "title": "SI to the moon, 100x incoming"}], why="This is a buy signal for SI."),
                  where, SI, NOW)
    assert got["threads"][0]["title"] == "dev sold half the supply"            # the model's title was another thread's
    assert got["why"] == "" and got["status"] == "read"
    keep = r.judge(_read([{"url": link, "title": "Dev sold half the supply?!"}]), where, SI, NOW)
    assert keep["threads"][0]["title"] == "Dev sold half the supply?!" and keep["why"] == "Reddit noticed the launch."


def test_duplicates_collapse_and_flags_are_capped():
    (link,), where = _world("1ddddd1")
    flags = [{"kind": "scam", "detail": f"report {i}", "url": link} for i in range(6)]
    got = r.judge(_read([{"url": link, "title": "si"}, {"url": where[link].replace("www.", "old."), "title": "si"}],
                        flags), where, SI, NOW)
    assert len(got["threads"]) == 1 and len(got["flags"]) == r.MAX_FLAGS == 4


# ─── the call, the budget and the pace ───────────────────────────────────────
def test_one_retry_for_an_empty_answer_then_the_read(world):
    pool, posted, replies, _ = world
    replies.extend([response(""), response(answer())])
    asyncio.run(r.read(SI))
    assert len(posted) == 2 and len(pool.runs) == 2 and [run[6] for run in pool.runs] == [False, True]
    assert pool.stored()[0]["agent_run_id"] == 9002


def test_a_failed_call_is_stored_failed_and_raised(world):
    pool, _, replies, _ = world
    replies.append(response('{"control": null, "threads": [], "mood": 9, "summary": "x"}'))   # a schema slip
    with pytest.raises(RuntimeError, match="deep_reddit_scan call failed: ValidationError"):
        asyncio.run(r.read(SI))
    (stored,) = pool.stored()
    assert stored["status"] == "failed" and stored["agent_run_id"] is None and stored["threads"] == []


@pytest.mark.parametrize("pool_kw, why", [({"reads": 12}, "deep_reddit_scan: 12/12 reads today"),
                                          ({"spent": 1.0}, r"deep_reddit_scan spent \$1.00 of \$1.00 today")])
def test_the_days_reads_and_dollars_both_cap_it(world, pool_kw, why):
    pool, posted, _, _ = world
    for k, v in pool_kw.items():
        setattr(pool, k, v)
    with pytest.raises(openrouter.BudgetExceeded, match=why):
        asyncio.run(r.read(SI))
    assert posted == [] and pool.stored() == []


def test_the_day_is_paced_evenly_with_an_hours_slack():
    at = lambda h, m=0: datetime(2026, 10, 1, h, m, tzinfo=UTC)
    assert r.room(0, at(0, 12), 12) == 1                  # the first read right after midnight
    assert r.room(1, at(0, 42), 12) == 0
    assert r.room(1, at(1, 42), 12) == 1                  # one every two hours after that
    assert r.room(2, at(1, 42), 12) == 0
    assert r.room(0, at(12, 12), 12) == r.PER_RUN == 3    # a quiet morning catches up a few per run, not all at once
    assert r.room(11, at(23, 42), 12) == 1 and r.room(12, at(23, 42), 12) == 0
    assert r.room(0, at(23, 42), 0) == 0


def test_two_blind_reads_in_a_row_pause_the_scout_for_a_day():
    blind = lambda h: {"status": "blind", "read_at": NOW - h * H}
    assert r.paused_blind([blind(1), blind(3)], NOW)
    assert not r.paused_blind([blind(1)], NOW)                                  # one blind read may be the model's slip
    assert not r.paused_blind([blind(1), {"status": "none", "read_at": NOW - 2 * H}], NOW)
    assert not r.paused_blind([blind(25), blind(26)], NOW)                      # a day on: probe again


# ─── what to read ────────────────────────────────────────────────────────────
def test_hot_tokens_with_the_most_group_calls_first_then_majors_with_news():
    toks = [{"address": "a", "n_groups": 2, "n_calls": 9, "liquidity": 50_000},
            {"address": "b", "n_groups": 3, "n_calls": 4, "liquidity": 20_000},
            {"address": "c", "n_groups": 1, "n_calls": 30, "liquidity": 900_000},                 # one group only
            {"address": "d", "n_groups": 5, "n_calls": 40, "liquidity": 4_000},                   # dust
            {"address": "e", "n_groups": 4, "n_calls": 9, "liquidity": 80_000, "last_read": NOW - 3 * H},
            {"address": "f", "n_groups": 4, "n_calls": 8, "liquidity": 80_000, "last_failed": NOW - 2 * H},
            {"address": "g", "n_groups": 2, "n_calls": 9, "liquidity": 60_000, "last_read": NOW - 25 * H,
             "last_failed": NOW - 7 * H}]
    majs = [{"symbol": "ETH", "impact": 3, "items": 9}, {"symbol": "BTC", "impact": 4, "items": 2},
            {"symbol": "SOL", "impact": 5, "items": 1, "last_read": NOW - 5 * H}]
    got = r.pick(toks, majs, NOW, 9)
    assert [c.get("address") or c["symbol"] for c in got] == ["b", "g", "a", "BTC", "ETH"]
    assert [c.get("address") or c["symbol"] for c in r.pick(toks, majs, NOW, 2)] == ["b", "g"]
    assert r.pick(toks, majs, NOW, 0) == []


def test_the_prompt_spells_out_the_control_and_the_searches():
    s = SI | {"created_at": datetime(2026, 9, 21, 15, 44, tzinfo=UTC)}
    sys_, user = (m["content"] for m in r.messages(s, NOW))
    assert "never write or rebuild a\nreddit.com URL yourself" in sys_ and '"control"' in sys_
    assert user.startswith("Today is 2026-10-01 15:00 UTC.")
    assert "1. site:reddit.com inurl:comments cryptocurrency daily discussion   (the control" in user
    assert f'2. site:reddit.com inurl:comments "{CA}"' in user and '3. site:reddit.com inurl:comments "SI" Solana coin' in user
    assert '4. site:reddit.com inurl:comments "Super Inu" crypto' in user and "created 2026-09-21 15:44 UTC" in user
    assert "18 other recent tokens use the ticker $SI" in user and "called it 16 times from 2 groups" in user
    assert "- control: the link of the first Reddit result of search 1" in user
    assert r.plan({"kind": "token", "chain": "robinhood", "address": "0xabc", "symbol": None, "name": None})[1:] == \
        ['site:reddit.com inurl:comments "0xabc"']
    m = {"kind": "major", "symbol": "BTC", "name": "Bitcoin",
         "headlines": [{"title": "Bitcoin ETFs see record outflows", "at": datetime(2026, 10, 1, 14, 5, tzinfo=UTC)}]}
    user = r.messages(m, NOW)[1]["content"]
    assert "Bitcoin (BTC), a major crypto asset" in user and "- 2026-10-01 14:05 Bitcoin ETFs see record outflows" in user
    # its own subreddit named (the first evening's "<Bitcoin's own subreddit>" found nothing for 12 of 12 majors),
    # the general sub, and its top headline's own words
    assert "2. site:reddit.com/r/Bitcoin inurl:comments daily discussion" in user
    assert "3. site:reddit.com/r/Bitcoin inurl:comments" in user
    assert "4. site:reddit.com/r/CryptoCurrency inurl:comments Bitcoin" in user
    assert "5. site:reddit.com inurl:comments Bitcoin Bitcoin ETFs see record outflows" in user
    unknown = r.plan({"kind": "major", "symbol": "ZZZ", "name": "Zed", "headlines": []})
    assert unknown[1:] == ["site:reddit.com/r/CryptoCurrency inurl:comments Zed"]


def test_the_answer_parses_from_a_fence_and_needs_mood_summary_and_threads():
    assert set(r.RedditRead.model_json_schema()["required"]) == {"control", "threads", "mood", "summary"}
    assert r.parse("Here it is:\n```json\n" + json.dumps(answer()) + "\n```").control == g("Ctrl")
    for raw in (None, "", "no json here"):
        with pytest.raises(ValueError):
            r.parse(raw)
    with pytest.raises(ValueError):
        r.parse(json.dumps(answer(threads=[{"url": POD, "title": "t", "stance": "moon"}])))


def test_a_scan_keeps_a_read_for_a_major_then_reads_tokens_and_survives_a_failure(world, monkeypatch):
    pool, posted, replies, _ = world
    pool.tokens = [{"chain": "solana", "address": CA, "symbol": "SI", "name": "Super Inu", "liquidity": 1.4e6,
                    "created_at": datetime.now(UTC) - 10 * D, "n_groups": 2, "n_calls": 16, "last_read": None,
                    "last_failed": None, "shared_ticker": 0}]
    pool.majors = [{"symbol": "BTC", "name": "Bitcoin", "impact": 4, "items": 3, "last_read": None, "last_failed": None,
                    "headlines": json.dumps([{"title": "Bitcoin ETFs see record outflows", "at": "2026-10-01T14:05:00+00:00"}])},
                   {"symbol": "ETH", "name": "Ethereum", "impact": 3, "items": 1, "last_read": None, "last_failed": None,
                    "headlines": json.dumps([{"title": "Ethereum ships Glamsterdam", "at": "2026-10-01T13:00:00+00:00"}])}]

    async def hot(tier=None):
        assert tier == "hot"
        return [{"chain": "solana", "address": CA}]

    replies.extend([httpx.HTTPStatusError("boom", request=httpx.Request("POST", "u"), response=httpx.Response(402)),
                    response(planted_si()), response(answer())])
    monkeypatch.setattr(r.watch, "tokens", hot)
    monkeypatch.setattr(r, "room", lambda done, now, daily: 3)
    assert asyncio.run(r.scan()) == 2
    stored = pool.stored()
    assert [(s["symbol"], s["chain"], s["status"]) for s in stored] == \
        [("BTC", None, "failed"), ("SI", "solana", "read"), ("ETH", None, "unverified")]   # a major first; a major's
    # "no thread" is our search's miss, never served as a fact
    assert [s["picked_for"] for s in stored] == ["news impact 4", "2 groups, 16 calls", "news impact 3"]
    assert "Bitcoin ETFs see record outflows" in posted[0]["messages"][1]["content"]


def test_majors_keep_their_share_of_the_days_reads():
    now = datetime.now(UTC)
    tok = lambda a, g: {"address": a, "n_groups": g, "n_calls": g, "liquidity": 5e4, "last_read": None,  # noqa: E731
                        "last_failed": None}
    maj = lambda sym, i: {"symbol": sym, "impact": i, "items": 1, "last_read": None, "last_failed": None}  # noqa: E731
    toks, majs = [tok("A", 3), tok("B", 5)], [maj("ETH", 3), maj("BTC", 5)]
    assert [x.get("address") or x["symbol"] for x in r.pick(toks, majs, now, 3)] == ["B", "A", "BTC"]
    assert [x.get("address") or x["symbol"] for x in r.pick(toks, majs, now, 3, majors_first=True)] == \
        ["BTC", "B", "A"]
    assert [x.get("address") or x["symbol"] for x in r.pick(toks, [], now, 2, majors_first=True)] == ["B", "A"]


def test_a_red_flag_counts_only_on_a_thread_kept_about_this_coin():
    links, where = _world("1bbbbb1", "1bbbbb2")
    kept = {"url": links[0], "title": "SI rugged?", "posted": "2026-09-30"}
    flag = {"kind": "rug", "detail": "says the dev sold", "url": links[0]}
    got = r.judge(_read([kept], [flag]), where, {"chain": "solana", "created_at": NOW - 10 * D}, NOW)
    assert [f["kind"] for f in got["flags"]] == ["rug"] and got["status"] == "read"
    unlisted = r.judge(_read([], [flag | {"url": links[1]}]), where, {"chain": "solana", "created_at": NOW - 10 * D}, NOW)
    assert unlisted["flags"] == [] and unlisted["status"] == "unverified"         # a thread it never listed
    old = kept | {"posted": "2025-01-02"}                                         # a namesake from before the token
    namesake = r.judge(_read([old], [flag]), where, {"chain": "solana", "created_at": NOW - 10 * D}, NOW)
    assert namesake["flags"] == [] and namesake["status"] == "none" and namesake["citations_dropped"] == 0


def test_a_scan_waits_out_blind_reads_and_the_days_cap(world, monkeypatch):
    pool, posted, _, _ = world

    async def hot(tier=None):
        raise AssertionError("no candidates are looked up")

    monkeypatch.setattr(r.watch, "tokens", hot)
    now = datetime.now(UTC)
    pool.last = [{"status": "blind", "read_at": now - H}, {"status": "blind", "read_at": now - 2 * H}]
    assert asyncio.run(r.scan()) == 0
    pool.last, pool.reads = [], 12
    assert asyncio.run(r.scan()) == 0 and posted == [] and pool.stored() == []
    pool.reads = 0
    monkeypatch.setattr(settings, "openrouter_api_key", None)        # no key: nothing to do, nothing marked failed
    assert asyncio.run(r.scan()) == 0 and posted == [] and pool.stored() == []


# ─── the dossier section ─────────────────────────────────────────────────────
SECTION = {"status", "as_of", "found", "mood", "mood_label", "activity", "activity_label", "summary", "why_it_matters",
           "threads", "independent", "red_flags", "citations_dropped"}


def test_a_coins_own_subreddit_is_not_reddit_talking_about_it():
    assert r.own_sub("r/SAWcoin", "SAW") and r.own_sub("r/superinu", "$INU")
    assert not r.own_sub("r/solana_meme", "SAW") and not r.own_sub("r/solana", "AI")              # 2 letters: too short
    assert r.own_sub("r/CryptoMoonShots", "SAW") and r.own_sub("r/SatoshiStreetBets", None)       # promotion subs


def test_the_section_is_the_latest_read_or_none(world):
    pool = world[0]
    at = datetime.now(UTC) - 2 * H
    pool.latest = {"read_at": at, "status": "read", "mood": 1, "activity": "few", "summary": "Split.", "why": None,
                   "threads": json.dumps([{"subreddit": "r/solana", "title": "t", "url": POD, "posted": None,
                                           "score": None, "comments": None, "stance": "bullish", "notable": True}]),
                   "flags": "[]", "citations_dropped": 3}
    s = asyncio.run(r.section("solana", CA))
    assert set(s) == SECTION and s["status"] == "fresh" and s["found"] is True and s["as_of"] == at
    assert (s["mood_label"], s["activity_label"], s["why_it_matters"]) == ("leaning bullish", "A few threads", "")
    assert s["threads"][0]["url"] == POD and s["red_flags"] == [] and s["citations_dropped"] == 3
    pool.latest = pool.latest | {"read_at": at - D, "status": "none", "mood": None, "activity": "none",
                                 "summary": r.NONE_FOUND, "threads": "[]"}
    s = asyncio.run(r.section_major("btc"))
    assert s["status"] == "stale" and s["found"] is False and s["mood_label"] is None and s["summary"] == r.NONE_FOUND
    assert pool.writes[-1] == ("fetchrow", ("btc",))
    assert "'blind'" not in r.TOKEN_SQL and "status IN ('read', 'none')" in r.TOKEN_SQL == r.TOKEN_SQL
    pool.latest = None
    assert asyncio.run(r.section("solana", CA)) is None
    pool.latest = asyncpg.UndefinedTableError("relation de_reddit_reads does not exist")
    assert asyncio.run(r.section("solana", CA)) is None and asyncio.run(r.section_major("BTC")) is None


def test_mood_words_match_the_x_scout():
    from albatross.deepend import xintel
    assert [r.mood_label(m) for m in range(-5, 6)] + [r.mood_label(None)] == \
        [xintel.mood_label(m) for m in range(-5, 6)] + [None]


# ─── wiring ──────────────────────────────────────────────────────────────────
def test_registry_runs_the_scout_in_the_container_every_half_hour():
    assert "reddit" in registry.MODULES
    assert registry.JOBS[r.AGENT] is r.scan and registry.CADENCE[r.AGENT] == 1800
    assert (r.AGENT, {"minute": "12,42", "second": 5}) in registry.SCHEDULE
    assert r.AGENT not in registry.HOST_JOBS               # no CLI: an OpenRouter call


def test_the_agents_map_places_the_scout_beside_the_x_scout():
    a = r.AGENT
    assert graph.KIND[a] == graph.MODEL and graph.LABEL[a] == "Deep End Reddit scout" and graph.READS[a] == []
    assert graph.LANE[a] == "deep" and graph.ROLE[a] and graph.depth()[a] == 0
    assert graph.ORDER.index(a) == graph.ORDER.index("deep_x_scan") + 1


# ─── the SQL on a real database (ALBATROSS_TEST_DSN: a migrated Postgres, never production) ───────────────────
DSN = os.environ.get("ALBATROSS_TEST_DSN")
db_only = pytest.mark.skipif(not DSN, reason="set ALBATROSS_TEST_DSN to a migrated Postgres")
MIGRATION = Path(__file__).parents[1] / "db" / "migrations" / "031_reddit.sql"
TA, TB = "RedditTestMint11111111111111111111111111111", "RedditTestMint22222222222222222222222222222"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


@db_only
def test_the_queries_and_the_table_on_postgres(monkeypatch):
    async def go():
        conn = await asyncpg.connect(DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            await conn.execute(MIGRATION.read_text())     # a no-op where 031 is applied
            pool = _Pool(conn)

            async def fake_pool():
                return pool

            async def hot(tier=None):
                return [{"chain": "solana", "address": TA}]
            monkeypatch.setattr(db, "pool", fake_pool)
            monkeypatch.setattr(r.watch, "tokens", hot)
            now = datetime.now(UTC)
            for addr in (TA, TB):
                await conn.execute("INSERT INTO micro_tokens (chain, address, symbol, first_seen, first_seen_via) "
                                   "VALUES ('solana', $1, 'RDTSTX', $2, 'call')", addr, now - 5 * H)
                await conn.execute("INSERT INTO micro_state (chain, token, symbol, name, liquidity_usd, created_at) "
                                   "VALUES ('solana', $1, 'RDTSTX', 'Reddit Test', 42000, $2)", addr, now - 6 * H)
            for i, grp in enumerate(["alpha", "alpha", "beta"]):
                await conn.execute("INSERT INTO micro_calls (src_id, chain, address, group_name, called_at) "
                                   "VALUES ($1, 'solana', $2, $3, $4)", -900_000 - i, TA, grp, now - H)
            await conn.execute("INSERT INTO assets (symbol, name, tier, active) VALUES ('RDTSTM', 'Reddit Major', 2, TRUE)")
            for i, (impact, title) in enumerate([(4, "Reddit Major halts withdrawals"), (2, "minor"), (3, "Reddit Major lists")]):
                await conn.execute("INSERT INTO news_items (source, url, title, published_at, assets, impact) "
                                   "VALUES ('t', $1, $2, $3, ARRAY['RDTSTM'], $4)", f"https://t.example/{i}", title,
                                   now - (i + 1) * H, impact)

            (tok,) = await r.tokens()
            assert (tok["n_groups"], tok["n_calls"], tok["shared_ticker"], tok["liquidity"]) == (2, 3, 1, 42000)
            assert tok["symbol"] == "RDTSTX" and tok["name"] == "Reddit Test" and tok["last_read"] is None
            maj = next(m for m in await r.majors() if m["symbol"] == "RDTSTM")
            assert (maj["impact"], maj["items"]) == (4, 2)            # the impact-2 item is not news enough
            assert [h["title"] for h in maj["headlines"]] == ["Reddit Major halts withdrawals", "Reddit Major lists"]
            assert maj["chain"] is None and maj["headlines"][0]["at"].tzinfo is not None

            before = await r.reads_today()
            (link,), where = _world("1eeeee1")
            await r.store(tok, r.judge(_read([{"url": link, "title": "si", "posted": None}]), where, tok, now), now, None)
            await r.store(maj, r.judge(_read(), {}, maj, now), now + timedelta(milliseconds=500), None)   # no control: blind
            assert await r.reads_today() == before + 2
            (tok,) = await r.tokens()
            maj = next(m for m in await r.majors() if m["symbol"] == "RDTSTM")
            assert tok["last_read"] is not None and maj["last_failed"] is not None and maj["last_read"] is None
            s = await r.section("solana", TA)
            assert set(s) == SECTION and s["found"] and s["threads"][0]["url"] == where[link] and s["activity"] == "few"
            assert await r.section_major("rdtstm") is None              # a blind read is never served
            last = [dict(x) for x in await conn.fetch(r.LAST_SQL, r.BLIND_STREAK)]
            assert last[0]["status"] == "blind" and not r.paused_blind(last, now)
            await r.store(maj, r.judge(_read(), {g("Ctrl"): CONTROL}, maj, now), now + timedelta(seconds=1), None)
            # Reddit was reached but nothing came back: for a major that is our search's miss ('unverified'),
            # stored for the audit and never served as "no discussion"
            assert await r.section_major("rdtstm") is None
            assert await conn.fetchval("SELECT status FROM de_reddit_reads WHERE symbol = 'RDTSTM' "
                                       "ORDER BY read_at DESC LIMIT 1") == "unverified"
            with pytest.raises(asyncpg.CheckViolationError):         # a token row needs both chain and address
                await conn.execute("INSERT INTO de_reddit_reads (chain, symbol, read_at, status) "
                                   "VALUES ('solana', 'X', now(), 'none')")
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def test_dates_are_plain_iso_days():
    assert r.posted_day("2026-09-30T12:00:00Z", NOW) == date(2026, 9, 30)
    assert r.posted_day("yesterday", NOW) is None and r.posted_day(None, NOW) is None


def test_the_coin_page_shows_a_majors_read_and_survives_a_failed_one(monkeypatch):
    import asyncio

    from albatross.api import app as api

    async def read(symbol):
        return {"status": "fresh", "found": True, "mood": 1, "symbol": symbol}

    async def boom(symbol):
        raise RuntimeError("db down")

    monkeypatch.setattr(api.deep_reddit, "section_major", read)
    assert asyncio.run(api._reddit("BTC"))["symbol"] == "BTC"
    monkeypatch.setattr(api.deep_reddit, "section_major", boom)
    assert asyncio.run(api._reddit("BTC")) is None


def test_a_read_whose_search_saw_github_keeps_its_threads_but_never_its_prose(world):
    """Google's native search ignores excluded_domains (tested 2026-10-01), so GitHub is kept out after the fact:
    nothing GitHub-derived is stored, only the threads the gate proved."""
    pool, _, replies, _ = world
    gh = [{"type": "url_citation", "url_citation": {"url": g("Gh"), "title": "github.com"}}]
    replies.append(response(planted_si(), annotations=gh))
    asyncio.run(r.read(SI))
    (stored,) = pool.stored()
    assert stored["status"] == "read" and stored["threads"] and stored["summary"] is None and stored["why"] == ""
    assert stored["audit"]["github_results"] == 1
    assert r.SEARCH["parameters"]["engine"] == "native" and "excluded_domains" not in r.SEARCH["parameters"]


def test_a_majors_empty_read_is_never_served_as_no_discussion():
    links, where = _world("1ccccc1")
    got = r.judge(_read([]), where, {"symbol": "BTC"}, NOW)                # a major: no chain
    assert got["status"] == "unverified" and got["summary"] is None
    token = r.judge(_read([]), where, {"chain": "solana", "symbol": "SI"}, NOW)
    assert token["status"] == "none" and token["summary"] == r.NONE_FOUND == "Our searches returned no Reddit thread about it."
    assert "status IN ('read')" in r.MAJOR_SQL and "('read', 'none')" in r.TOKEN_SQL
