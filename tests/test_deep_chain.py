"""Chain intel (albatross/deepend/chain.py) on payloads recorded 2026-09-30 from the live APIs (tests/fixtures/deep_chain/):
Jupiter tokens v2 for five Solana tokens; RugCheck for Saw (as sent) and FOMO / Nullmask (MASK) (stored compacted, as
the database keeps them); Birdeye security, holders, top traders and creation info; honeypot.is. Tokens: Saw
(pump.fun, graduated), MASK (Raydium LaunchLab, 3% Token-2022 transfer fee, the dev holds 39%), boar (Base, Clanker),
TIPPED (BSC), WOOF (Ethereum), SI (Base, Uniswap v4) and BI (BSC, four.meme) which neither Birdeye nor honeypot.is
had indexed yet."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from albatross.deepend import chain as c

FX = Path(__file__).parent / "fixtures" / "deep_chain"
CONTRACT = Path(__file__).parents[1] / "docs" / "deepend" / "fixtures" / "token.json"
SAW = "9twiuSdTVkwtAC9XQDG57dFRhF4iqPih461HJfMZKci9"
MASK = "HuAXPyDWDaMYFKuwQHpqL1oPnj93zdzWmtvFGzCeCUa7"
FOMO = "BDdzUjksj1J4bSnMveQ5tV9Up8A9c6YS1tHrxNw3pump"
T = datetime(2026, 9, 30, 17, 10, tzinfo=UTC)


def load(name):
    return json.loads((FX / f"{name}.json").read_text())


def jup(mint):
    return next(t for t in load("jupiter_search") if t["id"] == mint)


def saw_raw(**over):
    raw = {"jupiter": jup(SAW), "rugcheck": c.compact_rugcheck(load("rugcheck_saw")),
           "birdeye_traders": load("birdeye_traders_saw")}
    return {k: v for k, v in (raw | over).items() if v is not None}


AT = {s: T - timedelta(minutes=i) for i, s in enumerate(c.RAW)}


# ─── Jupiter ────────────────────────────────────────────────────────────────
def test_jupiter_gives_the_audit_dev_record_and_holder_flow():
    j = c.from_jupiter(jup(SAW))
    assert (j["holders"], round(j["top10_pct"], 2), j["dev_launches"], j["dev_migrated"]) == (4047, 19.03, 4077, 71)
    assert j["dev_address"] == "2hDNyc7aj139iuwnhQAU8RZ9VdTXSkdXnduY8qbQ5rW8"
    assert j["mint_revoked"] is True and j["freeze_revoked"] is True
    assert (j["organic_score"], j["organic_label"]) == (75, "medium")
    assert (round(j["change_h1_pct"], 2), j["net_buyers_h1"]) == (6.89, 1455)
    assert j["dev_balance_pct"] is None                    # Jupiter left it out: unknown, not 0
    assert round(c.from_jupiter(jup(MASK))["dev_balance_pct"], 2) == 38.84
    quiet = c.from_jupiter(jup(FOMO))                      # no trade in the last 5 minutes: no stats5m at all
    assert "stats5m" not in jup(FOMO) and quiet["holders"] == 2643


# ─── RugCheck ───────────────────────────────────────────────────────────────
def test_rugcheck_compaction_keeps_everything_we_read():
    full = load("rugcheck_saw")
    small = c.compact_rugcheck(full)
    assert len(json.dumps(small)) < len(json.dumps(full)) / 2
    assert c.from_rugcheck(small) == c.from_rugcheck(full)
    assert {m["pubkey"] for m in small["markets"]} == {m["pubkey"] for m in full["markets"]}
    owners = {h[k] for h in full["topHolders"] for k in ("owner", "address")}
    assert set(small["knownAccounts"]) <= owners and small["insiderNetworks"] == full["insiderNetworks"]


def test_rugcheck_reads_lp_insiders_and_concentration_like_jupiter():
    r = c.from_rugcheck(load("rugcheck_saw"))
    assert (r["lp_locked_pct"], r["insiders"], r["rugcheck_score"]) == (100, 44, 1)   # pump AMM LP burned
    assert r["mint_revoked"] and r["freeze_revoked"] and r["transfer_fee_pct"] == 0
    # Top 10 without the pools (pump AMM, Meteora DLMM) is Jupiter's topHoldersPercentage, from another source.
    assert abs(r["top10_pct"] - jup(SAW)["audit"]["topHoldersPercentage"]) < 0.05
    m = c.from_rugcheck(load("rugcheck_mask"))
    assert abs(m["top10_pct"] - jup(MASK)["audit"]["topHoldersPercentage"]) < 0.05
    assert round(m["lp_locked_pct"], 2) == 85.01                  # the deepest market, a Raydium CPMM pool
    assert m["rugcheck_score"] == 40


def test_a_token_2022_transfer_fee_is_read_from_the_extension():
    rep = load("rugcheck_mask")
    assert rep["transferFee"]["pct"] == 0                         # RugCheck's own summary misses it
    assert c.transfer_fee_pct(rep) == 3.0                          # 300 basis points, old and new
    v, src = c.extract("solana", {"jupiter": jup(MASK), "rugcheck": rep})
    assert (v["transfer_fee"], v["transfer_fee_pct"], src["transfer_fee"]) == (True, 3.0, "RugCheck")
    assert c.transfer_fee_pct({"token_extensions": {"transferFeeConfig": None}}) == 0.0
    assert c.transfer_fee_pct({"transferFee": {"pct": 1.5}}) == 1.5   # a report without extension data


# ─── which source wins ──────────────────────────────────────────────────────
def test_each_field_comes_from_the_first_source_that_knows_it():
    v, src = c.extract("solana", saw_raw())
    assert (src["mint_revoked"], src["top10_pct"], src["holders"]) == ("Jupiter", "Jupiter", "Jupiter")
    assert (src["insiders"], src["lp_locked_pct"], src["dev"]) == ("RugCheck", "RugCheck", "Jupiter")
    v, src = c.extract("solana", saw_raw(jupiter=None))
    assert (src["mint_revoked"], src["top10_pct"]) == ("RugCheck", "RugCheck")
    assert v["holders"] is None                                   # RugCheck's totalHolders counts empty accounts
    # RugCheck names another creator than Jupiter's dev; without Jupiter there is no launch record for it.
    assert (v["dev_address"], v["dev_launches"]) == ("5GWmcmu7nhYrLH3kKbpoNvhjHxRfVCGNQU7MPCQHVLGT", None)
    v, src = c.extract("solana", {"birdeye_security": load("birdeye_security_saw"),
                                  "birdeye_holders": load("birdeye_holder_saw")})
    assert (round(v["top10_pct"], 2), v["holders"], src["top10_pct"]) == (18.81, 4050, "Birdeye")
    assert (v["dev_address"], v["dev_balance_pct"], src["dev.balance_pct"]) == (
        "2hDNyc7aj139iuwnhQAU8RZ9VdTXSkdXnduY8qbQ5rW8", 0, "Birdeye")
    assert v["mint_revoked"] is None                              # Birdeye's Solana record has no mint authority


def test_the_dev_balance_is_only_taken_for_the_same_wallet():
    v, src = c.extract("solana", saw_raw())
    assert v["dev_balance_pct"] is None and "dev.balance_pct" not in src     # dev not in the top 20: unknown
    no_pct = jup(MASK) | {"audit": {k: x for k, x in jup(MASK)["audit"].items() if k != "devBalancePercentage"}}
    v, src = c.extract("solana", {"jupiter": no_pct, "rugcheck": load("rugcheck_mask")})
    assert (round(v["dev_balance_pct"], 2), src["dev.balance_pct"]) == (38.84, "RugCheck")   # its top holder


# ─── EVM ────────────────────────────────────────────────────────────────────
def test_evm_security_from_goplus_flags_and_the_honeypot_simulation():
    raw = {"birdeye_security": load("birdeye_security_boar_base"), "honeypot": load("honeypot_boar_base")}
    s = c.build("base", raw, AT)["security"]
    assert (s["mint_revoked"], s["freeze_revoked"], s["transfer_fee"]) == (True, True, False)
    assert s["honeypot"] == {"is_honeypot": False, "buy_tax": 0, "sell_tax": 0}
    assert (s["sources"]["honeypot"], s["sources"]["mint_revoked"]) == ("honeypot.is", "Birdeye")
    assert s["lp_locked_pct"] is None           # Clanker's locker holds the LP; GoPlus doesn't know it: unknown, not 0
    assert s["dev"]["address"] == "0xbcedd8b4150afb39b4d3af4b303d99f0424caf84" and s["rugcheck"] is None
    h = c.build("base", raw, AT)["holders"]
    assert (h["count"], h["sources"]["count"], h["top"]) == (3709, "honeypot.is", [])   # GoPlus said 4
    woof = c.build("ethereum", {"birdeye_security": load("birdeye_security_woof_eth")}, AT)["security"]
    assert woof["lp_locked_pct"] == 0           # one ordinary wallet holds all the LP: it can pull it
    assert (woof["dev"]["balance_pct"], woof["mint_revoked"], woof["honeypot"]["is_honeypot"]) == (0.166, None, None)


def test_without_honeypot_is_the_goplus_verdict_stands_in_and_says_so():
    raw = {"birdeye_security": load("birdeye_security_tipped_bsc"), "honeypot": {"code": 404, "error": "pair not found"}}
    s = c.build("bsc", raw, AT)["security"]
    assert s["honeypot"] == {"is_honeypot": False, "buy_tax": 0, "sell_tax": 0} and s["sources"]["honeypot"] == "Birdeye"
    assert load("honeypot_bi_bsc")["error"] == "pair not found"
    # Neither Birdeye nor honeypot.is had indexed BI (four.meme) or SI (Uniswap v4): no section, not a clean bill.
    for chain, sec in (("bsc", "birdeye_security_bi_bsc"), ("base", "birdeye_security_si_base")):
        out = c.build(chain, {"birdeye_security": load(sec), "honeypot": load("honeypot_bi_bsc")}, AT)
        assert out["security"] is None and out["holders"] is None and c.from_birdeye_security(chain, load(sec)) == {}


# ─── holders, tags, clusters ────────────────────────────────────────────────
def test_top_holders_are_tagged_pool_insider_dev_and_smart():
    top = c.build("solana", saw_raw(), AT)["holders"]["top"]
    assert len(top) == 20 and [h["tags"] for h in top[:3]] == [["pool"], ["pool"], []]   # pump AMM, Meteora DLMM
    mask = c.build("solana", {"jupiter": jup(MASK), "rugcheck": load("rugcheck_mask")}, AT)["holders"]["top"]
    assert mask[0] == {"owner": "3EcXMwuB5ACNb2mc3RfNMyq6h79tbAVLxoC2bdVLhhWH", "pct": 38.844, "tags": ["dev"]}
    assert "pool" in next(h for h in mask if h["owner"] == "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL")["tags"]
    rc = c.compact_rugcheck(load("rugcheck_saw"))
    rc["topHolders"][3]["insider"] = True
    tagged = c.build("solana", saw_raw(rugcheck=rc), AT)["holders"]["top"][3]
    assert tagged["tags"] == ["insider"]


def test_birdeye_holders_stand_in_when_rugcheck_has_nothing():
    raw = {"jupiter": jup(SAW), "birdeye_holders": load("birdeye_holder_saw")}
    h = c.build("solana", raw, AT, pools={"ATrJhrc46UkY4gTmJry2SUZvtaLJX3LydBufPnkWHTC3"})["holders"]
    assert h["sources"]["top"] == "Birdeye" and len(h["top"]) == 20
    assert h["top"][0] == {"owner": "ATrJhrc46UkY4gTmJry2SUZvtaLJX3LydBufPnkWHTC3", "pct": 8.415, "tags": ["pool"]}
    assert h["count"] == 4047 and h["sources"]["count"] == "Jupiter"


def test_clusters_are_the_insider_networks_and_the_snipers():
    cl = c.build("solana", saw_raw(), AT)["holders"]["clusters"]
    networks = [x for x in cl if x["source"] == "RugCheck"]
    assert [x["pct"] for x in networks] == [2.1, 1.17, 0.73, 0.59]
    assert sum(x["wallets"] for x in networks) == load("rugcheck_saw")["graphInsidersDetected"] == 44
    snipers = next(x for x in cl if x["source"] == "Birdeye")
    assert (snipers["wallets"], snipers["pct"]) == (2, 0)          # the dev and one sniper, both sold out
    rc = c.compact_rugcheck(load("rugcheck_saw")) | {"insiderNetworks": None}
    rc["topHolders"][2]["insider"] = True
    only = c.clusters({"rugcheck": rc})
    assert only == [{"label": "Insider wallets", "wallets": 44, "pct": 2.4, "source": "RugCheck",
                     "reason": "RugCheck flags these wallets as insiders; the share counts those among the top 20 holders"}]
    assert c.clusters({"rugcheck": load("rugcheck_mask")}) == []   # no insider graph for MASK


# ─── traders ────────────────────────────────────────────────────────────────
def test_smart_means_a_large_realized_gain_that_is_also_a_large_return():
    items = {t["owner"]: t for t in load("birdeye_traders_saw")["items"]}
    assert c.is_smart(items["ww32L4NA8ebVtstm6okgoWHJ3Dr5VcCYFUkHRg7B9eZo"])      # $13.1k on $17.9k bought
    assert not c.is_smart(items["4ZdviYkRJnuCL7BSLQCGQeAcZX33XNyeHr1zVMrF1JW"])   # $5.7k on $102k: a volume bot
    assert not c.is_smart({"realizedPnl": 50_000, "volumeBuyUSD": 0})             # sold what it never bought
    assert not c.is_smart({"realizedPnl": 50_000, "volumeBuyUSD": 1_000, "tags": ["arbitrage-bot"]})
    tr = c.build("solana", saw_raw(), AT)["traders"]
    assert len(tr["top"]) == 10 and tr["as_of"] == AT["birdeye_traders"]
    dev = next(t for t in tr["top"] if t["owner"] == "2hDNyc7aj139iuwnhQAU8RZ9VdTXSkdXnduY8qbQ5rW8")
    assert dev == {"owner": dev["owner"], "pnl_usd": 941, "volume_usd": 1887, "tags": ["bundler", "chef", "dev", "sniper"]}
    assert tr["smart_holders"] == 0                                # the one smart wallet holds nothing now


def test_evm_traders_are_lowercased_and_their_holdings_unknown():
    tr = c.build("base", {"birdeye_traders": load("birdeye_traders_si_base")}, AT)["traders"]
    smart = [t for t in tr["top"] if "smart" in t["tags"]]
    assert all(t["owner"] == t["owner"].lower() for t in tr["top"])
    assert [t["owner"] for t in smart][:1] == ["0x5a3f7af7b50d1e7e8628851795ae8f6f671ef3d6"]
    assert not any("smart" in t["tags"] for t in tr["top"] if "arbitrage-bot" in t["tags"])
    assert tr["smart_holders"] is None          # no holdVolume on EVM, no holder list: unknown


# ─── the contract ───────────────────────────────────────────────────────────
def test_sections_carry_every_contract_key():
    want = json.loads(CONTRACT.read_text())
    out = c.build("solana", saw_raw(birdeye_holders=load("birdeye_holder_saw")), AT, history=[])
    for name in ("security", "holders", "traders"):
        assert set(want[name]) <= set(out[name]), name
    s = out["security"]
    assert set(want["security"]["dev"]) <= set(s["dev"]) and set(want["security"]["rugcheck"]) <= set(s["rugcheck"])
    assert set(want["holders"]["top"][0]) <= set(out["holders"]["top"][0])
    assert set(want["holders"]["clusters"][0]) <= set(out["holders"]["clusters"][0])
    assert set(want["traders"]["top"][0]) <= set(out["traders"]["top"][0])
    assert s["as_of"] == max(s["source_as_of"].values()) == AT["jupiter"]
    assert set(s["sources"].values()) <= {"Jupiter", "RugCheck", "Birdeye", "honeypot.is"}
    assert s["honeypot"] is None                                  # Solana: no honeypot section


def test_nothing_known_is_none_not_a_clean_bill():
    assert c.build("robinhood", {}, {}) == {"security": None, "holders": None, "traders": None}
    out = c.build("solana", {"birdeye_traders": {"items": []}}, AT)
    assert out["traders"] == {"top": [], "smart_holders": 0, "as_of": AT["birdeye_traders"]} and out["security"] is None


# ─── scheduling ─────────────────────────────────────────────────────────────
def test_the_birdeye_share_is_spent_pro_rata_through_the_utc_day():
    day = datetime(2026, 9, 30, tzinfo=UTC)
    assert c.allowance(24_000, 0, day) == pytest.approx(1_000)               # an hour's worth up front
    assert c.allowance(24_000, 5_000, day + timedelta(hours=12)) == pytest.approx(13_000 - 5_000)
    assert c.allowance(24_000, 24_000, day + timedelta(hours=23, minutes=30)) == 0   # never past the share


def test_due_puts_never_asked_first_then_the_most_overdue():
    now = T
    toks = [{"chain": "solana", "address": a, "tier": tier, "why": why} for a, tier, why in (
        ("fresh", "hot", ["new call"]), ("bag", "hot", ["bot holds it"]), ("warm", "warm", ["last 24 h"]),
        ("new", "warm", ["last 24 h"]))]
    stamps = {("solana", "fresh"): {"birdeye_traders_at": now - timedelta(minutes=30)},
              ("solana", "bag"): {"birdeye_traders_at": now - timedelta(minutes=30)},
              ("solana", "warm"): {"birdeye_traders_at": now - timedelta(hours=4),
                                   "errors": {"birdeye_traders": {"at": (now - timedelta(hours=2)).isoformat()}}}}
    order = [t["address"] for t in c.due(toks, stamps, "birdeye_traders", c.birdeye_every, now)]
    # The bag is hot only because the bot holds it: warm cadence, not due. A failure 2 h ago counts as an ask.
    assert order == ["new", "fresh"]


def test_birdeye_asks_quiet_tokens_less_often():
    fresh = {"tier": "hot", "why": ["new call"]}
    assert c.birdeye_every(fresh) == c.birdeye_every(fresh, 400) == timedelta(minutes=15)
    assert c.birdeye_every(fresh, 12) == timedelta(hours=1)                  # 12 trades in the last hour
    assert c.birdeye_every({"tier": "warm", "why": ["last 24 h"]}, 0) == timedelta(hours=12)
    assert c.birdeye_every({"tier": "hot", "why": ["bot holds it"]}, None) == timedelta(hours=3)   # unknown: active


def test_what_birdeye_is_asked_for_each_token():
    assert c.birdeye_kinds("base", None) == ["security", "traders"] and c.birdeye_kinds("robinhood", None) == []
    seen = {"birdeye_security_at": T - timedelta(hours=2)}               # GoPlus's record: at most every 6 h
    assert c.birdeye_kinds("bsc", seen, T) == ["traders"]
    assert c.birdeye_kinds("bsc", seen, T + timedelta(hours=4)) == ["security", "traders"]
    ok = {"has_rugcheck": True, "dev_address": "x", "rugcheck_at": T}
    assert c.birdeye_kinds("solana", ok) == ["traders"]
    down = {"errors": {"rugcheck": {"at": T.isoformat(), "error": "HTTPStatusError: 502"}}}
    assert c.birdeye_kinds("solana", down) == ["traders", "security", "holders", "creation"]
    bad_mint = {"errors": {"rugcheck": {"at": T.isoformat(), "error": "no report"}}, "birdeye_creation_at": T}
    assert c.birdeye_kinds("solana", bad_mint) == ["traders"]
    cu = c.birdeye.CU                          # the measured table (builder B corrected it), never a copy of it
    assert c.birdeye_cost(["security", "traders"]) == cu["/defi/token_security"] + cu["/defi/v2/tokens/top_traders"]
    assert c.birdeye_cost(["traders"]) == cu["/defi/v2/tokens/top_traders"]


def test_jobs_schedules_and_cadences_line_up():
    names = {n for n, _ in c.SCHEDULE}
    assert set(c.JOBS) == names == set(c.CADENCE) and all(n.startswith("deep_chain_") for n in names)
    assert all(isinstance(v, int) for v in c.CADENCE.values())
