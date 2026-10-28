"""The Deep End discovery sensor and news (albatross/deepend/discovery.py): list parsing on trimmed real answers
(tests/fixtures/deep_dossier/), the radar's liquidity floor, de_watch merging, the Birdeye budget, CryptoPanic and
how a headline is matched to a token. Nothing here touches a database or the network."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import orjson
import pytest

from albatross import db
from albatross.deepend import birdeye
from albatross.deepend import discovery as d

FIX = Path(__file__).parent / "fixtures" / "deep_dossier"
NOW = datetime(2026, 9, 30, 17, 20, 35, tzinfo=UTC)


def load(name: str):
    return json.loads((FIX / name).read_text())


def test_dexscreener_lists_keep_our_chains_and_normalise_them():
    finds = d.parse_dexscreener("dexscreener profile", load("dexscreener_profiles.json"))
    assert {f["chain"] for f in finds} == {"robinhood", "solana"}                   # NEAR is not a chain we price
    rh = next(f for f in finds if f["chain"] == "robinhood")
    assert rh["address"] == "0xf922bace3fbe3f46c5de49b62e08edee1ee3cb22"             # EVM lowercase, as micro stores it
    assert rh["info"]["links"]["x"].startswith("https://x.com/")
    boosts = d.parse_dexscreener("dexscreener boost", load("dexscreener_boosts.json"))
    assert boosts[0]["info"]["logo"].startswith("https://cdn.dexscreener.com/cms/images/rAPQf82L8XUIk04L?")  # bare id
    assert boosts[0]["seen"] == {"amount": 30, "totalAmount": 30}
    assert boosts[1]["info"]["links"]["telegram"] and boosts[1]["info"]["links"]["website"]  # untyped link = site
    assert d.parse_dexscreener("dexscreener boost", None) == []


def test_birdeye_lists():
    tr = d.parse_trending("solana", load("birdeye_trending.json"))
    assert [f["rank"] for f in tr] == [1, 2, 3] and all(f["trending"] for f in tr)
    assert tr[0]["info"]["symbol"] == "Bonk" and tr[0]["pair"]["liquidity"] > 1e6
    nl = d.parse_new_listing("solana", load("birdeye_new_listing.json"))
    assert nl[0]["pair"]["created_at"] == "2026-09-30T17:17:52+00:00"               # Birdeye's naive time is UTC
    assert nl[0]["info"]["launchpad"] == "pump_dot_fun" and not nl[0]["trending"]
    assert d.parse_trending("solana", None) == [] and d.parse_new_listing("solana", {"items": None}) == []


def test_pair_stats_from_a_dexscreener_pair():
    curve, pool = load("dexscreener_tokens.json")
    s = d.pair_stats(pool)
    assert (s["symbol"], s["liquidity"], s["mcap"]) == ("SIC", 2805.17, 2609)
    assert s["links"] == {"x": "https://x.com/sicatsol", "telegram": "https://t.me/sicatsol_portal",
                          "website": "https://www.sicatsol.site"}
    c = d.pair_stats(curve)
    assert c["liquidity"] is None and c["mcap"] == 17376.84 and c["created_at"].startswith("2026-09-30")


def test_the_radar_floor():
    assert d.tradeable({"liquidity": 12_000}, 10_000)
    assert not d.tradeable({"liquidity": 4_000, "mcap": 900_000}, 10_000)       # liquidity known, and thin
    assert d.tradeable({"liquidity": None, "mcap": 25_000}, 10_000)             # a bonding curve: 2x the floor in mcap
    assert not d.tradeable({"mcap": 15_000}, 10_000)
    assert not d.tradeable(None, 10_000)


def test_merge_keeps_the_first_sighting_and_the_lists_own_readings():
    boost = d.parse_dexscreener("dexscreener boost", load("dexscreener_boosts.json"))[0]
    row = d.merge(None, [boost], NOW)
    assert row["first_seen"] == NOW and row["sources"] == ["dexscreener boost"] and row["trending_at"] is None
    later = NOW + timedelta(minutes=20)
    trend = {**d.parse_trending("solana", load("birdeye_trending.json"))[0], "address": boost["address"]}
    read = d._find("solana", boost["address"], d.PAIR_READ,
                   pair={"price": 2.0, "liquidity": 1.0, "created_at": "2026-09-30T11:00:00+00:00", "src": "dexscreener"})
    again = d.merge({**row, "meta": row["meta"]}, [read, trend], later)
    assert again["first_seen"] == NOW and again["trending_at"] == later and again["rank"] == 1
    assert again["sources"] == ["birdeye trending", "dexscreener boost"]           # display order; never the pair read
    assert again["meta"]["seen"]["dexscreener boost"]["first"] == NOW.isoformat()
    assert again["meta"]["seen"][d.PAIR_READ]["last"] == later.isoformat()        # when to read the pair again
    pair = again["meta"]["pair"]
    assert pair["liquidity"] == trend["pair"]["liquidity"] and pair["price"] == trend["pair"]["price"]
    assert pair["created_at"] == "2026-09-30T11:00:00+00:00"                       # only our read knows the pool age


def test_birdeye_lists_fit_our_share_of_the_units():
    runs = [datetime(2026, 9, 30, h, m, 35, tzinfo=UTC) for h in range(24) for m in range(0, 60, 5)]
    trending = sum(d.due(t, d.TRENDING_EVERY_MIN) for t in runs)
    listing = sum(d.due(t, d.NEW_LISTING_EVERY_MIN) for t in runs)
    assert (trending, listing) == (72, 12)
    assert trending * birdeye.CU["/defi/token_trending"] + listing * birdeye.CU["/defi/v2/tokens/new_listing"] <= 5000


class Pool:
    def __init__(self, tracked=()):
        self.tracked = tracked

    async def fetch(self, sql, *args):
        if "FROM de_watch" in sql:
            return []
        if "FROM micro_tokens" in sql:
            return [{"chain": c, "address": a} for c, a in self.tracked]
        raise AssertionError(sql)


def test_a_discovery_pass(monkeypatch):
    """Lists in, tokens under the floor out unless we already track them, one batched pair read, upsert."""
    lists = {"/token-profiles/latest/v1": load("dexscreener_profiles.json"),
             "/token-boosts/latest/v1": load("dexscreener_boosts.json"), "/token-boosts/top/v1": []}
    pairs = load("dexscreener_tokens.json")
    reads = []

    async def ds_get(path):
        return lists[path], NOW

    async def ds_tokens(chain, addresses):
        reads.append((chain, list(addresses)))
        return [p for p in pairs if p["chainId"] == chain], NOW

    async def be_get(path, params, chain):
        return load("birdeye_trending.json") if "trending" in path else load("birdeye_new_listing.json")

    written = {}

    async def upsert(table, cols, rows, conflict, update=None):
        written[table] = [dict(zip(cols, r)) for r in rows]
        return len(rows)

    tracked = {("solana", "67pyUBgbBKjMumTz4ZnQp9GaX3ag1JNVAMoLeyMipump")}
    monkeypatch.setattr(d.dexscreener, "_get", ds_get)
    monkeypatch.setattr(d.dexscreener, "tokens", ds_tokens)
    monkeypatch.setattr(d.birdeye, "get", be_get)
    monkeypatch.setattr(db, "upsert", upsert)

    async def pool():
        return Pool(tracked)
    monkeypatch.setattr(db, "pool", pool)
    monkeypatch.setenv("DEEP_WATCH_MIN_LIQ", "10000")
    n = asyncio.run(d.run(force=True))
    rows = {(r["chain"], r["address"]): r for r in written["de_watch"]}
    assert n == len(rows)
    assert ("solana", "67pyUBgbBKjMumTz4ZnQp9GaX3ag1JNVAMoLeyMipump") in rows        # tracked: kept at $2.8K
    assert ("solana", "6bMmUjQdr2cLCaQi21URJLuU8CDZZxseACBMMgWmpump") not in rows    # a curve at $17K mcap < 2x floor
    assert ("solana", "ggyVizBkhf1pVo4g8vYHzYZQuwEMwJ5nxfZAtarA849") not in rows     # new listing at $4.2K
    assert len(rows) == 4                                                          # SIC + Birdeye's top 3
    bonk = rows[("solana", "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263")]
    assert bonk["rank"] == 1 and bonk["trending_at"] and bonk["sources"] == ["birdeye trending"]
    assert all(("solana", a) not in tracked for _, addrs in reads for a in addrs)    # tracked tokens: no pair read
    meta = orjson.loads(rows[("solana", "67pyUBgbBKjMumTz4ZnQp9GaX3ag1JNVAMoLeyMipump")]["meta"])
    assert meta["links"]["telegram"] and meta["seen"]["dexscreener boost"]["totalAmount"] == 50


def test_cryptopanic_posts_become_news_items_and_votes():
    rows, votes = d.parse_posts(load("cryptopanic_posts.json")["results"])
    assert len(rows) == 2                                                          # the untitled post is dropped
    source, url, _, _, published, assets = rows[0]
    assert (source, url) == ("cryptopanic", "https://www.wire.example.com/post/370001/bonk-dao-burn")  # the article
    assert assets == ["BONK", "SOL"] and published == datetime(2026, 9, 30, 16, 41, tzinfo=UTC)
    assert rows[1][1].startswith("https://cryptopanic.com/news/") and rows[1][5] == ["SAW"]   # v1 'currencies' too
    cp = orjson.loads(votes[0][1])
    assert cp["votes"]["positive"] == 14 and cp["panic_score"] == 61 and cp["source"] == "wire.example.com"


def test_cryptopanic_without_a_key_is_quiet(monkeypatch):
    monkeypatch.setattr(d, "cryptopanic_key", lambda: None)

    def no_client():
        raise AssertionError("no request without a key")
    monkeypatch.setattr(d, "client", no_client)
    assert asyncio.run(d.cryptopanic()) == 0


def test_a_cryptopanic_failure_never_carries_the_key(monkeypatch):
    monkeypatch.setattr(d, "cryptopanic_key", lambda: "SECRET-KEY")

    class Client:
        async def get(self, url, **kw):
            raise httpx.ConnectError(f"failed {url}?auth_token={kw['params']['auth_token']}")
    monkeypatch.setattr(d, "client", Client)
    with pytest.raises(RuntimeError) as e:
        asyncio.run(d.cryptopanic())
    assert "SECRET" not in str(e.value) and e.value.__suppress_context__


def test_a_headline_names_a_token_by_contract_full_name_or_cashtag():
    m = d.news_match
    addr = "9twiuSdTVkwtAC9XQDG57dFRhF4iqPih461HJfMZKci9"
    assert m(f"New listing: {addr}", None, "u", "solana", addr, "SAW", "The Torture Chamber") == "address"
    assert m("The Torture Chamber doubles", None, "u", "solana", addr, "SAW", "The Torture Chamber") == "name"
    assert m("Traders pile into $SAW", None, "u", "solana", addr, "SAW", "The Torture Chamber") == "ticker"
    assert m("SAW blades and chainsaws", None, "u", "solana", addr, "SAW", "The Torture Chamber") is None  # bare ticker
    assert m("Mask Network rallies", None, "u", "solana", addr, "MASK", "Nullmask") is None
    assert m("Solana hits a record", None, "u", "solana", addr, "SOLANA", "Solana") is None   # a generic name
    evm = "0xcaf66eb2c00d206d741a654face34768cf2a7777"
    assert m("Contract 0xCAF66EB2C00D206D741A654FACE34768CF2A7777 renounced", None, "u", "bsc", evm, "BI", None) == "address"
