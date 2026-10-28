"""The X Scout (Deep End A1, docs/SPEC-DEEP-END.md): what X is saying about a memecoin right now, read through the
Grok Build CLI on a SuperGrok subscription pool with Social Pulse's lean flags (x_search is the only tool), on the host
worker; or through the xAI API with your key, from the ingest container (llm/xsearch.py picks the lane).

Every 5 minutes `scan` prices the run against the pool, picks the tokens that most need a read (`priority`: a group
call under 30 minutes old first, then a dossier a trader just opened, then hot tokens whose calls or volume keep
rising (or trending) every 30-60 minutes, then the bot's holdings every 4 hours; never under $10k liquidity, never
twice within 20 minutes) and reads up to MAX_TOKENS of them in ONE Grok call. Per token the searches are spelled out:
  contract  the address, Latest, over the token's window. CT shares memecoins by contract address, so every hit is
            about this token: its snowflake times give the post rate, and the bots and shills show up here.
  talk      the cashtag (the name when the ticker is a major coin's, is shared by 3+ recent tokens, or can't be a
            cashtag) and mentions of the project's account, Top with min_faves: the engaged conversation, large
            accounts, the mood.
  account   from:<handle>, Latest, any age, on a first read and twice a day after: is the project's account alive.
Grok lists every post those searches returned about the token (URL, stance, kind, likes) and writes only the
interpretation: mood, summary, why it matters, notable posts, the account, red flags. Code does the arithmetic from
the listed posts (posts in the last hour, unique authors, shill/bot share, large accounts, first mention), and the
evidence gate keeps only posts whose status id decodes to a time inside their search's window (Social Pulse's check).
A token without one verifiable post is not a read; one whose searches found nothing is stored as 'quiet' only when
another token in the same call was grounded (proof that the searches ran).

Measured 2026-09-30 on the host (tests/fixtures/deep_x): one call for 4 tokens (11 searches) cost $0.32 of the pool,
$0.08 a token, in 4 minutes at medium reasoning effort; 3 tokens at the default effort cost $0.21 in 4 minutes. The
cost follows the posts returned (~$0.005 each, as Social Pulse measured), not the reasoning: low effort cost the
same, finished in 80 s and dropped the post list. x_search runs server-side (all searches in one model turn), caps a
limit at 10, and shows like counts but never follower counts or join dates, so large accounts are known by likes and
by name, and an account's age is unknown.

Asked reads ("Search X now" on a dossier, 2026-10-06): `ask` queues one in de_x_asks and the deep_x_ask job (where
the X lane runs) reads it within a minute or so, in a Grok call of its own (with any other waiting asks), past the
liquidity floor, the tier and the hourly pace, never past the week (`ask_room`). The scan and the asked reads take
turns (`_grok`): one Grok call at a time.

Pool: Social Pulse and the X Scout spend one SuperGrok week (about $42 of equivalent cost on one plan). The
scout's week is DEEP_X_WEEKLY_USD, never more than GROK_POOL_WEEKLY_USD less Social Pulse's allowance
(settings.social_weekly_usd); it is paced like Social Pulse (an hourly share plus catch-up, at most 1.5x the daily
share in any 24 h, never past the week; a week too small to fund a read an hour is paced over the hours it takes
to fund one), and the two agents' actual spend (agent_runs) never passes the pool. A bigger pool only means more
tokens per run and more runs. On the xAI API the same pacing runs in real dollars: each allowance becomes the same
share of settings.xai_daily_usd x 7 (xsearch.week), and the spend is agent_runs.cost_usd.
"""

import asyncio
import importlib
import inspect
import logging
import math
import re
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlparse

import asyncpg
import orjson
from pydantic import BaseModel, Field

from albatross import controls, db
from albatross.agents import social_pulse as sp
from albatross.config import settings
from albatross.deepend import watch
from albatross.deepend.cfg import env_float
from albatross.llm import cli, xsearch
from albatross.venues import dexscreener

log = logging.getLogger(__name__)

AGENT = "deep_x_scan"                    # the job, and the agent of its agent_runs rows
POOL_AGENTS = [sp.AGENT, AGENT]          # everything that spends the SuperGrok pool
H = timedelta(hours=1)

# ─── searches ────────────────────────────────────────────────────────────────
MAX_TOKENS = 4                           # per Grok call
CONTRACT_POSTS, TALK_POSTS, ACCOUNT_POSTS = 8, 5, 3
TALK_MIN_FAVES = 3
NAME_MIN = 8                             # the name stands in for an unusable ticker only from this length
SHARED_MAX = 2                           # a ticker this many other recent tokens use finds them, not this one
ACCOUNT_EVERY = timedelta(hours=12)      # the account search: on a first read, then at most this often
EFFORT = "medium"                        # low dropped the post list; the default (high) cost the same per token
TIMEOUT = 420
MAX_CALLS_PER_DAY = 150                  # a runaway guard; the dollar pace is the real limit
SLACK = H                                # X's within_time is approximate
FUTURE = timedelta(minutes=10)
KOL_LIKES = 100                          # no follower counts in x_search: a post this liked marks a large account
MAX_NOTABLE, MAX_FLAGS = 3, 4
SYSTEM = ("You are the X Scout on a memecoin trading desk. You read X (Twitter) through your x_search tool and "
          "report only what the posts you retrieved say. Answer with the requested JSON only.")
GROK_ARGS = [SYSTEM if a == sp.SYSTEM else a for a in sp.GROK_ARGS] + ["--reasoning-effort", EFFORT]

# ─── what to read ────────────────────────────────────────────────────────────
MIN_LIQUIDITY = 10_000                   # dust is never read
NEW_CALL = timedelta(minutes=30)
OPENED = timedelta(minutes=10)           # watch.touch(): a trader opened the dossier
MIN_GAP = timedelta(minutes=20)          # never the same token twice within this
RESCAN_FAST, RESCAN, RESCAN_HELD = timedelta(minutes=30), timedelta(minutes=60), timedelta(hours=4)
TRENDING = timedelta(minutes=30)
VOLUME_RISE, VOLUME_MIN = 1.25, 5_000    # last 30 min vs the 30 before, in USD

# ─── pool ────────────────────────────────────────────────────────────────────
USD_PER_CALL = sp.USD_PER_CALL
USD_PER_TOKEN = sp.USD_PER_POST * (CONTRACT_POSTS + TALK_POSTS + ACCOUNT_POSTS)     # $0.08; measured $0.07-0.08
DEFAULT_WEEKLY_USD = 15.0
DEFAULT_POOL_USD = xsearch.POOL_USD      # 95% of the ~$42 week (Social Pulse's measurement)

# ─── sections ────────────────────────────────────────────────────────────────
FRESH = timedelta(minutes=60)
HEAT_MAX_AGE = timedelta(hours=3)
HISTORY = timedelta(hours=24)
SHOWN = ("read", "quiet")                # statuses that are evidence; ungrounded and failed rows are audit only

PROMPT = """Current time: {now} UTC.

These tokens are memecoins that trading groups called minutes to hours ago. Run exactly these X searches with
x_search, each once, with the mode and limit given; run them all before you answer (several at once if you can).
Run no other search.
{searches}

For each token, answer from its own searches only:
- posts: every post its searches returned that is about this token, each with the number of the search that
  returned it, the post's x.com status URL exactly as returned, its text as the result shows it (up to 280
  characters, never reworded), stance (bullish, bearish or neutral), kind (project:
  the token's own account; bot: automated alerts such as new pairs, buys, trending lists, "CA:" feeds; shill: paid
  or copy-pasted promotion, raid replies, "100x gem" spam; organic: a person's own take), well_known (true if the
  author is a large or well-known crypto account), and the like and follower counts when the result shows them
  (else null). A post about a different token with the same ticker is not about this token: leave it out and count
  it in other_token_posts.
- mood: integer -5..+5 for how the posts about it feel, weighting organic posts by real accounts over bots and
  shills: -5 capitulation or "rugged", -3 clearly bearish, -1 mildly bearish, 0 mixed or nothing said, +1 mildly
  bullish, +3 clearly bullish, +5 euphoric.
- summary: one plain sentence on what X is saying about it right now.
- why_it_matters: one sentence on what a trader should take from it, or "" if nothing stands out.
- notable: up to 3 of the most informative posts (a large or well-known account, a warning with evidence, the
  project's own news), each with author (@handle), followers, the post's text, its URL and stance.
- account: the project's own account when a search returned its posts: handle, followers, created (the join
  date if the result shows it, else null), name_changes (how many earlier handles it shows, else null); else null.
- red_flags: up to 4, only what the posts show, each with kind (paid_raid, copy_paste, dev_alt, recycled_handle,
  impersonation, rug_warning or other), one sentence of detail and the URL of a post that shows it.
Never invent a post or a URL. A token whose searches found nothing about it gets posts [], mood 0 and a summary
saying so."""


Stance = Literal["bullish", "bearish", "neutral"]


class SeenPost(BaseModel):
    search: int = Field(description="the number of the search that returned it")
    url: str = Field(description="the post's x.com status URL, exactly as returned")
    text: str = Field("", description="the post's text as the result shows it, up to 280 characters")
    stance: Stance
    kind: Literal["organic", "shill", "bot", "project"]
    well_known: bool = Field(False, description="the author is a large or well-known crypto account")
    likes: int | None = Field(None, description="the post's like count if the result shows it")
    followers: int | None = Field(None, description="the author's follower count if the result shows it")


class NotablePost(BaseModel):
    author: str
    followers: int | None = None
    text: str
    url: str
    stance: Stance


class Account(BaseModel):
    handle: str
    followers: int | None = None
    created: str | None = None
    name_changes: int | None = None


class RedFlag(BaseModel):
    kind: Literal["paid_raid", "copy_paste", "dev_alt", "recycled_handle", "impersonation", "rug_warning", "other"]
    detail: str
    url: str


class TokenRead(BaseModel):
    token: str = Field(description='exactly as given, e.g. "T1"')
    posts: list[SeenPost] = Field(description="every post its searches returned about this token")
    other_token_posts: int = 0
    mood: int = Field(ge=-5, le=5)
    summary: str
    why_it_matters: str = ""
    notable: list[NotablePost] = Field(default_factory=list)
    account: Account | None = None
    red_flags: list[RedFlag] = Field(default_factory=list)


class Scout(BaseModel):
    reads: list[TokenRead]


# ─── the searches ────────────────────────────────────────────────────────────
HANDLE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
NOT_HANDLES = {"i", "search", "intent", "home", "hashtag", "share", "explore", "messages", "notifications",
               "settings", "compose", "login", "signup", "tos", "privacy", "status"}
CASHTAG = re.compile(r"^[A-Za-z][A-Za-z0-9]{1,11}$")
POST_URL = re.compile(r"^https://(?:www\.)?(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status/(\d{15,20})(?:[/?#].*)?$")


def handle_from(link: str | None) -> str | None:
    """The account an X link names (x.com/<handle>, @handle or a bare handle). A post link (often the tweet a coin
    is named after), a community, a search or an intent link names no project account."""
    s = (link or "").strip()
    if HANDLE.match(s.removeprefix("@")):
        return s.removeprefix("@")
    u = urlparse(s if "://" in s else "https://" + s)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("mobile.")
    parts = [p for p in u.path.split("/") if p]
    if host not in {"x.com", "twitter.com"} or len(parts) != 1:
        return None
    h = parts[0].removeprefix("@")
    return h if HANDLE.match(h) and h.lower() not in NOT_HANDLES else None


def talk_query(t: dict, window: int, majors: set[str]) -> str | None:
    """The cashtag (or the quoted name when the ticker is a major coin's, is shared by SHARED_MAX or more other recent
    tokens, or can't be a cashtag) and @account."""
    sym, name, handle = (t.get("symbol") or "").strip(), (t.get("name") or "").replace('"', "").strip(), t.get("handle")
    terms = []
    if CASHTAG.match(sym) and sym.upper() not in majors and (t.get("shared_ticker") or 0) < SHARED_MAX:
        terms.append(f"${sym}")
    elif name and (not name.isascii() or (len(name) >= NAME_MIN and name.lower() != sym.lower())):
        terms.append(f'"{name}"')                        # a short Latin name is a common word; 招财猫 is not
    if handle:
        terms.append(f"@{handle}")
    if not terms:
        return None
    q = terms[0] if len(terms) == 1 else f"({' OR '.join(terms)})"
    return f"{q} min_faves:{TALK_MIN_FAVES} within_time:{window}h"


def window_h(t: dict, now: datetime) -> int:
    """Hours the contract and talk searches look back: since the last read (1-3 h), or on a first read the token's
    whole life (1-6 h), so the earliest posts about it are in reach."""
    if t.get("last_read"):
        return max(1, min(3, math.ceil((now - t["last_read"]) / H)))
    born = t.get("created_at") or t.get("first_call")
    return max(1, min(6, math.ceil((now - born) / H))) if born else 6


def wants_account(t: dict, now: datetime) -> bool:
    return bool(t.get("handle")) and (t.get("account_at") is None or now - t["account_at"] >= ACCOUNT_EVERY)


def plan(tokens: list[dict], majors: set[str]) -> list[dict]:
    """The numbered searches: [{n, token ("T1"..), facet, query, mode, limit, window_h (None: any age)}]."""
    out = []
    for i, t in enumerate(tokens, 1):
        tid, w = f"T{i}", t["window_h"]
        out.append({"token": tid, "facet": "contract", "query": f"{t['address']} within_time:{w}h", "mode": "Latest",
                    "limit": CONTRACT_POSTS, "window_h": w})
        if q := talk_query(t, w, majors):
            out.append({"token": tid, "facet": "talk", "query": q, "mode": "Top", "limit": TALK_POSTS, "window_h": w})
        if t.get("account_search", bool(t.get("handle"))):
            out.append({"token": tid, "facet": "account", "query": f"from:{t['handle']}", "mode": "Latest",
                        "limit": ACCOUNT_POSTS, "window_h": None})
    for n, s in enumerate(out, 1):
        s["n"] = n
    return out


def prompt(tokens: list[dict], searches: list[dict], now: datetime) -> str:
    lines = []
    for i, t in enumerate(tokens, 1):
        tid = f"T{i}"
        head = f'Token "{tid}": ' + (f"${t['symbol']}" if t.get("symbol") else "ticker unknown")
        if t.get("name") and t["name"] != t.get("symbol"):
            head += f' ("{t["name"]}")'
        head += f" on {t['chain']}, contract {t['address']}, "
        head += f"project account @{t['handle']}" if t.get("handle") else "no known project account"
        if t.get("shared_ticker"):
            head += (f"; {t['shared_ticker']} other recent tokens use this ticker, so a post counts only if it shows "
                     "this contract or is clearly about this project")
        lines.append(head)
        lines += [f'  {s["n"]}. query `{s["query"]}`, mode {s["mode"]}, limit {s["limit"]}'
                  for s in searches if s["token"] == tid]
    return PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"), searches="\n".join(lines))


# ─── the evidence gate and the arithmetic ────────────────────────────────────
def parse_post(url: str | None) -> tuple[str, int, datetime] | None:
    """(author handle, status id, time) of a real-shaped X post link; the time is in the id (snowflake)."""
    m = POST_URL.match(url or "")
    if not m:
        return None
    sid = int(m.group(2))
    return m.group(1), sid, datetime.fromtimestamp(((sid >> 22) + sp.TWITTER_EPOCH_MS) / 1000, UTC)


def in_window(at: datetime, started: datetime, finished: datetime, window: int | None) -> bool:
    """Not in the future, and inside its search's window (None: any age, the project's own timeline)."""
    return at <= finished + FUTURE and (window is None or at >= started - window * H - SLACK)


def judge(scout: Scout, tokens: list[dict], searches: list[dict], started: datetime, finished: datetime) -> list[dict]:
    """One row per token asked about, with the evidence gate applied and the numbers computed from the verified
    posts. status: read (verified posts) | quiet (its searches found nothing, and another token of the call proves
    the searches ran) | ungrounded (nothing verifiable)."""
    got: dict[str, TokenRead] = {}
    for r in scout.reads:
        got.setdefault(r.token.strip().strip('"'), r)
    rows = [_read(t, f"T{i}", got.get(f"T{i}"), searches, started, finished) for i, t in enumerate(tokens, 1)]
    if not any(r["status"] == "read" for r in rows):
        for r in rows:
            if r["status"] == "quiet":
                r["status"] = "ungrounded"
    return rows


def _read(t: dict, tid: str, r: TokenRead | None, searches: list[dict], started: datetime, finished: datetime) -> dict:
    mine = {s["n"]: s for s in searches if s["token"] == tid}
    row = {"chain": t["chain"], "address": t["address"], "symbol": t.get("symbol"), "picked_for": t.get("picked_for"),
           "window_h": t["window_h"], "searches": [s["query"] for s in mine.values()], "status": "ungrounded",
           "mood": None, "posts_h": None, "unique_authors": None, "posts_seen": 0,
           "summary": r.summary if r else None, "why": r.why_it_matters if r else None, "notable": [], "kols": [],
           "account": None, "red_flags": [], "shill_share": None, "first_mention": None, "posts": []}
    if r is None:
        return row
    posts, seen = [], set()
    for p in r.posts:
        s, parsed = mine.get(p.search), parse_post(p.url)
        if s is None or parsed is None or parsed[1] in seen:
            continue
        handle, sid, at = parsed
        if not in_window(at, started, finished, s["window_h"]):
            continue
        seen.add(sid)
        posts.append({"url": f"https://x.com/{handle}/status/{sid}", "author": "@" + handle, "at": at,
                      "facet": s["facet"], "stance": p.stance, "kind": p.kind, "well_known": p.well_known,
                      "likes": p.likes, "followers": p.followers, "text": " ".join(p.text.split())[:500] or None})
    if not posts:
        if not r.posts:                                  # its searches found nothing about it
            row.update(status="quiet", posts_h=0.0, unique_authors=0)
        return row
    about = [p for p in posts if p["facet"] != "account" and p["kind"] != "project"]
    kinds = [p["kind"] for p in about]
    kols: dict[str, dict] = {}
    for p in sorted(about, key=lambda p: -(p["likes"] or 0)):
        if p["kind"] != "bot" and (p["well_known"] or (p["likes"] or 0) >= KOL_LIKES):
            kols.setdefault(p["author"].lower(), {k: p[k] for k in ("author", "likes", "well_known", "stance", "url")})
    row.update(status="read", mood=r.mood, posts_h=posts_per_hour(posts, mine, started),
               unique_authors=len({p["author"].lower() for p in about}), posts_seen=len(posts),
               shill_share=round(sum(k in ("shill", "bot") for k in kinds) / len(kinds), 2) if kinds else None,
               kols=list(kols.values()), first_mention=first_mention(t, posts, mine, started),
               notable=_notable(r.notable, posts, t["window_h"], started, finished),
               account=_account(r.account, t.get("handle"), posts), red_flags=_flags(r.red_flags, finished),
               posts=posts)
    return row


def _saturated(posts: list[dict], searches: dict[int, dict]) -> bool:
    """The contract search returned its limit: the window held more posts than it showed."""
    limit = next((s["limit"] for s in searches.values() if s["facet"] == "contract"), CONTRACT_POSTS)
    return sum(p["facet"] == "contract" for p in posts) >= limit


def first_mention(t: dict, posts: list[dict], searches: dict[int, dict], started: datetime) -> datetime | None:
    """The earliest post about the token, known only when the searches' window reached back to its creation and the
    contract search showed the whole window; otherwise None (an older post may exist)."""
    born = t.get("created_at")
    mentions = [p["at"] for p in posts if p["facet"] != "account"]
    if not mentions or born is None or started - born > t["window_h"] * H or _saturated(posts, searches):
        return None
    return min(mentions)


def posts_per_hour(posts: list[dict], searches: dict[int, dict], started: datetime) -> float:
    """Posts about the token (its own account's aside) in the hour before the searches. The contract search asks for
    the latest posts: under its limit it returned the whole window, so the hour's count is exact; at its limit the
    window held more, and the rate is what it returned over the time those posts span."""
    about = [p for p in posts if p["facet"] != "account" and p["kind"] != "project"]
    rate = float(sum(p["at"] >= started - H for p in about))
    contract = [p for p in posts if p["facet"] == "contract"]
    if _saturated(posts, searches):
        span = max((started - min(p["at"] for p in contract)) / H, 1 / 12)
        rate = max(rate, sum(p["kind"] != "project" for p in contract) / span)
    return round(rate, 1)


def _notable(notes: list[NotablePost], posts: list[dict], window: int, started: datetime,
             finished: datetime) -> list[dict]:
    """The model's pick of notable posts, each a listed post or one inside the token's window."""
    listed = {parse_post(p["url"])[1]: p for p in posts}
    out = []
    for n in notes:
        parsed = parse_post(n.url)
        if parsed is None or (parsed[1] not in listed and not in_window(parsed[2], started, finished, window)):
            continue
        handle, sid, at = parsed
        followers = n.followers if n.followers is not None else (listed.get(sid) or {}).get("followers")
        out.append({"author": "@" + handle, "followers": followers, "text": n.text.strip()[:500],
                    "url": f"https://x.com/{handle}/status/{sid}", "at": at, "stance": n.stance})
    return out[:MAX_NOTABLE]


def _account(a: Account | None, known: str | None, posts: list[dict]) -> dict | None:
    """The project's account: the one we searched, or one whose own posts the searches returned; never a name the
    model offers without either."""
    own = {p["author"][1:].lower() for p in posts if p["kind"] == "project" or p["facet"] == "account"}
    h = ((a.handle if a else known) or "").strip().removeprefix("@")
    if not h or (h.lower() not in own and (a is None or h.lower() != (known or "").lower())):
        return None
    last = max((p["at"] for p in posts if p["author"][1:].lower() == h.lower()), default=None)
    return {"handle": "@" + h, "followers": a.followers if a else None, "created": a.created if a else None,
            "name_changes": a.name_changes if a else None, "last_post": last}


def _flags(flags: list[RedFlag], finished: datetime) -> list[dict]:
    out = []
    for f in flags:
        parsed = parse_post(f.url)
        if parsed and parsed[2] <= finished + FUTURE:
            out.append({"kind": f.kind, "detail": f.detail.strip(), "url": f"https://x.com/{parsed[0]}/status/{parsed[1]}"})
    return out[:MAX_FLAGS]


# ─── pool ────────────────────────────────────────────────────────────────────
def allowance(weekly: float, pool: float, social: float) -> float:
    """The scout's week: its own setting, never more than the pool leaves after Social Pulse's allowance."""
    return max(0.0, min(weekly, pool - social))


def pace_hours(weekly: float) -> float:
    """The window the pace is kept over: an hour, or as long as the pace takes to fund one read (a small allowance
    reads now and then instead of never)."""
    hourly = weekly / 7 / 24
    return 168.0 if hourly <= 0 else min(168.0, max(1.0, (USD_PER_CALL + USD_PER_TOKEN) / hourly))


def run_budget(spent_w: float, spent_24h: float, spent_7d: float, weekly: float) -> float:
    """Dollars this run may spend, Social Pulse's pace: the share of the last pace_hours (spent_w is the spend over
    them), plus up to twice that when the last 24 h ran under pace (calls come in bursts); never past 1.5x the daily
    share in 24 h or the week in 7 days."""
    daily = weekly / 7
    share = daily / 24 * pace_hours(weekly)
    share += max(0.0, min((daily - spent_24h) / 4, 2 * share))
    return min(share - spent_w, 1.5 * daily - spent_24h, weekly - spent_7d)


def pool_room(pool_24h: float, pool_7d: float, pool: float) -> float:
    """What the whole pool (Social Pulse + the scout, as spent) still allows: 1.5x its daily share, and the week."""
    return min(1.5 * pool / 7 - pool_24h, pool - pool_7d)


def affordable(usd: float) -> int:
    """Tokens one call can carry on `usd`."""
    return max(0, min(MAX_TOKENS, int((usd - USD_PER_CALL) // USD_PER_TOKEN)))


def weekly_usd() -> float:
    return xsearch.week(env_float("DEEP_X_WEEKLY_USD", DEFAULT_WEEKLY_USD))


def pool_usd() -> float:
    return xsearch.week(xsearch.pool_week())


def social_usd() -> float:
    """Social Pulse's week, which the scout's never takes, before its budget scale."""
    return xsearch.week(settings.social_weekly_usd)


SPEND_SQL = """
SELECT coalesce(sum(c) FILTER (WHERE agent = $1 AND started_at > now() - $3::interval), 0) AS own_w,
       coalesce(sum(c) FILTER (WHERE agent = $1 AND started_at > now() - interval '24 hours'), 0) AS own_24h,
       coalesce(sum(c) FILTER (WHERE agent = $1), 0) AS own_7d,
       coalesce(sum(c) FILTER (WHERE started_at > now() - interval '24 hours'), 0) AS pool_24h,
       coalesce(sum(c), 0) AS pool_7d
FROM (SELECT agent, started_at, {cost} AS c FROM agent_runs
      WHERE agent = ANY($2::text[]) AND started_at > now() - interval '7 days') x"""


async def spend(window: timedelta = H) -> dict[str, float]:
    """The pool's spend from agent_runs: the scout's over `window` / 24 h / 7 d, and both agents' over 24 h / 7 d."""
    pool = await db.pool()
    sql = SPEND_SQL.format(cost=xsearch.spend_sql())          # the lane's own dollars
    return {k: float(v) for k, v in dict(await pool.fetchrow(sql, AGENT, POOL_AGENTS, window)).items()}


async def budget() -> tuple[float, dict]:
    pool = pool_usd()
    own = allowance(weekly_usd() * await controls.scale(AGENT), pool, social_usd() * await controls.scale("social_pulse"))
    s = await spend(timedelta(hours=pace_hours(own)))
    return min(run_budget(s["own_w"], s["own_24h"], s["own_7d"], own), pool_room(s["pool_24h"], s["pool_7d"], pool)), s


_exhausted: tuple[float, bool] = (0.0, False)


async def exhausted() -> bool:
    """The X allowance is used up for now: not one token read fits the scout's day or week, or the pool's
    (the hourly pace aside). Cached a minute: the dossier asks for every token."""
    global _exhausted
    if time.monotonic() - _exhausted[0] < 60:
        return _exhausted[1]
    s, pool = await spend(), pool_usd()
    own = allowance(weekly_usd() * await controls.scale(AGENT), pool, social_usd() * await controls.scale("social_pulse"))
    room = min(1.5 * own / 7 - s["own_24h"], own - s["own_7d"], pool_room(s["pool_24h"], s["pool_7d"], pool))
    _exhausted = (time.monotonic(), room < USD_PER_CALL + USD_PER_TOKEN)
    return _exhausted[1]


# ─── what to read ────────────────────────────────────────────────────────────
def calls_rising(t: dict) -> bool:
    n = t.get("calls_1h") or 0
    return n >= 2 and n >= (t.get("calls_prev_1h") or 0)


def volume_rising(t: dict) -> bool:
    v, prev = t.get("vol_recent"), t.get("vol_prev")
    return v is not None and v >= VOLUME_MIN and (not prev or v >= VOLUME_RISE * prev)


def priority(t: dict, now: datetime) -> tuple[float, str] | None:
    """(score, why) if the token needs an X read now, higher first: a group call under 30 minutes old that was never
    read (3-4, the newest first), a dossier a trader just opened (2.5), a hot token whose calls or volume keep rising
    or that is trending, when never read or read 30-60 minutes ago (1-2, the most overdue first), then one the bot
    holds, every 4 hours (0.5-1). A hot token with nothing happening waits. Never under MIN_LIQUIDITY (or not
    priced yet), never twice within MIN_GAP."""
    last = t.get("last_attempt")
    if last is not None and now - last < MIN_GAP:
        return None
    if (t.get("liquidity") or 0) < MIN_LIQUIDITY:
        return None
    read = t.get("last_read")
    age = now - t["first_call"] if t.get("first_call") else None
    if age is not None and age < NEW_CALL and read is None:
        return 3 + (1 - age / NEW_CALL), "new call"
    if t.get("viewed_at") and now - t["viewed_at"] < OPENED and (read is None or now - read >= RESCAN_FAST):
        return 2.5, "opened"
    if t.get("tier") != "hot":
        return None
    calls, volume = calls_rising(t), volume_rising(t)
    trending = bool(t.get("trending_at") and now - t["trending_at"] < TRENDING)
    if calls or volume or trending:
        due, floor = RESCAN_FAST if (calls and volume) or trending else RESCAN, 1.0
        why = "trending" if trending else "calls and volume rising" if calls and volume else \
            "calls rising" if calls else "volume rising"
    elif t.get("held"):                                  # the bot holds dozens: a slow lane, after everything else
        due, floor, why = RESCAN_HELD, 0.5, "bot holds it"
    else:
        return None                                      # hot but flat: nothing new to read
    if read is not None and now - read < due:
        return None
    return floor + floor * (min((now - read) / due, 3.0) if read else 3.0) / 3, why


def pick(cands: list[dict], now: datetime, n: int) -> list[dict]:
    ranked = []
    for t in cands:
        if p := priority(t, now):
            ranked.append({**t, "priority": round(p[0], 3), "picked_for": p[1]})
    ranked.sort(key=lambda t: -t["priority"])
    return ranked[:n]


CANDIDATES_SQL = """
WITH k AS (SELECT * FROM unnest($1::text[], $2::text[]) AS k(chain, address))
SELECT k.chain, k.address, s.symbol, s.name, s.liquidity_usd AS liquidity, s.created_at,
       r.last_attempt, r.last_read, a.handle, a.account_at, v.vol_recent, v.vol_prev,
       (SELECT count(*) FROM micro_calls c JOIN micro_tokens t ON t.chain = c.chain AND t.address = c.address
        WHERE c.address = k.address AND coalesce(t.resolved_chain, t.chain) = k.chain
          AND c.called_at > now() - interval '2 hours' AND c.called_at <= now() - interval '1 hour') AS calls_prev_1h
FROM k
LEFT JOIN micro_state s ON s.chain = k.chain AND s.token = k.address
LEFT JOIN LATERAL (SELECT max(read_at) AS last_attempt,
                          max(read_at) FILTER (WHERE status IN ('read', 'quiet')) AS last_read
                   FROM de_x_reads x WHERE x.chain = k.chain AND x.address = k.address) r ON TRUE
LEFT JOIN LATERAL (SELECT ltrim(account->>'handle', '@') AS handle, read_at AS account_at FROM de_x_reads x
                   WHERE x.chain = k.chain AND x.address = k.address AND x.account IS NOT NULL
                   ORDER BY read_at DESC LIMIT 1) a ON TRUE
LEFT JOIN LATERAL (SELECT sum(volume_usd) FILTER (WHERE ts > now() - interval '35 minutes') AS vol_recent,
                          sum(volume_usd) FILTER (WHERE ts <= now() - interval '35 minutes') AS vol_prev
                   FROM micro_candles_1m m WHERE m.chain = k.chain AND m.token = k.address
                     AND m.ts > now() - interval '65 minutes' AND m.ts <= now() - interval '5 minutes') v ON TRUE"""


async def candidates() -> list[dict]:
    """Every hot token (watch.tokens) with what `priority` needs: liquidity, the last reads, the calls in the hour
    before this one, and volume over the last two half hours."""
    hot = await watch.tokens("hot")
    if not hot:
        return []
    pool = await db.pool()
    extra = {(r["chain"], r["address"]): dict(r) for r in await pool.fetch(
        CANDIDATES_SQL, [t["chain"] for t in hot], [t["address"] for t in hot])}
    out = []
    for t in hot:
        e = extra.get((t["chain"], t["address"]), {})
        out.append({**t, **{k: v for k, v in e.items() if v is not None or k not in t},
                    "vol_recent": _f(e.get("vol_recent")), "vol_prev": _f(e.get("vol_prev"))})
    return out


def _f(v) -> float | None:
    return float(v) if v is not None else None


async def attach(picks: list[dict], now: datetime) -> None:
    """What the prompt needs beyond the candidate row: the project's account (our last read's, else DexScreener's
    socials, else Jupiter's), whether the account search is due, how many recent tokens share the ticker, the window."""
    for chain in {t["chain"] for t in picks}:
        need = [t for t in picks if t["chain"] == chain and not t.get("handle")]
        if need:
            found = await _dexscreener_handles(chain, [t["address"] for t in need])
            for t in need:
                t["handle"] = found.get(t["address"].lower()) or (
                    await _jupiter_handle(chain, t["address"]) if chain == "solana" else None)
    pool = await db.pool()
    shared = {r["s"]: r["n"] for r in await pool.fetch(
        """SELECT upper(symbol) AS s, count(*) AS n FROM micro_state WHERE upper(symbol) = ANY($1::text[])
           AND (created_at IS NULL OR created_at > now() - interval '14 days') GROUP BY 1""",
        [t["symbol"].upper() for t in picks if t.get("symbol")])}
    for t in picks:
        t["shared_ticker"] = max(0, shared.get((t.get("symbol") or "").upper(), 0) - 1)
        t["window_h"] = window_h(t, now)
        t["account_search"] = wants_account(t, now)


async def _dexscreener_handles(chain: str, addresses: list[str]) -> dict[str, str | None]:
    try:
        pairs, _ = await dexscreener.tokens(chain, addresses)
    except Exception as e:                               # the read goes ahead without an account
        log.warning("%s: DexScreener socials for %s failed: %s", AGENT, chain, e)
        return {}
    out: dict[str, str | None] = {}
    for p in pairs:
        links = [s.get("url") for s in (p.get("info") or {}).get("socials") or [] if s.get("type") == "twitter"]
        out.setdefault(((p.get("baseToken") or {}).get("address") or "").lower(),
                       next((h for h in map(handle_from, links) if h), None))
    return out


async def _jupiter_handle(chain: str, address: str) -> str | None:
    """The X link in builder A's Jupiter intel (albatross.deepend.chain.jupiter_intel), when it has one."""
    try:
        fn = getattr(importlib.import_module("albatross.deepend.chain"), "jupiter_intel", None)
        if fn is None:
            return None
        r = fn(chain, address)
        if inspect.isawaitable(r):
            r = await r
        if not isinstance(r, dict):
            return None
        links = r.get("links") if isinstance(r.get("links"), dict) else {}
        return handle_from(r.get("twitter") or links.get("x") or links.get("twitter"))
    except Exception as e:
        log.warning("%s: Jupiter intel for %s failed: %s", AGENT, address, e)
        return None


async def majors() -> set[str]:
    """Tickers of the coins Albatross tracks: a new token's cashtag that collides with one is useless on X."""
    pool = await db.pool()
    return {r["s"] for r in await pool.fetch("SELECT DISTINCT upper(symbol) AS s FROM assets WHERE active")}


# ─── storage ─────────────────────────────────────────────────────────────────
COLS = ["chain", "address", "read_at", "run_id", "status", "symbol", "picked_for", "window_h", "mood", "posts_h",
        "unique_authors", "posts_seen", "summary", "why", "notable", "kols", "account", "red_flags", "shill_share",
        "first_mention", "posts", "searches"]


def _j(v) -> str | None:
    return None if v is None else orjson.dumps(v).decode()


async def store(rows: list[dict], run_id: int | None, read_at: datetime) -> int:
    recs = [(r["chain"], r["address"], read_at, run_id, r["status"], r.get("symbol"), r.get("picked_for"),
             r.get("window_h"), r.get("mood"), r.get("posts_h"), r.get("unique_authors"), r.get("posts_seen", 0),
             r.get("summary"), r.get("why"), _j(r.get("notable") or []), _j(r.get("kols") or []), _j(r.get("account")),
             _j(r.get("red_flags") or []), r.get("shill_share"), r.get("first_mention"), _j(r.get("posts") or []),
             _j(r.get("searches") or [])) for r in rows]
    return await db.upsert("de_x_reads", COLS, recs, ["chain", "address", "read_at"])


# ─── the job ─────────────────────────────────────────────────────────────────
_grok = asyncio.Lock()          # the scan and the asked reads take turns: one Grok call at a time (one host process)


async def scan() -> int:
    """One X Scout pass: the tokens that most need a read and the pool affords, in one Grok call. -> reads stored.
    An asked read in progress goes first (it takes a minute or two)."""
    async with _grok:
        return await _scan()


async def _scan() -> int:
    now = datetime.now(UTC)
    usd, s = await budget()
    n = affordable(usd)
    if n < 1:
        log.info("%s: $%.3f to spend this run (scout $%.2f today, pool $%.2f this week); no read fits", AGENT, usd,
                 s["own_24h"], s["pool_7d"])
        return 0
    picks = pick(await candidates(), now, n)
    if not picks:
        return 0
    await attach(picks, now)
    searches = plan(picks, await majors())
    started = datetime.now(UTC)
    try:
        scout, run_id = await xsearch.structured(AGENT, Scout, prompt(picks, searches, started), system=SYSTEM,
                                                 timeout=TIMEOUT, max_calls_per_day=MAX_CALLS_PER_DAY,
                                                 grok_args=GROK_ARGS, effort=EFFORT)
    except cli.CallLimitExceeded as e:
        log.warning("%s", e)
        return 0
    except RuntimeError:                                 # logged in agent_runs; the tokens wait MIN_GAP, not forever
        await store([{**t, "status": "failed", "searches": [s["query"] for s in searches if s["token"] == f"T{i}"]}
                     for i, t in enumerate(picks, 1)], None, started)
        raise
    rows = judge(scout, picks, searches, started, datetime.now(UTC))
    await store(rows, run_id, started)
    shown = [r for r in rows if r["status"] in SHOWN]
    if not any(r["status"] == "read" for r in rows):
        await (await db.pool()).execute(
            "UPDATE agent_runs SET ok = FALSE, error = $2 WHERE id = $1", run_id,
            f"ungrounded: none of {len(rows)} tokens cites a verifiable recent X post")
    log.info("%s: %d of %d tokens read (%s)", AGENT, len(shown), len(rows),
             ", ".join(f"{r['symbol']} {r['status']}" for r in rows))
    return len(shown)


# ─── what the dossier and the feed read ──────────────────────────────────────
MOODS = ((-4, "very bearish"), (-2, "bearish"), (-1, "leaning bearish"), (0, "mixed"), (1, "leaning bullish"),
         (3, "bullish"))


def mood_label(m: int | None) -> str | None:
    if m is None:
        return None
    return next((label for top, label in MOODS if m <= top), "euphoric")


def attention(posts_h: float | None) -> str | None:
    """The feed's word for how much X is posting about a token (posts in the last hour)."""
    if posts_h is None:
        return None
    if posts_h <= 0:
        return "silent"
    return next((label for top, label in ((10, "quiet"), (30, "steady"), (100, "warm")) if posts_h < top), "hot")


EMPTY = {"as_of": None, "mood": None, "mood_label": None, "posts_h": None, "unique_authors": None, "summary": None,
         "why_it_matters": None, "notable": [], "kols": [], "account": None, "history": [], "red_flags": [],
         "shill_share": None, "first_mention": None}


def _load(v):
    return orjson.loads(v) if isinstance(v, str | bytes) else v


def shape(rows: list[dict], now: datetime, out_of_budget: bool) -> dict:
    """The dossier's "x" section (docs/SPEC-DEEP-END.md section 4) from a token's reads, newest first. Beyond the
    contract: red_flags, shill_share and first_mention (the earliest post any read saw)."""
    if not rows:
        return {"status": "budget" if out_of_budget else "none", **EMPTY}
    latest = rows[0]
    fresh = now - latest["read_at"] <= FRESH
    account = next((_load(r["account"]) for r in rows if r.get("account")), None)
    if account:
        account = {k: account.get(k) for k in ("handle", "followers", "created", "name_changes", "last_post")}
    notable = [{"author": n["author"], "followers": n.get("followers"), "text": n["text"], "url": n["url"],
                "at": parse_post(n["url"])[2] if parse_post(n["url"]) else None, "stance": n["stance"]}
               for n in _load(latest.get("notable")) or []]
    return {
        "status": "fresh" if fresh else "budget" if out_of_budget else "stale",
        "as_of": latest["read_at"], "mood": latest["mood"], "mood_label": mood_label(latest["mood"]),
        "posts_h": latest["posts_h"], "unique_authors": latest["unique_authors"], "summary": latest["summary"],
        "why_it_matters": latest["why"] or "", "notable": notable,
        "kols": [k["author"] for k in _load(latest.get("kols")) or []], "account": account,
        "history": [{"ts": r["read_at"], "mood": r["mood"], "posts_h": r["posts_h"]}
                    for r in reversed(rows) if latest["read_at"] - r["read_at"] <= HISTORY],
        "red_flags": _load(latest.get("red_flags")) or [], "shill_share": latest.get("shill_share"),
        "first_mention": min((r["first_mention"] for r in rows if r.get("first_mention")), default=None),
    }


SECTION_SQL = f"""
SELECT read_at, status, mood, posts_h, unique_authors, summary, why, notable, kols, account, red_flags, shill_share,
       first_mention
FROM de_x_reads WHERE chain = $1 AND address = $2 AND status IN {SHOWN} ORDER BY read_at DESC LIMIT 100"""


async def section_x(chain: str, address: str) -> dict | None:
    """The dossier's "x" section: status fresh (read in the last hour) | stale | none (never read) | budget (not
    read, or stale, because the X allowance is used up for now). Plus the latest "Search X now" (ask: ask_state) and
    whether the scout's own picks skip it for liquidity (below_floor, floor_usd)."""
    pool = await db.pool()
    try:
        rows = [dict(r) for r in await pool.fetch(SECTION_SQL, chain, address)]
    except asyncpg.UndefinedTableError:                  # before migration 022
        rows = []
    out = shape(rows, datetime.now(UTC), await exhausted())
    try:
        liq = await pool.fetchval("SELECT liquidity_usd FROM micro_state WHERE chain = $1 AND token = $2", chain, address)
    except asyncpg.UndefinedTableError:
        liq = None
    return {**out, "ask": await ask_state(chain, address), "below_floor": float(liq or 0) < MIN_LIQUIDITY,
            "floor_usd": MIN_LIQUIDITY}


HEAT_SQL = f"""
SELECT DISTINCT ON (x.chain, x.address) x.chain, x.address, x.read_at, x.mood, x.posts_h
FROM de_x_reads x JOIN unnest($1::text[], $2::text[]) AS k(chain, address)
  ON x.chain = k.chain AND x.address = k.address
WHERE x.status IN {SHOWN} AND x.read_at > now() - $3::interval
ORDER BY x.chain, x.address, x.read_at DESC"""


async def heat(keys: list[tuple[str, str]]) -> dict:
    """The feed's X heat, one query for many tokens: {(chain, address): {mood, posts_h, label, as_of}} for tokens
    read in the last 3 hours (older reads are not "now")."""
    if not keys:
        return {}
    pool = await db.pool()
    try:
        rows = await pool.fetch(HEAT_SQL, [k[0] for k in keys], [k[1] for k in keys], HEAT_MAX_AGE)
    except asyncpg.UndefinedTableError:
        return {}
    return {(r["chain"], r["address"]): {"mood": r["mood"], "posts_h": r["posts_h"], "label": attention(r["posts_h"]),
                                         "as_of": r["read_at"]} for r in rows}


def compact(n: int | None) -> str | None:
    if n is None:
        return None
    if n >= 1e6:
        return f"{n / 1e6:.1f}M"
    return f"{n / 1e3:.0f}k" if n >= 1e4 else f"{n / 1e3:.1f}k" if n >= 1e3 else str(n)


def marker_label(n: dict) -> str:
    """'@author (48k followers): 'first words of the post'' as the chart shows it."""
    text = " ".join(n["text"].split())
    text = text if len(text) <= 70 else text[:69].rstrip() + "…"
    who = f"{n['author']} ({compact(n['followers'])} followers)" if n.get("followers") else n["author"]
    return f"{who}: '{text}'"


MARKERS_SQL = """
SELECT read_at, posts, notable, red_flags FROM de_x_reads WHERE chain = $1 AND address = $2 AND status = 'read'
  AND read_at >= $3::timestamptz AND read_at <= $4::timestamptz + interval '7 hours' ORDER BY read_at"""
FLAG_WORD = {"paid_raid": "Paid raid", "copy_paste": "Copy-paste posts", "dev_alt": "Dev alt account",
             "recycled_handle": "Recycled handle", "impersonation": "Impersonation", "rug_warning": "Rug warning",
             "other": "X warning"}


def post_markers(rows: list[dict], start: datetime, end: datetime) -> list[dict]:
    """Every verified post between start and end, once (the newest read's view of it, with its text: a notable pick's,
    else the post's own, kept since 2026-10-06), and each X red flag: [{t, kind: "x" | "xflag", label, price: None,
    id, detail}]."""
    posts: dict[int, dict] = {}
    texts: dict[int, dict] = {}
    flags: dict[str, dict] = {}
    for r in rows:
        for key, into in (("posts", posts), ("notable", texts)):
            for p in _load(r.get(key)) or []:
                parsed = parse_post(p.get("url"))
                if parsed and start <= parsed[2] <= end:
                    into[parsed[1]] = {**p, "handle": parsed[0], "at": parsed[2]}
        for f in _load(r.get("red_flags")) or []:
            parsed = parse_post(f.get("url"))
            if parsed and start <= parsed[2] <= end:
                flags.setdefault(f"{f.get('kind')}:{parsed[1]}", {**f, "handle": parsed[0], "at": parsed[2]})
    out = []
    for sid in sorted(posts.keys() | texts.keys()):
        p, n = posts.get(sid) or {}, texts.get(sid) or {}
        b = p or n
        detail = {"author": "@" + b["handle"], "url": f"https://x.com/{b['handle']}/status/{sid}",
                  "stance": p.get("stance") or n.get("stance"), "type": p.get("kind"), "likes": p.get("likes"),
                  "followers": n.get("followers") or p.get("followers"), "well_known": bool(p.get("well_known")),
                  "facet": p.get("facet"), "text": n.get("text") or p.get("text") or None}
        label = marker_label({"author": detail["author"], "followers": detail["followers"], "text": detail["text"]}) \
            if detail["text"] else detail["author"]
        out.append({"t": b["at"], "kind": "x", "label": label, "price": None, "id": str(sid), "detail": detail})
    for k, f in flags.items():
        out.append({"t": f["at"], "kind": "xflag", "label": FLAG_WORD.get(f.get("kind"), FLAG_WORD["other"]),
                    "price": None, "id": k, "detail": {"kind": f.get("kind"), "detail": f.get("detail"),
                                                      "url": f"https://x.com/{f['handle']}/status/{k.split(':')[1]}",
                                                      "author": "@" + f["handle"]}})
    return sorted(out, key=lambda m: m["t"])


async def markers(chain: str, address: str, start: datetime, end: datetime) -> list[dict]:
    """The chart's X markers between start and end (post_markers)."""
    pool = await db.pool()
    try:
        rows = [dict(r) for r in await pool.fetch(MARKERS_SQL, chain, address, start, end)]
    except asyncpg.UndefinedTableError:
        return []
    return post_markers(rows, start, end)


# ─── asked reads: "Search X now" on a dossier ────────────────────────────────
ASK_JOB = "deep_x_ask"
ASK_WAIT = timedelta(minutes=30)        # an ask not read by then is closed as expired (the host was down)
ASK_AGAIN = timedelta(minutes=2)        # a press this soon after the token's last read is refused (a double click)
ASK_KEEP = timedelta(hours=24)          # how long an ask's outcome shows on the dossier


async def ask_room() -> float:
    """What an asked read may spend: what the scout's week and the pool's week have left (the hourly pace and the
    daily cap are for the scout's own picks)."""
    s, pool = await spend(), pool_usd()
    own = allowance(weekly_usd() * await controls.scale(AGENT), pool,
                    social_usd() * await controls.scale("social_pulse"))
    return min(own - s["own_7d"], pool - s["pool_7d"])


ASK_STATE_SQL = """
SELECT asked_at, done_at, status, note FROM de_x_asks WHERE chain = $1 AND address = $2 AND asked_at > now() - $3::interval
ORDER BY asked_at DESC LIMIT 1"""


async def ask_state(chain: str, address: str) -> dict | None:
    """The token's latest ask in the last day: {asked_at, done_at, status, note}, or None."""
    pool = await db.pool()
    try:
        r = await pool.fetchrow(ASK_STATE_SQL, chain, address, ASK_KEEP)
    except asyncpg.UndefinedTableError:                  # before migration 040
        return None
    return dict(r) if r else None


async def last_attempt(chain: str, address: str) -> datetime | None:
    pool = await db.pool()
    return await pool.fetchval("SELECT max(read_at) FROM de_x_reads WHERE chain = $1 AND address = $2", chain, address)


async def ask(chain: str, address: str) -> dict:
    """Queue a read someone asked for on a dossier: the waiting ask when there is one, else a new one. -> ask_state."""
    pool = await db.pool()
    waiting = await pool.fetchval(
        "SELECT 1 FROM de_x_asks WHERE chain = $1 AND address = $2 AND done_at IS NULL AND asked_at > now() - $3::interval",
        chain, address, ASK_WAIT)
    if not waiting:
        await pool.execute("INSERT INTO de_x_asks (chain, address) VALUES ($1, $2)", chain, address)
    return await ask_state(chain, address)


ASK_TOKENS_SQL = """
SELECT chain, address, snapshot->'token'->>'symbol' AS symbol, snapshot->'token'->>'name' AS name,
       (snapshot->'token'->>'created_at')::timestamptz AS created_at
FROM de_dossier WHERE (chain, address) IN (SELECT * FROM unnest($1::text[], $2::text[]))"""


async def ask_tokens(keys: list[tuple[str, str]]) -> list[dict]:
    """The asked tokens as the prompt needs them: candidates()'s fields (the dossier's symbol, name and age when the
    price pipeline has none), whatever their tier or liquidity."""
    pool = await db.pool()
    chains, addrs = [k[0] for k in keys], [k[1] for k in keys]
    found = {(r["chain"], r["address"]): dict(r) for r in await pool.fetch(CANDIDATES_SQL, chains, addrs)}
    doss = {(r["chain"], r["address"]): dict(r) for r in await pool.fetch(ASK_TOKENS_SQL, chains, addrs)}
    out = []
    for k in keys:
        d, e = doss.get(k, {}), found.get(k, {})
        out.append({**{f: d.get(f) for f in ("symbol", "name", "created_at")},
                    **{f: v for f, v in e.items() if v is not None}, "chain": k[0], "address": k[1],
                    "picked_for": "asked"})
    return out


async def _close(keys: list[tuple[str, str]], status: str, note: str | None = None) -> None:
    pool = await db.pool()
    await pool.execute(
        """UPDATE de_x_asks a SET done_at = now(), status = $3, note = $4
           FROM unnest($1::text[], $2::text[]) AS k(chain, address)
           WHERE a.chain = k.chain AND a.address = k.address AND a.done_at IS NULL""",
        [k[0] for k in keys], [k[1] for k in keys], status, note)


async def asks() -> int:
    """deep_x_ask, every minute where the X lane runs: the reads asked for on dossiers, the oldest first, in one Grok call
    of their own. A token the scan read since it was asked counts as answered; asks the week can't fund are refused.
    -> reads."""
    pool = await db.pool()
    try:
        await pool.execute(
            """UPDATE de_x_asks SET done_at = now(), status = 'expired', note = 'not read within 30 minutes'
               WHERE done_at IS NULL AND asked_at <= now() - $1::interval""", ASK_WAIT)
        waiting = await pool.fetch(
            """SELECT w.*, x.read_at, x.status AS read_status
               FROM (SELECT chain, address, min(asked_at) AS asked_at FROM de_x_asks WHERE done_at IS NULL
                     GROUP BY 1, 2) w
               LEFT JOIN LATERAL (SELECT read_at, status FROM de_x_reads x WHERE x.chain = w.chain AND x.address = w.address
                                  ORDER BY read_at DESC LIMIT 1) x ON TRUE
               ORDER BY w.asked_at""")
    except asyncpg.UndefinedTableError:                  # before migration 040
        return 0
    if not waiting or _grok.locked():                    # the scan is reading: next minute
        return 0
    async with _grok:
        answered = [(r["chain"], r["address"]) for r in waiting if r["read_at"] and r["read_at"] >= r["asked_at"]]
        for r in waiting:                                # the scan read it since: that read is the answer
            if (r["chain"], r["address"]) in answered:
                await _close([(r["chain"], r["address"])], r["read_status"], "read by the X Scout's own pass")
        keys = [(r["chain"], r["address"]) for r in waiting if (r["chain"], r["address"]) not in answered]
        if not keys:
            return 0
        n = min(MAX_TOKENS, max(0, int((await ask_room() - USD_PER_CALL) // USD_PER_TOKEN)))
        if n < 1:
            await _close(keys, "refused", "this week's X budget is spent")
            return 0
        picks, now = await ask_tokens(keys[:n]), datetime.now(UTC)
        await attach(picks, now)
        searches = plan(picks, await majors())
        started = datetime.now(UTC)
        done = [(t["chain"], t["address"]) for t in picks]
        try:
            scout, run_id = await xsearch.structured(AGENT, Scout, prompt(picks, searches, started), system=SYSTEM,
                                                     timeout=TIMEOUT, max_calls_per_day=MAX_CALLS_PER_DAY,
                                                     grok_args=GROK_ARGS, effort=EFFORT)
        except cli.CallLimitExceeded as e:
            await _close(done, "refused", "the X Scout's daily limit is reached")
            log.warning("%s: %s", ASK_JOB, e)
            return 0
        except RuntimeError:
            await store([{**t, "status": "failed", "searches": [s["query"] for s in searches if s["token"] == f"T{i}"]}
                         for i, t in enumerate(picks, 1)], None, started)
            await _close(done, "failed", "the Grok call failed")
            raise
        rows = judge(scout, picks, searches, started, datetime.now(UTC))
        await store(rows, run_id, started)
        for r in rows:
            await _close([(r["chain"], r["address"])], r["status"])
        log.info("%s: %s", ASK_JOB, ", ".join(f"{r['symbol']} {r['status']}" for r in rows))
        return sum(r["status"] in SHOWN for r in rows)


# ─── registry (albatross/deepend/registry.py) ────────────────────────────────
JOBS: dict = {}
SCHEDULE: list = []
HOST_JOBS = {AGENT: scan, ASK_JOB: asks}
HOST_SCHEDULE = [(AGENT, {"minute": "*/5", "second": 30}), (ASK_JOB, {"second": 5})]
CADENCE = {AGENT: 300, ASK_JOB: 60}
