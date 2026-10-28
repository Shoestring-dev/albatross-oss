"""Discovery (sensor S2) and news (S5) for The Deep End (owner F; docs/SPEC-DEEP-END.md section 2).

Jobs
  deep_discovery    every 5 min  DexScreener latest profiles, latest boosts and top boosts (free, through the
                                 micro pipeline's DexScreener limiter); Birdeye trending (top 20) every 20 min and
                                 new listings every 2 h on each chain in DEEP_DISCOVERY_BIRDEYE_CHAINS -> de_watch
  deep_cryptopanic  every 5 min  CryptoPanic API posts -> news_items (only with CRYPTOPANIC_API_KEY; else 0)

Birdeye budget (our share is at most 5,000 compute units a day): trending 50 CU x 72 runs + new listings
80 CU x 12 runs = 4,560 CU a day for the default chain (solana); each extra chain adds as much. The Birdeye calls
fire on the 5-minute runs that open each 20-minute / 2-hour window, so a restart never repeats them.

The radar keeps a token only when it can be traded: liquidity >= DEEP_WATCH_MIN_LIQ (default $10K) or, on a bonding
curve whose liquidity nobody reports, a market cap of at least twice that. Tokens we already track (called in our
groups) are always kept: discovery then only records where else they showed up. Every kept token is warm for 24 h
(albatross/deepend/watch.py), which is what the other collectors refresh, so the floor also guards their budgets.

DexScreener's lists carry no symbol, name or price, so tokens we don't track get one batched /tokens/v1 read
(30 per call) for symbol, name, price, liquidity, market cap and 1 h change; Birdeye's lists carry their own.

Adding discovered tokens to the micro pipeline (micro_tokens, priced every 10 s to 1 h by tier) is off by default:
DEEP_DISCOVERY_TRACK=1 adds at most DEEP_DISCOVERY_TRACK_PER_RUN (5) a run with liquidity >= DEEP_DISCOVERY_TRACK_MIN_LIQ
($50K), first_seen_via 'discovery'.

CryptoPanic: Cloudflare blocks some datacenter addresses on cryptopanic.com and the RSS feed is gone
(410), so API only: https://cryptopanic.com/api/{CRYPTOPANIC_PLAN, default growth}/v2/posts/?public=true&kind=news.
Posts land in news_items with classification NULL, so the News Scout classifies them as it does every headline;
their CryptoPanic votes and panic score are merged into classification under "cryptopanic" once it has (the Scout
only reads rows whose classification is NULL, and writes the whole column). The request URL carries the key: it is
never logged.
"""

import html
import logging
import re
from datetime import UTC, datetime, timedelta

import httpx
import orjson

from albatross import db
from albatross.config import settings
from albatross.deepend import birdeye
from albatross.deepend.cfg import env_float, env_int, env_str
from albatross.http import client
from albatross.micro.rules import norm
from albatross.venues import dexscreener

log = logging.getLogger(__name__)

CHAINS = {"solana", "robinhood", "base", "bsc", "ethereum"}   # the chains the micro pipeline prices
DS_LISTS = {"dexscreener profile": "/token-profiles/latest/v1",
            "dexscreener boost": "/token-boosts/latest/v1",
            "dexscreener top boost": "/token-boosts/top/v1"}
TRENDING, NEW_LISTING = "birdeye trending", "birdeye new listing"
SOURCES = [TRENDING, NEW_LISTING, *DS_LISTS]           # display order of discovered_via
TRENDING_EVERY_MIN = 20
NEW_LISTING_EVERY_MIN = 120
RUN_EVERY_MIN = 5
PAIR_MAX_AGE = timedelta(minutes=10)                   # re-read DexScreener pair stats after this
CP_URL = "https://cryptopanic.com/api/{plan}/v2/posts/"
NEWS_DAYS = 14


def min_liq() -> float:
    return env_float("DEEP_WATCH_MIN_LIQ", 10_000.0)


def birdeye_chains() -> list[str]:
    return [c.strip() for c in (env_str("DEEP_DISCOVERY_BIRDEYE_CHAINS", "solana") or "").split(",")
            if c.strip() in birdeye.CHAINS]


def due(now: datetime, every_min: int) -> bool:
    """True on the one 5-minute run that opens each `every_min` window of the UTC day."""
    return (now.hour * 60 + now.minute) % every_min < RUN_EVERY_MIN


def _f(v) -> float | None:
    try:
        return float(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None


def _iso(t: datetime | None) -> str | None:
    return t.isoformat() if t else None


def _ts(v) -> datetime | None:
    """ISO text (naive = UTC), unix seconds or milliseconds -> aware datetime."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, UTC)
    try:
        t = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ─── parsing (pure) ──────────────────────────────────────────────────────────
def ds_icon(icon: str | None) -> str | None:
    """DexScreener gives a full URL on profiles and a bare image id on boosts."""
    if not icon:
        return None
    return icon if icon.startswith("http") else (
        f"https://cdn.dexscreener.com/cms/images/{icon}?width=64&height=64&fit=crop&quality=95&format=auto")


def ds_links(links: list[dict] | None) -> dict:
    out = {"x": None, "telegram": None, "website": None}
    for lk in links or []:
        url, kind = lk.get("url"), (lk.get("type") or "").lower()
        if not url:
            continue
        if kind == "twitter" or re.match(r"https?://(www\.)?(x|twitter)\.com/", url):
            out["x"] = out["x"] or url
        elif kind == "telegram" or re.match(r"https?://(www\.)?t\.me/", url):
            out["telegram"] = out["telegram"] or url
        elif not kind and (lk.get("label") or "website").lower() in ("website", "web", "site", "app"):
            out["website"] = out["website"] or url
    return out


def _find(chain: str, address: str, source: str, **kw) -> dict:
    return {"chain": chain, "address": norm(address), "source": source, "rank": kw.pop("rank", None),
            "trending": kw.pop("trending", False), "info": kw.pop("info", {}), "seen": kw.pop("seen", {}),
            "pair": kw.pop("pair", None)}


def parse_dexscreener(source: str, data) -> list[dict]:
    """A DexScreener profiles/boosts list -> finds on the chains we price."""
    out = []
    for x in data if isinstance(data, list) else []:
        chain, addr = x.get("chainId"), x.get("tokenAddress")
        if chain not in CHAINS or not addr:
            continue
        info = {"logo": ds_icon(x.get("icon")), "description": (x.get("description") or "").strip() or None,
                "links": ds_links(x.get("links")), "dexscreener": x.get("url")}
        seen = {k: x[k] for k in ("amount", "totalAmount") if x.get(k) is not None}
        out.append(_find(chain, addr, source, info=info, seen=seen))
    return out


def parse_trending(chain: str, data) -> list[dict]:
    """Birdeye /defi/token_trending data -> finds (rank = Birdeye's rank)."""
    out = []
    for t in (data or {}).get("tokens") or [] if isinstance(data, dict) else []:
        if not t.get("address"):
            continue
        pair = {"price": _f(t.get("price")), "liquidity": _f(t.get("liquidity")), "mcap": _f(t.get("marketcap")),
                "fdv": _f(t.get("fdv")), "volume_h24": _f(t.get("volume24hUSD")),
                "change_h24": _f(t.get("price24hChangePercent")), "src": "birdeye"}
        out.append(_find(chain, t["address"], TRENDING, rank=t.get("rank"), trending=True, pair=pair,
                         info={"symbol": t.get("symbol"), "name": t.get("name"), "logo": t.get("logoURI")},
                         seen={"rank": t.get("rank")}))
    return out


def parse_new_listing(chain: str, data) -> list[dict]:
    """Birdeye /defi/v2/tokens/new_listing data -> finds (liquidity and when it was added)."""
    out = []
    for t in (data or {}).get("items") or [] if isinstance(data, dict) else []:
        if not t.get("address"):
            continue
        added = _ts(t.get("liquidityAddedAt"))
        pair = {"liquidity": _f(t.get("liquidity")), "created_at": _iso(added), "src": "birdeye"}
        out.append(_find(chain, t["address"], NEW_LISTING, pair=pair,
                         info={"symbol": t.get("symbol"), "name": t.get("name"), "logo": t.get("logoURI"),
                               "launchpad": t.get("source")},
                         seen={"liquidity_added_at": _iso(added)}))
    return out


def pair_stats(p: dict) -> dict:
    """A DexScreener pair (the token as its base) -> the stats a feed row shows."""
    created = p.get("pairCreatedAt")
    info = p.get("info") or {}
    socials = [{"type": s.get("type"), "url": s.get("url")} for s in info.get("socials") or []]
    sites = [{"label": w.get("label") or "Website", "url": w.get("url")} for w in info.get("websites") or []]
    return {"symbol": p["baseToken"].get("symbol"), "name": p["baseToken"].get("name"),
            "price": _f(p.get("priceUsd")), "liquidity": _f((p.get("liquidity") or {}).get("usd")),
            "mcap": _f(p.get("marketCap")), "fdv": _f(p.get("fdv")),
            "change_h1": _f((p.get("priceChange") or {}).get("h1")),
            "volume_h24": _f((p.get("volume") or {}).get("h24")),
            "created_at": _iso(_ts(created)) if created else None, "dex": p.get("dexId"),
            "pair": p.get("pairAddress"), "logo": info.get("imageUrl"),
            "links": ds_links(socials + sites) if socials or sites else None, "src": "dexscreener"}


def tradeable(pair: dict | None, floor: float) -> bool:
    """Liquidity at or above the floor; a bonding curve (liquidity unreported) needs twice the floor in market cap."""
    if not pair:
        return False
    if pair.get("liquidity") is not None:
        return pair["liquidity"] >= floor
    return (pair.get("mcap") or 0) >= 2 * floor


PAIR_READ = "dexscreener pair"          # our own /tokens/v1 read: feeds meta, never `sources`


def merge(cur: dict | None, finds: list[dict], now: datetime) -> dict:
    """Fold one token's finds of this run into its de_watch row (or a new one). meta keeps the latest reading of
    each list (when it was first and last seen there), the latest symbol/name/logo/description, the first links
    found, and the latest pair stats (price, liquidity, market cap...)."""
    row = {"first_seen": now, "trending_at": None, "rank": None, "sources": [], "meta": {}} if cur is None else {
        "first_seen": cur["first_seen"], "trending_at": cur["trending_at"], "rank": cur["rank"],
        "sources": list(cur["sources"] or []), "meta": dict(cur["meta"] or {})}
    meta = row["meta"]
    seen = dict(meta.get("seen") or {})
    # Our own pair read first, so a list's reading of the same field (Birdeye's all-pool liquidity) wins.
    for f in sorted(finds, key=lambda f: f["source"] != PAIR_READ):
        prev = seen.get(f["source"]) or {}
        seen[f["source"]] = {**f["seen"], "first": prev.get("first") or now.isoformat(), "last": now.isoformat()}
        if f["source"] != PAIR_READ and f["source"] not in row["sources"]:
            row["sources"].append(f["source"])
        if f["trending"]:
            row["trending_at"], row["rank"] = now, f["rank"]
        for k, v in f["info"].items():
            if k == "links":
                old = meta.get("links") or {}
                meta["links"] = {n: old.get(n) or (v or {}).get(n) for n in ("x", "telegram", "website")}
            elif v:
                meta[k] = v
        if f["pair"]:
            meta["pair"] = {**(meta.get("pair") or {}), **{k: v for k, v in f["pair"].items() if v is not None},
                            "at": now.isoformat()}
    meta["seen"] = seen
    row["sources"] = sorted(row["sources"], key=lambda s: SOURCES.index(s) if s in SOURCES else len(SOURCES))
    row["last_seen"] = now
    return row


def pair_of(finds: list[dict]) -> dict | None:
    """What this run's finds say about the token's market (for the floor)."""
    out: dict = {}
    for f in finds:
        out.update({k: v for k, v in (f["pair"] or {}).items() if v is not None})
    return out or None


def _meta(v) -> dict:
    return orjson.loads(v) if isinstance(v, (str, bytes)) else dict(v or {})


# ─── the job ─────────────────────────────────────────────────────────────────
async def _birdeye(path: str, params: dict, chain: str) -> dict | None:
    try:
        return await birdeye.get(path, params, chain)
    except birdeye.BudgetExceeded as e:
        log.warning("deep discovery: %s", e)
        return None


async def _ds_list(path: str):
    try:
        data, _ = await dexscreener._get(path)
        return data
    except (httpx.HTTPError, ValueError) as e:
        log.warning("deep discovery: DexScreener %s: %s", path, type(e).__name__)
        return None


async def _pairs(chain: str, addresses: list[str]) -> dict[str, dict]:
    out = {}
    for i in range(0, len(addresses), dexscreener.MAX_ADDRESSES):
        batch = addresses[i:i + dexscreener.MAX_ADDRESSES]
        try:
            pairs, _ = await dexscreener.tokens(chain, batch)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("deep discovery: DexScreener tokens %s: %s", chain, type(e).__name__)
            continue
        for p in pairs:
            a = norm((p.get("baseToken") or {}).get("address") or "")
            if a in batch and a not in out:
                out[a] = pair_stats(p)
    return out


async def run(force: bool = False) -> int:
    """One discovery pass -> de_watch rows written. force=True also runs the Birdeye lists off their window."""
    now = datetime.now(UTC)
    finds: list[dict] = []
    for source, path in DS_LISTS.items():
        finds += parse_dexscreener(source, await _ds_list(path))
    for chain in birdeye_chains():
        if force or due(now, TRENDING_EVERY_MIN):
            finds += parse_trending(chain, await _birdeye(
                "/defi/token_trending", {"sort_by": "rank", "sort_type": "asc", "offset": 0, "limit": 20}, chain))
        if force or due(now, NEW_LISTING_EVERY_MIN):
            finds += parse_new_listing(chain, await _birdeye(
                "/defi/v2/tokens/new_listing", {"limit": 20, "meme_platform_enabled": "true"}, chain))
    if not finds:
        return 0
    by_key: dict[tuple[str, str], list[dict]] = {}
    for f in finds:
        by_key.setdefault((f["chain"], f["address"]), []).append(f)
    pool = await db.pool()
    chains, addrs = [k[0] for k in by_key], [k[1] for k in by_key]
    existing = {(r["chain"], r["address"]): {**dict(r), "meta": _meta(r["meta"])} for r in await pool.fetch(
        """SELECT w.* FROM de_watch w JOIN unnest($1::text[], $2::text[]) k(chain, address)
           ON w.chain = k.chain AND w.address = k.address""", chains, addrs)}
    tracked = {(r["chain"], r["address"]) for r in await pool.fetch(
        """SELECT coalesce(resolved_chain, chain) AS chain, address FROM micro_tokens
           WHERE status <> 'retired' AND address = ANY($1::text[])""", addrs)}
    # Symbol, name, price, liquidity, 1 h change and pool age for tokens we don't track, unless read in the last
    # 10 minutes (Birdeye's lists have no pool age or 1 h change, DexScreener's have no market data at all).
    need: dict[str, list[str]] = {}
    for k, fs in by_key.items():
        read_at = _ts(((existing.get(k) or {}).get("meta", {}).get("seen") or {}).get(PAIR_READ, {}).get("last"))
        if k not in tracked and (read_at is None or now - read_at > PAIR_MAX_AGE):
            need.setdefault(k[0], []).append(k[1])
    for chain, addresses in need.items():
        for a, stats in (await _pairs(chain, addresses)).items():
            by_key[(chain, a)].append(_find(chain, a, PAIR_READ, pair=stats, info={
                "symbol": stats["symbol"], "name": stats["name"], "logo": stats["logo"], "links": stats["links"]}))
    floor = min_liq()
    rows = []
    for k, fs in by_key.items():
        cur = existing.get(k)
        if k not in tracked and cur is None and not tradeable(pair_of(fs), floor):
            continue
        row = merge(cur, fs, now)
        rows.append((k[0], k[1], row["first_seen"], row["last_seen"], row["trending_at"], row["rank"],
                     row["sources"], orjson.dumps(row["meta"]).decode()))
    await db.upsert("de_watch", ["chain", "address", "first_seen", "last_seen", "trending_at", "rank", "sources", "meta"],
                    rows, ["chain", "address"], update=["last_seen", "trending_at", "rank", "sources", "meta"])
    await _track(rows, tracked)
    log.info("deep discovery: %d finds, %d tokens kept (%d already tracked)", len(finds), len(rows),
             sum((r[0], r[1]) in tracked for r in rows))
    return len(rows)


async def _track(rows: list[tuple], tracked: set) -> int:
    """DEEP_DISCOVERY_TRACK=1: hand the deepest untracked discoveries to the micro pipeline."""
    if env_str("DEEP_DISCOVERY_TRACK", "0") not in ("1", "true", "yes", "on"):
        return 0
    floor = env_float("DEEP_DISCOVERY_TRACK_MIN_LIQ", 50_000.0)
    picks = []
    for chain, address, *_, meta in rows:
        m = orjson.loads(meta)
        liq = (m.get("pair") or {}).get("liquidity")
        if (chain, address) not in tracked and (liq or 0) >= floor:
            picks.append((liq, chain, address, m.get("symbol")))
    picks = sorted(picks, reverse=True)[:env_int("DEEP_DISCOVERY_TRACK_PER_RUN", 5)]
    if not picks:
        return 0
    pool = await db.pool()
    await pool.executemany(
        """INSERT INTO micro_tokens (chain, address, symbol, first_seen, first_seen_via, resolved_chain)
           VALUES ($1, $2, $3, now(), 'discovery', $1) ON CONFLICT (chain, address) DO NOTHING""",
        [(c, a, s) for _, c, a, s in picks])
    log.info("deep discovery: tracking %d discovered tokens in the micro pipeline", len(picks))
    return len(picks)


# ─── CryptoPanic ─────────────────────────────────────────────────────────────
def parse_posts(posts: list[dict]) -> tuple[list[tuple], list[tuple]]:
    """CryptoPanic v2 posts -> (news_items rows, (url, cryptopanic json) for the votes)."""
    rows, votes = [], []
    for p in posts or []:
        url, title = p.get("original_url") or p.get("url"), (p.get("title") or "").strip()
        published = _ts(p.get("published_at"))
        if not url or not title or published is None:
            continue
        codes = p.get("instruments") or p.get("currencies") or []
        assets = sorted({c["code"].upper() for c in codes if isinstance(c, dict) and c.get("code")})
        summary = (p.get("description") or "").strip()[:2000] or None
        rows.append(("cryptopanic", url, html.unescape(title), summary, published, assets))
        v = p.get("votes") or {}
        votes.append((url, orjson.dumps({
            "id": p.get("id"), "votes": {k: v.get(k) for k in ("positive", "negative", "important", "liked",
                                                               "disliked", "toxic", "saved", "comments") if k in v},
            "panic_score": p.get("panic_score"), "panic_score_1h": p.get("panic_score_1h"),
            "source": (p.get("source") or {}).get("domain") or (p.get("source") or {}).get("title"),
            "kind": p.get("kind")}).decode()))
    return rows, votes


def cryptopanic_key() -> str | None:
    return settings.cryptopanic_api_key or env_str("CRYPTOPANIC_API_KEY")


async def cryptopanic() -> int:
    """Latest CryptoPanic news posts -> news_items. Without a key: 0, quietly."""
    key = cryptopanic_key()
    if not key:
        return 0
    try:
        r = await client().get(CP_URL.format(plan=env_str("CRYPTOPANIC_PLAN", "growth")), timeout=20,
                               params={"auth_token": key, "public": "true", "kind": "news"})
    except httpx.HTTPError as e:
        raise RuntimeError(f"cryptopanic: {type(e).__name__}") from None     # never the URL: it holds the key
    if r.status_code != 200:
        raise RuntimeError(f"cryptopanic: HTTP {r.status_code}")
    try:
        posts = r.json().get("results") or []
    except ValueError:
        raise RuntimeError("cryptopanic: the answer was not JSON") from None
    rows, votes = parse_posts(posts)
    n = await db.upsert("news_items", ["source", "url", "title", "summary", "published_at", "assets"], rows, ["url"],
                        update=[])
    if votes:
        pool = await db.pool()
        await pool.executemany(
            """UPDATE news_items SET classification = classification || jsonb_build_object('cryptopanic', $2::jsonb)
               WHERE url = $1 AND classification IS NOT NULL""", votes)
    return n


# ─── the dossier's news section ──────────────────────────────────────────────
GENERIC_NAMES = {"bitcoin", "ethereum", "solana", "crypto", "token", "coin", "memecoin", "the coin", "base", "binance",
                 "robinhood", "pump", "doge", "pepe", "trump", "elon", "ai", "cat", "dog", "inu", "moon", "news"}


def news_match(title: str, summary: str | None, url: str, chain: str, address: str, symbol: str | None,
               name: str | None) -> str | None:
    """How a headline names the token: 'address' (the contract), 'name' (its full name as a phrase) or 'ticker'
    (the $cashtag); None when it doesn't. A bare ticker is never enough: memecoin tickers collide with listed coins."""
    text = f"{title} {summary or ''}"
    hay = f"{text} {url}"
    if address and (address.lower() in hay.lower() if address.startswith("0x") else address in hay):
        return "address"
    nm = (name or "").strip()
    if len(nm) >= 4 and nm.lower() not in GENERIC_NAMES and re.search(
            rf"(?<![\w$]){re.escape(nm)}(?!\w)", title, re.IGNORECASE):
        return "name"
    sym = (symbol or "").strip()
    if len(sym) >= 3 and re.search(rf"\${re.escape(sym)}(?![A-Za-z0-9])", text, re.IGNORECASE):
        return "ticker"
    return None


async def section_news(chain: str, address: str, symbol: str | None, name: str | None) -> list[dict]:
    """Recent news_items that name this token (by contract, full name or $ticker), newest first."""
    pats = [f"%{address}%"]
    if name and len(name.strip()) >= 4 and name.strip().lower() not in GENERIC_NAMES:
        pats.append(f"%{name.strip()}%")
    if symbol and len(symbol.strip()) >= 3:
        pats.append(f"%${symbol.strip()}%")
    pool = await db.pool()
    rows = await pool.fetch(
        f"""SELECT source, url, title, summary, published_at, classification FROM news_items
            WHERE published_at > now() - interval '{NEWS_DAYS} days'
              AND (title ILIKE ANY($1::text[]) OR coalesce(summary, '') ILIKE ANY($1::text[]) OR url ILIKE $2)
            ORDER BY published_at DESC LIMIT 60""", pats, f"%{address}%")
    out = []
    for r in rows:
        how = news_match(r["title"], r["summary"], r["url"], chain, address, symbol, name)
        if not how:
            continue
        cls = r["classification"]
        cp = (orjson.loads(cls) if isinstance(cls, str) else cls or {}).get("cryptopanic") or {}
        votes = cp.get("votes") or None
        out.append({"title": r["title"], "url": r["url"], "source": cp.get("source") or r["source"],
                    "published_at": r["published_at"],
                    "votes": {k: votes.get(k) for k in ("positive", "negative", "important")} if votes else None,
                    "match": how})
        if len(out) >= 10:
            break
    return out


JOBS: dict = {"deep_discovery": run, "deep_cryptopanic": cryptopanic}
SCHEDULE: list = [("deep_discovery", {"minute": "*/5", "second": 35}),
                  ("deep_cryptopanic", {"minute": "1-59/5", "second": 5})]
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE: dict = {"deep_discovery": 300, "deep_cryptopanic": 300}
