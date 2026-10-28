"""Social Pulse: what crypto X is saying, via Grok's X search (the Grok Build CLI on a SuperGrok subscription, or the
xAI API).

Every hour, one lean Grok call runs a fixed list of X searches:
- the market pulse: the most-engaged crypto posts of the last hour -> market mood, the headlines a
  trader should know (each tied to a post) and the coins X is suddenly talking about;
- the memecoin trenches (for The Deep End): the most-engaged memecoin posts of the hour -> the tickers
  running and the narratives they ride, each tied to a post;
- one search per coin, for the few coins that matter right now (`pick`): Tier-1 coins as they come
  due, plus any coin that is moving hard, trending in the last pulse, in an open trade or in fresh
  high-impact news.
Each coin read says in one sentence what X is saying, why it matters (or that nothing does), a -5..+5
mood, chatter, crowding, positioning, catalysts and the two most informative posts, all checked
against the post ids (a read without a verifiable recent post is dropped).

Cost (measured 2026-09-30): X search is billed per post returned, about $0.005 of the pool each, plus
~$0.01-0.02 a call in tokens. The call used to cost 3-4x what it needed: Grok searched every coin twice
(once without a time filter, thrown away), looked up the time with a web search, and carried 21 unused
client tools and a coding-agent system prompt. Now the searches are spelled out, X search is the only
tool, and the spend is paced against the measured allowance (`budget`) rather than a call count.
Runs on the host (where Grok is logged in), or in the ingest container through the xAI API with your key, which
you pick on the Setup page (llm/xsearch.py). On the API the same searches cost real dollars at about the same
rate ($5 per 1,000 posts), paced to this agent's share of settings.xai_daily_usd.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Literal

import orjson
from pydantic import BaseModel, Field

from albatross import controls, db
from albatross.agents import analysts
from albatross.config import settings
from albatross.features.engine import HORIZON_BARS, TFS
from albatross.llm import cli, xsearch

log = logging.getLogger(__name__)

AGENT = "social_pulse"
MARKET = "market"                       # social_snapshots scope of the market pulse row
# The SuperGrok pool is measured in its own dollar equivalent; one plan's week measured about $42 (measure your own:
# the share of the week each call uses, against its total_cost_usd). settings.social_weekly_usd sits under it; runs aim at a
# seventh of it a day, may run to 1.5x that in a busy 24 hours, and never past it over 7 days.
USD_PER_POST = 0.005
USD_PER_CALL = 0.02                     # tokens and reasoning on top of the posts
PULSE_POSTS = 12
TRENCHES_POSTS = 8
COIN_POSTS = 8
MAX_COINS = 6
MAX_CALLS_PER_DAY = 36                  # hourly plus retries; the $ pace is the real limit
DUE_HOURS = {1: 4, 2: 24}               # a Tier-1 coin is due every 4 h; Tier-2 only when something happens
VOLUME = {"low": 0, "normal": 1, "elevated": 2, "extreme": 3}
SYSTEM = ("You are the Social Pulse analyst on a crypto trading desk. You read X (Twitter) through your "
          "x_search tool and report only what the posts you retrieved say. Answer with the requested JSON only.")
# X search is the only tool: no shell, files, web, subagents, schedulers or media tools. Each of these
# is also ~600 tokens of tool definition on every call.
DENY_TOOLS = ("run_terminal_command,run_terminal_cmd,bash,read_file,search_replace,list_dir,grep,glob,Grep,Glob,"
              "write,write_file,todo_write,monitor,search_tool,use_tool,workflow,enter_plan_mode,exit_plan_mode,"
              "ask_user_question,send_feedback,image_gen,image_edit,image_to_video,reference_to_video,"
              "scheduler_create,scheduler_delete,scheduler_list,kill_command_or_subagent,"
              "get_command_or_subagent_output,spawn_subagent,web_fetch,web_search")
GROK_ARGS = ["--tools", "x_search", "--disallowed-tools", DENY_TOOLS, "--disable-web-search", "--no-subagents",
             "--no-plan", "--system-prompt-override", SYSTEM, "--verbatim"]

PROMPT = """Current time: {now} UTC.

Run exactly these X searches with x_search, each once, with the mode and limit given. Run no other search.
{searches}

Search 1 is the market pulse. Use it for market_score, market_mood, headlines and trending. Search 2 is the
memecoin trenches; use it only for trenches and memecoin_narratives. Every later search is one coin; use it
for that coin's read. Weight traders, analysts, funds, exchanges and on-chain trackers; ignore giveaways,
airdrop bait, bots, paid shills and copy-pasted signals.

- market_score: integer -5..+5 for crypto X overall. market_mood: one sentence.
- headlines: up to 5 things a trader should know from the pulse posts, most important first (a flow, a
  dated catalyst, a positioning claim naming who, a launch or listing, a risk). Each: one plain sentence
  (text), kind, the coin tickers it concerns (coins; empty for the whole market), and the x.com URL of the
  post it came from (url, required).
  Leave out single-account "gem" or "10x" calls on small caps: a headline is something a trader would act on.
- trending: up to 6 coin tickers that several different accounts in the pulse are excited or worried about,
  each with why (a few words) and one post URL. Leave out BTC and ETH unless they are the story, and one
  account shilling its own pick.
- trenches: up to 6 memecoin tickers the trenches posts are about right now (a call that drew real engagement,
  a sharp move, a new meta), each with why (a few words) and one post URL. Leave out a coin's own team or dev
  promoting it, giveaways, engagement bait and posts that are not about a memecoin.
- memecoin_narratives: up to 3 specific themes memecoins ride right now (a meme, a person, a news story, an AI
  agent, a launchpad or chain), each as a short phrase (narrative) with the tickers riding it and one post URL.
  Leave out generic chatter such as "memecoins are pumping".
- reads: one per coin search, scope exactly as given:
  summary: one plain sentence on what X is saying about it right now.
  why_it_matters: one sentence on what a trader should take from it, or "" if nothing stands out.
  notable: true only for something new or unusual (a catalyst, a crowding extreme, a sharp change in mood,
    credible positioning); false for ordinary chatter.
  posts_reviewed; sentiment_score (integer -5..+5); post_volume vs its usual level; crowding (is
    positioning chatter one-sided?); narratives (up to 3 short phrases); positioning (up to 3 concrete
    claims, naming who); catalysts (up to 3, each with its date as posted); sample_posts (the 2 most
    informative posts: author, text, x.com URL, required).
Sentiment anchors: -5 capitulation, -3 clearly bearish, -1 mildly bearish, 0 mixed or neutral chop, +1 mildly
bullish, +3 clearly bullish, +5 euphoric. Ordinary chop is 0 or +-1. If a search returns almost nothing,
say post_volume "low", sentiment 0, notable false."""

PULSE_QUERY = "(crypto OR bitcoin OR altcoins OR $BTC OR $ETH OR $SOL) min_faves:50 -filter:replies within_time:1h"
# The memecoin meta, for The Deep End (docs/SPEC-DEEP-END.md A2): what the trenches are running this hour.
# Compared side by side on 2026-09-30: plain memecoin words return mostly bait ("put $50 in memecoins"), url:
# operators return unrelated posts, and trader words next to a chain word return posts naming coins.
TRENCHES_QUERY = ('(runner OR sending OR aped OR trenches OR "new meta") (sol OR solana OR bnb OR bsc OR base OR '
                  "memecoin OR memecoins) -giveaway min_faves:10 -filter:replies within_time:1h")


def coin_query(sym: str, hours: int) -> str:
    return f"${sym} min_faves:2 -filter:replies within_time:{hours}h"


# X status ids are snowflakes: the creation time is encoded in the id, so a cited post can be checked
# for existence-shaped-ness and recency without calling X. Grok once invented a whole batch with
# plausible handles and status id "000"; a bare "x.com/" substring check let it through.
STATUS_URL = re.compile(r"^https://(?:x|twitter)\.com/[A-Za-z0-9_]{1,15}/status/(\d{15,20})(?:[/?#].*)?$")
TWITTER_EPOCH_MS = 1288834974657
MAX_POST_AGE = timedelta(hours=6)          # coin windows are at most 4 h; allow slack for slow search


def post_time(url: str | None) -> datetime | None:
    m = STATUS_URL.match(url or "")
    if not m:
        return None
    return datetime.fromtimestamp(((int(m.group(1)) >> 22) + TWITTER_EPOCH_MS) / 1000, UTC)


def fresh_url(url: str | None, read_at: datetime) -> bool:
    t = post_time(url)
    return t is not None and read_at - MAX_POST_AGE <= t <= read_at + timedelta(minutes=10)


def verified(post: "Post", read_at: datetime) -> bool:
    return fresh_url(post.url, read_at)


class Post(BaseModel):
    author: str
    text: str
    url: str | None = None


class Headline(BaseModel):
    text: str
    kind: Literal["flow", "catalyst", "positioning", "launch", "narrative", "risk"]
    coins: list[str] = Field(default_factory=list)
    url: str


class Trending(BaseModel):
    ticker: str
    why: str
    url: str


class MemeNarrative(BaseModel):
    narrative: str
    tickers: list[str] = Field(default_factory=list)
    url: str


class ScopeRead(BaseModel):
    scope: str = Field(description="exactly as given in the search list")
    summary: str = ""
    why_it_matters: str = ""
    notable: bool = False
    posts_reviewed: int = Field(ge=0)
    sentiment_score: int = Field(ge=-5, le=5)
    post_volume: Literal["low", "normal", "elevated", "extreme"]
    crowding: Literal["none", "moderate", "one_sided_bullish", "one_sided_bearish"]
    narratives: list[str]
    positioning: list[str] = Field(default_factory=list)
    catalysts: list[str] = Field(default_factory=list)
    sample_posts: list[Post]


class Pulse(BaseModel):
    market_score: int = Field(ge=-5, le=5)
    market_mood: str = Field(description="one sentence on overall crypto X mood")
    headlines: list[Headline] = Field(default_factory=list)
    trending: list[Trending] = Field(default_factory=list)
    trenches: list[Trending] = Field(default_factory=list)
    memecoin_narratives: list[MemeNarrative] = Field(default_factory=list)
    reads: list[ScopeRead]


# ─── budget ──────────────────────────────────────────────────────────────────
def call_cost(n_coins: int) -> float:
    """Estimated pool spend of one call: posts dominate."""
    return USD_PER_CALL + USD_PER_POST * (PULSE_POSTS + TRENCHES_POSTS + COIN_POSTS * n_coins)


def coins_affordable(spent_24h: float, spent_7d: float, weekly_usd: float, runs_per_day: int = 24) -> int:
    """How many coin searches this run can carry. The aim is an even hourly share of the day, plus a
    catch-up when the last 24 hours ran under pace. Hard limits: 1.5x the daily share in any 24 hours
    (a busy stretch can run hot, not away) and the weekly allowance over any 7 days."""
    daily = weekly_usd / 7
    hourly = daily / runs_per_day
    share = hourly + max(0.0, min((daily - spent_24h) / 4, hourly))
    room = min(1.5 * daily - spent_24h, weekly_usd - spent_7d)
    n = int((min(share, room) - call_cost(0)) // (USD_PER_POST * COIN_POSTS))
    return max(-1, min(MAX_COINS, n))           # -1: not even the pulse fits


async def spent(hours: int) -> float:
    pool = await db.pool()
    return float(await pool.fetchval(
        f"""SELECT coalesce(sum({xsearch.spend_sql()}), 0) FROM agent_runs
            WHERE agent = $1 AND started_at > now() - make_interval(hours => $2)""", AGENT, hours))


# ─── what to read ────────────────────────────────────────────────────────────
def priority(tier: int, hours_since: float | None, *, moving: bool, trending: bool, trade: bool, news: bool) -> float:
    """>= 1 means worth a search this hour. Tier-1 coins come due every 4 h; any coin with something
    happening jumps the queue; nothing is read twice within the hour."""
    if hours_since is not None and hours_since < 1:
        return 0.0
    live = 1.5 * trending + 1.0 * moving + 0.75 * trade + 0.75 * news
    if tier > 1 and not live:
        return 0.0
    due = (hours_since if hours_since is not None else 48) / DUE_HOURS.get(tier, 24)
    return round(due + live, 3)


async def pick(limit: int, now: datetime) -> list[dict]:
    """The coins to search this run, most pressing first: [{symbol, hours (search window), why}]."""
    if limit <= 0:
        return []
    pool = await db.pool()
    coins = await analysts.universe()
    tiers = {r["symbol"]: r["tier"] for r in await pool.fetch(
        "SELECT symbol, tier FROM assets WHERE active AND tier <= 2")}
    last = {r["scope"]: r["ts"] for r in await pool.fetch(
        "SELECT scope, max(ts) AS ts FROM social_snapshots WHERE source = 'grok_x' GROUP BY scope")}
    feats = await analysts.latest_features([c["symbol"] for c in coins], ["1h"])
    trend_rows = await pool.fetch(
        """SELECT narratives->'trending' AS t FROM social_snapshots
           WHERE source = 'grok_x' AND scope = $1 AND ts > now() - interval '3 hours' ORDER BY ts DESC LIMIT 1""", MARKET)
    trending = {str(t.get("ticker", "")).upper().lstrip("$") for r in trend_rows for t in orjson.loads(r["t"] or "[]")}
    trades = {r["scope"] for r in await pool.fetch(
        """SELECT DISTINCT scope FROM trade_calls WHERE status = 'proposed' AND horizon_until > now()
           AND source_kind IN ('strategist', 'oracle') AND market <> 'dex'""")}   # not Jev's paper book: it'd crowd out Tier-1
    news = {a for r in await pool.fetch(
        "SELECT assets FROM news_items WHERE impact >= 4 AND published_at > now() - interval '3 hours'") for a in r["assets"]}
    picks = []
    for c in coins:
        sym = c["symbol"]
        f = (feats.get((sym, "1h")) or {}).get("data") or {}
        atr, ret, vz = f.get("atr_pct"), f.get("ret_1"), f.get("vol_z")
        moving = bool((atr and ret is not None and abs(ret) >= 1.5 * atr) or (vz is not None and vz >= 2))
        since = (now - last[sym]).total_seconds() / 3600 if sym in last else None
        flags = {"moving": moving, "trending": sym in trending, "trade": sym in trades, "news": sym in news}
        p = priority(tiers.get(sym, 2), since, **flags)
        if p >= 1:
            why = [k for k, v in flags.items() if v] or ["due"]
            picks.append({"symbol": sym, "priority": p, "why": why,
                          "hours": max(1, min(4, round(since))) if since is not None else 4})
    picks.sort(key=lambda x: -x["priority"])
    return picks[:limit]


def searches(coins: list[dict]) -> str:
    lines = [f'1. query "{PULSE_QUERY}", mode Top, limit {PULSE_POSTS}  (market pulse)',
             f"2. query '{TRENCHES_QUERY}', mode Top, limit {TRENCHES_POSTS}  (memecoin trenches)"]
    lines += [f'{i}. query "{coin_query(c["symbol"], c["hours"])}", mode Latest, limit {COIN_POSTS}  (scope "{c["symbol"]}")'
              for i, c in enumerate(coins, 3)]
    return "\n".join(lines)


# ─── run ─────────────────────────────────────────────────────────────────────
def grounded(pulse: Pulse, scopes: set[str], now: datetime) -> tuple[list[ScopeRead], list[Headline], list[Trending]]:
    """Keep only what cites a recent, real-shaped X post."""
    reads = []
    for r in pulse.reads:
        r.sample_posts = [p for p in r.sample_posts if verified(p, now)]
        if r.sample_posts and r.scope in scopes:
            reads.append(r)
    heads = [h for h in pulse.headlines if fresh_url(h.url, now)][:5]
    trend = [t for t in pulse.trending if fresh_url(t.url, now)][:6]
    return reads, heads, trend


def trenches(pulse: Pulse, now: datetime) -> tuple[list[Trending], list[MemeNarrative]]:
    """The memecoin meta, kept only where it cites a recent, real-shaped X post (as grounded() does)."""
    return ([t for t in pulse.trenches if fresh_url(t.url, now)][:6],
            [m for m in pulse.memecoin_narratives if fresh_url(m.url, now)][:3])


def market_narratives(pulse: Pulse, heads: list[Headline], trend: list[Trending], scopes: set[str], run_id: int,
                      now: datetime) -> dict:
    """The market row's narratives: the pulse, plus the trenches for The Deep End (fields only added: /api/social
    reads the others by name)."""
    meme, metas = trenches(pulse, now)
    return {"market_mood": pulse.market_mood, "headlines": [h.model_dump() for h in heads],
            "trending": [t.model_dump() for t in trend], "coins_read": sorted(scopes),
            "trenches": [t.model_dump() for t in meme], "memecoin_narratives": [m.model_dump() for m in metas],
            "agent_run_id": run_id}


async def run() -> int:
    now = datetime.now(UTC)
    n = coins_affordable(await spent(24), await spent(24 * 7),
                         xsearch.week(settings.social_weekly_usd) * await controls.scale(AGENT))
    if n < 0:
        log.info("%s: the X allowance is used up for now (24 h or 7 d); skipping this hour", AGENT)
        return 0
    coins = await pick(n, now)
    scopes = {c["symbol"] for c in coins}
    prompt = PROMPT.format(now=now.strftime("%Y-%m-%d %H:%M"), searches=searches(coins))
    # Grok occasionally skips the search and writes plausible filler; the evidence gate catches it
    # and one retry usually lands.
    for attempt in (1, 2):
        try:
            pulse, run_id = await xsearch.structured(
                AGENT, Pulse, prompt, system=SYSTEM, max_calls_per_day=MAX_CALLS_PER_DAY, timeout=300,
                grok_args=GROK_ARGS, from_date=(now - timedelta(days=1)).date())
        except cli.CallLimitExceeded as e:
            log.warning("%s", e)
            return 0
        except RuntimeError as e:                    # no JSON, or the wrong shape: logged by cli; one retry
            if attempt == 2:
                raise
            log.warning("%s: attempt 1 failed (%s); retrying", AGENT, e)
            continue
        now = datetime.now(UTC)
        reads, heads, trend = grounded(pulse, scopes, now)
        if (heads or trend) and len(reads) >= (len(scopes) + 1) // 2:
            break
        await (await db.pool()).execute(
            "UPDATE agent_runs SET ok = FALSE, error = $2 WHERE id = $1", run_id,
            f"ungrounded: {len(heads)} headlines, {len(reads)}/{len(scopes)} reads cite verifiable recent X posts")
        log.warning("%s: attempt %d not grounded in X posts; discarded", AGENT, attempt)
    else:
        raise RuntimeError(f"{AGENT}: two attempts not grounded in X posts; nothing stored")

    ts = now.replace(second=0, microsecond=0)
    why = {c["symbol"]: c["why"] for c in coins}
    meta = market_narratives(pulse, heads, trend, scopes, run_id, now)
    rows = [(ts, MARKET, "grok_x", pulse.market_score / 5, None, orjson.dumps(meta).decode(), None)]
    rows += [(ts, r.scope, "grok_x", r.sentiment_score / 5, float(VOLUME[r.post_volume]),
              orjson.dumps({"summary": r.summary, "why_it_matters": r.why_it_matters, "notable": r.notable,
                            "narratives": r.narratives, "crowding": r.crowding, "posts_reviewed": r.posts_reviewed,
                            "positioning": r.positioning, "catalysts": r.catalysts, "picked_for": why.get(r.scope),
                            "agent_run_id": run_id}).decode(),
              orjson.dumps([p.model_dump() for p in r.sample_posts]).decode())
             for r in reads]
    written = await db.upsert(
        "social_snapshots", ["ts", "scope", "source", "sentiment", "volume", "narratives", "samples"],
        rows, ["scope", "source", "ts"])
    await _as_signals(reads, now, run_id)
    log.info("%s: pulse + %d coins read (%s), %d headlines, %d trending, %d in the trenches", AGENT, len(reads),
             ", ".join(sorted(r.scope for r in reads)), len(heads), len(trend), len(meta["trenches"]))
    return written


def _bias(score: int) -> int:
    """-5..+5 X mood -> -2..+2 call: |3+| strong, |1-2| mild, 0 none."""
    return 0 if score == 0 else (2 if abs(score) >= 3 else 1) * (1 if score > 0 else -1)


async def _as_signals(reads: list[ScopeRead], now: datetime, run_id: int) -> None:
    """Each coin read becomes a graded call on 1h and 4h, anchored at read time (posts are live)."""
    prices = await analysts.latest_prices([r.scope for r in reads])
    rows = []
    for r in reads:
        if r.scope not in prices:
            continue
        for tf in ("1h", "4h"):
            as_of, ref = analysts.live_anchor(tf, now, prices[r.scope])
            rows.append((r.scope, tf, _bias(r.sentiment_score), 0.5, ref, None,
                         orjson.dumps({"thesis": r.summary or "; ".join(r.narratives[:2]), "crowding": r.crowding}).decode(),
                         as_of + TFS[tf][2] * (1 + HORIZON_BARS), AGENT, as_of, r.sentiment_score / 5, run_id))
    await db.upsert(
        "signals", ["scope", "tf", "bias", "confidence", "ref_price", "invalidation", "drivers", "valid_until",
                    "source", "as_of", "score", "agent_run_id"],
        rows, ["scope", "tf", "source", "as_of"],
    )
