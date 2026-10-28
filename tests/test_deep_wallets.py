"""Wallet labels and clusters (albatross/deepend/wallets.py) on recorded answers. tests/fixtures/deep_wallets, recorded
2026-10-01: RugCheck's insider graph for Saw, and three first-funder walks on a Solana RPC (2R3Z… created with
0.2 SOL by 5saZ…, 5Nng… with 21.08 SOL by 5c6E…, 3XtL… with 199.8 SOL straight from Binance's hot wallet). chain.py's
recorded Birdeye top traders and RugCheck report for Saw (tests/fixtures/deep_chain). The database test at the end
needs ALBATROSS_TEST_DSN (a migrated Postgres with 029 and 032; the scenario is rolled back)."""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg
import httpx
import pytest

from albatross import controls, db
from albatross.deepend import chain as c
from albatross.deepend import dossier, registry
from albatross.deepend import wallets as w
from albatross.http import WeightLimiter

FX = Path(__file__).parent / "fixtures" / "deep_wallets"
CHAIN_FX = Path(__file__).parent / "fixtures" / "deep_chain"
SAW = "9twiuSdTVkwtAC9XQDG57dFRhF4iqPih461HJfMZKci9"
BINANCE = "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9"
FRESH, FRESH_FUNDER = "2R3Ztw8oc2kNTaohwE4nAw9XZ5QXDmpX5bP4S9Lebpx3", "5saZwsdUySMeV9jdCjFA7FMXRWRnGARLk1noHjpMtgCT"
WHALE = "5NngckKTZe98YUmt1kBASRZ77SMsojKm7YMzbN6tkFu6"
FROM_BINANCE = "3XtL7w8APRahDQh5otHuXbtao1ZZaeYZKhvt5oAAdT3x"
T = datetime(2026, 10, 1, 12, tzinfo=UTC)
M = timedelta(minutes=1)


def load(name: str, folder: Path = FX):
    return json.loads((folder / f"{name}.json").read_text())


def saw_intel() -> dict:
    """Saw's de_chain_intel row as INTEL_SQL reads it (payloads decoded)."""
    jup = next(t for t in load("jupiter_search", CHAIN_FX) if t["id"] == SAW)
    return {"chain": "solana", "address": SAW, "dev_address": jup["dev"], "traders_at": T, "rugcheck_at": T,
            "traders": load("birdeye_traders_saw", CHAIN_FX)["items"],
            "rugcheck": c.compact_rugcheck(load("rugcheck_saw", CHAIN_FX)),
            "jupiter": {"totalSupply": jup["totalSupply"], "graduatedPool": jup["graduatedPool"]}}


# ─── labels, one rule at a time ─────────────────────────────────────────────────────────────────────────────────
def test_top_trader_by_fomo_rank_or_birdeye_profit_but_never_a_bots_profit():
    assert w.rules({"fomo_rank": 150}) == (["top_trader"], {"top_trader": {"fomo_rank": 150}})
    assert w.rules({"fomo_rank": 151})[0] == []
    assert w.rules({"profitable": 3}) == (["top_trader"], {"top_trader": {"profitable_tokens": 3}})
    assert w.rules({"profitable": 2})[0] == []
    assert w.rules({"profitable": 5, "bot_tokens": 1})[0] == ["bot"]
    assert w.rules({"fomo_rank": 9, "max_trades_1h": 30})[0] == ["top_trader", "bot"]     # a busy FOMO trader is both
    # Birdeye's profit makes a top trader only of someone who trades like a person (2026-10-01: 123 of 244 "top
    # traders" were snipers, and 20 wallets were "top" on 21-97 of our tokens).
    assert w.rules({"profitable": 5, "snipes": 2})[0] == ["sniper"]
    assert w.rules({"profitable": 5, "deployed": 1})[0] == ["deployer"]
    assert w.rules({"profitable": w.TOP_TOKENS_MAX + 1})[0] == []
    assert w.rules({"profitable": w.TOP_TOKENS_MAX})[0] == ["top_trader"]


def test_sniper_bought_within_60_s_of_creation_or_birdeye_says_so():
    def row(s, roles=("buyer",)):
        return w.seen(roles, T) | {"launch_s": s}
    assert w.sniped(row(60)) and w.sniped(row(0.4)) and w.sniped(row(-10))     # FOMO times are floored to 5 s
    assert not w.sniped(row(61)) and not w.sniped(row(-11)) and not w.sniped(row(None))
    assert w.sniped(row(None, ("trader", "sniper")))                           # Birdeye times it from the chain
    assert w.rules({"snipes": 2, "fastest_s": 3.5}) == (["sniper"], {"sniper": {"tokens": 2, "fastest_s": 3.5}})


def test_a_buy_is_timed_against_the_earliest_creation_we_know():
    created = w.creation({"jup_pool": "2026-09-30T11:00:17Z", "jup_created": "2026-09-30T11:00:17Z",
                          "be_block": "1790766017", "pool_created": datetime(2026, 9, 30, 11, 6, 29, tzinfo=UTC)})
    assert created == datetime(2026, 9, 30, 11, 0, 17, tzinfo=UTC)            # Saw's mint, not its graduated pool
    assert w.creation({"pool_created": None, "jup_pool": "not a time"}) is None
    buy = w.seen({"buyer"}, created + 42 * timedelta(seconds=1), first_buy_at=created + 42 * timedelta(seconds=1))
    assert w.timed(buy, created)["launch_s"] == 42 and w.timed(buy, None)["launch_s"] is None


def test_deployer_and_the_addresses_that_are_not_people():
    assert w.rules({"deployed": 2}) == (["deployer"], {"deployer": {"tokens": 2}})
    assert w.rules({"launchpad": "Bags"})[0] == ["launchpad"]
    assert w.rules({"launches": 167_373}) == (["mass_launch"], {"mass_launch": {"launches": 167_373}})
    bags = next(iter(dossier.PLATFORM_DEVS))
    assert w.person(bags, 188_041) is None and w.person("Dev1", dossier.SHARED_DEPLOYER) is None
    assert w.person("Dev1", 4_077) == "Dev1" and w.person("Dev1", None) == "Dev1" and w.person(None, 1) is None


def test_bot_by_the_fomo_feeds_busiest_hour():
    burst = [T + 2 * i * M for i in range(30)]                                # 30 trades in 58 minutes
    assert w.busiest_hour(burst) == 30
    assert w.busiest_hour(burst[:15] + [t + timedelta(hours=1) for t in burst[15:]]) == 15
    assert w.busiest_hour([T, T + 60 * M]) == 1 and w.busiest_hour([T, T + 59 * M]) == 2 and w.busiest_hour([]) == 0
    assert w.rules({"max_trades_1h": 30}) == (["bot"], {"bot": {"max_trades_1h": 30}})
    assert w.rules({"max_trades_1h": 29})[0] == []


def test_exchange_and_service():
    assert w.rules({"exchange": "Binance"}) == (["exchange"], {"exchange": {"name": "Binance"}})
    assert w.rules({"funded": 201}) == (["service"], {"service": {"funded": 201}})
    assert w.rules({"funded": 200})[0] == []


# ─── the sources ────────────────────────────────────────────────────────────────────────────────────────────────
def test_saws_top_traders_and_holders():
    got, pairable = w.from_intel(saw_intel())
    traders = {k[2]: v for k, v in got if "trader" in v["roles"]}
    assert len(traders) == 10
    assert traders["4ZdviYkRJnuCL7BSLQCGQeAcZX33XNyeHr1zVMrF1JW"]["roles"] == {"trader", "bot"}    # 1,082 trades in 24 h
    assert traders["5BvF3EHES4HrzxXo9TSKkf3zWfMJzJmn2NeyNxynPNKx"]["roles"] == {"trader", "bot"}    # 1,370
    assert traders["2hDNyc7aj139iuwnhQAU8RZ9VdTXSkdXnduY8qbQ5rW8"]["roles"] == {"trader", "sniper"}   # the dev, Birdeye's tag
    assert round(traders["ww32L4NA8ebVtstm6okgoWHJ3Dr5VcCYFUkHRg7B9eZo"]["pnl_usd"]) == 13285          # totalPnl
    assert len(pairable) == 8 and "4ZdviYkRJnuCL7BSLQCGQeAcZX33XNyeHr1zVMrF1JW" not in pairable   # bots never pair
    holders = [v for k, v in got if "holder" in v["roles"]]
    assert len(holders) == 18                                 # RugCheck's 20 without the pump AMM and Meteora pools
    assert (holders[0]["holding_pct"], holders[0]["held_at"]) == (2.4, T)
    unpriced = {"chain": "base", "address": "0xabc", "traders_at": T,
                "traders": [{"owner": "0xAB", "tags": ["arbitrage-bot"], "trade": 3}, {"owner": None}]}
    got, pairable = w.from_intel(unpriced)
    assert [(k[2], v["roles"]) for k, v in got] == [("0xab", {"trader", "bot"})] and pairable == []   # EVM lowercased


def test_insiders_are_the_participants_of_the_tokens_rugcheck_graph():
    nets = w.compact_graph(load("rugcheck_graph_saw"))
    assert len(nets) == 4 and sum(len(n["wallets"]) for n in nets) == 44      # the report's graphInsidersDetected
    assert len(w.insiders(nets)) == 32                                          # some wallets sit in two networks
    assert nets[0]["links"][0] == {"from": "2Vmkviq4MbUqYJqHBNUakp3uck4ao8uUnQ7f3nvLNRiv",
                                   "to": "2qYBr6zp1rVEcjaexwQTE684c3B86tfAd4sSPopWLYxg", "amount": None}
    linker = nets[0]["wallets"][0]["address"]
    nets[0]["wallets"][0]["participant"] = False                                # links others, took no part
    assert linker not in w.insiders(nets) and len(w.insiders(nets)) == 31
    assert w.compact_graph(None) == [] and w.compact_graph({"error": "not found"}) == []


def test_when_an_insider_graph_is_fetched():
    r = {"token": "A", "detected": 44, "fetched_detected": None, "fetched_at": None}
    assert w.graph_due(r, set(), T)                                             # never fetched
    r |= {"fetched_detected": 44, "fetched_at": T - 25 * timedelta(hours=1)}
    assert not w.graph_due(r, set(), T) and w.graph_due(r, {"A"}, T)            # same count: a hot token, a day on
    assert not w.graph_due(r | {"fetched_at": T - 23 * timedelta(hours=1)}, {"A"}, T)
    assert w.graph_due(r | {"detected": 45}, set(), T)                          # the count moved
    assert not w.graph_due(r | {"detected": 0}, {"A"}, T) and not w.graph_due(r | {"detected": None}, {"A"}, T)


TRADERS = [
    {"handle": "OldName", "user_id": "u1", "solana_wallet": "SolA", "solana_evidence": "onchain-holdings",
     "evm_wallet": "0xAAAA", "evm_evidence": "provider-claimed", "best_rank": 12, "updated_at": T - timedelta(days=1)},
    {"handle": "NewName", "user_id": "u1", "solana_wallet": "SolA", "solana_evidence": "onchain-holdings",
     "evm_wallet": "0xAAAA", "evm_evidence": "provider-claimed", "best_rank": 9, "updated_at": T},
    {"handle": "Claimer", "user_id": "u2", "solana_wallet": "SolB", "solana_evidence": "provider-claimed",
     "evm_wallet": None, "evm_evidence": None, "best_rank": 3, "updated_at": T},
    {"handle": "Prover", "user_id": "u3", "solana_wallet": "SolB", "solana_evidence": "onchain-holdings",
     "evm_wallet": None, "evm_evidence": None, "best_rank": 140, "updated_at": T},
]


def test_fomo_traders_by_user_id_then_handle_and_proven_wallets_first():
    by_uid, by_handle, by_wallet = w.trader_index(TRADERS)
    assert by_uid["u1"]["handle"] == "NewName"                                  # renamed: the newest row is the trader
    assert w.trader_for({"user_id": None, "handle": "OLDNAME"}, by_uid, by_handle)["handle"] == "NewName"
    assert w.trader_for({"user_id": "u9", "handle": "prover"}, by_uid, by_handle)["handle"] == "Prover"
    assert by_wallet[("solana", "SolB")]["handle"] == "Prover"                  # a proven claim beats a provider's
    assert by_wallet[("evm", "0xaaaa")]["handle"] == "NewName"
    assert w.wallet_of(by_uid["u1"], "base") == ("0xaaaa", False) and w.wallet_of(by_uid["u1"], "solana") == ("SolA", True)


def test_fomo_buys_by_proven_wallets_only_the_rest_are_leads():
    by_uid, by_handle, _ = w.trader_index(TRADERS)
    exact = T + 3 * timedelta(seconds=1)
    events = [
        {"type": "buy", "user_id": "u1", "handle": "NewName", "chain": "solana", "token_address": "Tok1", "ts": T,
         "exec_ms": str(exact.timestamp() * 1000)},
        {"type": "sell", "user_id": "u1", "handle": "NewName", "chain": "solana", "token_address": "Tok1", "ts": T + M,
         "exec_ms": None},
        {"type": "buy", "user_id": "u1", "handle": "NewName", "chain": "base", "token_address": "0xTOK", "ts": T,
         "exec_ms": None},                                                      # its EVM wallet is only claimed
        {"type": "buy", "user_id": "u7", "handle": "Whale", "chain": "solana", "token_address": "Tok1", "ts": T,
         "exec_ms": None},                                                      # not on a leaderboard: no wallet known
    ]
    got, leads = w.from_fomo(events, by_uid, by_handle)
    merged = w.merge(got[0][1], got[1][1])
    assert [k for k, _ in got] == [("solana", "Tok1", "SolA")] * 2
    assert merged["roles"] == {"buyer", "seller"} and merged["first_buy_at"] == exact and merged["last_at"] == T + M
    assert leads == {("base", "0xaaaa"): "NewName"}


def test_readings_merge_roles_first_buys_and_the_newest_reading():
    old = w.seen({"trader"}, T, pnl_usd=10.0, traded_at=T) | {"first_buy_at": T + M, "launch_s": 30.0}
    new = w.seen({"holder"}, T + 5 * M, holding_pct=2.5, held_at=T + 5 * M, pnl_usd=None)
    m = w.merge(old, new)
    assert m["roles"] == {"trader", "holder"} and (m["pnl_usd"], m["holding_pct"]) == (10.0, 2.5)
    assert (m["first_at"], m["last_at"], m["launch_s"]) == (T, T + 5 * M, 30.0)
    earlier = w.merge(m, w.seen({"buyer"}, T, first_buy_at=T))
    assert earlier["first_buy_at"] == T and earlier["launch_s"] is None        # a new first buy: timed again later
    stale = w.merge(m, w.seen({"trader"}, T - M, pnl_usd=-5.0, traded_at=T - M))
    assert stale["pnl_usd"] == 10.0                                             # an older reading never wins


# ─── edges and clusters ─────────────────────────────────────────────────────────────────────────────────────────
def test_cobuys_pair_wallets_that_entered_within_10_minutes():
    s = timedelta(seconds=1)
    got = w.cobuys([("A", T), ("B", T + 10 * M), ("C", T + 10 * M + s), ("D", T + 30 * M)])
    assert got == {("A", "B"): T + 10 * M, ("B", "C"): T + 10 * M + s}         # A and C 10m01s apart; D alone
    assert w.cobuys([("B", T), ("A", T + M)], excluded={"B"}) == {}


def test_only_a_tokens_first_50_buyers_form_edges():
    buyers = [(f"W{i:03d}", T + i * timedelta(seconds=1)) for i in range(60)]
    got = w.cobuys(buyers)
    assert len(got) == 50 * 49 // 2 and not any("W050" in p for p in got)      # a crowd past the 50th
    got = w.cobuys(buyers, excluded={"W000"})                                   # a bot still counts toward the crowd
    assert len(got) == 49 * 48 // 2 and not any("W050" in p for p in got)




def test_union_find_joins_chains_and_keeps_groups_apart():
    assert w.components([("a", "b"), ("c", "d"), ("b", "c"), ("x", "y")]) == [{"a", "b", "c", "d"}, {"x", "y"}]
    assert w.components([("b", "a"), ("a", "b")]) == [{"a", "b"}] and w.components([]) == []


def test_a_bot_or_an_exchange_never_links_two_groups():
    edges = [("solana", "A", "B"), ("solana", "B", "HUB"), ("solana", "HUB", "C"), ("solana", "C", "D"),
             ("base", "A", "B")]
    assert w.cotrade_clusters(edges, set()) == [("base", {"A", "B"}), ("solana", {"A", "B", "C", "D", "HUB"})]
    assert w.cotrade_clusters(edges, {("solana", "HUB")}) == [("base", {"A", "B"}), ("solana", {"A", "B"}),
                                                              ("solana", {"C", "D"})]


def test_a_copy_trading_crowd_is_not_a_cluster():
    chain = [("solana", f"W{i:02d}", f"W{i + 1:02d}") for i in range(w.MAX_COTRADE + 4)]   # one long chain of copiers
    small = [("solana", "A", "B"), ("solana", "B", "C")]
    got = w.cotrade_clusters(chain + small, set())
    assert got == [("solana", {"A", "B", "C"})]


def test_wallets_funded_by_an_exchange_are_not_a_cluster():
    """Three of the fresh wallets we walked on 2026-10-01 were funded straight from Binance's hot wallet."""
    assert c.CEX_WALLETS[BINANCE] == "Binance"
    funded = [("solana", "mV19JYfU8RwEbFoQpBSx9S4xvy6CFwoPFWgXuoQdZf7t", BINANCE),
              ("solana", "5a6f4YTFvWETz31WXrdSQM6b4KZSSbH48P18yULP13kt", BINANCE), ("solana", FROM_BINANCE, BINANCE),
              ("solana", "X1", "F"), ("solana", "X2", "F"), ("solana", "X3", "G"), ("solana", "Launchpad1", "F")]
    blocked = w.services(funded)
    assert blocked == {("solana", BINANCE): 3}                                 # a known exchange, however few
    assert w.funding_clusters(funded, set(blocked), {("solana", "Launchpad1")}) == [("solana", "F", {"X1", "X2"})]
    assert ("solana", BINANCE, {"mV19JYfU8RwEbFoQpBSx9S4xvy6CFwoPFWgXuoQdZf7t", "5a6f4YTFvWETz31WXrdSQM6b4KZSSbH48P18yULP13kt",
                                FROM_BINANCE}) in w.funding_clusters(funded, set(), set())   # what the rule prevents


def test_an_app_that_sponsors_wallets_links_nobody_but_a_repeat_launcher_does():
    """2026-10-01: 3jbNKwes... sent exactly 0.01 SOL to four of our wallets between Apr 28 and Sep 22, two of them
    FOMO users: an activation sponsor, not a bundler. Funders whose 1-20 SOL grants spread over weeks were repeat
    launchers seeding deployers and snipers: they must keep linking wallets."""
    d = lambda n: T - timedelta(days=n)                                        # noqa: E731
    grants = [("solana", "U1", "Sponsor", d(150), 0.01), ("solana", "U2", "Sponsor", d(40), 0.01),
              ("solana", "D1", "Launcher", d(300), 1.9), ("solana", "D2", "Launcher", d(8), 19.84),
              ("solana", "B1", "Bundler", d(1), 0.8), ("solana", "B2", "Bundler", d(1) + M, 0.8),
              ("solana", "S1", "Single", d(1), 0.01), ("solana", "M1", "Mixed", d(3), 0.01),
              ("solana", "M2", "Mixed", d(2), 1.0)]
    assert w.sponsors(grants) == {("solana", "Sponsor"): "dust"}


def test_a_funder_of_more_than_200_of_our_wallets_is_a_service():
    many = [("solana", f"W{i}", "Hub") for i in range(201)]
    assert w.services(many) == {("solana", "Hub"): 201} and w.services(many[:200]) == {}
    assert w.funding_clusters(many, set(w.services(many)), set()) == []


def test_a_cluster_row():
    tokens = {"A": [("T1", T, T + M), ("T2", T, T)], "B": [("T1", T - M, T), ("T3", T, T + 2 * M)]}
    row = w.cluster_row("cotrade:solana:A", "solana", "cotrade", None, {"B", "A"}, tokens,
                        {"A": ["sniper", "bot"], "B": ["sniper"]}, T)
    assert row == ("cotrade:solana:A", "solana", "cotrade", None, 2, ["A", "B"], '{"bot":1,"sniper":2}', ["T1"], 1,
                   T - M, T + 2 * M, T)


# ─── first funders on recorded RPC answers ──────────────────────────────────────────────────────────────────────
class Recorded:
    """Serves recorded walks' RPC answers; anything else is a busy wallet (a full page of 1,000 signatures, as
    4ZdviYkR… returned on 2026-10-01)."""

    def __init__(self, *walks, extra: dict | None = None):
        self.answers = {(x["method"], x["params"][0]): x["result"] for name in walks for x in load(name)["calls"]}
        self.answers |= extra or {}
        self.calls: list = []

    async def __call__(self, method, params):
        self.calls.append((method, params[0]))
        if (method, params[0]) in self.answers:
            return self.answers[(method, params[0])]
        assert method == "getSignaturesForAddress"
        return [{"signature": f"sig{i}", "err": None} for i in range(w.SIG_LIMIT)]


def test_a_fresh_wallet_walks_back_to_its_funder():
    rpc = Recorded("funder_0p2_sol")
    got = asyncio.run(w.first_funder(FRESH, rpc))
    assert got == {"status": "found", "signatures": 162, "funder": FRESH_FUNDER, "lamports": 200_000_000,
                   "tx": "LsiHHRk3S1YUhzPZBtnLQ2vgkKWArGsiZn3u7QzTisyK5rbzF3MBwqn9jaFdoJvciWLQXijqm4bGEp46SH1tH5d4",
                   "at": datetime(2026, 9, 8, 8, 4, 3, tzinfo=UTC)}
    assert [m for m, _ in rpc.calls] == ["getSignaturesForAddress", "getTransaction"]   # 2 calls
    big = asyncio.run(w.first_funder(WHALE, Recorded("funder_21_sol")))
    assert (big["funder"], big["lamports"]) == ("5c6EoTt6ARVQtWpUufWEMBkiNaKSdWJQcT3szhnT7Y9i", 21_080_187_044)
    cex = asyncio.run(w.first_funder(FROM_BINANCE, Recorded("funder_binance")))
    assert (cex["funder"], cex["signatures"], cex["lamports"]) == (BINANCE, 64, 199_799_000_000)


def test_a_wallet_with_1000_signatures_is_not_fresh_after_one_call():
    rpc = Recorded()
    assert asyncio.run(w.first_funder("BusyWallet", rpc)) == {"status": "not fresh", "signatures": 1000}
    assert rpc.calls == [("getSignaturesForAddress", "BusyWallet")]


def test_a_wallet_that_already_held_sol_means_the_history_is_incomplete():
    walk = load("funder_0p2_sol")["calls"]
    tx = json.loads(json.dumps(walk[1]["result"]))
    i = [k["pubkey"] for k in tx["transaction"]["message"]["accountKeys"]].index(FRESH)
    tx["meta"]["preBalances"][i] = 5_000                                      # it held SOL before its "first" transaction
    rpc = Recorded("funder_0p2_sol", extra={("getTransaction", walk[1]["params"][0]): tx})
    assert asyncio.run(w.first_funder(FRESH, rpc)) == {"status": "history incomplete", "signatures": 162}
    assert w.funding(tx, FRESH) == w.INCOMPLETE and w.funding(walk[1]["result"], "SomeoneElse") is None


def test_the_walk_skips_failed_and_unfunding_transactions_up_to_three():
    walk = load("funder_0p2_sol")["calls"]
    sigs, other = walk[0]["result"], load("funder_21_sol")["calls"][1]["result"]   # a transaction that never names FRESH
    older = [{"signature": "failed", "err": {"InstructionError": [0, "Custom"]}}, {"signature": "noise1", "err": None}]
    rpc = Recorded("funder_0p2_sol", extra={("getSignaturesForAddress", FRESH): sigs + older,
                                            ("getTransaction", "noise1"): other})
    got = asyncio.run(w.first_funder(FRESH, rpc))
    assert got["funder"] == FRESH_FUNDER and got["signatures"] == 164
    assert rpc.calls == [("getSignaturesForAddress", FRESH), ("getTransaction", "noise1"),
                         ("getTransaction", walk[1]["params"][0])]               # the failed one is never fetched
    noise = [{"signature": f"noise{i}", "err": None} for i in range(4)]
    rpc = Recorded(extra={("getSignaturesForAddress", "W"): noise, **{("getTransaction", f"noise{i}"): other for i in range(4)}})
    assert asyncio.run(w.first_funder("W", rpc)) == {"status": "no transfer", "signatures": 4}
    assert len(rpc.calls) == 1 + w.FUNDER_TX_TRIES


# ─── budgets ────────────────────────────────────────────────────────────────────────────────────────────────────
class Answer:
    def __init__(self, status: int = 200, body=None, raises: Exception | None = None):
        self.status, self.body, self.raises, self.posted = status, body, raises, []

    async def post(self, url, json, timeout):
        self.posted.append(json["method"])
        if self.raises:
            raise self.raises
        return httpx.Response(self.status, json=self.body if self.body is not None else {"result": []})


@pytest.fixture
def rpc_env(monkeypatch):
    """rpc() on a fake endpoint: (usage counter, metered providers, the answer)."""
    used, metered, answer = {"n": 0}, [], Answer()

    async def used_today(provider):
        return used["n"]

    async def meter(provider):
        metered.append(provider)
        used["n"] += 1
    monkeypatch.setattr(w, "used_today", used_today)
    monkeypatch.setattr(w, "_meter", meter)
    monkeypatch.setattr(w, "rpc_url", lambda: "https://solana.test/SECRET-KEY")
    monkeypatch.setattr(w, "client", lambda: answer)
    monkeypatch.setattr(w, "_rpc_pace", WeightLimiter(per_minute=1e9))
    monkeypatch.setenv("DEEP_FUNDER_DAILY_CALLS", "2000")
    return used, metered, answer


def test_every_rpc_call_is_metered_and_none_passes_the_days_calls(rpc_env):
    used, metered, answer = rpc_env
    used["n"] = 1998
    assert asyncio.run(w.rpc("getSignaturesForAddress", ["W"])) == [] and metered == ["chainstack"]
    assert asyncio.run(w.rpc("getSignaturesForAddress", ["W"])) == []         # call 2,000
    with pytest.raises(w.BudgetExceeded):
        asyncio.run(w.rpc("getSignaturesForAddress", ["W"]))
    assert answer.posted == ["getSignaturesForAddress"] * 2 and len(metered) == 2   # the refused call was never sent


def test_the_days_calls_follow_the_setting_and_the_output_pages_scale(monkeypatch):
    monkeypatch.setenv("DEEP_FUNDER_DAILY_CALLS", "300")
    assert asyncio.run(w.daily_calls()) == 300

    async def half(key):
        return 0.5 if key == "deep_funders" else 1.0
    monkeypatch.setattr(controls, "scale", half)
    assert asyncio.run(w.daily_calls()) == 150


def test_an_rpc_failure_never_carries_the_url(rpc_env):
    _, _, answer = rpc_env
    answer.raises = httpx.ConnectError("cannot reach https://solana.test/SECRET-KEY")
    with pytest.raises(w.RpcFailed) as e:
        asyncio.run(w.rpc("getTransaction", ["sig"]))
    assert "SECRET-KEY" not in str(e.value) and e.value.__suppress_context__
    answer.raises, answer.status = None, 500
    with pytest.raises(w.RpcFailed, match="HTTP 500"):
        asyncio.run(w.rpc("getTransaction", ["sig"]))
    answer.status = 429
    with pytest.raises(w.Throttled):
        asyncio.run(w.rpc("getTransaction", ["sig"]))
    answer.status, answer.body = 200, {"error": {"code": -32009, "message": "method not available"}}
    with pytest.raises(w.RpcFailed, match="-32009"):
        asyncio.run(w.rpc("getTransaction", ["sig"]))


class FakePool:
    """Answers wallets.py's queries with canned rows (by the SQL constant) and records what it is asked and written."""

    def __init__(self, rows: dict | None = None, tables=()):
        self.rows, self.tables, self.fetched, self.writes = rows or {}, set(tables), [], []

    async def fetch(self, sql, *args):
        self.fetched.append(sql)
        rows = self.rows.get(sql, [])
        if isinstance(rows, Exception):
            raise rows
        return rows

    async def fetchrow(self, sql, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        if "to_regclass" in sql:
            return args[0] in self.tables
        raise AssertionError(sql)

    async def execute(self, sql, *args):
        self.writes.append((sql, args))

    async def executemany(self, sql, rows):
        self.writes.append((sql, list(rows)))

    @asynccontextmanager
    async def acquire(self):
        yield self

    @asynccontextmanager
    async def transaction(self):
        yield


def use(monkeypatch, pool: FakePool) -> dict:
    """db.pool() -> pool; db.upsert recorded as {table: [row dicts]}."""
    upserts: dict = {}

    async def get_pool():
        return pool

    async def upsert(table, cols, rows, conflict, update=None):
        upserts.setdefault(table, []).extend(dict(zip(cols, r)) for r in rows)
        return len(rows)
    monkeypatch.setattr(db, "pool", get_pool)
    monkeypatch.setattr(db, "upsert", upsert)
    return upserts


def test_deep_funders_spends_the_paced_allowance_then_stops(monkeypatch):
    pool = FakePool({w.CANDIDATES_SQL: [{"address": a} for a in (FRESH, "Busy1", FROM_BINANCE, "Busy2", "Busy3")]})
    use(monkeypatch, pool)
    asked = []

    def allowance(share, spent, now):
        asked.append((share, spent))
        return 9

    async def used_today(provider):
        return 40
    rpc = Recorded("funder_0p2_sol", "funder_binance")
    monkeypatch.setattr(c, "allowance", allowance)
    monkeypatch.setattr(w, "used_today", used_today)
    monkeypatch.setattr(w, "rpc_url", lambda: "https://solana.test/KEY")
    monkeypatch.setattr(w, "rpc", rpc)
    monkeypatch.setenv("DEEP_FUNDER_DAILY_CALLS", "2000")
    assert asyncio.run(w.funders()) == 4                     # 2 + 1 + 2 + 1 calls; then 3 left, under one full walk
    assert asked == [(2000, 40)] and len(rpc.calls) == 6
    stored = {a[0]: a for sql, a in pool.writes if sql is w.FUNDER_SQL}
    assert set(stored) == {FRESH, "Busy1", FROM_BINANCE, "Busy2"}
    assert stored[FRESH][1:7] == ("found", 162, FRESH_FUNDER, datetime(2026, 9, 8, 8, 4, 3, tzinfo=UTC), 0.2,
                                  "LsiHHRk3S1YUhzPZBtnLQ2vgkKWArGsiZn3u7QzTisyK5rbzF3MBwqn9jaFdoJvciWLQXijqm4bGEp46SH1tH5d4")
    assert stored["Busy1"][1:4] == ("not fresh", 1000, None)


def test_deep_funders_waits_when_the_allowance_cannot_fund_one_walk(monkeypatch):
    pool = FakePool()
    use(monkeypatch, pool)

    async def used_today(provider):
        return 0
    monkeypatch.setattr(c, "allowance", lambda share, spent, now: w.FUNDER_TX_TRIES)
    monkeypatch.setattr(w, "used_today", used_today)
    monkeypatch.setattr(w, "rpc_url", lambda: "https://solana.test/KEY")
    assert asyncio.run(w.funders()) == 0 and pool.fetched == []
    monkeypatch.setattr(w, "rpc_url", lambda: None)                            # no endpoint: nothing at all
    monkeypatch.setattr(c, "allowance", lambda share, spent, now: 100)
    assert asyncio.run(w.funders()) == 0 and pool.fetched == []


def test_deep_funders_stops_on_the_days_ceiling_and_on_repeated_failures(monkeypatch):
    pool = FakePool({w.CANDIDATES_SQL: [{"address": f"W{i}"} for i in range(6)]})
    use(monkeypatch, pool)

    async def used_today(provider):
        return 0
    monkeypatch.setattr(c, "allowance", lambda share, spent, now: 60)
    monkeypatch.setattr(w, "used_today", used_today)
    monkeypatch.setattr(w, "rpc_url", lambda: "https://solana.test/KEY")

    async def spent(method, params):
        raise w.BudgetExceeded("chainstack: the day's 2,000 calls are spent")
    monkeypatch.setattr(w, "rpc", spent)
    assert asyncio.run(w.funders()) == 0 and pool.writes == []                 # nothing marked: they wait

    async def broken(method, params):
        raise w.RpcFailed("getSignaturesForAddress: HTTP 503")
    monkeypatch.setattr(w, "rpc", broken)
    assert asyncio.run(w.funders()) == w.MAX_FAILURES
    assert [a[1] for _, a in pool.writes] == ["failed"] * w.MAX_FAILURES       # retried after RETRY_FAILED


def test_insider_graphs_stay_inside_the_run_and_the_day(monkeypatch):
    rows = [{"token": f"T{i}", "rugcheck_at": T - i * M, "detected": 5, "fetched_detected": None, "fetched_at": None}
            for i in range(30)]
    rows.append({"token": "Old", "rugcheck_at": T, "detected": 5, "fetched_detected": 5, "fetched_at": T - M})
    pool = FakePool({w.GRAPH_DUE_SQL: rows})
    use(monkeypatch, pool)
    used, fetched = {"n": 490}, []

    async def used_today(provider):
        assert provider == "rugcheck"
        return used["n"]

    async def hot(tier=None):
        return [{"chain": "solana", "address": "T7"}]

    async def graph(mint):
        fetched.append(mint)
        return [{"id": "n", "type": "transfer", "links": [],
                 "wallets": [{"address": f"{mint}-in", "participant": True, "holdings": 1.0},
                             {"address": f"{mint}-link", "participant": False, "holdings": 0.0}]}]
    monkeypatch.setattr(w, "used_today", used_today)
    monkeypatch.setattr(w.watch, "tokens", hot)
    monkeypatch.setattr(w, "fetch_graph", graph)
    b = w.Batch()
    assert asyncio.run(w._from_graphs(b, T)) == 10                              # 500 a day, 490 already spent
    assert fetched == ["T7", "T0", "T1", "T2", "T3", "T4", "T5", "T6", "T8", "T9"]   # hot first, then the newest report
    assert set(b.obs) == {("solana", t, f"{t}-in") for t in fetched}            # participants only
    used["n"] = 0
    assert asyncio.run(w._from_graphs(w.Batch(), T)) == w.GRAPHS_PER_RUN


def test_a_failed_or_throttled_graph_never_fails_the_run(monkeypatch):
    rows = [{"token": f"T{i}", "rugcheck_at": T - i * M, "detected": 5, "fetched_detected": None, "fetched_at": None}
            for i in range(5)]
    pool = FakePool({w.GRAPH_DUE_SQL: rows})
    use(monkeypatch, pool)

    async def used_today(provider):
        return 0

    async def hot(tier=None):
        return []

    fetched = []

    async def graph(mint):
        fetched.append(mint)
        if mint == "T0":
            return None                                                         # a 404 or 5xx, logged
        if mint == "T2":
            raise w.Throttled("rugcheck: HTTP 429")
        return []
    monkeypatch.setattr(w, "used_today", used_today)
    monkeypatch.setattr(w.watch, "tokens", hot)
    monkeypatch.setattr(w, "fetch_graph", graph)
    assert asyncio.run(w._from_graphs(w.Batch(), T)) == 1                       # T1 stored, T2 stopped the round
    assert fetched == ["T0", "T1", "T2"]
    assert [a[1] for sql, a in pool.writes if sql is w.GRAPH_UPSERT_SQL] == ["T1"]   # T0 stays due: tried next run


def test_a_run_without_the_fomo_tables(monkeypatch):
    """The FOMO lane's migration not applied yet: the run labels and pairs from chain intel alone."""
    intel = saw_intel()
    as_db = intel | {k: json.dumps(intel[k]) for k in ("rugcheck", "traders", "jupiter")} | \
        {"birdeye_holders": None, "birdeye_holders_at": None, "pools": None, "at": T}
    pool = FakePool({w.INTEL_SQL: [as_db],
                     w.DEPLOYERS_SQL: [{"chain": "solana", "token": SAW, "dev_address": intel["dev_address"],
                                        "updated_at": T}]})
    upserts = use(monkeypatch, pool)

    async def none(*a, **k):
        return []

    async def zero(provider):
        return 0
    monkeypatch.setattr(w.watch, "tokens", none)
    monkeypatch.setattr(w, "used_today", zero)
    out = asyncio.run(w.run())
    assert w.TRADERS_SQL not in pool.fetched and w.EVENTS_SQL not in pool.fetched
    got, _ = w.from_intel(intel)
    wallets = {k[2] for k, _ in got}
    assert out["readings"] == len(wallets) == 28 and out["edges"] == 0   # no FOMO buys: no co-trading edges
    rows = {r["address"]: r for r in upserts["de_wallet_tokens"]}
    assert rows[intel["dev_address"]]["roles"] == ["deployer", "sniper", "trader"]
    assert {r["address"] for r in upserts["de_wallets"]} == wallets
    assert not any(sql is w.EDGE_UPSERT_SQL for sql, _ in pool.writes)        # top-trader pair edges: no reader, none written
    cursors = [a for sql, a in pool.writes if "de_wallet_cursor" in sql]
    assert cursors == [("chain_intel", T)]                                      # FOMO's cursor waits for its tables


def test_section_is_none_before_the_migration_or_without_wallets(monkeypatch):
    pool = FakePool({w.SECTION_SQL: asyncpg.UndefinedTableError("relation does not exist")})
    use(monkeypatch, pool)
    assert asyncio.run(w.section("solana", SAW)) is None
    pool.rows[w.SECTION_SQL] = []
    assert asyncio.run(w.section("solana", SAW)) is None


# ─── the dossier section ────────────────────────────────────────────────────────────────────────────────────────
def row(addr: str, roles=(), labels=(), **kw) -> dict:
    return {"address": addr, "roles": list(roles), "labels": list(labels), "first_buy_at": None, "launch_s": None,
            "holding_pct": None, "held_at": None, "traded_at": None, "pnl_usd": None, "last_at": T,
            "first_funder": None, "funder_status": None, "cluster_id": None, "funding_cluster_id": None,
            "fomo_handle": None} | kw


def test_the_section_numbers_and_findings():
    report, older = T, T - timedelta(hours=2)
    rows = [
        row("DevWallet", ["deployer", "trader", "sniper"], ["deployer", "sniper"], first_funder="FunderF0000001"),
        row("A", ["buyer"], ["top_trader", "sniper"], first_buy_at=T, launch_s=20.0, cluster_id="cotrade:solana:A"),
        row("B", ["buyer"], ["sniper"], first_buy_at=T + 3 * M, cluster_id="cotrade:solana:A"),
        *[row(f"E{i}", ["buyer"], first_buy_at=T + (10 + i) * M) for i in range(4)],
        row("OtherDev", ["holder"], ["deployer"], held_at=report, holding_pct=1.0),
        row("H1", ["holder", "insider"], held_at=report, holding_pct=5.0, first_funder="FunderF0000001",
            funder_status="found", funding_cluster_id="funding:solana:FunderF0000001"),
        row("H2", ["holder"], ["bot"], held_at=report, holding_pct=4.0, first_funder="FunderF0000001",
            funder_status="found", funding_cluster_id="funding:solana:FunderF0000001"),
        row("H3", ["holder"], held_at=report, holding_pct=3.0, first_funder="DevWallet", funder_status="found"),
        row("H4", ["holder"], held_at=report, holding_pct=2.0, first_funder=BINANCE, funder_status="found"),
        row("H5", ["holder"], held_at=report, holding_pct=0.5, funder_status="not fresh"),
        row("Gone", ["holder"], held_at=older, holding_pct=9.0, first_funder="DevWallet", funder_status="found"),
    ]
    clusters = {"cotrade:solana:A": {"kind": "cotrade", "size": 5, "funder": None, "labels": {"sniper": 3}},
                "funding:solana:FunderF0000001": {"kind": "funding", "size": 3, "funder": "FunderF0000001",
                                                  "labels": {"bot": 1}}}
    s = w.shape(rows, clusters, "DevWallet", "FunderF0000001", {BINANCE})
    assert s["wallets"] == 14
    assert s["labels"] == {"top_trader": 1, "sniper": 3, "insider": 1, "bot": 1, "deployer": 1}   # its own dev aside
    assert s["clusters"] == [
        {"id": "cotrade:solana:A", "kind": "cotrade", "size": 5, "here": 2, "bought": 2, "within_min": 3.0,
         "funded_lead_h": None, "funder": None, "labels": {"sniper": 3}},
        {"id": "funding:solana:FunderF0000001", "kind": "funding", "size": 3, "here": 2, "bought": 0,
         "within_min": None, "funded_lead_h": None, "funder": "FunderF0000001", "labels": {"bot": 1}}]
    assert s["early"] == {"buyers": 6, "snipers": 2, "share": 0.33, "sniped_it": 1}
    assert s["funding"] == {"holders": 6, "checked": 5, "funders_known": 4, "by_deployer": ["H3"],
                            "shared": [{"funder": "FunderF0000001", "holders": ["H1", "H2"], "deployer_too": True}]}
    assert [h["owner"] for h in s["top_holders"]] == ["H1", "H2", "H3", "H4", "OtherDev"]
    assert s["top_holders"][0] == {"owner": "H1", "pct": 5.0, "labels": ["insider"], "first_funder": "FunderF0000001"}
    assert s["findings"] == [
        "Of the 14 wallets seen trading or holding it: 1 ranked trader, 3 snipers, 1 insider, 1 bot, "
        "1 deployer of another coin.",
        "2 wallets that often trade together (5 in their group) are on it, 2 of them bought within 3 minutes of "
        "each other; on FOMO that is often copy-trading.",
        "2 wallets created by one funder (Fund…0001, 3 of the wallets we know) are on it.",
        "2 of the first 6 FOMO buyers we saw on it are snipers (33%).",
        "1 top holder was funded by its deployer.",
        "2 top holders were funded by one wallet (Fund…0001), as was its deployer."]


def test_the_section_says_so_when_nothing_stands_out():
    s = w.shape([row("X", ["trader"]), row("Y", ["holder"], held_at=T)], {}, None, None, set())
    assert s["findings"] == ["None of the 2 wallets seen around it carries a label yet."]
    assert (s["early"], s["clusters"], s["funding"]["shared"]) == (None, [], [])
    assert w.shape([], {}, None, None, set()) is None


def test_an_insider_elsewhere_is_not_an_insider_here():
    rows = [row("I", ["trader"], ["sniper"])]                                   # an insider on another coin only
    assert w.shape(rows, {}, None, None, set())["labels"] == {"sniper": 1}


# ─── registry ───────────────────────────────────────────────────────────────────────────────────────────────────
def test_jobs_schedules_and_cadences_line_up():
    assert set(w.JOBS) == {n for n, _ in w.SCHEDULE} == set(w.CADENCE) == {"deep_wallets", "deep_funders"}
    assert "wallets" in registry.MODULES and registry.JOBS["deep_funders"] is w.funders
    assert registry.CADENCE["deep_wallets"] == 900 and registry.CADENCE["deep_funders"] == 600


# ─── the shared tables (needs ALBATROSS_TEST_DSN, a migrated Postgres; the scenario is rolled back) ──────────────
DSN = os.environ.get("ALBATROSS_TEST_DSN")
db_only = pytest.mark.skipif(not DSN, reason="set ALBATROSS_TEST_DSN to a migrated Postgres (029 and 032 applied)")


class _Pool:
    """db.pool() stand-in: every acquire() hands out the test's connection."""

    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn

    def __getattr__(self, name):
        return getattr(self.conn, name)


def addr(name: str) -> str:
    return (name + "1" * 44)[:44]


@db_only
def test_a_run_against_the_real_tables(monkeypatch):
    """FOMO buys and chain intel in, labels, edges, clusters, the section and the funder queue out, through every SQL
    statement wallets.py sends."""
    A, B, C_, X, Y, D1, F = (addr(n) for n in ("TstA", "TstB", "TstC", "TstX", "TstY", "TstDev", "TstFunder"))
    H1, H2, H3, H4, I1, E1, E2 = (addr(n) for n in ("TstH1", "TstH2", "TstH3", "TstH4", "TstI1", "TstE1", "TstE2"))
    T1, T2, T3, T4 = (addr(f"TstTok{i}") for i in range(1, 5))

    async def scenario(conn):
        if not await conn.fetchval("SELECT to_regclass('de_fomo_events') IS NOT NULL"):
            pytest.skip("the FOMO tables (029) are not in this database")
        now = datetime.now(UTC)
        start = now - 50 * M
        for handle, wallet, rank in (("tst_a", A, 5), ("tst_b", B, 500), ("tst_c", C_, 600)):
            await conn.execute("""INSERT INTO de_fomo_traders (handle, user_id, solana_wallet, solana_evidence, best_rank,
                                  updated_at) VALUES ($1, $1, $2, 'onchain-holdings', $3, now())""", handle, wallet, rank)
        events = []
        for i, tok in enumerate((T1, T2, T3)):
            events += [(f"tst-a{i}", start + 10 * i * M, "buy", "tst_a", tok),
                       (f"tst-b{i}", start + (10 * i + 2) * M, "buy", "tst_b", tok)]
        events.append(("tst-c-late", start + 30 * M, "buy", "tst_c", T1))
        events += [(f"tst-c{i}", start + i * M, "sell", "tst_c", T4) for i in range(30)]
        for eid, ts, kind, handle, tok in events:
            raw = {"fillMatch": "onchain-exact", "execTs": int(ts.timestamp() * 1000)} if eid == "tst-a0" else {}
            await conn.execute("""INSERT INTO de_fomo_events (event_id, ts, type, handle, user_id, chain, token_address,
                                  raw, received_at) VALUES ($1, $2, $3, $4, $4, 'solana', $5, $6::jsonb, now())""",
                               eid, ts, kind, handle, tok, json.dumps(raw))
        holders = [{"owner": h, "address": f"acct{i}", "pct": 5.0 - i, "insider": False}
                   for i, h in enumerate((H1, H2, H3, H4))]
        await conn.execute(
            """INSERT INTO de_chain_intel (chain, address, jupiter, jupiter_at, rugcheck, rugcheck_at, birdeye_traders,
                                           birdeye_traders_at, dev_address, dev_launches)
               VALUES ('solana', $1, $2::jsonb, now(), $3::jsonb, now(), $4::jsonb, now(), $5, 3)""",
            T1, json.dumps({"createdAt": (start - 30 * timedelta(seconds=1)).isoformat(), "totalSupply": 1e9}),
            json.dumps({"topHolders": holders, "knownAccounts": {}, "markets": [], "graphInsidersDetected": 2}),
            json.dumps({"items": [{"owner": X, "tags": ["sniper"], "trade": 10, "totalPnl": 100},
                                  {"owner": Y, "tags": [], "trade": 800, "totalPnl": 50}]}), D1)

        async def graph(mint):
            return [{"id": "n1", "type": "transfer", "links": [{"from": D1, "to": I1, "amount": None}],
                     "wallets": [{"address": H1, "participant": True, "holdings": 1.0},
                                 {"address": I1, "participant": True, "holdings": 1.0},
                                 {"address": D1, "participant": False, "holdings": 0.0}]}] if mint == T1 else []

        async def no_heat(tier=None):
            return []
        monkeypatch.setattr(w, "fetch_graph", graph)
        monkeypatch.setattr(w.watch, "tokens", no_heat)

        since, b = now - M, w.Batch()
        assert await w._from_chain_intel(b, since) is not None
        await w._from_deployers(b)
        assert await w._from_graphs(b, now) >= 1
        idx = await w._traders()
        assert await w._from_fomo(b, idx, since) is not None
        await w._store_tokens(b, now)
        await w._relabel(b, idx, now)
        await w._store_edges(b, now)

        def labels(x):
            return conn.fetchval("SELECT labels FROM de_wallets WHERE chain = 'solana' AND address = $1", x)
        assert set(await labels(A)) == {"top_trader", "sniper"}                 # rank 5; bought 30 s after creation
        assert "bot" in await labels(C_) and "bot" in await labels(Y)           # 30 FOMO trades in 30 min; 800 in 24 h
        assert await labels(X) == ["sniper"] and await labels(D1) == ["deployer"] and await labels(H1) == []
        assert await conn.fetchval("SELECT launch_s FROM de_wallet_tokens WHERE token = $1 AND address = $2", T1, A) == 30
        assert await conn.fetchval("SELECT roles FROM de_wallet_tokens WHERE token = $1 AND address = $2", T1, H1) \
            == ["holder", "insider"]                                            # the insider is scoped to T1
        edge = await conn.fetchrow("SELECT n, tokens FROM de_wallet_edges WHERE chain = 'solana' AND a = $1 AND b = $2 "
                                   "AND kind = 'cotrade'", min(A, B), max(A, B))
        assert edge["n"] == 3 and sorted(edge["tokens"]) == sorted([T1, T2, T3])
        assert not await conn.fetchval("SELECT count(*) FROM de_wallet_edges WHERE kind = 'cotrade' AND $1 IN (a, b)", C_)

        for wallet, funder in ((H1, F), (H2, F), (D1, F), (C_, F), (H3, D1), (E1, BINANCE), (E2, BINANCE)):
            await conn.execute(
                """INSERT INTO de_wallets (chain, address, first_seen) VALUES ('solana', $1, now()) ON CONFLICT DO NOTHING""",
                wallet)
            await conn.execute("""UPDATE de_wallets SET funder_status = 'found', first_funder = $2, funder_checked_at = now()
                                  WHERE chain = 'solana' AND address = $1""", wallet, funder)
        await w.recluster(now)
        cluster = await conn.fetchrow("SELECT * FROM de_wallet_clusters WHERE cluster_id = $1", f"cotrade:solana:{min(A, B)}")
        assert sorted(cluster["members"]) == sorted([A, B]) and cluster["kind"] == "cotrade" and cluster["n_tokens"] == 3
        funding = await conn.fetchrow("SELECT * FROM de_wallet_clusters WHERE cluster_id = $1", f"funding:solana:{F}")
        assert sorted(funding["members"]) == sorted([H1, H2, D1, C_]) and funding["funder"] == F   # a bot may join
        assert not await conn.fetchval("SELECT count(*) FROM de_wallet_clusters WHERE funder = $1", BINANCE)
        assert "exchange" in await labels(BINANCE)

        s = await w.section("solana", T1)
        assert s["labels"]["sniper"] == 2 and s["labels"]["insider"] == 2 and s["labels"]["top_trader"] == 1
        kinds = {x["kind"]: x for x in s["clusters"]}
        assert (kinds["cotrade"]["here"], kinds["cotrade"]["within_min"]) == (2, 2.0)
        assert kinds["funding"]["here"] == 4                       # H1, H2, the dev and the bot C
        assert s["funding"]["by_deployer"] == [H3]
        assert s["funding"]["shared"] == [{"funder": F, "holders": sorted([H1, H2]), "deployer_too": True}]

        posted = []

        async def busy(method, params):
            posted.append(params[0])
            return [{"signature": f"s{i}", "err": None} for i in range(w.SIG_LIMIT)]
        monkeypatch.setattr(w, "rpc_url", lambda: "https://solana.test/KEY")
        monkeypatch.setattr(w, "rpc", busy)
        monkeypatch.setattr(c, "allowance", lambda share, spent, now: 6)
        assert await w.funders() == 3
        assert posted[0] == H4                          # the one current top holder not checked yet goes first
        assert await conn.fetchval("SELECT funder_status FROM de_wallets WHERE address = $1", H4) == "not fresh"

    async def go():
        conn = await asyncpg.connect(DSN)
        tx = conn.transaction()
        await tx.start()
        try:
            pool = _Pool(conn)

            async def fake_pool():
                return pool
            monkeypatch.setattr(db, "pool", fake_pool)
            await scenario(conn)
        finally:
            await tx.rollback()
            await conn.close()
    asyncio.run(go())


def test_stale_readings_and_empty_wallets_are_pruned(monkeypatch):
    seen = []

    class P:
        async def execute(self, sql, *args):
            seen.append(sql)
            return "DELETE 40" if "de_wallet_tokens WHERE ctid" in sql else "DELETE 7"

    async def pool():
        return P()
    monkeypatch.setattr(w.db, "pool", pool)
    assert asyncio.run(w.prune(T)) == {"wallet_tokens": 40, "wallets": 7}
    assert "first_funder IS NULL" in seen[1] and "cardinality(w2.labels) = 0" in seen[1]   # learned ones are kept


def test_a_huge_insider_graph_keeps_its_tightest_networks_up_to_the_cap():
    big = {"wallets": [{"address": f"B{i}", "participant": True} for i in range(w.MAX_INSIDERS + 300)]}
    small = {"wallets": [{"address": "S1", "participant": True}, {"address": "L1", "participant": False}]}
    got = w.insiders([big, small])
    assert len(got) == w.MAX_INSIDERS and "S1" in got and "L1" not in got        # the tight group is kept whole
    assert w.insiders([small]) == {"S1"}
