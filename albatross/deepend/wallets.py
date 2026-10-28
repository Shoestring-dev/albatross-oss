"""Wallet labels and clusters for The Deep End (docs/SPEC-DEEP-END.md): who the wallets around our coins are, which of
them trade together, and who funded the fresh ones. Deterministic rules on data we already collect; none of it is
graded yet, so the dossier shows it as evidence, never as a verdict.

Sources
  FOMO        de_fomo_traders (leaderboard traders, each one's wallet per chain) and de_fomo_events (FOMO users' large
              trades). A wallet counts only when proven ('onchain-holdings'); a provider-claimed one is kept as a lead
              (source fomo-claimed) and earns nothing. Events join traders on user_id (a handle can be renamed), else
              on the handle, case-insensitively. A buy is timed by FOMO's matched on-chain time when it has one, else
              by its event time (floored to 5 s).
  Chain intel de_chain_intel (chain.py): Birdeye's top traders per token (PnL and Birdeye's own tags), the top holders
              as chain.py reads them (pools, lockers and exchanges aside), the token's dev.
  RugCheck    the insider graph (GET /v1/tokens/{mint}/insiders/graph: the wallets and the transfers linking them) of a
              token whose report counts insiders: when that count changes, or a day after the last fetch for a hot
              token. At most 20 a run and 500 a day, metered in de_api_usage as 'rugcheck', through chain.py's own 1/s
              RugCheck limiter so the two modules together stay at 1/s. Kept whole in de_insider_graphs.
  Solana RPC  each fresh wallet's first funder, on your Solana RPC (settings.solana_rpc).

Labels (de_wallets.labels; a wallet can carry several, and evidence keeps the numbers behind each)
  top_trader  FOMO leaderboard best rank <= 150 (a proven wallet), or a Birdeye top trader in profit on >= 3 of our
              tokens when nothing marks it a bot (a bot's profit is not a trader's)
  sniper      bought a token within 60 s of its creation: a FOMO buy timed against the earliest creation we know
              (Jupiter's first pool, Birdeye's creation info, the micro pipeline's earliest pool; 10 s of slack for
              FOMO's 5 s floor), or Birdeye's own 'sniper' tag on a top trader
  deployer    the dev of a token we track; never a launchpad's signer (dossier.PLATFORM_DEVS: label launchpad) or a
              mass-launch address (>= dossier.SHARED_DEPLOYER launches: label mass_launch)
  bot         >= 30 trades in an hour: in any 60 minutes of the FOMO feed, or on average over Birdeye's 24 h window on a
              token (>= 720 trades), or Birdeye tags it a bot (arbitrage, MEV)
  exchange    a known exchange hot wallet (chain.CEX_WALLETS)
  service     the first funder of more than 200 of our wallets: an exchange or a service, not a person
Insider is scoped to the token, never a wallet-wide label: de_wallet_tokens role 'insider' = a participant in that
token's RugCheck insider graph (or a top holder RugCheck flags), which says nothing about the wallet's other coins.

Clusters (de_wallet_clusters, rebuilt on every run)
  co-trading  an edge for each token two wallets entered within 10 minutes of each other (their first FOMO buys; only
              a token's first 50 FOMO buyers, past that it's a crowd); a weaker edge, kept apart, for each token both
              were Birdeye top traders on. Pairs sharing >= 3 tokens are strong, and a cluster is a connected component
              over strong co-trading edges (union-find), leaving out exchanges, services, launchpads, mass-launch
              addresses and bots (a bot enters everything and would chain unrelated wallets together).
  funding     the wallets one first funder created, unless that funder is not a person (an exchange, a service, a
              launchpad, a mass-launch address). Bots may fund or join one: a bot seeding fresh wallets is a bundle.

First funders (Solana): a wallet with under 1,000 signatures is fresh. Its oldest successful transactions (up to 3)
are read until one moves SOL into it while it held none; when the oldest one finds it already holding SOL, the node's
history is incomplete and that is recorded instead of a guess. DEEP_FUNDER_DAILY_CALLS (2,000) RPC calls a day, paced
pro rata through the UTC day (chain.allowance) and at most 60 a run, metered in de_api_usage as 'chainstack': the plan
may be live trading's too, if you share it. Top holders of tokens whose holders were just read go first, then snipers,
deployers and co-trading cluster members, top traders, insiders (RugCheck already links them; a graph can hold over a
thousand), then the most recently seen.

Jobs: deep_wallets every 15 minutes (incremental from where each source left off), deep_funders every 10 minutes.
"""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import asyncpg
import httpx
import orjson

from albatross import controls, db
from albatross.config import settings
from albatross.deepend import chain as chain_intel
from albatross.deepend import dossier, watch
from albatross.deepend.cfg import env_int
from albatross.http import WeightLimiter, client
from albatross.micro.rules import norm

log = logging.getLogger(__name__)

# ─── rules ─────────────────────────────────────────────────────────────────────────────────────────────────────
TOP_RANK = 150
TOP_TOKENS = 3
# A wallet "in profit as a top trader" on this many of our tokens is a farm or a bot, not a trader to follow (the
# first evening: 20 wallets on 21-97 of our 931 tokens, 52 "good" lines resting on them alone).
TOP_TOKENS_MAX = 20
MAX_COTRADE = 25                         # a co-trading component bigger than this is a copy-trading crowd, not a cluster
SNIPE_S, SNIPE_SLACK_S = 60, 10
BOT_TRADES_1H = 30
BOT_TRADES_24H = 24 * BOT_TRADES_1H
COBUY_WINDOW = timedelta(minutes=10)
FIRST_BUYERS = 50
STRONG = 3
SERVICE_FANOUT = 200
# An app or a sponsor, not a person: a funder whose every grant was dust (FOMO-style activation sends of exactly 0.01
# SOL, seen 2026-10-01). Funding spread over weeks is NOT a service: on prod every such funder was a repeat launcher
# seeding deployers and snipers (the intel the funding groups exist for); funded_lead_h already stops old grants from
# reading as a bundle.
DUST_SOL = 0.05
EARLY_BUYERS = 20
PROVEN = "onchain-holdings"
CHAINS = ("solana", "ethereum", "base", "bsc", "robinhood")
NOT_PEOPLE = ("exchange", "service", "launchpad", "mass_launch")
NO_CLUSTER = (*NOT_PEOPLE, "bot")
LABELS = ("top_trader", "sniper", "deployer", "bot", *NOT_PEOPLE)
SOURCE = {"buyer": "fomo", "seller": "fomo", "trader": "birdeye", "holder": "holders", "insider": "rugcheck",
          "deployer": "deployer"}

# ─── runs ──────────────────────────────────────────────────────────────────────────────────────────────────────
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)
OVERLAP = timedelta(minutes=5)           # each source is re-read this far back, so a row committed late isn't missed
INTEL_PER_RUN = 2000
EVENTS_PER_RUN = 50_000
EDGE_TTL = timedelta(days=30)            # a pair that never got strong is dropped this long after it was last seen
PENDING = timedelta(days=2)              # FOMO buys still waiting for their token's creation time

# ─── RugCheck insider graphs ───────────────────────────────────────────────────────────────────────────────────
GRAPH_URL = "https://api.rugcheck.xyz/v1/tokens/{}/insiders/graph"
GRAPHS_PER_RUN, GRAPHS_PER_DAY = 20, 500
GRAPH_REFRESH = timedelta(hours=24)
GRAPH_TIMEOUT = 8.0

# ─── first funders ─────────────────────────────────────────────────────────────────────────────────────────────
SIG_LIMIT = 1000
FUNDER_TX_TRIES = 3
PER_RUN_CALLS = 60
WALLETS_PER_RUN = 60
RETRY_FAILED = timedelta(hours=6)
MAX_FAILURES = 3                         # failed wallets in a row end the run: that's the endpoint, not the wallets
RPC_TIMEOUT = 15.0
RPC_TALLY = "chainstack"
_rpc_pace = WeightLimiter(per_minute=120, burst=1)


class BudgetExceeded(RuntimeError):
    """The day's RPC calls are spent."""


class Throttled(RuntimeError):
    """The API said slow down (HTTP 429): stop asking it this run."""


class RpcFailed(RuntimeError):
    """One RPC call failed. Never carries the endpoint URL: it holds the key."""


def _num(v) -> float | None:
    if v is None or v == "" or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _lo(a, b):
    return b if a is None else a if b is None else min(a, b)


def _hi(a, b):
    return b if a is None else a if b is None else max(a, b)


def _load(v):
    return orjson.loads(v) if isinstance(v, str | bytes) else v


def _json(v) -> str:
    return orjson.dumps(v, default=str).decode()


def _when(v) -> datetime | None:
    """An ISO time or unix seconds -> an aware datetime; None for anything else."""
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if v is None or v == "":
        return None
    try:
        return datetime.fromtimestamp(float(v), UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=UTC)


# ─── readings: one wallet around one token (pure) ──────────────────────────────────────────────────────────────
def seen(roles, at: datetime, **kw) -> dict:
    """One reading of a wallet around a token, in de_wallet_tokens' shape."""
    return {"roles": set(roles), "first_buy_at": None, "launch_s": None, "pnl_usd": None, "traded_at": None,
            "holding_pct": None, "held_at": None, "first_at": at, "last_at": at, **kw}


def merge(old: dict | None, new: dict) -> dict:
    """Two readings of one wallet on one token as one: roles accumulate, the first buy is the earliest, PnL and holding
    are the newer reading's. launch_s holds while the first buy does (timed() sets it)."""
    if old is None:
        return {**new, "roles": set(new["roles"])}
    newer_trade = new["traded_at"] is not None and (old["traded_at"] is None or new["traded_at"] >= old["traded_at"])
    newer_hold = new["held_at"] is not None and (old["held_at"] is None or new["held_at"] >= old["held_at"])
    first_buy = _lo(old["first_buy_at"], new["first_buy_at"])
    return {"roles": set(old["roles"]) | set(new["roles"]), "first_buy_at": first_buy,
            "launch_s": old["launch_s"] if first_buy == old["first_buy_at"] else None,
            "pnl_usd": new["pnl_usd"] if newer_trade else old["pnl_usd"], "traded_at": _hi(old["traded_at"], new["traded_at"]),
            "holding_pct": new["holding_pct"] if newer_hold else old["holding_pct"],
            "held_at": _hi(old["held_at"], new["held_at"]),
            "first_at": _lo(old["first_at"], new["first_at"]), "last_at": _hi(old["last_at"], new["last_at"])}


def timed(row: dict, created: datetime | None) -> dict:
    """launch_s: the first buy's seconds after the token's creation, once both are known."""
    if row["first_buy_at"] is None or created is None:
        return row
    return {**row, "launch_s": round((row["first_buy_at"] - created).total_seconds(), 1)}


def sniped(row: dict) -> bool:
    """Bought within SNIPE_S of the token's creation: Birdeye's tag, or a FOMO first buy timed against the creation."""
    s = row.get("launch_s")
    return "sniper" in row["roles"] or (s is not None and -SNIPE_SLACK_S <= s <= SNIPE_S)


def creation(r: dict) -> datetime | None:
    """A token's creation: the earliest of Jupiter's first pool, Jupiter's createdAt, Birdeye's creation info and the
    micro pipeline's earliest pool. The earliest, because a later one (a graduated pool's) would make ordinary buys
    look like snipes."""
    return min((t for k in ("jup_pool", "jup_created", "be_block", "pool_created") if (t := _when(r.get(k)))), default=None)


# ─── the sources (pure) ────────────────────────────────────────────────────────────────────────────────────────
def from_intel(r: dict) -> tuple[list[tuple[tuple, dict]], list[str]]:
    """What one de_chain_intel row says about wallets: its Birdeye top traders, and its top holders as chain.py reads
    them, without pools, lockers and exchanges. -> (readings, the top traders that may form a top-trader edge: Birdeye's
    bots can't)."""
    chain, token = r["chain"], r["address"]
    out, pairable = [], []
    if r.get("traders_at") and isinstance(r.get("traders"), list):
        at = r["traders_at"]
        for t in r["traders"]:
            owner = norm(t.get("owner") or "")
            if not owner or owner in chain_intel.CEX_WALLETS:
                continue
            tags = {x.lower() for x in t.get("tags") or []}
            roles = {"trader"} | ({"sniper"} & tags)
            if any("bot" in x or "mev" in x for x in tags) or (_num(t.get("trade")) or 0) >= BOT_TRADES_24H:
                roles.add("bot")
            else:
                pairable.append(owner)
            pnl = _num(t.get("totalPnl"))
            out.append(((chain, token, owner), seen(roles, at, traded_at=at,
                                                    pnl_usd=pnl if pnl is not None else _num(t.get("realizedPnl")))))
    rc = r.get("rugcheck") or {}
    held_at = r.get("rugcheck_at") if rc.get("topHolders") else r.get("birdeye_holders_at")
    if held_at:
        raw = {"rugcheck": rc, "jupiter": r.get("jupiter") or {}, "birdeye_holders": r.get("birdeye_holders")}
        top, _ = chain_intel.top_holders(raw, {r.get("dev_address")} - {None}, set(r.get("pools") or []), set())
        for h in top:
            if h["owner"] and not {"pool", "lp", "cex"} & set(h["tags"]):
                out.append(((chain, token, h["owner"]), seen({"holder"} | ({"insider"} & set(h["tags"])), held_at,
                                                              holding_pct=h["pct"], held_at=held_at)))
    return out, pairable


def compact_graph(body) -> list[dict]:
    """RugCheck's insider graph -> [{id, type, wallets: [{address, participant, holdings}], links: [{from, to,
    amount}]}] (amount when RugCheck gives one)."""
    out = []
    for n in body if isinstance(body, list) else []:
        if not isinstance(n, dict):
            continue
        out.append({"id": n.get("net_id"), "type": n.get("network_type"),
                    "wallets": [{"address": w["id"], "participant": bool(w.get("participant")),
                                 "holdings": _num(w.get("holdings"))}
                                for w in n.get("nodes") or [] if isinstance(w, dict) and w.get("id")],
                    "links": [{"from": x["source"], "to": x["target"], "amount": _num(x.get("amount"))}
                              for x in n.get("links") or [] if isinstance(x, dict) and x.get("source") and x.get("target")]})
    return out


MAX_INSIDERS = 500                      # one RugCheck graph held 17,257 participants (2026-10-01); a token's insider
                                         # line already counts them: we keep its biggest networks' wallets up to this


def insiders(networks: list[dict]) -> set[str]:
    """The wallets that took part in the token (RugCheck's participants), the smallest networks first (a tight group
    is the most specific; the biggest are transfer webs of thousands), at most MAX_INSIDERS; the others only link
    them."""
    out: list[str] = []
    for n in sorted(networks, key=lambda n: len(n.get("wallets") or [])):
        out += [w["address"] for w in n.get("wallets") or [] if w.get("participant") and w["address"] not in out]
        if len(out) >= MAX_INSIDERS:
            break
    return set(out[:MAX_INSIDERS])


def graph_due(r: dict, hot: set, now: datetime) -> bool:
    """A token's insider graph is fetched when its report counts insiders and we have no graph, the count changed since
    our fetch, or (a hot token) our fetch is a day old."""
    if not r.get("detected"):
        return False
    if r.get("fetched_at") is None or r.get("fetched_detected") != r["detected"]:
        return True
    return r["token"] in hot and now - r["fetched_at"] >= GRAPH_REFRESH


def trader_index(rows) -> tuple[dict, dict, dict]:
    """FOMO traders by user_id, by lower(handle) and by wallet ((family, address), family solana or evm). A renamed
    trader can sit on two rows (the key is the handle): the newest row per user_id is the trader. On one wallet, a
    proven claim beats a provider's, then the better rank."""
    rows = sorted((dict(r) for r in rows), key=lambda r: (r.get("updated_at") or EPOCH, r.get("best_rank") is not None))
    by_uid = {r["user_id"]: r for r in rows if r.get("user_id") is not None}
    by_handle = {(r.get("handle") or "").lower(): r for r in rows}
    best: dict = {}
    for r in rows:
        if r.get("user_id") is not None and by_uid[r["user_id"]] is not r:
            continue
        for fam, wallet, ev in (("solana", r.get("solana_wallet"), r.get("solana_evidence")),
                                ("evm", norm(r.get("evm_wallet") or ""), r.get("evm_evidence"))):
            rank = (ev == PROVEN, -(r.get("best_rank") or 1e9))
            if wallet and ((fam, wallet) not in best or rank > best[(fam, wallet)][0]):
                best[(fam, wallet)] = (rank, r)
    return by_uid, by_handle, {k: r for k, (_, r) in best.items()}


def trader_for(e: dict, by_uid: dict, by_handle: dict) -> dict | None:
    """The trader behind a FOMO event: by user_id, else by handle (then its newest row)."""
    r = by_uid.get(e.get("user_id")) if e.get("user_id") is not None else None
    if r is None:
        r = by_handle.get((e.get("handle") or "").lower())
        if r is not None and r.get("user_id") is not None:
            r = by_uid.get(r["user_id"], r)
    return r


def wallet_of(t: dict, chain: str) -> tuple[str | None, bool]:
    """A trader's wallet on a chain, and whether it is proven."""
    if chain == "solana":
        return t.get("solana_wallet") or None, t.get("solana_evidence") == PROVEN
    return norm(t.get("evm_wallet") or "") or None, t.get("evm_evidence") == PROVEN


def fomo_time(e: dict) -> datetime:
    """FOMO's matched on-chain time (ms) when it has one, else its event time."""
    try:
        return datetime.fromtimestamp(float(e["exec_ms"]) / 1000, UTC) if e.get("exec_ms") else e["ts"]
    except (TypeError, ValueError, OverflowError, OSError):
        return e["ts"]


def from_fomo(events, by_uid: dict, by_handle: dict) -> tuple[list[tuple[tuple, dict]], dict]:
    """Buys and sells by proven wallets -> (readings, {(chain, wallet): handle} of the provider-claimed wallets we saw
    trade, kept as leads)."""
    out, leads = [], {}
    for e in events:
        t = trader_for(e, by_uid, by_handle)
        wallet, proven = wallet_of(t, e["chain"]) if t else (None, False)
        token = norm(e.get("token_address") or "")
        if not wallet or not token:
            continue
        if not proven:
            leads[(e["chain"], wallet)] = t["handle"]
            continue
        at, buy = fomo_time(e), e["type"] == "buy"
        out.append(((e["chain"], token, wallet), seen({"buyer" if buy else "seller"}, at, first_buy_at=at if buy else None)))
    return out, leads


def busiest_hour(times: list[datetime]) -> int:
    """The most of these times inside any 60 minutes."""
    ts, best, i = sorted(times), 0, 0
    for j, t in enumerate(ts):
        while t - ts[i] >= timedelta(hours=1):
            i += 1
        best = max(best, j - i + 1)
    return best


# ─── labels (pure) ─────────────────────────────────────────────────────────────────────────────────────────────
def rules(s: dict) -> tuple[list[str], dict]:
    """A wallet's labels and the numbers behind each, from its stats: fomo_rank (a proven wallet's), profitable
    (tokens it is a Birdeye top trader in profit on, not tagged a bot there), snipes and fastest_s, deployed,
    max_trades_1h, bot_tokens, exchange (name), funded (wallets it first-funded), launchpad (name), launches."""
    ev: dict = {}
    bot = {}
    if (s.get("max_trades_1h") or 0) >= BOT_TRADES_1H:
        bot["max_trades_1h"] = s["max_trades_1h"]
    if s.get("bot_tokens"):
        bot["birdeye_tokens"] = s["bot_tokens"]
    top = {}
    if s.get("fomo_rank") is not None and s["fomo_rank"] <= TOP_RANK:    # FOMO's board is proof, busy or not
        top["fomo_rank"] = s["fomo_rank"]
    # Birdeye's profit alone makes a top trader only for someone who trades: never a bot, a sniper, a deployer, a
    # launch machine or an exchange, and never on so many of our tokens that it can only be a farm.
    trades_like_a_person = not (bot or s.get("snipes") or s.get("deployed") or s.get("launches") or s.get("exchange")
                                or s.get("launchpad") or (s.get("funded") or 0) > SERVICE_FANOUT)
    if TOP_TOKENS <= (s.get("profitable") or 0) <= TOP_TOKENS_MAX and trades_like_a_person:
        top["profitable_tokens"] = s["profitable"]
    if top:
        ev["top_trader"] = top
    if s.get("snipes"):
        ev["sniper"] = {"tokens": s["snipes"]} | ({"fastest_s": s["fastest_s"]} if s.get("fastest_s") is not None else {})
    if s.get("deployed"):
        ev["deployer"] = {"tokens": s["deployed"]}
    if bot:
        ev["bot"] = bot
    if s.get("exchange"):
        ev["exchange"] = {"name": s["exchange"]}
    if (s.get("funded") or 0) > SERVICE_FANOUT:
        ev["service"] = {"funded": s["funded"]}
    if s.get("launchpad"):
        ev["launchpad"] = {"name": s["launchpad"]}
    if s.get("launches"):
        ev["mass_launch"] = {"launches": s["launches"]}
    return [label for label in LABELS if label in ev], ev


# ─── edges and clusters (pure) ─────────────────────────────────────────────────────────────────────────────────
def cobuys(buyers: list[tuple[str, datetime]], excluded=frozenset()) -> dict[tuple[str, str], datetime]:
    """Co-trading pairs on one token: among its first FIRST_BUYERS buyers (wallet, first buy), every two that entered
    within COBUY_WINDOW of each other -> when the later one entered. Excluded wallets still count toward the crowd."""
    first = sorted(buyers, key=lambda b: b[1])[:FIRST_BUYERS]
    out = {}
    for i, (a, ta) in enumerate(first):
        for b, tb in first[i + 1:]:
            if tb - ta > COBUY_WINDOW:
                break
            if a != b and a not in excluded and b not in excluded:
                out[(min(a, b), max(a, b))] = tb
    return out


def components(edges) -> list[set]:
    """Connected components over undirected edges (union-find), largest first."""
    parent: dict = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    for a, b in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)
    groups = defaultdict(set)
    for x in parent:
        groups[find(x)].add(x)
    return sorted(groups.values(), key=lambda g: (-len(g), min(g)))


def cotrade_clusters(edges: list[tuple[str, str, str]], excluded: set) -> list[tuple[str, set]]:
    """(chain, members) for each connected component over strong co-trading edges (chain, a, b), leaving out wallets
    that must not link others. FOMO is a copy-trading app: followers chain the traders they copy into one component,
    so a component bigger than MAX_COTRADE is a crowd, not a cluster, and is left out."""
    by_chain = defaultdict(list)
    for chain, a, b in edges:
        if (chain, a) not in excluded and (chain, b) not in excluded:
            by_chain[chain].append((a, b))
    return [(c, m) for c in sorted(by_chain) for m in components(by_chain[c]) if len(m) <= MAX_COTRADE]


def services(funded: list[tuple[str, str, str]]) -> dict[tuple[str, str], int]:
    """First funders that must never link wallets -> how many of ours each funded: known exchanges, and anyone who funded
    more than SERVICE_FANOUT of them (an exchange or a service we don't know by name)."""
    fan = Counter((c, f) for c, _, f in funded)
    return {k: n for k, n in fan.items() if n > SERVICE_FANOUT or k[1] in chain_intel.CEX_WALLETS}


def sponsors(grants: list[tuple[str, str, str, datetime | None, float | None]]) -> dict[tuple[str, str], str]:
    """Funders that fund like an app, not a person -> why: 'dust' (two or more grants to our wallets, every one
    DUST_SOL or less). grants: (chain, wallet, funder, funded_at, funded_sol)."""
    by = defaultdict(list)
    for chain, _, funder, at, sol in grants:
        by[(chain, funder)].append((at, sol))
    out = {}
    for k, gs in by.items():
        if len(gs) < 2:
            continue
        sols = [sol for _, sol in gs if sol is not None]
        if sols and len(sols) == len(gs) and max(sols) <= DUST_SOL:
            out[k] = "dust"
    return out


def funding_clusters(funded: list[tuple[str, str, str]], not_linking: set, excluded: set) -> list[tuple[str, str, set]]:
    """(chain, funder, members) for each first funder of two or more of our wallets (chain, wallet, funder), unless the
    funder is an exchange, a service or another address that must not link wallets."""
    groups = defaultdict(set)
    for chain, wallet, funder in funded:
        if (chain, funder) not in not_linking and (chain, wallet) not in excluded:
            groups[(chain, funder)].add(wallet)
    return [(c, f, m) for (c, f), m in sorted(groups.items()) if len(m) >= 2]


def cluster_row(cid: str, chain: str, kind: str, funder: str | None, members: set, tokens: dict, labels: dict,
                now: datetime) -> tuple:
    """A de_wallet_clusters row. tokens: member -> [(token, first_at, last_at)]; labels: member -> its labels."""
    shared = Counter(t for m in members for t in {x[0] for x in tokens.get(m, [])})
    common = [t for t, n in shared.most_common() if n >= 2]
    seen_at = [x for m in members for x in tokens.get(m, [])]
    mix = Counter(label for m in members for label in labels.get(m, []))
    return (cid, chain, kind, funder, len(members), sorted(members), _json(dict(sorted(mix.items()))), common[:50],
            len(common), min((x[1] for x in seen_at), default=None), max((x[2] for x in seen_at), default=None), now)


# ─── first funders (pure; the RPC is passed in) ────────────────────────────────────────────────────────────────
INCOMPLETE = "incomplete"


def funding(tx: dict | None, wallet: str) -> dict | str | None:
    """The SOL transfer into `wallet` in a jsonParsed transaction, when the wallet held nothing before it: {funder,
    lamports}. INCOMPLETE when it already held SOL (an older transaction moved it there that the node didn't list);
    None when this transaction moved no SOL into it (an account someone opened for it, say)."""
    if not isinstance(tx, dict):
        return None
    meta, msg = tx.get("meta") or {}, (tx.get("transaction") or {}).get("message") or {}
    keys = [k.get("pubkey") if isinstance(k, dict) else k for k in msg.get("accountKeys") or []]
    if wallet not in keys or meta.get("err") is not None:
        return None
    pre = meta.get("preBalances") or []
    i = keys.index(wallet)
    if i < len(pre) and pre[i]:
        return INCOMPLETE
    inner = {x.get("index"): x.get("instructions") or [] for x in meta.get("innerInstructions") or []}
    for j, ix in enumerate(msg.get("instructions") or []):
        for step in (ix, *inner.get(j, [])):
            p = step.get("parsed") if step.get("program") == "system" else None
            if not isinstance(p, dict):
                continue
            info = p.get("info") or {}
            to = info.get("destination") if p.get("type") in ("transfer", "transferWithSeed") else \
                info.get("newAccount") if p.get("type") in ("createAccount", "createAccountWithSeed") else None
            if to == wallet and info.get("source") and info["source"] != wallet:
                return {"funder": info["source"], "lamports": int(_num(info.get("lamports")) or 0)}
    return None


async def first_funder(address: str, rpc) -> dict:
    """Walk a Solana wallet back to the transfer that created it -> {status, signatures, and when found: funder,
    lamports, at, tx}. rpc(method, params) is one metered call. Status: found | not fresh (>= SIG_LIMIT signatures) |
    no transfer (none of its oldest FUNDER_TX_TRIES transactions funded it) | history incomplete."""
    sigs = await rpc("getSignaturesForAddress", [address, {"limit": SIG_LIMIT, "commitment": "finalized"}])
    sigs = sigs if isinstance(sigs, list) else []
    if len(sigs) >= SIG_LIMIT:
        return {"status": "not fresh", "signatures": len(sigs)}
    for s in [s for s in reversed(sigs) if s.get("err") is None][:FUNDER_TX_TRIES]:
        tx = await rpc("getTransaction", [s["signature"], {"encoding": "jsonParsed", "commitment": "finalized",
                                                           "maxSupportedTransactionVersion": 0}])
        got = funding(tx, address)
        if got == INCOMPLETE:
            return {"status": "history incomplete", "signatures": len(sigs)}
        if got:
            at = (tx or {}).get("blockTime") or s.get("blockTime")
            return {"status": "found", "signatures": len(sigs), **got, "tx": s["signature"],
                    "at": datetime.fromtimestamp(at, UTC) if at else None}
    return {"status": "no transfer", "signatures": len(sigs)}


# ─── the dossier section (pure) ────────────────────────────────────────────────────────────────────────────────
SHOWN = ("top_trader", "sniper", "insider", "bot", "deployer", *NOT_PEOPLE)
WORDS = {"top_trader": ("ranked trader", "ranked traders"), "sniper": ("sniper", "snipers"),
         "insider": ("insider", "insiders"), "bot": ("bot", "bots"),
         "deployer": ("deployer of another coin", "deployers of other coins")}


def _n(n: int, one: str, many: str | None = None) -> str:
    return f"{n:,} {one if n == 1 else many or one + 's'}"


def short(a: str | None) -> str | None:
    return f"{a[:4]}…{a[-4:]}" if a and len(a) > 12 else a


def _minutes(m: float) -> str:
    return "a minute" if m < 1 else _n(round(m), "minute")


def shape(rows: list[dict], clusters: dict, dev: str | None, dev_funder: str | None, not_linking: set) -> dict | None:
    """The dossier's wallets section from one token's de_wallet_tokens rows, each with its wallet's labels, first funder
    and cluster ids. dev: its deployer when that is a person (not a launchpad's signer or a mass-launch address);
    not_linking: funders that are exchanges or services. -> label counts (insider counts this token's only, deployer
    other coins' deployers), clusters with two or more members on it, snipers among its first FOMO buyers, its top
    holders' funding, and one-line findings; None when no wallet around it is known."""
    if not rows:
        return None
    counts: Counter = Counter()
    for r in rows:
        mine = set(r.get("labels") or [])
        if "deployer" in r["roles"]:
            mine.discard("deployer")                   # its own dev: the security section's
        if "insider" in r["roles"]:
            mine.add("insider")                        # an insider of this token
        counts.update(mine)
    by_cluster = defaultdict(list)
    for r in rows:
        for cid in {r.get("cluster_id"), r.get("funding_cluster_id")} & set(clusters):
            by_cluster[cid].append(r)
    active = []
    for cid, ms in by_cluster.items():
        if len(ms) < 2:
            continue
        c, buys = clusters[cid], sorted(m["first_buy_at"] for m in ms if m["first_buy_at"])
        # how long before its buy the earliest-funded buyer got its SOL: a bundle is funded just before it buys
        leads = [(m["first_buy_at"] - m["funded_at"]) / timedelta(hours=1) for m in ms
                 if m.get("first_buy_at") and m.get("funded_at")]
        active.append({"id": cid, "kind": c["kind"], "size": c["size"], "here": len(ms), "bought": len(buys),
                       "within_min": round((buys[-1] - buys[0]).total_seconds() / 60, 1) if len(buys) >= 2 else None,
                       "funded_lead_h": round(max(leads), 1) if c["kind"] == "funding" and leads else None,
                       "funder": c.get("funder"), "labels": c.get("labels") or {}})
    active.sort(key=lambda c: (-c["here"], -c["size"], c["id"]))
    buyers = sorted((r for r in rows if r["first_buy_at"]), key=lambda r: r["first_buy_at"])[:EARLY_BUYERS]
    snipers = sum("sniper" in (r.get("labels") or []) for r in buyers)
    early = {"buyers": len(buyers), "snipers": snipers, "share": round(snipers / len(buyers), 2),
             "sniped_it": sum(sniped(r) for r in buyers)} if buyers else None
    latest = max((r["held_at"] for r in rows if r["held_at"]), default=None)
    holders = [r for r in rows if latest and r["held_at"] == latest]
    funding_ = None
    if holders:
        known = [h for h in holders if h.get("first_funder")]
        groups = defaultdict(list)
        for h in known:
            if h["first_funder"] not in not_linking and h["first_funder"] != dev:
                groups[h["first_funder"]].append(h["address"])
        funding_ = {"holders": len(holders), "checked": sum(h.get("funder_status") is not None for h in holders),
                    "funders_known": len(known),
                    "by_deployer": sorted(h["address"] for h in known if dev and h["first_funder"] == dev),
                    "shared": [{"funder": f, "holders": sorted(hs), "deployer_too": f == dev_funder}
                               for f, hs in sorted(groups.items(), key=lambda x: (-len(x[1]), x[0]))
                               if len(hs) >= 2 or f == dev_funder]}
    tagged = [{"owner": h["address"], "pct": h["holding_pct"], "labels": sorted(set(h.get("labels") or []) |
                                                                                 ({"insider"} & set(h["roles"]))),
               "first_funder": h.get("first_funder")}
              for h in sorted(holders, key=lambda h: -(h["holding_pct"] or 0))
              if h.get("labels") or "insider" in h["roles"] or h.get("first_funder")]
    out = {"wallets": len(rows), "labels": {k: counts[k] for k in SHOWN if counts.get(k)}, "clusters": active,
           "early": early, "funding": funding_, "top_holders": tagged,
           "as_of": max((r["last_at"] for r in rows if r.get("last_at")), default=None)}
    out["findings"] = findings(out)
    return out


def findings(s: dict) -> list[str]:
    """Plain-English lines for the section's numbers, strongest first."""
    out, n = [], s["wallets"]
    people = [_n(s["labels"][k], *WORDS[k]) for k in WORDS if s["labels"].get(k)]
    if people:
        out.append(f"Of the {_n(n, 'wallet')} seen trading or holding it: {', '.join(people)}.")
    for c in s["clusters"][:3]:
        timing = f", {c['bought']} of them bought within {_minutes(c['within_min'])} of each other" \
            if c["within_min"] is not None else ""
        if c["kind"] == "cotrade":
            out.append(f"{c['here']} wallets that often trade together ({c['size']} in their group) are on it{timing}; "
                       "on FOMO that is often copy-trading.")
        else:
            out.append(f"{c['here']} wallets created by one funder ({short(c['funder'])}, {c['size']} of the wallets we "
                       f"know) are on it{timing}.")
    e = s["early"]
    if e and e["buyers"] >= 5 and e["snipers"]:
        out.append(f"{e['snipers']} of the first {e['buyers']} FOMO buyers we saw on it are snipers ({e['share']:.0%}).")
    f = s["funding"]
    if f and f["by_deployer"]:
        k = len(f["by_deployer"])
        out.append(f"{_n(k, 'top holder')} {'was' if k == 1 else 'were'} funded by its deployer.")
    for g in (f or {}).get("shared", [])[:2]:
        k = len(g["holders"])
        who = f"{_n(k, 'top holder')} {'was' if k == 1 else 'were'} funded by one wallet ({short(g['funder'])})"
        out.append(who + (", as was its deployer." if g["deployer_too"] else "."))
    if not out:
        out.append(f"None of the {_n(n, 'wallet')} seen around it carries a label yet.")
    return out


# ─── metering ──────────────────────────────────────────────────────────────────────────────────────────────────
async def used_today(provider: str) -> int:
    pool = await db.pool()
    return int(await pool.fetchval(
        "SELECT coalesce(sum(calls), 0) FROM de_api_usage WHERE provider = $1 AND day = (now() AT TIME ZONE 'UTC')::date",
        provider) or 0)


async def _meter(provider: str) -> None:
    pool = await db.pool()
    await pool.execute(
        """INSERT INTO de_api_usage (provider, day, calls, units) VALUES ($1, (now() AT TIME ZONE 'UTC')::date, 1, 1)
           ON CONFLICT (provider, day) DO UPDATE SET calls = de_api_usage.calls + 1, units = de_api_usage.units + 1""",
        provider)


async def daily_calls() -> int:
    """Today's RPC calls for first funders: DEEP_FUNDER_DAILY_CALLS, scaled from the Output page."""
    return int(env_int("DEEP_FUNDER_DAILY_CALLS", 2000) * await controls.scale("deep_funders"))


def rpc_url() -> str | None:
    return settings.solana_rpc or settings.chainstack_url


async def rpc(method: str, params: list):
    """One Solana JSON-RPC call on our endpoint, counted in de_api_usage before it is sent (answered or not). Raises
    BudgetExceeded past the day's calls, Throttled on HTTP 429, RpcFailed otherwise; no message carries the URL."""
    url = rpc_url()
    if not url:
        raise RpcFailed("no Solana RPC configured")
    if await used_today(RPC_TALLY) + 1 > (daily := await daily_calls()):
        raise BudgetExceeded(f"{RPC_TALLY}: the day's {daily:,} calls are spent")
    await _rpc_pace.acquire()
    await _meter(RPC_TALLY)
    try:
        r = await client().post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                                timeout=RPC_TIMEOUT)
    except httpx.HTTPError as e:
        raise RpcFailed(f"{method}: {type(e).__name__}") from None
    if r.status_code == 429:
        raise Throttled(f"{method}: HTTP 429")
    if r.status_code != 200:
        raise RpcFailed(f"{method}: HTTP {r.status_code}")
    try:
        body = r.json()
    except ValueError:
        raise RpcFailed(f"{method}: not JSON") from None
    if body.get("error"):
        err = body["error"] if isinstance(body["error"], dict) else {"message": body["error"]}
        raise RpcFailed(f"{method}: RPC error {err.get('code')}: {str(err.get('message'))[:120]}")
    return body.get("result")


# ─── RugCheck insider graphs ───────────────────────────────────────────────────────────────────────────────────
async def fetch_graph(mint: str) -> list[dict] | None:
    """RugCheck's insider graph for a mint, compacted; None when it failed (404, 5xx, network: logged, tried again next
    run). Raises Throttled on a 429. Metered, and paced by chain.py's RugCheck limiter."""
    await chain_intel._rug_pace.acquire()
    await _meter("rugcheck")
    try:
        r = await client().get(GRAPH_URL.format(mint), timeout=GRAPH_TIMEOUT)
    except httpx.HTTPError as e:
        log.warning("deep wallets: RugCheck insider graph %s: %s", mint, type(e).__name__)
        return None
    if r.status_code == 429:
        raise Throttled("rugcheck: HTTP 429")
    if r.status_code != 200:
        log.warning("deep wallets: RugCheck insider graph %s: HTTP %s", mint, r.status_code)
        return None
    try:
        return compact_graph(r.json())
    except ValueError:
        log.warning("deep wallets: RugCheck insider graph %s: not JSON", mint)
        return None


# ─── deep_wallets ──────────────────────────────────────────────────────────────────────────────────────────────
@dataclass
class Batch:
    """What one deep_wallets run learned, before it is stored."""
    obs: dict = field(default_factory=dict)          # (chain, token, address) -> reading
    bought: set = field(default_factory=set)         # (chain, token) with new FOMO buys
    trades_1h: dict = field(default_factory=dict)    # (chain, wallet) -> its busiest hour in the FOMO feed
    leads: dict = field(default_factory=dict)        # (chain, wallet) -> FOMO handle (provider-claimed)

    def add(self, key: tuple, row: dict) -> None:
        self.obs[key] = merge(self.obs.get(key), row)


INTEL_SQL = """
SELECT ci.chain, ci.address, ci.dev_address, ci.rugcheck, ci.rugcheck_at, ci.birdeye_traders->'items' AS traders,
       ci.birdeye_traders_at AS traders_at, ci.birdeye_holders, ci.birdeye_holders_at,
       jsonb_build_object('totalSupply', ci.jupiter->'totalSupply', 'graduatedPool', ci.jupiter->'graduatedPool') AS jupiter,
       (SELECT array_agg(p.pool) FROM micro_pools p WHERE p.chain = ci.chain AND p.token = ci.address) AS pools,
       greatest(ci.rugcheck_at, ci.birdeye_traders_at, ci.birdeye_holders_at) AS at
FROM de_chain_intel ci
WHERE ci.rugcheck_at > $1 OR ci.birdeye_traders_at > $1 OR ci.birdeye_holders_at > $1
ORDER BY at LIMIT $2"""

DEPLOYERS_SQL = """
SELECT ci.chain, ci.address AS token, ci.dev_address, ci.updated_at FROM de_chain_intel ci
WHERE ci.dev_address IS NOT NULL AND coalesce(ci.dev_launches, 0) < $1 AND NOT (ci.dev_address = ANY($2::text[]))
  AND ci.updated_at > now() - interval '30 days'          -- prune() drops older readings: never re-add them
  AND NOT EXISTS (SELECT 1 FROM de_wallet_tokens t WHERE t.chain = ci.chain AND t.token = ci.address
                  AND t.address = ci.dev_address AND 'deployer' = ANY(t.roles))
LIMIT $3"""

MASS_SQL = """SELECT chain, dev_address, max(dev_launches) AS launches FROM de_chain_intel
              WHERE dev_launches >= $1 GROUP BY 1, 2"""

GRAPH_DUE_SQL = """
SELECT ci.address AS token, ci.rugcheck_at,
       CASE WHEN jsonb_typeof(ci.rugcheck->'graphInsidersDetected') = 'number'
            THEN (ci.rugcheck->>'graphInsidersDetected')::numeric::int END AS detected,
       g.detected AS fetched_detected, g.fetched_at
FROM de_chain_intel ci LEFT JOIN de_insider_graphs g ON g.chain = ci.chain AND g.token = ci.address
WHERE ci.chain = 'solana' AND ci.rugcheck IS NOT NULL"""

GRAPH_UPSERT_SQL = """
INSERT INTO de_insider_graphs (chain, token, detected, networks, fetched_at) VALUES ($1, $2, $3, $4::jsonb, $5)
ON CONFLICT (chain, token) DO UPDATE SET detected = EXCLUDED.detected, networks = EXCLUDED.networks,
                                         fetched_at = EXCLUDED.fetched_at"""

TRADERS_SQL = """SELECT handle, user_id, solana_wallet, solana_evidence, evm_wallet, evm_evidence, best_rank, updated_at
                 FROM de_fomo_traders"""

EVENTS_SQL = """
SELECT event_id, ts, lower(type) AS type, handle, user_id, chain, token_address, received_at,
       CASE WHEN raw->>'fillMatch' = 'onchain-exact' THEN raw->>'execTs' END AS exec_ms
FROM de_fomo_events WHERE received_at > $1 AND lower(type) IN ('buy', 'sell') ORDER BY received_at LIMIT $2"""

WINDOW_SQL = """SELECT handle, user_id, chain, ts FROM de_fomo_events
                WHERE handle = ANY($1::text[]) AND ts >= $2 AND ts <= $3 AND lower(type) IN ('buy', 'sell')"""

TOKEN_COLS = ["chain", "token", "address", "roles", "first_buy_at", "launch_s", "pnl_usd", "traded_at", "holding_pct",
              "held_at", "first_at", "last_at"]
TOKENS_GET_SQL = f"""
SELECT {", ".join(f"t.{c}" for c in TOKEN_COLS)} FROM de_wallet_tokens t
JOIN unnest($1::text[], $2::text[], $3::text[]) k(chain, token, address)
  ON t.chain = k.chain AND t.token = k.token AND t.address = k.address"""
PENDING_SQL = f"""SELECT {", ".join(TOKEN_COLS)} FROM de_wallet_tokens
                  WHERE first_buy_at > $1 AND launch_s IS NULL LIMIT 20000"""
CREATED_SQL = """
SELECT k.chain, k.token, ci.jupiter->'firstPool'->>'createdAt' AS jup_pool, ci.jupiter->>'createdAt' AS jup_created,
       ci.birdeye_creation->>'blockUnixTime' AS be_block, s.created_at AS pool_created
FROM unnest($1::text[], $2::text[]) k(chain, token)
LEFT JOIN de_chain_intel ci ON ci.chain = k.chain AND ci.address = k.token
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.token"""

WALLETS_GET_SQL = """SELECT chain, address, first_seen, sources, evidence, fomo_handle, max_trades_1h FROM de_wallets
                     WHERE (chain, address) IN (SELECT * FROM unnest($1::text[], $2::text[]))"""
STATS_SQL = """
SELECT t.chain, t.address,
       count(*) FILTER (WHERE 'trader' = ANY(t.roles) AND t.pnl_usd > 0 AND NOT ('bot' = ANY(t.roles))) AS profitable,
       count(*) FILTER (WHERE 'sniper' = ANY(t.roles) OR t.launch_s BETWEEN $3 AND $4) AS snipes,
       min(t.launch_s) FILTER (WHERE t.launch_s BETWEEN $3 AND $4) AS fastest_s,
       count(*) FILTER (WHERE 'deployer' = ANY(t.roles)) AS deployed,
       count(*) FILTER (WHERE 'bot' = ANY(t.roles)) AS bot_tokens
FROM de_wallet_tokens t JOIN unnest($1::text[], $2::text[]) k(chain, address) ON t.chain = k.chain AND t.address = k.address
GROUP BY t.chain, t.address"""
WALLET_COLS = ["chain", "address", "first_seen", "labels", "sources", "evidence", "fomo_handle", "max_trades_1h",
               "updated_at"]

BUYERS_SQL = """
SELECT chain, token, address, first_buy_at FROM (
  SELECT t.chain, t.token, t.address, t.first_buy_at,
         row_number() OVER (PARTITION BY t.chain, t.token ORDER BY t.first_buy_at, t.address) AS rn
  FROM de_wallet_tokens t JOIN unnest($1::text[], $2::text[]) k(chain, token) ON t.chain = k.chain AND t.token = k.token
  WHERE t.first_buy_at IS NOT NULL) x
WHERE rn <= $3"""
EDGE_UPSERT_SQL = """
INSERT INTO de_wallet_edges AS e (chain, a, b, kind, tokens, n, first_at, last_at)
VALUES ($1, $2, $3, $4, $5::text[], cardinality($5::text[]), $6, $7)
ON CONFLICT (chain, a, b, kind) DO UPDATE SET
  tokens = ARRAY(SELECT DISTINCT x FROM unnest(e.tokens || EXCLUDED.tokens) x ORDER BY x),
  n = (SELECT count(DISTINCT x) FROM unnest(e.tokens || EXCLUDED.tokens) x),
  first_at = least(e.first_at, EXCLUDED.first_at), last_at = greatest(e.last_at, EXCLUDED.last_at)"""
EDGE_PRUNE_SQL = "DELETE FROM de_wallet_edges WHERE n < $1 AND last_at < $2"

EXCLUDED_SQL = "SELECT chain, address FROM de_wallets WHERE labels && $1::text[]"
STRONG_SQL = "SELECT chain, a, b FROM de_wallet_edges WHERE kind = 'cotrade' AND n >= $1"
FUNDED_SQL = """SELECT chain, address, first_funder, funded_at, funded_sol FROM de_wallets
WHERE funder_status = 'found' AND first_funder IS NOT NULL"""
FUNDER_LABEL_SQL = """
INSERT INTO de_wallets AS w (chain, address, first_seen, labels, sources, evidence, updated_at)
VALUES ($1, $2, $3, $4::text[], ARRAY['funder'], $5::jsonb, $3)
ON CONFLICT (chain, address) DO UPDATE SET
  labels = ARRAY(SELECT DISTINCT x FROM unnest(w.labels || EXCLUDED.labels) x ORDER BY x),
  sources = ARRAY(SELECT DISTINCT x FROM unnest(w.sources || EXCLUDED.sources) x ORDER BY x),
  evidence = w.evidence || EXCLUDED.evidence, updated_at = EXCLUDED.updated_at"""
MEMBER_TOKENS_SQL = """SELECT t.chain, t.address, t.token, t.first_at, t.last_at FROM de_wallet_tokens t
                       JOIN unnest($1::text[], $2::text[]) k(chain, address) ON t.chain = k.chain AND t.address = k.address"""
MEMBER_LABELS_SQL = """SELECT chain, address, labels FROM de_wallets
                       WHERE (chain, address) IN (SELECT * FROM unnest($1::text[], $2::text[]))"""
CLUSTER_COLS = ["cluster_id", "chain", "kind", "funder", "size", "members", "labels", "tokens", "n_tokens", "first_seen",
                "last_active", "updated_at"]
CLUSTER_INSERT_SQL = f"""INSERT INTO de_wallet_clusters ({", ".join(CLUSTER_COLS)})
                         VALUES ($1, $2, $3, $4, $5, $6::text[], $7::jsonb, $8::text[], $9, $10, $11, $12)"""
SET_CLUSTER_SQL = """
UPDATE de_wallets w SET cluster_id = k.co, funding_cluster_id = k.fu
FROM unnest($1::text[], $2::text[], $3::text[], $4::text[]) k(chain, address, co, fu)
WHERE w.chain = k.chain AND w.address = k.address"""


async def _table(name: str) -> bool:
    pool = await db.pool()
    return bool(await pool.fetchval("SELECT to_regclass($1) IS NOT NULL", name))


def _keys(xs, n: int) -> list[list]:
    """Parallel arrays for unnest() from tuples of n fields."""
    xs = list(xs)
    return [[x[i] for x in xs] for i in range(n)]


async def _from_chain_intel(b: Batch, since: datetime | None) -> datetime | None:
    """Readings from the chain-intel rows fetched since the cursor. -> the newest fetch time read."""
    pool = await db.pool()
    newest = None
    for r in await pool.fetch(INTEL_SQL, (since or EPOCH) - OVERLAP, INTEL_PER_RUN):
        d = dict(r) | {k: _load(r[k]) for k in ("rugcheck", "traders", "birdeye_holders", "jupiter")}
        got, _ = from_intel(d)
        for key, row in got:
            b.add(key, row)
        newest = _hi(newest, d["at"])
    return newest


async def _from_deployers(b: Batch) -> None:
    """Each tracked token's dev, once: never a launchpad's signer or a mass-launch address (not people)."""
    pool = await db.pool()
    for r in await pool.fetch(DEPLOYERS_SQL, dossier.SHARED_DEPLOYER, list(dossier.PLATFORM_DEVS), INTEL_PER_RUN):
        b.add((r["chain"], r["token"], r["dev_address"]), seen({"deployer"}, r["updated_at"]))


async def _from_graphs(b: Batch, now: datetime) -> int:
    """RugCheck insider graphs that are due, hot tokens and never-fetched ones first, inside the run's and the day's
    allowance. A failed fetch stores nothing, so the token is still due next run. -> graphs stored."""
    pool = await db.pool()
    rows = [dict(r) for r in await pool.fetch(GRAPH_DUE_SQL)]
    hot = {t["address"] for t in await watch.tokens("hot") if t["chain"] == "solana"}
    due = [r for r in rows if graph_due(r, hot, now)]
    due.sort(key=lambda r: (r["token"] not in hot, r["fetched_at"] is not None, -(r["rugcheck_at"] or EPOCH).timestamp()))
    room = min(GRAPHS_PER_RUN, GRAPHS_PER_DAY - await used_today("rugcheck"))
    n = 0
    for r in due[:max(0, room)]:
        try:
            networks = await fetch_graph(r["token"])
        except Throttled:
            log.warning("deep wallets: RugCheck said slow down; insider graphs wait for the next run")
            break
        if networks is None:
            continue
        at = datetime.now(UTC)
        await pool.execute(GRAPH_UPSERT_SQL, "solana", r["token"], r["detected"], _json(networks), at)
        for w in insiders(networks):
            b.add(("solana", r["token"], w), seen({"insider"}, at))
        n += 1
    return n


async def _traders() -> tuple[dict, dict, dict]:
    if not await _table("de_fomo_traders"):            # the FOMO lane's migration may not be applied yet
        return {}, {}, {}
    pool = await db.pool()
    return trader_index(await pool.fetch(TRADERS_SQL))


async def _from_fomo(b: Batch, idx: tuple, since: datetime | None) -> datetime | None:
    """Readings from the FOMO events received since the cursor, and each trading wallet's busiest hour (its trades
    from an hour before the batch on). -> the newest received_at read."""
    if not idx[1] or not await _table("de_fomo_events"):        # no traders (or no FOMO tables yet): nothing to map
        return None
    pool = await db.pool()
    events = [dict(r) for r in await pool.fetch(EVENTS_SQL, (since or EPOCH) - OVERLAP, EVENTS_PER_RUN)]
    if not events:
        return None
    by_uid, by_handle, _ = idx
    got, leads = from_fomo(events, by_uid, by_handle)
    for key, row in got:
        b.add(key, row)
        if row["first_buy_at"]:
            b.bought.add(key[:2])
    b.leads |= leads
    times = defaultdict(list)
    for e in await pool.fetch(WINDOW_SQL, sorted({e["handle"] for e in events if e["handle"]}),
                              min(e["ts"] for e in events) - timedelta(hours=1), max(e["ts"] for e in events)):
        t = trader_for(dict(e), by_uid, by_handle)
        wallet, proven = wallet_of(t, e["chain"]) if t else (None, False)
        if wallet and proven:
            times[(e["chain"], wallet)].append(e["ts"])
    b.trades_1h |= {k: busiest_hour(ts) for k, ts in times.items()}
    return max(e["received_at"] for e in events)


def _reading(r) -> dict:
    return {**{c: r[c] for c in TOKEN_COLS[4:]}, "roles": set(r["roles"] or [])}


async def _store_tokens(b: Batch, now: datetime) -> int:
    """Merge this run's readings into de_wallet_tokens, timing every first buy whose token's creation is known (and
    the recent ones still waiting for it)."""
    pool = await db.pool()
    old = {(r["chain"], r["token"], r["address"]): _reading(r)
           for r in await pool.fetch(TOKENS_GET_SQL, *_keys(b.obs, 3))} if b.obs else {}
    rows = {k: merge(old.get(k), v) for k, v in b.obs.items()}
    for r in await pool.fetch(PENDING_SQL, now - PENDING):
        rows.setdefault((r["chain"], r["token"], r["address"]), _reading(r))
    tokens = {k[:2] for k, v in rows.items() if v["first_buy_at"]}
    created = {(r["chain"], r["token"]): creation(dict(r)) for r in await pool.fetch(CREATED_SQL, *_keys(tokens, 2))} \
        if tokens else {}
    out = []
    for k, v in rows.items():
        v = timed(v, created.get(k[:2]))
        if k in b.obs or v["launch_s"] is not None:
            out.append((*k, sorted(v["roles"]), *(v[c] for c in TOKEN_COLS[4:])))
    return await db.upsert("de_wallet_tokens", TOKEN_COLS, out, ["chain", "token", "address"])


async def _relabel(b: Batch, idx: tuple, now: datetime) -> int:
    """Recompute de_wallets for every wallet this run saw: sources, FOMO identity, busiest hour, labels and evidence."""
    keys = {(c, a) for c, _, a in b.obs} | set(b.leads) | set(b.trades_1h)
    if not keys:
        return 0
    pool = await db.pool()
    arrays = _keys(keys, 2)
    old = {(r["chain"], r["address"]): dict(r) for r in await pool.fetch(WALLETS_GET_SQL, *arrays)}
    stats = {(r["chain"], r["address"]): dict(r)
             for r in await pool.fetch(STATS_SQL, *arrays, float(-SNIPE_SLACK_S), float(SNIPE_S))}
    mass = {(r["chain"], r["dev_address"]): r["launches"] for r in await pool.fetch(MASS_SQL, dossier.SHARED_DEPLOYER)}
    by_wallet = idx[2]
    sources, first = defaultdict(set), {}
    for (c, _, a), row in b.obs.items():
        sources[(c, a)] |= {SOURCE[x] for x in row["roles"] if x in SOURCE}
        first[(c, a)] = _lo(first.get((c, a)), row["first_at"])
    out = []
    for key in keys:
        o, s = old.get(key) or {}, dict(stats.get(key) or {})
        src, handle = set(o.get("sources") or []) | sources[key], o.get("fomo_handle")
        t = by_wallet.get(("solana" if key[0] == "solana" else "evm", key[1]))
        if t:
            proven = wallet_of(t, key[0]) == (key[1], True)
            handle = t["handle"]
            src.add("fomo" if proven else "fomo-claimed")
            if proven:
                s["fomo_rank"] = t.get("best_rank")
        if key in b.leads:
            src.add("fomo-claimed")
            handle = handle or b.leads[key]
        trades = max(o.get("max_trades_1h") or 0, b.trades_1h.get(key) or 0) or None
        s |= {"max_trades_1h": trades, "exchange": chain_intel.CEX_WALLETS.get(key[1]),
              "funded": ((_load(o.get("evidence")) or {}).get("service") or {}).get("funded"),
              "launchpad": dossier.PLATFORM_DEVS.get(key[1]), "launches": mass.get(key)}
        labels, evidence = rules(s)
        out.append((*key, _lo(o.get("first_seen"), first.get(key)) or now, labels, sorted(src), _json(evidence), handle,
                    trades, now))
    return await db.upsert("de_wallets", WALLET_COLS, out, ["chain", "address"])


async def _excluded(labels=NO_CLUSTER) -> set[tuple[str, str]]:
    """Wallets that never link others: exchanges, services, launchpads, mass-launch addresses and AMM authorities, and
    (for co-trading, by default) bots."""
    pool = await db.pool()
    out = {(r["chain"], r["address"]) for r in await pool.fetch(EXCLUDED_SQL, list(labels))}
    out |= {(r["chain"], r["dev_address"]) for r in await pool.fetch(MASS_SQL, dossier.SHARED_DEPLOYER)}
    fixed = set(chain_intel.CEX_WALLETS) | set(dossier.PLATFORM_DEVS) | chain_intel.AMM_AUTHORITIES
    return out | {(c, a) for c in CHAINS for a in fixed}


async def _store_edges(b: Batch, now: datetime) -> int:
    """Co-trading edges for the tokens with new FOMO buys; then drop the pairs that never got strong. (Top-trader pair
    edges were written here until 2026-10-01; nothing read them.)"""
    pool = await db.pool()
    excluded = await _excluded()
    found: dict = {}

    def add(chain, a, b_, kind, token, at):
        e = found.setdefault((chain, a, b_, kind), [set(), at, at])
        e[0].add(token)
        e[1], e[2] = min(e[1], at), max(e[2], at)
    if b.bought:
        per = defaultdict(list)
        for r in await pool.fetch(BUYERS_SQL, *_keys(b.bought, 2), FIRST_BUYERS):
            per[(r["chain"], r["token"])].append((r["address"], r["first_buy_at"]))
        for (c, t), buyers in per.items():
            for (x, y), at in cobuys(buyers, {a for a, _ in buyers if (c, a) in excluded}).items():
                add(c, x, y, "cotrade", t, at)
    rows = [(c, x, y, kind, sorted(tokens), first, last) for (c, x, y, kind), (tokens, first, last) in found.items()]
    if rows:
        await pool.executemany(EDGE_UPSERT_SQL, rows)
    await pool.execute(EDGE_PRUNE_SQL, STRONG, now - EDGE_TTL)
    return len(rows)


async def recluster(now: datetime) -> dict:
    """Rebuild every cluster: co-trading components and funding groups, after labelling the funders that are
    exchanges or services (they never link wallets). A bot may fund or join a funding group: a bot seeding fresh
    wallets is the bundle that group exists to show, and one funder per wallet means no hub can chain groups."""
    pool = await db.pool()
    rows = [dict(r) for r in await pool.fetch(FUNDED_SQL)]
    funded = [(r["chain"], r["address"], r["first_funder"]) for r in rows]
    blocked = services(funded)
    apps = sponsors([(r["chain"], r["address"], r["first_funder"], r["funded_at"], r["funded_sol"]) for r in rows])
    fan = Counter((c, f) for c, _, f in funded)
    for (c, f), n in blocked.items():
        cex = chain_intel.CEX_WALLETS.get(f)
        ev = {"exchange": {"name": cex, "funded": n}} if cex else {"service": {"funded": n}}
        await pool.execute(FUNDER_LABEL_SQL, c, f, now, ["exchange" if cex else "service"], _json(ev))
    for (c, f), why in apps.items():
        if (c, f) not in blocked:
            await pool.execute(FUNDER_LABEL_SQL, c, f, now, ["service"], _json({"service": {"funded": fan[(c, f)],
                                                                                           "pattern": why}}))
    blocked = {**blocked, **{k: fan[k] for k in apps}}
    strong = [(r["chain"], r["a"], r["b"]) for r in await pool.fetch(STRONG_SQL, STRONG)]
    co = [(f"cotrade:{c}:{min(m)}", c, "cotrade", None, m) for c, m in cotrade_clusters(strong, await _excluded())]
    not_people = await _excluded(NOT_PEOPLE)
    fu = [(f"funding:{c}:{f}", c, "funding", f, m)
          for c, f, m in funding_clusters(funded, not_people | set(blocked), not_people)]
    members = {(c, a) for _, c, _, _, m in co + fu for a in m}
    tokens, labels = defaultdict(list), {}
    if members:
        arrays = _keys(members, 2)
        for r in await pool.fetch(MEMBER_TOKENS_SQL, *arrays):
            tokens[(r["chain"], r["address"])].append((r["token"], r["first_at"], r["last_at"]))
        labels = {(r["chain"], r["address"]): list(r["labels"]) for r in await pool.fetch(MEMBER_LABELS_SQL, *arrays)}
    rows = [cluster_row(cid, c, kind, f, m, {a: tokens[(c, a)] for a in m}, {a: labels.get((c, a), []) for a in m}, now)
            for cid, c, kind, f, m in co + fu]
    want: dict = defaultdict(lambda: [None, None])
    for i, group in enumerate((co, fu)):
        for cid, c, _, _, m in group:
            for a in m:
                want[(c, a)][i] = cid
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("DELETE FROM de_wallet_clusters")
        if rows:
            await conn.executemany(CLUSTER_INSERT_SQL, rows)
        now_in = {(r["chain"], r["address"]): (r["cluster_id"], r["funding_cluster_id"]) for r in await conn.fetch(
            "SELECT chain, address, cluster_id, funding_cluster_id FROM de_wallets "
            "WHERE cluster_id IS NOT NULL OR funding_cluster_id IS NOT NULL")}
        moved = []                                  # only the wallets whose clusters changed
        for k in now_in.keys() | want.keys():
            ids = tuple(want[k]) if k in want else (None, None)
            if ids != now_in.get(k, (None, None)):
                moved.append((*k, *ids))
        if moved:
            await conn.execute(SET_CLUSTER_SQL, *_keys(moved, 4))
    return {"cotrade_clusters": len(co), "funding_clusters": len(fu), "not_linking_funders": len(blocked)}


async def run() -> dict:
    """deep_wallets: fold in what the sources learned since the last run, relabel the wallets it touched, add the new
    edges and rebuild the clusters."""
    now = datetime.now(UTC)
    pool = await db.pool()
    cursor = {r["source"]: r["at"] for r in await pool.fetch("SELECT source, at FROM de_wallet_cursor")}
    b = Batch()
    intel_at = await _from_chain_intel(b, cursor.get("chain_intel"))
    await _from_deployers(b)
    graphs = await _from_graphs(b, now)
    idx = await _traders()
    fomo_at = await _from_fomo(b, idx, cursor.get("fomo_events"))
    tokens = await _store_tokens(b, now)
    wallets = await _relabel(b, idx, now)
    edges = await _store_edges(b, now)
    clusters = await recluster(now)
    for source, at in (("chain_intel", intel_at), ("fomo_events", fomo_at)):
        if at:
            await pool.execute("""INSERT INTO de_wallet_cursor (source, at) VALUES ($1, $2) ON CONFLICT (source)
                                  DO UPDATE SET at = greatest(de_wallet_cursor.at, EXCLUDED.at)""", source, at)
    out = {"rows": tokens + wallets, "readings": len(b.obs), "wallet_tokens": tokens, "wallets": wallets,
           "edges": edges, "graphs": graphs, **clusters, "pruned": await prune(now)}
    log.info("deep wallets: %s", out)
    return out


# A wallet's reading on a token is kept RETAIN after it was last seen there; a wallet with nothing left (no token,
# no label, no funder: a funder costs Chainstack calls to learn) goes too. The first evening wrote ~57k wallet-token
# rows (28 MB) in 80 minutes, most of it the first fold of every token's traders and holders.
RETAIN = timedelta(days=30)
PRUNE_BATCH = 20_000


async def prune(now: datetime) -> dict:
    """Drop stale wallet-token readings and the wallets they leave with nothing, a batch at a time."""
    pool = await db.pool()
    cut = now - RETAIN
    readings = await pool.execute(
        """DELETE FROM de_wallet_tokens WHERE ctid IN (
             SELECT ctid FROM de_wallet_tokens WHERE last_at < $1 LIMIT $2)""", cut, PRUNE_BATCH)
    wallets = await pool.execute(
        """DELETE FROM de_wallets w WHERE w.ctid IN (
             SELECT w2.ctid FROM de_wallets w2
             WHERE w2.updated_at < $1 AND cardinality(w2.labels) = 0 AND w2.first_funder IS NULL
               AND NOT EXISTS (SELECT 1 FROM de_wallet_tokens t WHERE t.chain = w2.chain AND t.address = w2.address)
             LIMIT $2)""", cut, PRUNE_BATCH)
    return {"wallet_tokens": _deleted(readings), "wallets": _deleted(wallets)}


def _deleted(status) -> int:
    """asyncpg's command tag ("DELETE 12") -> 12."""
    tail = status.split()[-1] if isinstance(status, str) and status.split() else ""
    return int(tail) if tail.isdigit() else 0


# ─── deep_funders ──────────────────────────────────────────────────────────────────────────────────────────────
CANDIDATES_SQL = """
SELECT w.address FROM de_wallets w
LEFT JOIN LATERAL (SELECT max(t.held_at) AS held_at, max(t.last_at) AS last_at, bool_or('insider' = ANY(t.roles)) AS insider
                   FROM de_wallet_tokens t WHERE t.chain = w.chain AND t.address = w.address) t ON TRUE
WHERE w.chain = 'solana' AND NOT (w.labels && $1::text[])
  AND (w.funder_checked_at IS NULL OR (w.funder_status = 'failed' AND w.funder_checked_at < now() - $2::interval))
ORDER BY (t.held_at > now() - interval '1 hour') DESC NULLS LAST,
         (w.labels && ARRAY['sniper', 'deployer'] OR w.cluster_id IS NOT NULL) DESC,
         ('top_trader' = ANY(w.labels)) DESC, t.insider DESC NULLS LAST, t.last_at DESC NULLS LAST
LIMIT $3"""
FUNDER_SQL = """
UPDATE de_wallets SET funder_status = $2, signatures = coalesce($3, signatures), first_funder = $4, funded_at = $5,
                      funded_sol = $6, funding_tx = $7, funder_checked_at = $8
WHERE chain = 'solana' AND address = $1"""


async def funders() -> int:
    """deep_funders: first funders for the wallets that matter most, inside the day's paced RPC allowance. -> wallets
    checked."""
    if not rpc_url():
        return 0
    now = datetime.now(UTC)
    budget = min(PER_RUN_CALLS, chain_intel.allowance(await daily_calls(), await used_today(RPC_TALLY), now))
    if budget < 1 + FUNDER_TX_TRIES:
        return 0
    pool = await db.pool()
    calls, n, failures = 0, 0, 0

    async def counted(method, params):
        nonlocal calls
        calls += 1
        return await rpc(method, params)
    for r in await pool.fetch(CANDIDATES_SQL, list(NOT_PEOPLE), RETRY_FAILED, WALLETS_PER_RUN):
        if budget - calls < 1 + FUNDER_TX_TRIES:
            break
        try:
            res = await first_funder(r["address"], counted)
            failures = 0
        except (BudgetExceeded, Throttled) as e:
            log.info("deep funders: %s; the rest wait for the next run", e)
            break
        except RpcFailed as e:
            log.warning("deep funders %s: %s", r["address"], e)
            res, failures = {"status": "failed"}, failures + 1
        lamports = res.get("lamports")
        await pool.execute(FUNDER_SQL, r["address"], res["status"], res.get("signatures"), res.get("funder"), res.get("at"),
                           lamports / 1e9 if lamports is not None else None, res.get("tx"), datetime.now(UTC))
        n += 1
        if failures >= MAX_FAILURES:
            log.warning("deep funders: %d failures in a row; stopping this run", failures)
            break
    log.info("deep funders: %d wallets checked with %d RPC calls", n, calls)
    return n


# ─── the dossier section ───────────────────────────────────────────────────────────────────────────────────────
SECTION_SQL = """
SELECT t.address, t.roles, t.first_buy_at, t.launch_s, t.holding_pct, t.held_at, t.traded_at, t.pnl_usd, t.last_at,
       w.labels, w.first_funder, w.funded_at, w.funder_status, w.cluster_id, w.funding_cluster_id, w.fomo_handle
FROM de_wallet_tokens t LEFT JOIN de_wallets w ON w.chain = t.chain AND w.address = t.address
WHERE t.chain = $1 AND t.token = $2"""
CLUSTERS_SQL = "SELECT cluster_id, kind, size, funder, labels FROM de_wallet_clusters WHERE cluster_id = ANY($1::text[])"
DEV_SQL = "SELECT dev_address, dev_launches FROM de_chain_intel WHERE chain = $1 AND address = $2"
WHO_SQL = "SELECT address, labels, first_funder FROM de_wallets WHERE chain = $1 AND address = ANY($2::text[])"


def person(dev: str | None, launches) -> str | None:
    """The deployer as a person: not a launchpad's signer, not a mass-launch address (dossier's dev rule)."""
    if not dev or dev in dossier.PLATFORM_DEVS or (launches or 0) >= dossier.SHARED_DEPLOYER:
        return None
    return dev


async def section(chain: str, address: str) -> dict | None:
    """The dossier's "wallets" section (shape() says what is in it), or None when no wallet around the token is known."""
    address = norm(address)
    pool = await db.pool()
    try:
        rows = [dict(r) for r in await pool.fetch(SECTION_SQL, chain, address)]
    except asyncpg.UndefinedTableError:              # before migration 032
        return None
    if not rows:
        return None
    ids = sorted({r[k] for r in rows for k in ("cluster_id", "funding_cluster_id") if r[k]})
    clusters = {r["cluster_id"]: dict(r) | {"labels": _load(r["labels"])}
                for r in await pool.fetch(CLUSTERS_SQL, ids)} if ids else {}
    d = await pool.fetchrow(DEV_SQL, chain, address)
    dev = person(d["dev_address"], d["dev_launches"]) if d else None
    funders = {r["first_funder"] for r in rows if r["first_funder"]}
    who = {r["address"]: dict(r) for r in await pool.fetch(WHO_SQL, chain, sorted(funders | {dev} - {None}))}
    not_linking = {a for a, r in who.items() if set(r["labels"] or []) & set(NOT_PEOPLE)} | \
        (funders & set(chain_intel.CEX_WALLETS))
    return shape(rows, clusters, dev, (who.get(dev) or {}).get("first_funder") if dev else None, not_linking)


# ─── registry (albatross/deepend/registry.py) ──────────────────────────────────────────────────────────────────
JOBS = {"deep_wallets": run, "deep_funders": funders}
SCHEDULE = [("deep_wallets", {"minute": "*/15", "second": 40}), ("deep_funders", {"minute": "3-59/10", "second": 20})]
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE = {"deep_wallets": 900, "deep_funders": 600}
