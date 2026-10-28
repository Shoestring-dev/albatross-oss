"""Chain intel for The Deep End (owner A, docs/SPEC-DEEP-END.md): who holds a token, who made it, who trades it and
whether its contract can hurt you. One row per token in de_chain_intel (each source's payload plus extracted
columns), holder history in de_holder_snapshots, and three dossier sections: security, holders, traders.

Sources, what each adds, and cadence by heat (albatross/deepend/watch.py):
  Jupiter tokens v2  Solana. Holders, the audit (mint/freeze, top-10 share, dev mints, migrations and balance),
                     organic score, 5m-24h trade stats. Every hot and warm token every 2 min, 100 mints a call, at most
                     one call per 2 s and inside venues/jupiter.py's bucket and pause: the key may be shared with live trading and
                     shared with live trading, so we yield while its window is busy. Stored whole for market.py.
  RugCheck           Solana. Top 20 holders with insider flags, the insider graph, each market's LP lock, Token-2022
                     extensions, risks. Hot every 10 min, warm every 2 h, at most 1 request/s.
  Birdeye            Top traders with PnL and Birdeye's wallet tags (Solana, Base, BSC, Ethereum); on EVM also the
                     security record (GoPlus's flags), at most every 6 h. Solana security, holders and creation info
                     only as fallbacks: they repeat RugCheck and Jupiter. Hot every 15 min, warm every 3 h, 4x that for
                     a token trading under 50 times an hour, inside our share (below).
  honeypot.is        EVM. A simulated buy and sell: the honeypot verdict and taxes. Once per token, then daily, 1/s.
Robinhood Chain has none of these sources: its tokens get no chain intel.

Birdeye share: DEEP_CHAIN_BIRDEYE_CU_PER_DAY (35,000 of the plan's ~80,000 a day), tallied in de_api_usage as
provider 'birdeye:chain' next to birdeye.py's own 'birdeye' total. It is spent pro rata through the UTC day (an
hour's worth up front), most overdue token first, so when demand exceeds it every interval stretches by the same
factor instead of the day running dry by noon; a token never looked at always goes first. A token hot only because
the bot holds it gets the warm cadence.
Measured 2026-09-30 on staging heat (35 hot by a call or trending, 247 warm or held, 282 in all on Birdeye's chains;
~112 new ones a day): the nominal cadence would take ~138,000 CU a day, 83,000 of it the 29 busy hot Solana tokens'
top traders at 15 min. The share stretches it ~4x: a busy hot token's top traders every ~1 h, a busy warm one's
every ~12 h. First looks take ~3,900 CU a day. Jupiter: 3 calls a pass (~260 mints), 2,160 a day. RugCheck: ~460
reports an hour (0.13/s). honeypot.is: ~25 a day, and it knows no pair for most BSC four.meme and Base Uniswap v4
tokens: there GoPlus's flags (Birdeye) stand in, and the dossier says so.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import orjson

from albatross import db
from albatross.config import settings
from albatross.deepend import birdeye, watch
from albatross.deepend.cfg import env_int, env_str
from albatross.http import WeightLimiter, client
from albatross.micro.rules import norm
from albatross.venues import jupiter

log = logging.getLogger(__name__)

EVM_CHAIN_IDS = {"base": 8453, "bsc": 56, "ethereum": 1}           # honeypot.is chain ids; Birdeye covers the same
BIRDEYE_CHAINS = {"solana", *EVM_CHAIN_IDS}
JUP_BATCH = 100
RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{}/report"
HONEYPOT_URL = "https://api.honeypot.is/v2/IsHoneypot"
BE_PATHS = {"security": "/defi/token_security", "holders": "/defi/v3/token/holder",
            "traders": "/defi/v2/tokens/top_traders", "creation": "/defi/token_creation_info"}
BE_PARAMS = {"holders": {"offset": 0, "limit": 20},
             "traders": {"time_frame": "24h", "sort_type": "desc", "sort_by": "volume", "offset": 0, "limit": 10}}
EVERY = {
    "jupiter": {"hot": timedelta(minutes=2), "warm": timedelta(minutes=2)},
    "rugcheck": {"hot": timedelta(minutes=10), "warm": timedelta(hours=2)},
    "birdeye": {"hot": timedelta(minutes=15), "warm": timedelta(hours=3)},
    "honeypot": {"hot": timedelta(days=1), "warm": timedelta(days=1)},
}
SLACK = timedelta(seconds=10)            # scheduler jitter: due in 10 s is due now
SNAPSHOT_WARM = timedelta(minutes=30)    # holder history: hot tokens every Jupiter pass, warm ones this often
PER_RUN = 60                             # RugCheck / honeypot.is calls per run: at 1/s a run stays inside its slot
TIMEOUT = 8.0
REFRESH_S = 7.5                          # refresh(): every source together, well inside the dossier's wait
BURST = timedelta(hours=1)
QUIET_TXNS_H1 = 50                       # under this many trades in the last hour (micro_state.txns_h1, DexScreener)
QUIET_FACTOR = 4                         # a token's 24 h top traders barely move: Birdeye asks 4x less often
EVM_SECURITY_EVERY = timedelta(hours=6)
SMART_MIN_PNL_USD = 2_500                # "smart": realized at least this on the token in Birdeye's 24 h window,
SMART_MIN_RETURN = 0.5                   # at least half again what it bought, and no bot tag
TALLY = "birdeye:chain"

# Accounts that hold a pool's tokens for every pool of an AMM (RugCheck labels most pools itself; these catch the
# rest): Raydium AMM v4, CPMM and LaunchLab, Meteora DAMM v2 and DBC.
AMM_AUTHORITIES = {"5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",
                   "WLHv2UAZm6z4KyaaELi5pjdbJh6RESMva1Rnn8pJVVh", "HLnpSz9h2S4hiLQ43rnSD9XkcUThA7B8hQMKmDaiTLcC",
                   "FhVo3mqL8PW5pH5U2CN4XE33DokiyZnUwuGpH2hmHLuM"}
# Well-known exchange hot wallets (Solscan / Etherscan labels, September 2026); extend as they turn up.
CEX_WALLETS = {
    "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9": "Binance", "9WzDXwBbmkg8ZTbNMqUxvQRAyrZzDsGYdLVL9zYtAWWM": "Binance",
    "2ojv9BAiHUrvsm9gxDe7fJSzbNZSJcxZvf8dqmWGHG8S": "Binance", "H8sMJSCQxfKiFTCfDR3DUMLPwcRbM61LGFJ8N4dK3WjS": "Coinbase",
    "2AQdpHJ2JpcEgPiATUXjQxA8QmafFegfQwSLWSprPicm": "Coinbase", "5VCwKtCXgCJ6kit5FybXjvriW3xELsFDhYrPSqtJNmcD": "OKX",
    "AC5RDfQFmDS1deWZos921JfqscXdByf8BKHs5ACWjtW2": "Bybit", "FWznbcNXWQuHTawe9RxvQ2LdCENssh12dsznf4RiouN5": "Kraken",
    "BmFdpraQhkiDQE6SnfG5omcA1VwzqfXrwtNYBwWTymy6": "KuCoin", "u6PJ8DtQuPFnfmwHbGFULQ4u4EgjDiyYKjVEsynXq2w": "Gate",
    "ASTyfSima4LLAdDgoFGkgqoKowG1LZFDr9fAQrg7iaJZ": "MEXC", "A77HErqtfN1hLLpvZ9pCtu66FEtM8BveoaKbbMoZ4RiR": "Bitget",
    "0x28c6c06298d514db089934071355e5743bf21d60": "Binance", "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance",
    "0xdfd5293d8e347dfe59e90efd55b2956a1343963d": "Binance", "0xf977814e90da44bfa03b6295a0616a897441acec": "Binance",
    "0x8894e0a0c962cb723c1976a4421c95949be2d4e3": "Binance", "0x71660c4005ba85c37ccec55d0c4493e66fe775d3": "Coinbase",
    "0x503828976d22510aad0201ac7ec88293211d23da": "Coinbase", "0xa9d1e08c7793af67e9d92fe308d5697fb81d3e43": "Coinbase",
    "0xf89d7b9c864f589bbf53a82105107622b35eaa40": "Bybit", "0x0d0707963952f2fba59dd06f2b425ace40b492fe": "Gate",
    "0x75e89d5979e4f6fba9f97c104c2f0afb3f1dcb88": "MEXC",
}
BURN = {"0x0000000000000000000000000000000000000000", "0x000000000000000000000000000000000000dead"}

RAW = ("jupiter", "rugcheck", "birdeye_security", "birdeye_holders", "birdeye_traders", "birdeye_creation", "honeypot")
COLUMNS = ("holders", "top10_pct", "dev_address", "dev_launches", "dev_migrated", "dev_balance_pct", "insiders",
           "mint_revoked", "freeze_revoked", "lp_locked_pct", "rugcheck_score", "organic_score", "organic_label",
           "ct_likes", "smart_ct_likes", "transfer_fee", "transfer_fee_pct", "is_honeypot", "buy_tax", "sell_tax")
PICKED = ("holders", "change_h1_pct", "net_buyers_h1", "top10_pct", "mint_revoked", "freeze_revoked", "lp_locked_pct",
          "rugcheck_score", "insiders", "organic_score", "organic_label", "ct_likes", "smart_ct_likes",
          "transfer_fee_pct", "transfer_fee", "is_honeypot", "buy_tax", "sell_tax", "creator")

_jup_pace = WeightLimiter(per_minute=30, burst=1)       # one call per 2 s
_rug_pace = WeightLimiter(per_minute=60, burst=1)
_hp_pace = WeightLimiter(per_minute=60, burst=1)
_snapped: dict[str, datetime] = {}                      # warm token -> its last holder snapshot
_inflight: dict[tuple[str, str], asyncio.Task] = {}
_EPOCH = datetime.min.replace(tzinfo=UTC)


class Throttled(Exception):
    """A free API answered HTTP 429: stop this round."""


# ─── reading the payloads (pure; tests/test_deep_chain.py feeds them recorded ones) ─────────────────────────────
def _f(v) -> float | None:
    """Vendors send numbers as numbers or numeric strings, and "" for unknown."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v) -> int | None:
    f = _f(v)
    return int(f) if f is not None else None


def _flag(v) -> bool | None:
    """GoPlus-style "0" / "1" ("" = unknown) or a plain boolean."""
    return v if isinstance(v, bool) else {"0": False, "1": True, 0: False, 1: True}.get(v)


def _pct(fraction) -> float | None:
    f = _f(fraction)
    return f * 100 if f is not None else None


def _r(v, n: int = 2) -> float | None:
    return round(v, n) if v is not None else None


def compact_rugcheck(r: dict) -> dict:
    """The report without what makes it heavy and what nothing reads: per-market internals (most of a 30-230 KB
    report) keep the pool, its type and its LP lock; known accounts only those among the top holders."""
    keep = {h.get(k) for h in r.get("topHolders") or [] for k in ("owner", "address")}
    out = {k: v for k, v in r.items() if k not in ("markets", "knownAccounts")}
    out["markets"] = [{"pubkey": m.get("pubkey"), "marketType": m.get("marketType"),
                       "lp": {k: (m.get("lp") or {}).get(k) for k in ("lpLockedPct", "lpLockedUSD", "baseUSD", "quoteUSD")}}
                      for m in r.get("markets") or []]
    out["knownAccounts"] = {a: v for a, v in (r.get("knownAccounts") or {}).items() if a in keep}
    return out


def transfer_fee_pct(r: dict) -> float | None:
    """A Token-2022 transfer fee, % of every transfer: the higher of the current and the scheduled fee. Read from the
    extension itself: RugCheck's transferFee summary said 0% for a token charging 3% (Nullmask, 2026-09-30)."""
    ext = r.get("token_extensions")
    if ext is None:
        return _f((r.get("transferFee") or {}).get("pct"))
    cfg = ext.get("transferFeeConfig")
    if not cfg:
        return 0.0
    bps = [b for k in ("olderTransferFee", "newerTransferFee")
           if (b := _f((cfg.get(k) or {}).get("transferFeeBasisPoints"))) is not None]
    return max(bps) / 100 if bps else None


def _kinds(h: dict, known: dict) -> set[str]:
    """RugCheck's kind of a top holder (AMM, CLOB, LOCKER, CREATOR), by its wallet or its token account."""
    return {t for k in ("owner", "address") if (t := (known.get(h.get(k)) or {}).get("type"))}


def _venue(h: dict, known: dict) -> bool:
    """A pool or a locker: it holds the token for others, so it isn't a holder in the concentration sense."""
    return bool(_kinds(h, known) & {"AMM", "CLOB", "LOCKER"}) or h.get("owner") in AMM_AUTHORITIES


def from_jupiter(t: dict) -> dict:
    a, h1 = t.get("audit") or {}, t.get("stats1h") or {}
    return {"holders": _i(t.get("holderCount")), "top10_pct": _f(a.get("topHoldersPercentage")),
            "dev_address": t.get("dev"), "dev_launches": _i(a.get("devMints")), "dev_migrated": _i(a.get("devMigrations")),
            "dev_balance_pct": _f(a.get("devBalancePercentage")),
            "mint_revoked": a.get("mintAuthorityDisabled"), "freeze_revoked": a.get("freezeAuthorityDisabled"),
            "organic_score": _f(t.get("organicScore")), "organic_label": t.get("organicScoreLabel"),
            "ct_likes": _i(t.get("ctLikes")), "smart_ct_likes": _i(t.get("smartCtLikes")),
            "change_h1_pct": _f(h1.get("holderChange")), "net_buyers_h1": _i(h1.get("numNetBuyers"))}


def from_rugcheck(r: dict) -> dict:
    """LP locked = the deepest market's share (liquidity by baseUSD + quoteUSD): the standard read. Pools without LP
    tokens (DLMM, CLMM) read 0, which is true: their liquidity can leave. Top 10 leaves out pools and lockers."""
    known, holders = r.get("knownAccounts") or {}, r.get("topHolders") or []
    lps = [m.get("lp") or {} for m in r.get("markets") or []]
    deepest = max(lps, key=lambda lp: (_f(lp.get("baseUSD")) or 0) + (_f(lp.get("quoteUSD")) or 0), default=None)
    supply, fee = _f((r.get("token") or {}).get("supply")), transfer_fee_pct(r)
    creator, balance = r.get("creator"), _f(r.get("creatorBalance"))
    return {"mint_revoked": r["mintAuthority"] is None if "mintAuthority" in r else None,
            "freeze_revoked": r["freezeAuthority"] is None if "freezeAuthority" in r else None,
            "lp_locked_pct": _f(deepest.get("lpLockedPct")) if deepest else None,
            "rugcheck_score": _f(r.get("score_normalised")), "insiders": _i(r.get("graphInsidersDetected")),
            "top10_pct": sum(_f(h.get("pct")) or 0 for h in [h for h in holders if not _venue(h, known)][:10])
            if holders else None,
            "transfer_fee_pct": fee, "creator": creator,
            "creator_pct": 100 * balance / supply if creator and supply and balance is not None else None}


def _lp_locked(lp_holders) -> float | None:
    """GoPlus LP holders -> % of the LP locked or burned. LP sitting unlocked in a contract may be a locker GoPlus
    doesn't know (Clanker's is one), so that reads unknown, not 0."""
    if not lp_holders:
        return None
    locked = unsure = 0.0
    for h in lp_holders:
        share = _f(h.get("percent")) or 0
        if _flag(h.get("is_locked")) or norm(h.get("address") or "") in BURN:
            locked += share
        elif _flag(h.get("is_contract")):
            unsure += share
    return None if unsure else 100 * locked


def from_birdeye_security(chain: str, s) -> dict:
    """Birdeye's /defi/token_security: its own record on Solana, GoPlus's flags on EVM ("0" / "1", fractions as
    strings). A token nobody has indexed comes back in another shape or all blanks: nothing known."""
    if not isinstance(s, dict) or not (s.get("creatorAddress") or "isHoneypot" in s or "top10HolderPercent" in s):
        return {}
    creator = s.get("creatorAddress") or None
    if chain == "solana":
        return {"top10_pct": _pct(s.get("top10UserPercent")), "transfer_fee": s.get("transferFeeEnable"),
                "creator": creator, "creator_pct": _pct(s.get("creatorPercentage"))}
    mintable, pausable, blacklist = (_flag(s.get(k)) for k in ("isMintable", "transferPausable", "isBlacklisted"))
    return {"mint_revoked": None if mintable is None else not mintable,
            "freeze_revoked": None if None in (pausable, blacklist) else not (pausable or blacklist),
            "lp_locked_pct": _lp_locked(s.get("lpHolders")), "is_honeypot": _flag(s.get("isHoneypot")),
            "buy_tax": _pct(s.get("buyTax")), "sell_tax": _pct(s.get("sellTax")),
            "transfer_fee_pct": _pct(s.get("transferTax")),
            "creator": norm(creator) if creator else None, "creator_pct": _pct(s.get("creatorPercentage"))}


def from_honeypot(h) -> dict:
    """honeypot.is: a simulated buy and sell on the token's main pair, taxes in %. Its 404 body (no pair it knows,
    as for Uniswap v4 and four.meme pools): nothing known."""
    if not isinstance(h, dict) or ("simulationResult" not in h and "honeypotResult" not in h):
        return {}
    sim = h.get("simulationResult") or {}
    return {"is_honeypot": (h.get("honeypotResult") or {}).get("isHoneypot"), "buy_tax": _f(sim.get("buyTax")),
            "sell_tax": _f(sim.get("sellTax")), "transfer_fee_pct": _f(sim.get("transferTax")),
            "holders": _i((h.get("token") or {}).get("totalHolders"))}


def _parts(chain: str, raw: dict) -> list[tuple[str, dict]]:
    """Each stored payload's fields, in the order sources are trusted: Jupiter (refreshed every 2 min), RugCheck,
    honeypot.is (a simulated trade beats static flags), then Birdeye."""
    readers = (("jupiter", "Jupiter", from_jupiter), ("rugcheck", "RugCheck", from_rugcheck),
               ("honeypot", "honeypot.is", from_honeypot),
               ("birdeye_security", "Birdeye", lambda s: from_birdeye_security(chain, s)),
               ("birdeye_holders", "Birdeye", lambda d: {"holders": _i(d.get("holder"))} if isinstance(d, dict) else {}),
               ("birdeye_creation", "Birdeye", lambda d: {"creator": d.get("creator")} if isinstance(d, dict) else {}))
    return [(label, read(raw[key])) for key, label, read in readers if raw.get(key)]


def extract(chain: str, raw: dict) -> tuple[dict, dict]:
    """-> (values, sources): every field, from the first source in _parts' order that knows it, and that source's name.
    The dev is Jupiter's (it comes with the dev's launch record), else the first creator a source names; the dev's
    balance only from a source that states it for that same wallet, else from RugCheck's top holders."""
    parts = _parts(chain, raw)
    v, src = {}, {}
    for field in PICKED:
        v[field] = None
        for label, p in parts:
            if p.get(field) is not None:
                v[field], src[field] = p[field], label
                break
    if v["transfer_fee_pct"] is not None:
        v["transfer_fee"], src["transfer_fee"] = v["transfer_fee_pct"] > 0, src["transfer_fee_pct"]
    jup = next((p for label, p in parts if label == "Jupiter"), {})
    dev, dev_src = (jup.get("dev_address"), "Jupiter") if jup.get("dev_address") else (v["creator"], src.get("creator"))
    balance, balance_src = (jup["dev_balance_pct"], "Jupiter") if jup.get("dev_balance_pct") is not None else (None, None)
    if dev and balance is None:
        stated = next(((p["creator_pct"], label) for label, p in parts
                       if p.get("creator") == dev and p.get("creator_pct") is not None), None)
        held = next((h for h in (raw.get("rugcheck") or {}).get("topHolders") or [] if h.get("owner") == dev), None)
        if stated:
            balance, balance_src = stated
        elif held:
            balance, balance_src = _f(held.get("pct")), "RugCheck"
    v |= {"dev_address": dev, "dev_launches": jup.get("dev_launches") if dev_src == "Jupiter" else None,
          "dev_migrated": jup.get("dev_migrated") if dev_src == "Jupiter" else None, "dev_balance_pct": balance}
    if dev:
        src["dev"] = dev_src
    if balance is not None:
        src["dev.balance_pct"] = balance_src
    return v, src


def _supply(raw: dict) -> float | None:
    """Total supply in whole tokens: Jupiter's, else RugCheck's raw supply over its decimals, else Birdeye's."""
    if (s := _f((raw.get("jupiter") or {}).get("totalSupply"))) is not None:
        return s
    t = (raw.get("rugcheck") or {}).get("token") or {}
    if _f(t.get("supply")) is not None and t.get("decimals") is not None:
        return _f(t["supply"]) / 10 ** int(t["decimals"])
    return _f((raw.get("birdeye_security") or {}).get("totalSupply"))


def _items(raw: dict) -> list[dict]:
    d = raw.get("birdeye_traders")
    return (d.get("items") or []) if isinstance(d, dict) else []


def is_smart(t: dict) -> bool:
    """Our label, not graded yet: a Birdeye top trader that realized at least SMART_MIN_PNL_USD on this token in the
    24 h window, at least SMART_MIN_RETURN of what it bought there, and carries no bot tag (arbitrage and MEV bots
    make money on every token and say nothing about this one)."""
    if any("bot" in x.lower() or "mev" in x.lower() for x in t.get("tags") or []):
        return False
    pnl, bought = _f(t.get("realizedPnl")), _f(t.get("volumeBuyUSD"))
    return pnl is not None and pnl >= SMART_MIN_PNL_USD and bool(bought) and pnl >= SMART_MIN_RETURN * bought


def top_holders(raw: dict, dev: set, pools: set, smart: set) -> tuple[list[dict], str | None]:
    """Top holders with tags: RugCheck's 20 (with its insider flags and account labels), else Birdeye's (Solana)."""
    rc = raw.get("rugcheck") or {}
    venues = pools | AMM_AUTHORITIES | {m.get("pubkey") for m in rc.get("markets") or []} \
        | {(raw.get("jupiter") or {}).get("graduatedPool")}
    lockers = set(rc.get("lockers") or {})
    if rc.get("topHolders"):
        known = rc.get("knownAccounts") or {}
        rows = [(h.get("owner"), _f(h.get("pct")), _kinds(h, known), bool(h.get("insider")), {h.get("address")})
                for h in rc["topHolders"]]
        src = "RugCheck"
    elif (items := (raw.get("birdeye_holders") or {}).get("items")) and (supply := _supply(raw)):
        rows = [(norm(i.get("owner") or ""), 100 * (_f(i.get("ui_amount")) or 0) / supply, set(), False,
                 {i.get("token_account")}) for i in items]
        src = "Birdeye"
    else:
        return [], None
    out = []
    for owner, pct, kinds, insider, accounts in rows:
        mine = {owner} | accounts
        tags = [tag for tag, hit in (("insider", insider), ("dev", owner in dev or "CREATOR" in kinds),
                                     ("lp", "LOCKER" in kinds or bool(mine & lockers)), ("cex", owner in CEX_WALLETS),
                                     ("smart", owner in smart), ("pool", bool(kinds & {"AMM", "CLOB"} or mine & venues)))
                if hit]
        out.append({"owner": owner, "pct": _r(pct, 3), "tags": tags})
    return out, src


def clusters(raw: dict) -> list[dict]:
    """Bubblemap-lite: RugCheck's insider networks (share = what the network holds now), else its insider count with
    the share its flagged top holders hold; and the launch snipers among Birdeye's top traders, when there are two
    or more, with what they still hold. Wallet-funding traces would need Helius (Chainstack's shared nodes refuse
    the calls)."""
    rc = raw.get("rugcheck") or {}
    supply, out = _f((rc.get("token") or {}).get("supply")), []
    for i, n in enumerate(rc.get("insiderNetworks") or [], 1):
        held = _f(n.get("currentHolding"))
        out.append({"label": f"Insider network {i}", "wallets": _i(n.get("size")),
                    "pct": _r(100 * held / supply) if supply and held is not None else None,
                    "reason": f"linked by {n.get('type') or 'on-chain'}s in RugCheck's insider graph", "source": "RugCheck"})
    if not out and _i(rc.get("graphInsidersDetected")):
        flagged = [_f(h.get("pct")) or 0 for h in rc.get("topHolders") or [] if h.get("insider")]
        out.append({"label": "Insider wallets", "wallets": _i(rc["graphInsidersDetected"]),
                    "pct": _r(sum(flagged)) if flagged else None,
                    "reason": "RugCheck flags these wallets as insiders; the share counts those among the top 20 holders",
                    "source": "RugCheck"})
    snipers = [t for t in _items(raw) if "sniper" in {x.lower() for x in t.get("tags") or []}]
    if len(snipers) >= 2:
        held, total = [_f(t.get("holdVolume")) for t in snipers], _supply(raw)
        out.append({"label": "Snipers among top traders", "wallets": len(snipers),
                    "pct": _r(100 * sum(held) / total) if total and None not in held else None,
                    "reason": "Birdeye tags these top-10 traders as launch snipers; the share is what they still hold",
                    "source": "Birdeye"})
    return sorted(out, key=lambda c: -(c["pct"] or 0))


def _as_of(labels, at: dict, birdeye_keys: tuple) -> dict:
    keys = {"Jupiter": ("jupiter",), "RugCheck": ("rugcheck",), "honeypot.is": ("honeypot",), "Birdeye": birdeye_keys}
    return {label: max((at[k] for k in keys[label] if at.get(k)), default=None) for label in sorted(set(labels))}


def build(chain: str, raw: dict, at: dict, pools: set | frozenset = frozenset(), history: list | None = None) -> dict:
    """The three dossier sections (docs/SPEC-DEEP-END.md section 4) from one token's stored payloads, each None when
    no source knows anything. Beyond the contract: `sources` (field -> the source that gave it) and `source_as_of`
    (source -> when we fetched it) on security and holders, so every checklist line can name its source and age;
    security.transfer_fee_pct; clusters[].source."""
    v, src = extract(chain, raw)
    rc, items = raw.get("rugcheck") or {}, _items(raw)
    dev = ({v["dev_address"]} | {p.get("creator") for _, p in _parts(chain, raw)}) - {None}
    smart = {norm(t.get("owner") or "") for t in items if is_smart(t)}
    top, top_src = top_holders(raw, dev, set(pools), smart)
    insiders = {h["owner"] for h in top if "insider" in h["tags"]}

    fields = {"mint_revoked": "mint_revoked", "freeze_revoked": "freeze_revoked", "lp_locked_pct": "lp_locked_pct",
              "rugcheck": "rugcheck_score", "insiders": "insiders", "top10_pct": "top10_pct", "dev": "dev",
              "dev.balance_pct": "dev.balance_pct", "transfer_fee": "transfer_fee", "honeypot": "is_honeypot"}
    sec_src = {k: src[f] for k, f in fields.items() if f in src}
    if chain in EVM_CHAIN_IDS and "honeypot" not in sec_src and (s := src.get("sell_tax") or src.get("buy_tax")):
        sec_src["honeypot"] = s
    security = None
    if sec_src:
        when = _as_of(sec_src.values(), at, ("birdeye_security", "birdeye_creation"))
        hp = {k: v[k] for k in ("is_honeypot", "buy_tax", "sell_tax")}
        security = {
            "mint_revoked": v["mint_revoked"], "freeze_revoked": v["freeze_revoked"], "lp_locked_pct": _r(v["lp_locked_pct"]),
            "rugcheck": {"score": v["rugcheck_score"],
                         "risks": [{"name": x.get("name"), "level": x.get("level"), "description": x.get("description")}
                                   for x in rc.get("risks") or []]} if rc else None,
            "insiders": v["insiders"], "top10_pct": _r(v["top10_pct"]),
            "dev": {"address": v["dev_address"], "launches": v["dev_launches"], "migrated": v["dev_migrated"],
                    "balance_pct": _r(v["dev_balance_pct"], 3)},
            "transfer_fee": v["transfer_fee"], "transfer_fee_pct": _r(v["transfer_fee_pct"]),
            "honeypot": {k: _r(x) if k != "is_honeypot" else x for k, x in hp.items()}
            if chain in EVM_CHAIN_IDS and any(x is not None for x in hp.values()) else None,
            "sources": sec_src, "source_as_of": when, "as_of": max((t for t in when.values() if t), default=None)}

    cl = clusters(raw)
    hold_src = {k: s for k, s in (("count", src.get("holders")), ("change_h1_pct", src.get("change_h1_pct")),
                                  ("top10_pct", src.get("top10_pct")), ("top", top_src),
                                  ("clusters", cl[0]["source"] if cl else None)) if s}
    holders = None
    if v["holders"] is not None or top:
        when = _as_of(hold_src.values(), at, ("birdeye_holders", "birdeye_traders", "birdeye_security"))
        holders = {"count": v["holders"], "change_h1_pct": _r(v["change_h1_pct"]), "top10_pct": _r(v["top10_pct"]),
                   "top": top, "clusters": cl, "history": history or [],
                   "sources": hold_src, "source_as_of": when, "as_of": max((t for t in when.values() if t), default=None)}

    traders = None
    if isinstance(raw.get("birdeye_traders"), dict):
        top_t = []
        for t in items:
            owner = norm(t.get("owner") or "")
            tags = {x.lower() for x in t.get("tags") or []} | {tag for tag, hit in (
                ("smart", owner in smart), ("dev", owner in dev), ("cex", owner in CEX_WALLETS),
                ("insider", owner in insiders)) if hit}
            pnl, vol = _f(t.get("totalPnl")), _f(t.get("volumeUsd"))
            top_t.append({"owner": owner, "pnl_usd": round(pnl) if pnl is not None else None,
                          "volume_usd": round(vol) if vol is not None else None, "tags": sorted(tags)})
        # Smart wallets still holding: Birdeye's holdVolume (Solana), or found among the top holders; EVM rows carry
        # no holdVolume and have no holder list, so there it is unknown.
        owners, smart_rows = {h["owner"] for h in top}, [t for t in items if is_smart(t)]
        if not smart_rows:
            n = 0
        elif all("holdVolume" in t for t in smart_rows) or owners:
            n = sum((_f(t.get("holdVolume")) or 0) > 0 or norm(t.get("owner") or "") in owners for t in smart_rows)
        else:
            n = None
        traders = {"top": top_t, "smart_holders": n, "as_of": at.get("birdeye_traders")}
    return {"security": security, "holders": holders, "traders": traders}


# ─── scheduling (pure) ─────────────────────────────────────────────────────────────────────────────────────────
def allowance(share: int, spent: int, now: datetime) -> float:
    """Birdeye units the pass may still spend: our daily share pro rata through the UTC day, an hour's worth up front,
    minus what this module already spent today (passes and refreshes alike)."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return share * min(1.0, (now - midnight + BURST) / timedelta(days=1)) - spent


def birdeye_every(t: dict, txns_h1: int | None = None) -> timedelta:
    """A token hot only because the bot holds it gets the warm cadence (an open bag's top traders change slowly), and a
    quiet one QUIET_FACTOR times either: the units go where the trading is. Unknown activity counts as active."""
    fresh = t["tier"] == "hot" and bool(set(t["why"]) - {"bot holds it"})
    every = EVERY["birdeye"]["hot" if fresh else "warm"]
    return every * QUIET_FACTOR if txns_h1 is not None and txns_h1 < QUIET_TXNS_H1 else every


def birdeye_kinds(chain: str, stamp: dict | None, now: datetime | None = None) -> list[str]:
    """What Birdeye adds for a token. EVM: the top traders, and GoPlus's security record at most every
    EVM_SECURITY_EVERY (a static read of the contract; honeypot.is re-simulates the taxes daily). Solana: the top
    traders; security and holders only while RugCheck fails on the token (they repeat it), creation info once when no
    source names a creator (it repeats Jupiter). Robinhood: nothing (Birdeye doesn't cover it)."""
    s = stamp or {}
    if chain in EVM_CHAIN_IDS:
        last = _tried(s, "birdeye_security")
        return (["security"] if last is None or (now or datetime.now(UTC)) - last >= EVM_SECURITY_EVERY else []) \
            + ["traders"]
    if chain != "solana":
        return []
    kinds = ["traders"]
    failed = (s.get("errors") or {}).get("rugcheck")
    if not s.get("has_rugcheck") and failed and failed.get("error") != "no report":   # RugCheck down, not a bad mint
        kinds += ["security", "holders"]
    if not s.get("dev_address") and _tried(s, "rugcheck") and not _tried(s, "birdeye_creation"):
        kinds.append("creation")
    return kinds


def birdeye_cost(kinds: list[str]) -> int:
    return sum(birdeye.CU.get(BE_PATHS[k], birdeye.DEFAULT_CU) for k in kinds)


def _tried(stamp: dict | None, source: str) -> datetime | None:
    """When a source was last asked about the token: its last fetch, or its last failure if later."""
    if not stamp:
        return None
    err = (stamp.get("errors") or {}).get(source)
    times = [stamp.get(f"{source}_at"), datetime.fromisoformat(err["at"]) if err else None]
    return max((t for t in times if t), default=None)


def due(toks: list[dict], stamps: dict, source: str, every, now: datetime) -> list[dict]:
    """Tokens whose `source` is due, most overdue first (never asked: first of all; hot before warm on ties)."""
    out = []
    for t in toks:
        last = _tried(stamps.get((t["chain"], t["address"])), source)
        late = float("inf") if last is None else (now - last + SLACK) / every(t)
        if late >= 1:
            out.append((late, t["tier"] == "hot", t))
    out.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [t for *_, t in out]


def _chunks(xs: list, n: int):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def _err(e: BaseException) -> str:
    return (f"{type(e).__name__}: {e}" if str(e) else type(e).__name__)[:300]


# ─── storage ───────────────────────────────────────────────────────────────────────────────────────────────────
def _load(v):
    return orjson.loads(v) if isinstance(v, str | bytes) else v


def _split(row: dict) -> tuple[dict, dict]:
    """A de_chain_intel row -> (payload per source, fetch time per source)."""
    return {s: _load(row.get(s)) for s in RAW}, {s: row.get(f"{s}_at") for s in RAW}


async def _row(chain: str, address: str) -> dict | None:
    pool = await db.pool()
    try:
        r = await pool.fetchrow("SELECT * FROM de_chain_intel WHERE chain = $1 AND address = $2", chain, address)
    except asyncpg.UndefinedTableError:          # migration 020 not applied yet (the dashboard can start first)
        return None
    return dict(r) if r else None


async def _stamps(keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """What the schedulers need per token, without the payloads: fetch times, errors, what is known, and how much it
    trades (the micro pipeline's last-hour transaction count)."""
    pool = await db.pool()
    rows = await pool.fetch(
        f"""SELECT d.chain, d.address, {", ".join(f"d.{s}_at" for s in RAW)}, d.errors, d.dev_address,
                   d.rugcheck IS NOT NULL AS has_rugcheck, s.txns_h1
            FROM de_chain_intel d LEFT JOIN micro_state s ON s.chain = d.chain AND s.token = d.address
            WHERE (d.chain, d.address) IN (SELECT * FROM unnest($1::text[], $2::text[]))""",
        [k[0] for k in keys], [k[1] for k in keys])
    return {(r["chain"], r["address"]): dict(r) | {"errors": _load(r["errors"]) or {}} for r in rows}


async def _store(chain: str, address: str, got: dict, failed: dict | None = None) -> dict:
    """Write each fetched source's payload ({source: (payload, fetched_at)}) and each failure ({source: error}), then
    recompute the extracted columns from everything stored, under the row's lock, so two jobs writing one token
    can't leave the columns out of step with the payloads. -> the extracted values."""
    pool = await db.pool()
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("INSERT INTO de_chain_intel (chain, address) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                           chain, address)
        row = await conn.fetchrow(f"SELECT {', '.join(RAW)}, errors FROM de_chain_intel "
                                  "WHERE chain = $1 AND address = $2 FOR UPDATE", chain, address)
        raw, errors = {s: _load(row[s]) for s in RAW}, _load(row["errors"]) or {}
        now = datetime.now(UTC).isoformat()
        for s, (payload, _) in got.items():
            raw[s] = payload
            errors.pop(s, None)
        for s, e in (failed or {}).items():
            errors[s] = {"at": now, "error": str(e)[:300]}
        v, _ = extract(chain, raw)
        sets, args = [], [chain, address]
        for s, (payload, at) in got.items():
            args += [orjson.dumps(payload).decode(), at]
            sets.append(f"{s} = ${len(args) - 1}::jsonb, {s}_at = ${len(args)}")
        args.append(orjson.dumps(errors).decode())
        sets.append(f"errors = ${len(args)}::jsonb")
        for c in COLUMNS:
            args.append(v[c])
            sets.append(f"{c} = ${len(args)}")
        await conn.execute(f"UPDATE de_chain_intel SET {', '.join(sets)}, updated_at = now() "
                           "WHERE chain = $1 AND address = $2", *args)
    return v


async def _spend(units: int) -> None:
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_api_usage (provider, day, calls, units) VALUES ($1, (now() AT TIME ZONE 'UTC')::date, 1, $2)
           ON CONFLICT (provider, day) DO UPDATE SET calls = de_api_usage.calls + 1, units = de_api_usage.units + $2""",
        TALLY, units)


async def spent_today() -> int:
    pool = await db.pool()
    return int(await pool.fetchval(
        "SELECT coalesce(sum(units), 0) FROM de_api_usage WHERE provider = $1 AND day = (now() AT TIME ZONE 'UTC')::date",
        TALLY) or 0)


def birdeye_share() -> int:
    return env_int("DEEP_CHAIN_BIRDEYE_CU_PER_DAY", 35_000)


# ─── fetching ──────────────────────────────────────────────────────────────────────────────────────────────────
async def jupiter_search(mints: list[str]) -> dict[str, dict]:
    """{mint: token} for up to 100 Solana mints (Jupiter leaves out mints it doesn't know). Raises jupiter.Busy while
    live trading needs the shared key's window; our calls also count in venues/jupiter.py's bucket and pause."""
    if time.time() < jupiter._paused_until:
        raise jupiter.Busy
    await _jup_pace.acquire()
    await jupiter.limiter().acquire()
    headers = {"x-api-key": settings.jupiter_api_key} if settings.jupiter_api_key else {}
    r = await client().get(jupiter.host() + "/tokens/v2/search", params={"query": ",".join(mints)}, headers=headers,
                           timeout=TIMEOUT)
    jupiter._note_limits(r)
    if r.status_code == 429:
        raise jupiter.Busy
    r.raise_for_status()
    want = set(mints)
    return {t["id"]: t for t in r.json() or [] if isinstance(t, dict) and t.get("id") in want}


async def rugcheck_report(mint: str) -> dict | None:
    """The report, compacted; None when RugCheck has none (an unknown or malformed mint)."""
    await _rug_pace.acquire()
    r = await client().get(RUGCHECK_URL.format(mint), timeout=TIMEOUT)
    if r.status_code == 429:
        raise Throttled("rugcheck")
    if r.status_code in (400, 404):
        return None
    r.raise_for_status()
    return compact_rugcheck(r.json())


async def honeypot_check(chain: str, address: str) -> dict:
    """honeypot.is's answer; its 404 body when it knows no pair for the token (stored, so the daily cadence holds)."""
    await _hp_pace.acquire()
    r = await client().get(HONEYPOT_URL, params={"address": address, "chainID": EVM_CHAIN_IDS[chain]}, timeout=TIMEOUT)
    if r.status_code == 429:
        raise Throttled("honeypot.is")
    if r.status_code == 404:
        try:
            return {"code": 404, "error": r.json().get("error")}
        except ValueError:
            return {"code": 404, "error": None}
    r.raise_for_status()
    return r.json()


async def _birdeye(kinds: list[str], chain: str, address: str, priority: bool) -> tuple[dict, dict]:
    """-> ({source: (payload, at)}, {source: error}). Every call is tallied against our share, answered or not
    (Birdeye bills it either way). Raises birdeye.BudgetExceeded when the plan's day is spent."""
    got, failed = {}, {}
    for k in kinds:
        path = BE_PATHS[k]
        try:
            d = await birdeye.get(path, {"address": address, **BE_PARAMS.get(k, {})}, chain, priority=priority)
        except httpx.HTTPError as e:
            failed[f"birdeye_{k}"] = _err(e)
            continue
        await _spend(birdeye.CU.get(path, birdeye.DEFAULT_CU))
        if d is None:
            failed[f"birdeye_{k}"] = "no data"
        else:
            got[f"birdeye_{k}"] = (d, datetime.now(UTC))
    return got, failed


# ─── jobs ──────────────────────────────────────────────────────────────────────────────────────────────────────
def _keys(toks: list[dict]) -> list[tuple[str, str]]:
    return [(t["chain"], t["address"]) for t in toks]


async def jupiter_pass() -> int:
    """Every hot and warm Solana token, 100 mints a call, hot ones first; a holder snapshot for each hot token (warm:
    every SNAPSHOT_WARM). A busy window ends the pass: live trading comes first."""
    toks = sorted((t for t in await watch.tokens() if t["chain"] == "solana"), key=lambda t: t["tier"] != "hot")
    tiers = {t["address"]: t["tier"] for t in toks}
    n, snaps = 0, []
    for batch in _chunks(list(tiers), JUP_BATCH):
        try:
            got = await jupiter_search(batch)
        except jupiter.Busy:
            log.info("deep chain: Jupiter window busy, yielding to live trading this round")
            break
        except httpx.HTTPError as e:
            log.warning("deep chain jupiter: %s", _err(e))
            continue
        at = datetime.now(UTC)
        for mint, tok in got.items():
            v = await _store("solana", mint, {"jupiter": (tok, at)})
            n += 1
            if tiers[mint] == "hot" or at - _snapped.get(mint, _EPOCH) >= SNAPSHOT_WARM - SLACK:
                _snapped[mint] = at
                snaps.append(("solana", mint, at, v["holders"], v["top10_pct"], v["organic_score"], v["net_buyers_h1"]))
    await db.upsert("de_holder_snapshots", ["chain", "address", "ts", "holders", "top10_pct", "organic_score",
                                            "net_buyers_h1"], snaps, ["chain", "address", "ts"], update=[])
    return n


async def rugcheck_pass() -> int:
    """The Solana tokens whose report is due (hot 10 min, warm 2 h), most overdue first, PER_RUN at most."""
    toks = [t for t in await watch.tokens() if t["chain"] == "solana"]
    work = due(toks, await _stamps(_keys(toks)), "rugcheck", lambda t: EVERY["rugcheck"][t["tier"]], datetime.now(UTC))
    n = 0
    for t in work[:PER_RUN]:
        try:
            rep = await rugcheck_report(t["address"])
        except Throttled:
            log.warning("deep chain: RugCheck said slow down; stopping this round")
            break
        except httpx.HTTPError as e:
            await _store("solana", t["address"], {}, {"rugcheck": _err(e)})
            continue
        if rep is None:
            await _store("solana", t["address"], {}, {"rugcheck": "no report"})
        else:
            await _store("solana", t["address"], {"rugcheck": (rep, datetime.now(UTC))})
            n += 1
    return n


async def birdeye_pass() -> int:
    """Birdeye for the most overdue tokens (hot 15 min, warm 3 h, quiet ones 4x that) while today's pro-rata allowance
    lasts."""
    if not env_str("BIRDEYE_API_KEY"):
        return 0
    toks = [t for t in await watch.tokens() if t["chain"] in BIRDEYE_CHAINS]
    stamps, now = await _stamps(_keys(toks)), datetime.now(UTC)
    budget, n = allowance(birdeye_share(), await spent_today(), now), 0

    def every(t: dict) -> timedelta:
        return birdeye_every(t, (stamps.get((t["chain"], t["address"])) or {}).get("txns_h1"))

    for t in due(toks, stamps, "birdeye_traders", every, now):
        key = (t["chain"], t["address"])
        kinds = birdeye_kinds(t["chain"], stamps.get(key), now)
        if (cost := birdeye_cost(kinds)) > budget:
            break
        budget -= cost
        try:
            got, failed = await _birdeye(kinds, *key, priority=False)
        except birdeye.BudgetExceeded as e:
            log.info("deep chain: %s", e)
            break
        await _store(*key, got, failed)
        n += len(got)
    return n


async def honeypot_pass() -> int:
    """EVM tokens never simulated, or not for a day, PER_RUN at most."""
    toks = [t for t in await watch.tokens() if t["chain"] in EVM_CHAIN_IDS]
    work = due(toks, await _stamps(_keys(toks)), "honeypot", lambda t: EVERY["honeypot"][t["tier"]], datetime.now(UTC))
    n = 0
    for t in work[:PER_RUN]:
        try:
            res = await honeypot_check(t["chain"], t["address"])
        except Throttled:
            log.warning("deep chain: honeypot.is said slow down; stopping this round")
            break
        except httpx.HTTPError as e:
            await _store(t["chain"], t["address"], {}, {"honeypot": _err(e)})
            continue
        await _store(t["chain"], t["address"], {"honeypot": (res, datetime.now(UTC))})
        n += 1
    return n


# ─── on demand ─────────────────────────────────────────────────────────────────────────────────────────────────
FRESH = {"jupiter": EVERY["jupiter"]["hot"], "rugcheck": EVERY["rugcheck"]["hot"],
         "birdeye_traders": EVERY["birdeye"]["hot"], "honeypot": EVERY["honeypot"]["hot"]}


async def refresh(chain: str, address: str, priority: bool = True) -> int:
    """A trader opened this token's dossier: fetch every source not fetched within its hot cadence, all at once, each
    inside REFRESH_S, and store what came back. -> how many sources were fetched (0: all fresh, or none covers the
    chain). Concurrent calls for one token share a fetch, and a caller that stops waiting doesn't cancel it."""
    key = (chain, norm(address))
    task = _inflight.get(key)
    if task is None:
        task = _inflight[key] = asyncio.create_task(_refresh(*key, priority))

        def done(t: asyncio.Task) -> None:
            if _inflight.get(key) is t:
                del _inflight[key]
            if not t.cancelled() and t.exception():
                log.warning("deep chain refresh %s/%s: %s", *key, _err(t.exception()))
        task.add_done_callback(done)
    return await asyncio.shield(task)


async def _refresh(chain: str, address: str, priority: bool) -> int:
    stamp, now = (await _stamps([(chain, address)])).get((chain, address)), datetime.now(UTC)

    def stale(source: str) -> bool:
        last = _tried(stamp, source)
        return last is None or now - last >= FRESH[source]

    async def one_jupiter():
        return (await jupiter_search([address])).get(address)

    calls = {}
    if chain == "solana" and stale("jupiter"):
        calls["jupiter"] = one_jupiter()
    if chain == "solana" and stale("rugcheck"):
        calls["rugcheck"] = rugcheck_report(address)
    if chain in EVM_CHAIN_IDS and stale("honeypot"):
        calls["honeypot"] = honeypot_check(chain, address)
    kinds = birdeye_kinds(chain, stamp, now) if chain in BIRDEYE_CHAINS and env_str("BIRDEYE_API_KEY") else []
    if kinds and stale("birdeye_traders") and \
            await spent_today() + birdeye_cost(kinds) <= birdeye_share() * birdeye.PRIORITY_HEADROOM:
        calls["birdeye"] = _birdeye(kinds, chain, address, priority)
    if not calls:
        return 0
    results = await asyncio.gather(*(asyncio.wait_for(c, REFRESH_S) for c in calls.values()), return_exceptions=True)
    got, failed, at = {}, {}, datetime.now(UTC)
    for name, res in zip(calls, results):
        if isinstance(res, BaseException):          # timeout, HTTP error, busy window, throttled, budget spent
            failed["birdeye_traders" if name == "birdeye" else name] = _err(res)
        elif name == "birdeye":
            got |= res[0]
            failed |= res[1]
        elif res is None:
            failed[name] = "not found"
        else:
            got[name] = (res, at)
    await _store(chain, address, got, failed)
    return len(got)


# ─── reads for the dossier (F) and the market module (B) ──────────────────────────────────────────────────────
HISTORY_SQL = """SELECT time_bucket('10 minutes', ts) AS ts, last(holders, ts) AS holders, last(top10_pct, ts) AS top10_pct
                 FROM de_holder_snapshots WHERE chain = $1 AND address = $2 AND ts > now() - interval '24 hours'
                 GROUP BY 1 ORDER BY 1"""


async def jupiter_intel(chain: str, address: str) -> dict | None:
    """The stored Jupiter tokens v2 object of a Solana token as Jupiter sent it, plus "as_of" (when we fetched it):
    market.py reads its trade stats, organic score and holders here instead of asking Jupiter again."""
    return (await jupiter_intel_many([(chain, address)])).get((chain, address))


async def jupiter_intel_many(keys: list[tuple[str, str]]) -> dict[tuple[str, str], dict]:
    """jupiter_intel for many tokens in one query (the feed)."""
    keys = [k for k in keys if k[0] == "solana"]
    if not keys:
        return {}
    pool = await db.pool()
    try:
        rows = await pool.fetch(
            """SELECT chain, address, jupiter, jupiter_at FROM de_chain_intel
               WHERE jupiter IS NOT NULL AND (chain, address) IN (SELECT * FROM unnest($1::text[], $2::text[]))""",
            [k[0] for k in keys], [k[1] for k in keys])
    except asyncpg.UndefinedTableError:
        return {}
    return {(r["chain"], r["address"]): _load(r["jupiter"]) | {"as_of": r["jupiter_at"]} for r in rows}


async def section_security(chain: str, address: str) -> dict | None:
    row = await _row(chain, address)
    return build(chain, *_split(row))["security"] if row else None


async def section_holders(chain: str, address: str) -> dict | None:
    row = await _row(chain, address)
    if not row:
        return None
    pool = await db.pool()
    pools = {r["pool"] for r in await pool.fetch("SELECT pool FROM micro_pools WHERE chain = $1 AND token = $2",
                                                 chain, address)}
    history = [{"ts": r["ts"], "count": r["holders"], "top10_pct": _r(r["top10_pct"])}
               for r in await pool.fetch(HISTORY_SQL, chain, address)]
    return build(chain, *_split(row), pools, history)["holders"]


async def section_traders(chain: str, address: str) -> dict | None:
    row = await _row(chain, address)
    return build(chain, *_split(row))["traders"] if row else None


JOBS = {"deep_chain_jupiter": jupiter_pass, "deep_chain_rugcheck": rugcheck_pass,
        "deep_chain_birdeye": birdeye_pass, "deep_chain_honeypot": honeypot_pass}
SCHEDULE = [("deep_chain_jupiter", {"minute": "*/2", "second": 25}),
            ("deep_chain_rugcheck", {"minute": "1-59/2", "second": 5}),
            ("deep_chain_birdeye", {"minute": "*/5", "second": 50}),
            ("deep_chain_honeypot", {"minute": "*/10", "second": 35})]
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE = {"deep_chain_jupiter": 120, "deep_chain_rugcheck": 120, "deep_chain_birdeye": 300, "deep_chain_honeypot": 600}
