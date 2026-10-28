"""The Reddit scout (the Deep End): what Reddit is saying about a coin right now, read through Google search.

Reddit's own API needs approval and a paid contract, and Claude's web search can't see Reddit at all (2026-10-01 on the
host: WebSearch refuses reddit.com as a domain, site:reddit.com returns no reddit.com result, WebFetch's domain check
says can_fetch false). Google indexes Reddit under licence, so the scout asks Gemini through OpenRouter with the web
search tool on Google's own engine: one call per coin, as fit.py makes its research calls. A Deep End token is searched
by its contract address, ticker and name, a major by its name and its top headline, site:reddit.com, the last 72 hours
first. The model lists the threads it found about the coin and writes the interpretation (mood -5..+5, what people
say, red flags, why it matters); code counts the activity (none / a few threads / active) from the threads that pass.

Evidence gate (CLAUDE.md). Google gives the model each result as a grounding link (vertexaisearch.cloud.google.com/
grounding-api-redirect/...) that only Google can issue, so a link the model copies is proof the search returned it,
and where it leads names the thread: one GET to Google's redirector, never followed (the Location header only; Reddit
itself is never requested), 5 s, at most MAX_RESOLVE links a read, in the same run (the links expire). A thread or
red flag is kept only when its grounding link leads to a reddit.com thread; a reddit.com URL the model typed itself
counts only when a resolved link names the same thread (by post id, so old./np. spellings, comment permalinks and share
tags agree). The URL stored is the canonical thread URL, never the grounding link. Measured 2026-10-01: the
url_citation annotations come and go (none for 5 real threads on one answer, all 3 on another), so they can't be the
evidence; OpenAI's search through OpenRouter found the same threads as plain URLs but annotated one of five. A thread
dated before the token existed is about another coin and is left out. Claims that fail are dropped and counted
(citations_dropped; the agent_runs row names them).

"No Reddit discussion found" is a claim too. Every read runs one control search (a Reddit daily discussion) and cites a
link from it, so that when any of Google's links in the answer leads to Reddit, the search could see Reddit in this
very call and a coin with no verified thread is 'none'. Otherwise the read is 'blind' (never served, its agent_runs
row marked failed), and after BLIND_STREAK blind reads in a row the job waits BLIND_PAUSE before probing again. The
searches ask for inurl:comments: on a BTC read Google returned r/Bitcoin's front page, and the model gave it the title,
score and comments of the pinned daily thread; a front page proves Reddit was reached, never which thread was read.

What to read (scan, every 30 minutes, in the ingest container): hot Deep End tokens (watch.py) called by MIN_GROUPS or
more groups, the most group calls first, then majors (tier 1-2) with news of impact >= 3 in the last 6 hours; never a
coin read in the last day or failed in the last 6 hours, never dust. At most DEEP_REDDIT_DAILY reads a day (12) and
DEEP_REDDIT_DAILY_USD of OpenRouter spend ($1; both times the Output page's budget scale), spread evenly through the
UTC day, at most PER_RUN a run. Measured 2026-10-01: $0.002-0.005 a read when Google's searches aren't billed, $0.045
when they are ($0.014 a search, as fit.py found), 6-17 s; 12 reads stay under $1 even at four billed searches each.
"""

import asyncio
import hashlib
import logging
import math
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

import asyncpg
import httpx
import orjson
from pydantic import BaseModel, Field, ValidationError

from albatross import controls, db, github_guard
from albatross.config import settings
from albatross.deepend import cfg, watch
from albatross.http import client
from albatross.llm import codex, openrouter

log = logging.getLogger(__name__)

AGENT = "deep_reddit_scan"               # the job, and the agent of its agent_runs rows
# Since 2026-10-05 the ChatGPT plan's search reads first (ask_on_plan: the pages it opened are the evidence); Gemini on
# Google's own index, which proves what it saw through its grounding links (see above), is the OpenRouter fallback.
# LLM_MODEL_OVERRIDES=deep_reddit_scan=<model> switches the fallback's model; its gate needs Google's grounding links.
MODEL = "google/gemini-3.8-flash"
# Google's native search ignores excluded_domains (tested 2026-10-01: a GitHub page came back with and without it),
# so GitHub is kept out another way: every query is site:reddit.com, and a read whose search returned any GitHub page
# keeps only its proven threads, never its written summary (github_seen()). Nothing GitHub-derived is stored.
SEARCH = {"type": "openrouter:web_search", "parameters": {"engine": "native", "max_results": 10, "max_uses": 5}}
# max_uses covers the longest plan (a major's 5 searches); 12 reads at 5 billed searches stay under the $1 a day.
REASONING = "low"
MAX_TOKENS, DEADLINE_S = 8000, 120
DAILY, DAILY_USD = 12, 1.0               # reads and dollars a day (DEEP_REDDIT_DAILY, DEEP_REDDIT_DAILY_USD)
MAJOR_READS = 4                          # of them, kept for majors (BTC, ETH... with big news) while one is due
PER_RUN = 3
MAX_THREADS, MAX_FLAGS = 8, 4

# ─── what to read ────────────────────────────────────────────────────────────
MIN_GROUPS, MIN_LIQUIDITY = 2, 10_000    # a coin one group called, or dust, has no Reddit conversation worth a read
NEWS_FRESH, NEWS_MIN_IMPACT = timedelta(hours=6), 3
READ_EVERY, BACKOFF = timedelta(hours=24), timedelta(hours=6)
BLIND_STREAK, BLIND_PAUSE = 2, timedelta(hours=24)

# ─── the gate and the section ────────────────────────────────────────────────
GROUNDING = "vertexaisearch.cloud.google.com"
GROUNDING_LINK = re.compile(r"(?:https?://)?vertexaisearch\.cloud\.google\.com/grounding-api-redirect/[A-Za-z0-9_\-=]*")
REDDIT_HOSTS = {"reddit.com", "www.reddit.com", "old.reddit.com", "np.reddit.com", "redd.it"}
RESOLVE_S, MAX_RESOLVE = 5.0, 12
WINDOW = timedelta(hours=72)             # "right now" on Reddit: activity counts threads this young (or undated)
SLACK = timedelta(days=1)                # posted dates are days, as the search result shows them
ACTIVE = 4                               # verified recent threads that make the discussion "active"
FRESH = timedelta(hours=24)
NONE_FOUND = "Our searches returned no Reddit thread about it."
# The take's rule (dossier.FORBIDDEN): the scout's own sentence never tells anyone to buy or sell.
ADVICE = re.compile(r"\b(buy|sell) signal\b|\bshould (buy|sell)\b|\bbuy now\b|\bwill (pump|moon|dump|go up|rise)\b|"
                    r"\bto the moon\b|\bguarantee|\bfinancial advice\b|\bNFA\b|\bDYOR\b", re.IGNORECASE)
CHAINS = {"solana": "Solana", "base": "Base", "bsc": "BNB Chain", "ethereum": "Ethereum", "robinhood": "Robinhood Chain"}
THREADS = "site:reddit.com inurl:comments"

Stance = Literal["bullish", "bearish", "neutral"]


class Thread(BaseModel):
    url: str = Field(description="the thread's link, copied exactly as your search returned it")
    title: str
    posted: str | None = Field(None, description="YYYY-MM-DD when the result shows the date, else null")
    score: int | None = Field(None, description="upvotes, only when the result shows them")
    comments: int | None = Field(None, description="comment count, only when the result shows it")
    stance: Stance
    notable: bool = Field(False, description="one of the up to 3 most informative threads")


class Flag(BaseModel):
    kind: Literal["rug", "scam", "impersonation", "other"]
    detail: str = Field(description="one sentence")
    url: str = Field(description="the link of the Reddit thread that shows it, as your search returned it")


class RedditRead(BaseModel):
    control: str | None = Field(description="the link of the first Reddit result of the control search")
    threads: list[Thread] = Field(description="every Reddit thread your searches returned about this coin")
    other_coin_threads: int = Field(0, description="threads left out: about another coin with the same ticker or name")
    mood: int = Field(ge=-5, le=5)
    summary: str = Field(description="two or three plain sentences: what people on Reddit say about it")
    red_flags: list[Flag] = Field(default_factory=list)
    why_it_matters: str = Field("", description="one sentence for a trader, or empty")


SYSTEM = """You are the Reddit scout on a crypto trading desk. You read Reddit through Google search and report only
what your search results show. Copy every link exactly as your search returned it; never write or rebuild a
reddit.com URL yourself: a link we can't trace to your search is discarded, and the thread with it. Never advise
buying or selling and never predict the price. Keep every text field short and plain."""

PROMPT = """Today is {now} UTC.

THE COIN
{coin}

SEARCH with Google, these searches and no others:
{plan}
Look for threads from the last 72 hours, the last 24 first.

ANSWER
- control: the link of the first Reddit result of search 1, exactly as returned.
- threads: every Reddit thread your searches returned that is about this coin (up to {max_threads}, newest first),
  each with its link exactly as returned, its title, posted (YYYY-MM-DD when the result shows the date, else null),
  score and comments (only when the result shows them, else null), stance (bullish, bearish or neutral) and notable
  (true for the up to 3 most informative threads). A thread about another coin with the same ticker or name is not
  about this coin: leave it out and count it in other_coin_threads.
- mood: integer -5..+5 for how those threads feel: -5 rugged or scam, -3 clearly bearish, -1 mildly bearish, 0 mixed
  or nothing said, +1 mildly bullish, +3 clearly bullish, +5 euphoric.
- summary: two or three plain sentences on what people on Reddit are saying about it.
- red_flags: up to {max_flags}, only what the threads show: rug or scam reports, impersonation (fake accounts, a
  copycat contract passing as this coin), each with kind, one sentence of detail and the link of the thread.
- why_it_matters: one sentence on what a trader should take from it, or "" when nothing stands out.
If no Reddit thread is about this coin, answer threads [], mood 0 and a summary saying so."""


# On the ChatGPT plan the evidence is the pages the search opened:
# the gateway lists them from the run's own events, so the model can't invent one. A thread or red flag is kept only
# when its thread was opened in this call; "no Reddit discussion found" needs a reddit.com page opened in this call.
PLAN_SYSTEM = """You are the Reddit scout on a crypto trading desk. You read Reddit through web search and report only
what the pages you open show. Open every thread you list or flag and give its URL exactly as you opened it: a thread
whose page you didn't open in this search is discarded. Never open or cite anything hosted on GitHub. Never advise
buying or selling and never predict the price. Keep every text field short and plain."""


def plan_prompt(s: dict, now: datetime) -> str:
    """The same task as PROMPT for the plan's search: search the web (no Google operators needed) and open threads."""
    return (messages(s, now)[1]["content"]
            .replace("SEARCH with Google, these searches and no others:", "SEARCH the web for these, and nothing else:")
            .replace("- control: the link of the first Reddit result of search 1, exactly as returned.",
                     "- control: the URL of the first Reddit page you opened for search 1, exactly as opened.")
            .replace("each with its link exactly as returned,", "each with the URL you opened it at,")
            .replace("and the link of the thread.", "and the URL of the thread as you opened it."))


# ─── the prompt ──────────────────────────────────────────────────────────────
def _when(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%d %H:%M")


def coin(s: dict) -> str:
    """The coin as the model is told about it: what we know that tells it apart from its namesakes."""
    if s["kind"] == "major":
        lines = [f"{s.get('name') or s['symbol']} ({s['symbol']}), a major crypto asset. Fresh headlines about it (UTC):"]
        lines += [f"- {_when(h['at'])} {h['title']}" for h in s.get("headlines") or []]
        return "\n".join(lines)
    sym, name = s.get("symbol"), s.get("name")
    out = (f"${sym}" if sym else "Ticker unknown") + (f' ("{name}")' if name and name != sym else "")
    out += f", a memecoin on {CHAINS.get(s['chain'], s['chain'])}, contract {s['address']}"
    if s.get("created_at"):
        out += f", created {_when(s['created_at'])} UTC"
    out += f". Our trading groups called it {s.get('n_calls') or 0} times from {s.get('n_groups') or 0} groups."
    if s.get("shared_ticker"):
        out += (f" {s['shared_ticker']} other recent tokens use the ticker ${sym}: a thread counts only if it shows "
                "this contract or is clearly about this one (its name, chain or launch).")
    return out


# A major's own subreddit: where its daily thread lives. The first evening asked the model to fill in "<Bitcoin's own
# subreddit>" itself and every major came back with nothing (12 of 12 reads), so the searches name it.
SUBREDDITS = {"BTC": "Bitcoin", "ETH": "ethereum", "SOL": "solana", "XRP": "XRP", "DOGE": "dogecoin",
              "ADA": "cardano", "BNB": "binance", "AVAX": "Avax", "LINK": "Chainlink", "DOT": "dot", "LTC": "litecoin",
              "TRX": "Tronix", "SUI": "SuiNetwork", "NEAR": "nearprotocol", "TON": "TONcoin", "XLM": "Stellar",
              "HBAR": "Hedera", "BCH": "btc", "UNI": "UniSwap", "AAVE": "Aave_Official", "ARB": "arbitrum",
              "OP": "optimismCollective", "APT": "Aptos", "ATOM": "cosmosnetwork", "FIL": "filecoin", "ICP": "dfinity",
              "ETC": "EthereumClassic", "XMR": "Monero", "SHIB": "SHIBArmy", "HYPE": "HyperliquidX",
              "PEPE": "pepecoins", "WIF": "dogwifhat", "INJ": "injective", "TIA": "Celestia", "SEI": "sei",
              "ENA": "ethena", "ONDO": "ondofinance", "TAO": "bittensor_", "ZEC": "zec", "RENDER": "RenderToken",
              "LDO": "LidoFinance", "JUP": "jupiterexchange", "WLD": "worldcoin", "FET": "FetchAI_Community",
              "ALGO": "AlgorandOfficial", "AERO": "AerodromeFinance", "CAKE": "pancakeswap", "CRV": "CurveFinance",
              "DASH": "dashpay", "GRT": "thegraph", "PENDLE": "pendle", "PENGU": "pudgypenguins", "POL": "0xPolygon",
              "QNT": "QuantNetwork", "RAY": "raydium", "RUNE": "THORChain", "SAND": "TheSandboxGaming",
              "VIRTUAL": "VirtualsProtocol", "XPL": "Plasma", "ZRO": "LayerZero", "TRUMP": "TrumpCoin",
              "PUMP": "pumpfun", "PAXG": "Paxos", "NMR": "numerai", "GRASS": "GetGrass", "ASTER": "Aster_DEX"}
STOPWORDS = frozenset("a an and as at be by for from has have in into is it its of on or that the this to was were "
                      "will with after amid over under says said new".split())


def keywords(title: str, n: int = 5) -> str:
    """A headline's distinctive words, for a search."""
    words = [w for w in re.findall(r"[A-Za-z0-9$.-]+", title or "") if w.lower() not in STOPWORDS and len(w) > 2]
    return " ".join(words[:n])


def plan(s: dict) -> list[str]:
    """The searches the model is asked to run, spelled out; the first is the control. inurl:comments keeps them to
    threads: a subreddit's front page shows a pinned thread's title, but its link can't prove which thread."""
    out = [f"{THREADS} cryptocurrency daily discussion   (the control: it shows that Reddit is in your results)"]
    if s["kind"] == "major":
        name, sub = s.get("name") or s["symbol"], SUBREDDITS.get((s.get("symbol") or "").upper())
        top = keywords(((s.get("headlines") or [{}])[0]).get("title") or "")
        out += [f"site:reddit.com/r/{sub} inurl:comments daily discussion", f"site:reddit.com/r/{sub} inurl:comments"] \
            if sub else []
        out += [f"site:reddit.com/r/CryptoCurrency inurl:comments {name}"]
        return out + ([f"{THREADS} {name} {top}"] if top else [])
    sym, name = s.get("symbol"), s.get("name")
    out.append(f'{THREADS} "{s["address"]}"')
    if sym:
        out.append(f'{THREADS} "{sym}" {CHAINS.get(s["chain"], s["chain"])} coin')
    if name and len(name) >= 3 and name.lower() != (sym or "").lower():
        out.append(f'{THREADS} "{name}" crypto')
    return out


def messages(s: dict, now: datetime) -> list[dict]:
    text = PROMPT.format(now=_when(now), coin=coin(s), max_threads=MAX_THREADS, max_flags=MAX_FLAGS,
                         plan="\n".join(f"{i}. {p}" for i, p in enumerate(plan(s), 1)))
    schema = orjson.dumps(RedditRead.model_json_schema()).decode()
    return [{"role": "system", "content": SYSTEM + "\n\nAnswer with a single JSON object matching this JSON Schema:\n"
             + schema}, {"role": "user", "content": text}]


# ─── thread links ────────────────────────────────────────────────────────────
REDDIT = re.compile(r"(?:[a-z0-9-]+\.)?reddit\.com")
POST = re.compile(r"/(?:(?P<kind>r|u|user)/(?P<name>[A-Za-z0-9_-]{2,21})/)?(?:comments|gallery)/(?P<id>[a-z0-9]{3,10})"
                  r"(?:/(?P<slug>[^/]+))?(?:/.*)?", re.IGNORECASE)
SHORT = re.compile(r"/(?P<id>[a-z0-9]{3,10})/?", re.IGNORECASE)


def _split(url: str | None):
    try:
        u = urlsplit((url or "").strip())
    except ValueError:
        return None
    return u if u.scheme in ("http", "https") else None


def thread(url: str | None) -> dict | None:
    """A reddit.com thread link in one canonical form: {key, url, subreddit, slug}, key being the post id. Any host
    spelling (www., old., np., m.), a redd.it short link, a comment permalink (its thread) and share tags (?utm_,
    share_id) count. A subreddit, a search, a profile, an image or a /s/ share code (only Reddit knows which thread it
    is, and Reddit is never asked) is not a thread."""
    u = _split(url)
    if u is None:
        return None
    host = (u.hostname or "").lower()
    if host == "redd.it":
        m = SHORT.fullmatch(u.path)
        return m and {"key": m["id"].lower(), "url": f"https://www.reddit.com/comments/{m['id'].lower()}/",
                      "subreddit": None, "slug": None}
    if not REDDIT.fullmatch(host):
        return None
    m = POST.fullmatch(u.path)
    if not m:
        return None
    pid, slug = m["id"].lower(), m["slug"] if m["slug"] and m["slug"].lower() != "comment" else None
    if m["kind"]:
        where, sub = ((f"/r/{m['name']}", f"r/{m['name']}") if m["kind"].lower() == "r"
                      else (f"/user/{m['name']}", f"u/{m['name']}"))
    else:
        where, sub = "", None
    return {"key": pid, "url": f"https://www.reddit.com{where}/comments/{pid}/" + (f"{slug}/" if slug else ""),
            "subreddit": sub, "slug": slug}


def on_reddit(url: str | None) -> bool:
    """Where a grounding link may lead to count as Reddit: the main hosts only."""
    u = _split(url)
    return u is not None and (u.hostname or "").lower() in REDDIT_HOSTS


def grounding(url: str | None) -> bool:
    """A Google grounding link: the only kind of link the scout ever requests (from Google, never Reddit)."""
    u = _split(url)
    return (u is not None and u.scheme == "https" and (u.hostname or "").lower() == GROUNDING
            and u.path.startswith("/grounding-api-redirect/"))


def bare(url: str) -> str:
    """A URL for the audit: scheme, host and path only (no query, no tracking)."""
    u = _split(url)
    return f"{u.scheme}://{u.hostname}{u.path}" if u else url[:200]


def _masked(text: str) -> str:
    """Text for the audit with Google's links masked: what is stored names threads, never a grounding link."""
    return GROUNDING_LINK.sub("[google link]", text)


# ─── resolving Google's links ────────────────────────────────────────────────
async def location(link: str) -> str | None:
    """Where one grounding link leads: one GET to Google's redirector, not followed, its Location header only (the
    body is never read), within RESOLVE_S. None when it doesn't redirect, fails or is slow."""
    if not grounding(link):
        return None

    async def ask() -> str | None:
        async with client().stream("GET", link, follow_redirects=False, timeout=RESOLVE_S) as r:
            return r.headers.get("location") if r.is_redirect else None
    try:
        return await asyncio.wait_for(ask(), RESOLVE_S)
    except (httpx.HTTPError, TimeoutError) as e:
        log.info("%s: grounding link unresolved: %s", AGENT, type(e).__name__)
        return None


def cited(r: "RedditRead") -> list[str]:
    """The grounding links the answer cites, the control first, then the threads and the red flags, each once."""
    links = [r.control or ""] + [t.url for t in r.threads] + [f.url for f in r.red_flags]
    return list(dict.fromkeys(u.strip() for u in links if grounding(u)))


async def resolve(links: list[str]) -> dict[str, str | None]:
    """{grounding link: where it leads, or None} for the first MAX_RESOLVE links; the rest stay unresolved (None)."""
    found = await asyncio.gather(*(location(u) for u in links[:MAX_RESOLVE]))
    return dict(zip(links, found)) | dict.fromkeys(links[MAX_RESOLVE:])


# ─── the evidence gate ───────────────────────────────────────────────────────
def title(said: str, slug: str | None) -> str:
    """The model's title for the thread, unless the thread's own URL slug (its title's first words) says otherwise:
    then the slug's words. Half the slug's words must be in the title (apostrophes and emoji spell differently)."""
    t = " ".join((said or "").split())[:300]
    words = [w for w in (slug or "").lower().split("_") if w]
    if words and 2 * sum(w in set(re.findall(r"[a-z0-9]+", t.lower())) for w in words) < len(words):
        return slug.replace("_", " ")
    return t or (slug or "").replace("_", " ")


def slug_agrees(page_title: str, slug: str) -> bool:
    """Half the words of a thread's URL slug (its title's first words) appear in the title the page showed."""
    words = [w for w in slug.lower().split("_") if w]
    return not words or 2 * sum(w in set(re.findall(r"[a-z0-9]+", page_title.lower())) for w in words) >= len(words)


def posted_day(said: str | None, now: datetime) -> date | None:
    """A thread's posted day as the model read it from the result; unreadable or in the future is unknown."""
    try:
        d = date.fromisoformat((said or "").strip()[:10])
    except ValueError:
        return None
    return d if d <= (now + SLACK).date() else None


def level(recent: int) -> str:
    return "none" if recent == 0 else "few" if recent < ACTIVE else "active"


def check(url: str, where: dict[str, str | None], verified: dict[str, dict]) -> tuple[dict | None, str | None]:
    """(the thread a cited link stands for, None) or (None, why it fails). A grounding link stands for where it
    leads; anything else is plain text and stands only for a thread a grounding link led to."""
    url = (url or "").strip()
    if grounding(url):
        loc = where.get(url)
        if loc is None:
            return None, "google link: unresolved"
        th = thread(loc) if on_reddit(loc) else None
        return (th, None) if th else (None, f"google link -> {bare(loc)}")
    th = thread(url)
    if th and th["key"] in verified:
        return verified[th["key"]], None
    return None, _masked(url[:300]) or "(empty)"


def judge(r: RedditRead, where: dict[str, str | None], s: dict, now: datetime) -> dict:
    """The stored read: the model's threads and red flags that pass the gate, the activity counted from them, and
    "No Reddit discussion found" only when one of Google's links led to Reddit ('blind' otherwise)."""
    verified: dict[str, dict] = {}
    for loc in where.values():
        if (th := thread(loc) if on_reddit(loc) else None):
            verified.setdefault(th["key"], th)
    born = s.get("created_at")
    threads, flags, dropped, keys, elsewhere = [], [], [], set(), set()
    other = r.other_coin_threads
    for t in r.threads:
        th, why = check(t.url, where, verified)
        if th is None:
            dropped.append(why)
            continue
        if th["key"] in keys | elsewhere:
            continue
        posted = posted_day(t.posted, now)
        if born and posted and posted < (born - SLACK).date():   # older than the token: another coin of that name
            elsewhere.add(th["key"])
            other += 1
            continue
        keys.add(th["key"])
        threads.append({"subreddit": th["subreddit"], "title": title(t.title, th["slug"]), "url": th["url"],
                        "posted": posted.isoformat() if posted else None, "score": t.score, "comments": t.comments,
                        "stance": t.stance, "notable": t.notable})
    for f in r.red_flags:
        th, why = check(f.url, where, verified)
        if th is None:
            dropped.append(why)
        elif th["key"] in keys:                           # only a kept thread: dated, and about this coin
            if len(flags) < MAX_FLAGS:
                flags.append({"kind": f.kind, "detail": " ".join(f.detail.split())[:300], "url": th["url"]})
        elif th["key"] not in elsewhere:                  # a thread it never listed: undated, unchecked for namesakes
            dropped.append(f"flag on an unlisted thread: {bare(th['url'])}")
    threads.sort(key=lambda t: (not t["notable"], t["posted"] is None,
                                -date.fromisoformat(t["posted"]).toordinal() if t["posted"] else 0))
    control = where.get((r.control or "").strip())
    reached = any(on_reddit(loc) for loc in where.values())   # any of Google's links led to Reddit, the control's or not
    found = bool(threads or flags)
    since = (now - WINDOW - SLACK).date()
    recent = sum(t["posted"] is None or date.fromisoformat(t["posted"]) >= since for t in threads)
    why = " ".join(r.why_it_matters.split())[:400]
    # "No discussion found" only when nothing claimed failed the gate: claims that all failed are 'unverified',
    # stored for the audit and never served (an unverified negative is no more a fact than an unverified thread).
    status = "read" if found else ("unverified" if dropped else "none") if reached else "blind"
    if status == "none" and s.get("chain") is None:      # a major always has threads: a miss is ours, never a fact
        status = "unverified"
    return {"status": status, "mood": r.mood if found else None, "activity": level(recent) if reached else None,
            "summary": " ".join(r.summary.split())[:800] if found else NONE_FOUND if status == "none" else None,
            "why": why if found and not ADVICE.search(why) else "", "threads": threads[:MAX_THREADS], "flags": flags,
            "citations_dropped": len(dropped),
            "audit": {"dropped": dropped, "other_coin_threads": other, "control": bare(control) if control else None,
                      "links": len(where), "unresolved": sum(v is None for v in where.values()),
                      "reddit_threads": len(verified)}}


# ─── the call ────────────────────────────────────────────────────────────────
def parse(raw: str | None) -> RedditRead:
    """The JSON object in the answer (Gemini with search wraps it in a code fence)."""
    if not raw or not raw.strip():
        raise ValueError("empty answer")
    i, j = raw.find("{"), raw.rfind("}")
    if i < 0 or j < i:
        raise ValueError(f"no JSON object in the answer: {raw[:200]!r}")
    return RedditRead.model_validate_json(raw[i:j + 1])


def github_seen(resp: dict) -> int:
    """How many of the search's results were GitHub pages. Google's links are redirects; each result's title names
    its site."""
    msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
    return sum(1 for a in msg.get("annotations") or [] if a.get("type") == "url_citation"
               and (github_guard.hosted((a.get("url_citation") or {}).get("title"))
                    or github_guard.hosted((a.get("url_citation") or {}).get("url"))))


async def _attempt(msgs: list[dict], model: str, input_hash: str):
    """One call -> (read or None, agent_run_id, error, transient, GitHub results seen). usage.cost is OpenRouter's
    whole bill for it."""
    started = time.monotonic()
    body = {"model": model, "messages": msgs, "max_tokens": MAX_TOKENS, "tools": [SEARCH], "usage": {"include": True}}
    if REASONING and model not in openrouter.ROUTERS:
        body["reasoning"] = {"effort": REASONING}
    resp: dict = {}
    read, error, transient, raw = None, None, False, None
    try:
        resp = await asyncio.wait_for(openrouter._post(body), timeout=DEADLINE_S)
        choice = resp["choices"][0]
        raw = choice["message"].get("content")
        if choice.get("finish_reason") == "length":
            raise ValueError(f"output truncated at max_tokens={MAX_TOKENS}")
        read = parse(raw)
    except ValidationError as e:                         # the wrong shape: garbled JSON is retried, a schema slip isn't
        error = f"ValidationError: {str(e)[:400]}"
        transient = any(err.get("type", "").startswith("json_") for err in e.errors())
    except ValueError as e:                              # empty, cut off, or no JSON at all: worth one retry
        error, transient = f"ValueError: {str(e)[:400]}", True
    except (KeyError, IndexError, TypeError) as e:
        error = f"{type(e).__name__}: {str(e)[:400]}"
    except httpx.HTTPStatusError as e:
        error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        transient = e.response.status_code in (429, 500, 502, 503, 504)
    except TimeoutError:
        error, transient = f"no response within {DEADLINE_S}s", True
    except httpx.HTTPError as e:
        error, transient = f"{type(e).__name__}: {str(e)[:200]}", True
    usage = resp.get("usage") or {}
    pool = await db.pool()
    run_id = await pool.fetchval(
        """INSERT INTO agent_runs (agent, model, latency_ms, input_tokens, output_tokens, cost_usd, ok, error,
                                   input_hash, output)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10) RETURNING id""",
        AGENT, openrouter.recorded(model, resp.get("model")), int((time.monotonic() - started) * 1000),
        usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("cost"), read is not None, error,
        input_hash, _masked(read.model_dump_json()) if read else (_masked(orjson.dumps({"raw": raw[:4000]}).decode())
                                                                  if raw else None))
    return read, run_id, error, transient, github_seen(resp) if resp else 0


async def daily() -> int:
    """DEEP_REDDIT_DAILY reads times the budget scale set on the Output page."""
    return max(0, round(cfg.env_int("DEEP_REDDIT_DAILY", DAILY) * await controls.scale(AGENT)))


async def daily_usd() -> float:
    """DEEP_REDDIT_DAILY_USD of OpenRouter spend times the budget scale set on the Output page."""
    return cfg.env_float("DEEP_REDDIT_DAILY_USD", DAILY_USD) * await controls.scale(AGENT)


async def reads_today() -> int:
    pool = await db.pool()
    return await pool.fetchval("SELECT count(*) FROM de_reddit_reads WHERE read_at >= date_trunc('day', now())")


async def major_reads_today() -> int:
    pool = await db.pool()
    return await pool.fetchval("SELECT count(*) FROM de_reddit_reads WHERE read_at >= date_trunc('day', now()) "
                               "AND chain IS NULL")


async def ask_on_plan(s: dict, now: datetime) -> tuple[RedditRead, int, dict[str, str], int]:
    """-> (read, agent_run_id, {page opened: itself}, GitHub pages opened) from the plan's search. The pages map to
    themselves so judge() treats each like a resolved link. Raises BudgetExceeded when paused or past the day's reads;
    codex.Unavailable when the plan can't take it (the caller goes to OpenRouter)."""
    if await controls.paused(AGENT):
        raise openrouter.BudgetExceeded(f"{AGENT} is paused on the Output page")
    if (n := await reads_today()) >= (cap := await daily()):
        raise openrouter.BudgetExceeded(f"{AGENT}: {n}/{cap} reads today")
    r, run_id, opened, github = await codex.search(AGENT, RedditRead, PLAN_SYSTEM, plan_prompt(s, now),
                                                   effort=REASONING, deadline_s=DEADLINE_S + 180,
                                                   daily_budget_usd=await daily_usd())
    where = {u: u for u in opened}
    claimed = {(thread(t.url) or {}).get("key"): t.title for t in r.threads}
    for u, page_title in (await openrouter.opened_now(AGENT, r, opened, await daily_usd())).items():
        th = thread(u)          # a thread page must look like the thread: its URL slug, else the title the read gave it
        if th is None or (slug_agrees(page_title, th["slug"]) if th["slug"]
                          else codex.titles_agree(claimed.get(th["key"], ""), page_title)):
            where[u] = u
    return r, run_id, where, github


async def ask(msgs: list[dict], model: str) -> tuple[RedditRead, int, int]:
    """-> (read, agent_run_id, GitHub results its search returned). One retry for transient failures. Raises
    BudgetExceeded when paused, or past the day's reads or dollars."""
    if not settings.openrouter_api_key:                  # reached only once the plan said no: stop for this run
        raise openrouter.BudgetExceeded(f"{AGENT}: the ChatGPT plan can't read now and no OpenRouter key is set")
    if await controls.paused(AGENT):
        raise openrouter.BudgetExceeded(f"{AGENT} is paused on the Output page")
    if (spent := await openrouter.spent_today(AGENT)) >= (usd := await daily_usd()):
        raise openrouter.BudgetExceeded(f"{AGENT} spent ${spent:.2f} of ${usd:.2f} today")
    if (n := await reads_today()) >= (cap := await daily()):
        raise openrouter.BudgetExceeded(f"{AGENT}: {n}/{cap} reads today")
    input_hash = hashlib.sha256(orjson.dumps(msgs)).hexdigest()[:16]
    for attempt in (1, 2):
        read, run_id, error, transient, github = await _attempt(msgs, model, input_hash)
        if read is not None:
            return read, run_id, github
        if not transient or attempt == 2:
            break
        log.warning("%s: attempt 1 failed (%s); retrying once", AGENT, error)
    raise RuntimeError(f"{AGENT} call failed: {error}")


# ─── storage ─────────────────────────────────────────────────────────────────
COLS = ("chain", "address", "symbol", "read_at", "status", "picked_for", "mood", "activity", "summary", "threads",
        "flags", "why", "citations_dropped", "audit", "agent_run_id")
INSERT = (f"INSERT INTO de_reddit_reads ({', '.join(COLS)}) "
          f"VALUES ({', '.join(f'${i}' for i in range(1, len(COLS) + 1))})")


def _j(v) -> str | None:
    return None if v is None else orjson.dumps(v).decode()


async def store(s: dict, row: dict, read_at: datetime, run_id: int | None) -> None:
    pool = await db.pool()
    await pool.execute(INSERT, s.get("chain"), s.get("address"), s.get("symbol"), read_at, row["status"],
                       s.get("picked_for"), row.get("mood"), row.get("activity"), row.get("summary"),
                       _j(row.get("threads") or []), _j(row.get("flags") or []), row.get("why"),
                       row.get("citations_dropped", 0), _j(row.get("audit")), run_id)


# ─── one read ────────────────────────────────────────────────────────────────
async def read(s: dict) -> dict:
    """One coin's Reddit read: the call, Google's links resolved, the gate, the stored row. A failed call is stored
    as 'failed' (the coin waits BACKOFF) and raised; a call the budget or a pause refuses stores nothing."""
    started = datetime.now(UTC)
    where = None
    try:
        try:                                             # the ChatGPT plan's search first, OpenRouter when it can't
            r, run_id, where, github = await ask_on_plan(s, started)
        except codex.Unavailable as e:
            log.info("%s: the plan can't read now (%s), using OpenRouter", AGENT, str(e)[:200])
            r, run_id, github = await ask(messages(s, started), openrouter.model_for(AGENT, MODEL))
    except openrouter.BudgetExceeded:
        raise
    except RuntimeError:                                 # logged in agent_runs
        await store(s, {"status": "failed"}, started, None)
        raise
    on_plan = where is not None
    row = judge(r, where if on_plan else await resolve(cited(r)), s, started)
    if on_plan and row["status"] == "none":            # opening a reddit.com page proves no search hit: unproven
        row |= {"status": "unverified", "summary": None}
    if github:                                           # the search saw GitHub: keep the proven threads, not the prose
        row |= {"summary": None if row["status"] == "read" else row["summary"], "why": ""}
        row["audit"] = {**row["audit"], "github_results": github}
    await store(s, row, started, run_id)
    notes = []
    if row["status"] == "blind":
        what = "nothing it opened was on Reddit" if on_plan else "none of Google's links led to Reddit"
        notes.append(f"blind: {what} (the control: {row['audit']['control'] or 'none'})")
    if row["citations_dropped"]:
        notes.append(f"dropped {row['citations_dropped']}/{len(r.threads) + len(r.red_flags)} citations: "
                     + "; ".join(row["audit"]["dropped"]))
    if notes:                                            # the run counts as good if something survived, or it claimed nothing
        good = row["status"] == "read" or (row["status"] == "none" and not (r.threads or r.red_flags))
        await (await db.pool()).execute("UPDATE agent_runs SET ok = $3, error = $2 WHERE id = $1", run_id,
                                        " | ".join(notes)[:900], good)
    log.info("%s: %s %s, %d threads, %d flags, %d dropped, activity %s", AGENT, s.get("symbol") or s.get("address"),
             row["status"], len(row["threads"]), len(row["flags"]), row["citations_dropped"], row["activity"])
    return row


# ─── what to read ────────────────────────────────────────────────────────────
TOKENS_SQL = """
WITH k AS (SELECT * FROM unnest($1::text[], $2::text[]) AS k(chain, address)),
g AS (SELECT coalesce(t.resolved_chain, t.chain) AS chain, c.address,
             count(DISTINCT coalesce(c.group_id::text, c.group_name)) AS n_groups, count(*) AS n_calls
      FROM micro_calls c JOIN micro_tokens t ON t.chain = c.chain AND t.address = c.address
      WHERE c.address = ANY($2::text[]) GROUP BY 1, 2)
SELECT k.chain, k.address, coalesce(s.symbol, f.symbol) AS symbol, s.name, s.liquidity_usd AS liquidity,
       coalesce(s.created_at, f.first_seen) AS created_at, coalesce(g.n_groups, 0) AS n_groups,
       coalesce(g.n_calls, 0) AS n_calls, r.last_read, r.last_failed,
       (SELECT count(*) FROM micro_state o
        WHERE upper(o.symbol) = upper(coalesce(s.symbol, f.symbol)) AND (o.chain, o.token) <> (k.chain, k.address)
          AND (o.created_at IS NULL OR o.created_at > now() - interval '14 days')) AS shared_ticker
FROM k
LEFT JOIN g ON g.chain = k.chain AND g.address = k.address
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
LEFT JOIN LATERAL (SELECT t.symbol, t.first_seen FROM micro_tokens t
                   WHERE t.address = k.address AND coalesce(t.resolved_chain, t.chain) = k.chain
                   ORDER BY t.first_seen LIMIT 1) f ON TRUE
LEFT JOIN LATERAL (SELECT max(read_at) FILTER (WHERE status IN ('read', 'none')) AS last_read,
                          max(read_at) FILTER (WHERE status IN ('failed', 'blind', 'unverified')) AS last_failed
                   FROM de_reddit_reads r WHERE r.chain = k.chain AND r.address = k.address) r ON TRUE"""

MAJORS_SQL = """
SELECT a.symbol, a.name, max(n.impact) AS impact, count(*) AS items,
       jsonb_agg(jsonb_build_object('title', n.title, 'at', n.published_at)
                 ORDER BY n.impact DESC, n.published_at DESC) AS headlines,
       (SELECT max(read_at) FILTER (WHERE status IN ('read', 'none')) FROM de_reddit_reads r
        WHERE r.chain IS NULL AND r.symbol = a.symbol) AS last_read,
       (SELECT max(read_at) FILTER (WHERE status IN ('failed', 'blind', 'unverified')) FROM de_reddit_reads r
        WHERE r.chain IS NULL AND r.symbol = a.symbol) AS last_failed
FROM assets a JOIN news_items n ON n.assets @> ARRAY[a.symbol]
WHERE a.active AND a.tier IN (1, 2) AND n.published_at > now() - $1::interval AND n.impact >= $2
GROUP BY a.symbol, a.name"""

LAST_SQL = "SELECT status, read_at FROM de_reddit_reads WHERE status <> 'failed' ORDER BY read_at DESC LIMIT $1"


async def tokens() -> list[dict]:
    """Every hot Deep End token (watch.tokens) with its group calls, liquidity, creation, namesakes and last reads."""
    hot = await watch.tokens("hot")
    if not hot:
        return []
    pool = await db.pool()
    rows = await pool.fetch(TOKENS_SQL, [t["chain"] for t in hot], [t["address"] for t in hot])
    return [{**dict(r), "kind": "token", "picked_for": f"{r['n_groups']} groups, {r['n_calls']} calls"} for r in rows]


async def majors() -> list[dict]:
    """Tier 1-2 coins with news of impact >= NEWS_MIN_IMPACT in the last NEWS_FRESH, with their top headlines."""
    pool = await db.pool()
    out = []
    for r in await pool.fetch(MAJORS_SQL, NEWS_FRESH, NEWS_MIN_IMPACT):
        heads = [{"title": h["title"], "at": datetime.fromisoformat(h["at"])} for h in orjson.loads(r["headlines"])[:3]]
        out.append({**dict(r), "kind": "major", "chain": None, "address": None, "headlines": heads,
                    "picked_for": f"news impact {r['impact']}"})
    return out


def due(c: dict, now: datetime) -> bool:
    return not ((c.get("last_read") and now - c["last_read"] < READ_EVERY)
                or (c.get("last_failed") and now - c["last_failed"] < BACKOFF))


def pick(toks: list[dict], majs: list[dict], now: datetime, n: int, majors_first: bool = False) -> list[dict]:
    """Hot Deep End tokens that MIN_GROUPS groups called (no dust), the most group calls first, then majors with the
    weightiest, most covered fresh news; nothing read in the last READ_EVERY or failed in the last BACKOFF. While the
    majors have reads left of their MAJOR_READS a day (majors_first), a due major takes the run's first slot: called
    memecoins would otherwise take every read and the coin pages would never get one."""
    toks = sorted((t for t in toks if (t.get("n_groups") or 0) >= MIN_GROUPS
                   and (t.get("liquidity") or 0) >= MIN_LIQUIDITY and due(t, now)),
                  key=lambda t: (-t["n_groups"], -t["n_calls"], -(t.get("liquidity") or 0)))
    majs = sorted((m for m in majs if due(m, now)), key=lambda m: (-(m.get("impact") or 0), -(m.get("items") or 0)))
    order = (majs[:1] + toks + majs[1:]) if majors_first else (toks + majs)
    return order[:max(0, n)]


def room(done_today: int, now: datetime, daily: int) -> int:
    """Reads this run may make: the day's even pace so far, with one hour's share of slack (a busy morning can't spend
    the evening's reads), never past the day or PER_RUN."""
    day = (now - now.replace(hour=0, minute=0, second=0, microsecond=0)) / timedelta(days=1)
    return max(0, min(PER_RUN, math.ceil(daily * min(1.0, day + 1 / 24)) - done_today))


def paused_blind(last: list[dict], now: datetime) -> bool:
    """The last BLIND_STREAK reads couldn't see Reddit: wait BLIND_PAUSE after the latest before probing again."""
    return (len(last) >= BLIND_STREAK and all(r["status"] == "blind" for r in last)
            and now - last[0]["read_at"] < BLIND_PAUSE)


# ─── the job ─────────────────────────────────────────────────────────────────
async def scan() -> int:
    """One Reddit scout pass: the coins that most need a read, as many as the day's pace allows. -> reads stored."""
    now = datetime.now(UTC)
    if not settings.openrouter_api_key and not settings.codex_enabled:    # not a failure of any coin: nothing stored
        log.info("%s: neither an OpenRouter key nor the ChatGPT plan is set up", AGENT)
        return 0
    pool = await db.pool()
    if paused_blind([dict(r) for r in await pool.fetch(LAST_SQL, BLIND_STREAK)], now):
        log.info("%s: the last %d reads couldn't see Reddit; waiting before the next probe", AGENT, BLIND_STREAK)
        return 0
    n = room(await reads_today(), now, await daily())
    if n < 1:
        return 0
    done = 0
    for s in pick(await tokens(), await majors(), now, n, majors_first=await major_reads_today() < MAJOR_READS):
        try:
            await read(s)
            done += 1
        except openrouter.BudgetExceeded as e:
            log.info("%s", e)
            break
        except RuntimeError as e:                        # stored as failed; the next coin still gets its read
            log.warning("%s: %s %s", AGENT, s.get("symbol") or s.get("address"), e)
    return done


# ─── what the dossier reads ──────────────────────────────────────────────────
MOODS = ((-4, "very bearish"), (-2, "bearish"), (-1, "leaning bearish"), (0, "mixed"), (1, "leaning bullish"),
         (3, "bullish"))                 # xintel's words, so X and Reddit read alike
ACTIVITY = {"none": "No threads", "few": "A few threads", "active": "Active"}


def mood_label(m: int | None) -> str | None:
    return None if m is None else next((label for top, label in MOODS if m <= top), "euphoric")


def _load(v):
    return orjson.loads(v) if isinstance(v, str | bytes) else v


# Subreddits that exist to promote new coins: a thread there is a shill, not Reddit noticing.
PROMO_SUBS = frozenset({"cryptomoonshots", "satoshistreetbets", "altstreetbets", "cryptomarsshots", "moonshotcoins",
                        "cryptogemhunters", "shitcoinmoonshots", "memecoinmoonshots", "solanamemecoins", "pumpfun",
                        "cryptomoonshot", "altcoingems", "lowcapcrypto", "cryptocurrencytrading"})


def own_sub(subreddit: str | None, symbol: str | None) -> bool:
    """A subreddit named after the coin (r/sawcoin for SAW), or one that exists to promote new coins: its threads are
    the coin's own voice or a shill, not Reddit talking about it."""
    sub = (subreddit or "").lower().removeprefix("r/")
    sym = (symbol or "").lower().lstrip("$")
    return sub in PROMO_SUBS or (len(sym) >= 3 and sym in sub)


def shape(row: dict | None, now: datetime) -> dict | None:
    """The dossier's "reddit" block from the latest read (None: never read, or only blind, failed and unverified
    reads). found is False for "No Reddit discussion found"; status is fresh for a read in the last FRESH, else
    stale; independent counts the threads outside the coin's own subreddit."""
    if row is None:
        return None
    threads = _load(row["threads"]) or []
    return {"status": "fresh" if now - row["read_at"] <= FRESH else "stale", "as_of": row["read_at"],
            "found": row["status"] == "read", "mood": row["mood"], "mood_label": mood_label(row["mood"]),
            "activity": row["activity"], "activity_label": ACTIVITY.get(row["activity"]), "summary": row["summary"],
            "why_it_matters": row["why"] or "", "threads": threads,
            "independent": sum(not own_sub(t.get("subreddit"), row.get("symbol")) for t in threads),
            "red_flags": _load(row["flags"]) or [], "citations_dropped": row["citations_dropped"]}


LATEST = """SELECT read_at, status, symbol, mood, activity, summary, why, threads, flags, citations_dropped
            FROM de_reddit_reads
            WHERE {where} AND status IN ('read', 'none') ORDER BY read_at DESC LIMIT 1"""
TOKEN_SQL = LATEST.format(where="chain = $1 AND address = $2")
MAJOR_SQL = LATEST.format(where="chain IS NULL AND symbol = upper($1)").replace("('read', 'none')", "('read')")


async def _latest(sql: str, *args) -> dict | None:
    pool = await db.pool()
    try:
        row = await pool.fetchrow(sql, *args)
    except asyncpg.UndefinedTableError:                  # before migration 031
        return None
    return shape(dict(row) if row else None, datetime.now(UTC))


async def section(chain: str, address: str) -> dict | None:
    """A Deep End token's latest Reddit read for its dossier, or None when it has none worth showing."""
    return await _latest(TOKEN_SQL, chain, address)


async def section_major(symbol: str) -> dict | None:
    """A major's (BTC, ETH, ...) latest Reddit read, or None when it has none worth showing."""
    return await _latest(MAJOR_SQL, symbol)


# ─── registry (albatross/deepend/registry.py) ────────────────────────────────
JOBS = {AGENT: scan}                     # an OpenRouter call: the ingest container, no CLI needed
SCHEDULE = [(AGENT, {"minute": "12,42", "second": 5})]
HOST_JOBS: dict = {}
HOST_SCHEDULE: list = []
CADENCE = {AGENT: 1800}
