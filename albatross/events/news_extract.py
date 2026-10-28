"""events_news: dated upcoming events pulled out of the news we already ingest (news_items), every 30 minutes.

Items not yet read (news_items.events_run_id IS NULL) from the last two days that contain date words
(dates.mentions_date) go to the model in batches with their publication time, today's date and the tracked
coins. It returns events, each with the item's index, the sentence that states it (`quote`), the words that date
it (`date_text`) and the day it takes them to mean. Code keeps an event only if (gate()):
- the quote is in that item's title or text, verbatim apart from whitespace, case, HTML entities and curly quotes
- the date words are inside the quote, and dates.read() reads a period from them, counted from the item's
  publication day, that contains the model's day (the stored date and precision are dates.read()'s, not the
  model's)
- the date is no more than two days past and no more than HORIZON ahead
- the subject is named somewhere in the item (a tracked ticker also by its coin's name), and the status is one the
  store knows (an unknown kind becomes "other" in store.Event)
- it isn't a macro release: events_macro owns those and keys them with a venue, so ours would be duplicates.
The source URL is always the item's own: the model never supplies one. Dropped events and why go to agent_runs.error
like the analysts'. Items sent to the model are marked with the run that read them once their events are stored,
so each is read once; items without date words are never sent (no event of theirs could pass the date check).

Dry run on the server: `docker compose exec ingest python -m albatross job events_news_dry` (calls the model,
prints what would be written, writes no events and marks nothing).
"""

import html
import logging
import re
import unicodedata
from datetime import UTC, date, datetime, timedelta

import orjson
from pydantic import BaseModel, Field

from albatross import db
from albatross.agents.analysts import universe
from albatross.events import dates, store
from albatross.llm import openrouter

log = logging.getLogger(__name__)

AGENT = "events_news"
DAILY_BUDGET_USD = 1.0
BATCH = 15
MAX_BATCHES = 4                     # 60 items a run; a normal half hour brings ~5 with date words
TEXT_CHARS = 1200
PAST = timedelta(days=2)            # "just happened" events are kept this long
HORIZON = timedelta(days=180)
STATUSES = set(store.STATUS_RANK)
# events_macro owns these (FOMC from the Fed, releases from FRED, keyed with a venue); from here they'd be duplicates.
LEFT_TO_FEEDS = {"macro"}
SOURCE_NAMES = {"coindesk": "CoinDesk", "cointelegraph": "Cointelegraph", "theblock": "The Block",
                "decrypt": "Decrypt", "bitcoinmagazine": "Bitcoin Magazine", "cryptoslate": "CryptoSlate",
                "blockworks": "Blockworks", "dlnews": "DL News", "cryptopanic": "CryptoPanic"}


class Dated(BaseModel):
    """What both extractors return per event. Lenient types: a bad field drops that event in the gate, not the
    whole batch at validation."""
    kind: str = Field(description="tge | listing | delisting | unlock | mainnet | upgrade | airdrop | governance | "
                                  "conference | other")
    subject: str = Field(description="ticker for listing/delisting/unlock, else the project or event name")
    venue: str | None = Field(default=None, description="exchange in lower case for listings/delistings, else null")
    title: str = Field(description="plain words, <= 100 chars")
    date_text: str = Field(description="the words that give the date, copied exactly from the quote")
    date: str = Field(description="YYYY-MM-DD: the day date_text means (first day of a week/month/quarter)")
    precision: str = Field(default="day", description="day | week | month | quarter")
    assets: list[str] = Field(default_factory=list, description="tracked tickers the event directly touches")
    importance: int = Field(default=2, description="1..5")
    why: str = Field(default="", description="one line: why it matters, and to whom")
    status: str = Field(default="scheduled", description="scheduled | rumored | happened | cancelled")
    quote: str = Field(description="the sentence that states the event and its date, copied exactly")


class NewsEvent(Dated):
    item: int = Field(description="index of the news item it comes from")


class NewsEvents(BaseModel):
    events: list[NewsEvent] = Field(default_factory=list)


# ─── gate ───────────────────────────────────────────────────────────────────
_QUOTES = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-", "…": "..."})


def norm(s: str | None) -> str:
    """Text as compared by the gate: HTML entities decoded, curly quotes and dashes straightened, whitespace
    collapsed, case folded."""
    s = unicodedata.normalize("NFKC", html.unescape(s or "")).translate(_QUOTES)
    return " ".join(s.split()).casefold()


def _trim(quote: str) -> str:
    """Drop the wrapping quote marks and closing full stop models add around a quote."""
    return quote.strip().strip("\"'“”‘’").rstrip(".…").strip()


def named(subject: str, text: str, names: dict[str, str | None]) -> bool:
    """The item names the subject: one of its words (a short one, like a ticker, as a whole word), or, when the
    subject is a tracked ticker, the coin's name."""
    t = norm(text)
    for w in re.findall(r"[a-z0-9]+", norm(subject)):
        if re.search(r"\b" + re.escape(w) + (r"\b" if len(w) < 3 else ""), t):
            return True
    name = names.get(subject.strip().upper().lstrip("$"))
    return bool(name) and norm(name) in t


def check_date(date_text: str, model_date: str, published: datetime,
               now: datetime) -> tuple[dates.Period | None, str | None]:
    """-> (the period to store, None) or (None, why not). The period comes from the words, not the model."""
    try:
        said = date.fromisoformat(model_date.strip()[:10])
    except ValueError:
        return None, f"date {model_date!r} is not YYYY-MM-DD"
    periods = dates.read(date_text, published)
    if not periods:
        return None, f"date words {date_text!r} not understood"
    p = next((p for p in periods if said in p), None)
    if p is None:
        return None, f"{said} is not what {date_text!r} means ({', '.join(str(x.start) for x in periods)})"
    return window(p, now)


def window(p: dates.Period, now: datetime, horizon: timedelta = HORIZON) -> tuple[dates.Period | None, str | None]:
    if p.last < (now - PAST).date():
        return None, f"{p.start} is past"
    if p.start > (now + horizon).date():
        return None, f"{p.start} is beyond {horizon.days} days"
    return p, None


def gate(e: NewsEvent, item: dict, names: dict[str, str | None],
         now: datetime) -> tuple[dates.Period | None, str | None]:
    """-> (period, None) if the event is grounded in `item`, else (None, the reason it was dropped)."""
    quote, dt = norm(_trim(e.quote)), norm(e.date_text)
    if e.kind.strip().lower() in LEFT_TO_FEEDS:
        return None, f"{e.kind} events come from their own feed"
    if len(quote) < 20:
        return None, "quote too short"
    if quote not in norm(item["title"]) and quote not in norm(item["summary"]):
        return None, "quote not in the item"
    if not dt or dt not in quote:
        return None, f"date words {e.date_text!r} not in the quote"
    if not named(e.subject, f"{item['title']} {item['summary'] or ''}", names):
        return None, f"subject {e.subject!r} not named in the item"
    if e.status not in STATUSES:
        return None, f"status {e.status!r}"
    return check_date(e.date_text, e.date, item["published_at"], now)


def to_event(e: Dated, p: dates.Period, source: store.Source, tracked: dict[str, str | None]) -> store.Event:
    return store.Event(
        kind=e.kind, title=e.title.strip()[:120], subject=e.subject.strip(),
        venue=(e.venue or "").strip().lower() or None,
        starts_at=store.day_start(p.start), precision=p.precision,
        ends_at=store.day_start(p.last) if p.precision == "day" and p.last > p.start else None,
        assets=sorted({a.upper().lstrip("$") for a in e.assets} & tracked.keys()),
        importance=min(5, max(1, e.importance)), status=e.status, summary=e.why.strip() or None,
        sources=[source])


# ─── prompt ─────────────────────────────────────────────────────────────────
KINDS_GUIDE = """kinds: tge (token generation event / token launch), listing, delisting, unlock (scheduled token
unlock; put the share of supply in the title when given), mainnet, upgrade (network upgrade, hard fork, testnet
activation of a major upgrade), airdrop (claim, snapshot or distribution date), governance (a vote that decides
something material; date = the day voting ends), conference, other (a dated regulatory deadline, an ETF
decision or launch, a product launch that matters to a coin).
subject: for listing, delisting and unlock the token's ticker ("ARB"); otherwise the project, protocol or event
name as usually written ("MegaETH", "Ethereum Glamsterdam", "Token2049").
venue: the exchange for a listing or delisting, in lower case ("coinbase", "upbit", "okx"); otherwise null.
title: plain words, <= 100 chars: "Coinbase lists W", "Arbitrum unlocks 2.1% of ARB supply", "Ethereum
activates Glamsterdam on Sepolia".
assets: tickers from the tracked list that the event directly touches ([] if none).
importance 1-5, with a one-line why: 5 = market-moving (a top-20 coin's large unlock or major upgrade, a Coinbase,
Upbit or Binance listing of a tracked coin); 4 = big for one tracked coin or a whole sector (e.g. a tracked coin's
unlock of 2%+ of supply, a large project's mainnet or TGE); 3 = notable; 2 = minor; 1 = trivia.
status: scheduled (announced with a date), rumored (reported or expected, not confirmed), happened (already took
place), cancelled."""

SYSTEM = """You are the calendar clerk on a crypto desk. From news items, pull out the DATED events that are
coming up, or that happened in the last two days: token generation events (TGE) and token launches, exchange
listings and delistings, token unlocks, mainnet launches and network upgrades, airdrops, governance votes,
major conferences, and regulatory deadlines that matter to crypto markets. Leave out macro releases and
central-bank meetings (FOMC, CPI, jobs, PCE, GDP): another feed covers them.

Each event needs a date stated in the item itself. For each one give:
- item: the index of the item it comes from
- quote: the sentence from that item's title or text that states the event and its date, copied character for
  character (you may cut it down to the part that holds both). Events whose quote is not in the item are
  discarded.
- date_text: the words in the quote that give the date, copied exactly: "Oct. 6", "next Tuesday", "on
  Wednesday", "later this month", "Q4 2026", "tomorrow". They must be inside the quote.
- date: the day those words mean, YYYY-MM-DD. Count relative words ("tomorrow", "Tuesday", "next week") from the
  item's published day (its weekday is given). For a week, month or quarter give its first day.
- precision: day, week, month or quarter
""" + KINDS_GUIDE + """

Skip: forecasts and price predictions ("could hit $100k by March", "a pullback in October"); dates that only say
when something was said, reported or measured ("said on Monday", "a Sept. 28 report", "inflows on Sept. 28");
events more than two days in the past; anything unrelated to crypto markets. One event per real-world event and
item. Most items have none: return {"events": []} then."""


def _payload(items: list[dict], today: date, tracked: list[str]) -> str:
    return orjson.dumps({
        "today": f"{today.isoformat()} ({today:%A})",
        "tracked": tracked,
        "items": [{"i": i, "published": f"{r['published_at'].astimezone(UTC):%Y-%m-%dT%H:%MZ (%A)}",
                   "source": r["source"], "title": html.unescape(r["title"]),
                   "text": html.unescape(r["summary"] or "")[:TEXT_CHARS]}
                  for i, r in enumerate(items)],
    }).decode()


# ─── run ────────────────────────────────────────────────────────────────────
async def _unread() -> list[dict]:
    pool = await db.pool()
    rows = await pool.fetch(
        """SELECT id, source, url, title, summary, published_at FROM news_items
           WHERE events_run_id IS NULL AND published_at > now() - interval '2 days' ORDER BY published_at DESC""")
    return [dict(r) for r in rows if dates.mentions_date(f"{r['title']} {r['summary'] or ''}")]


async def tracked_assets() -> dict[str, str | None]:
    """Every active symbol -> its coin name."""
    pool = await db.pool()
    return {r["symbol"]: r["name"] for r in await pool.fetch("SELECT symbol, name FROM assets WHERE active")}


def extract(found: NewsEvents, items: list[dict], tracked: dict[str, str | None],
            now: datetime) -> tuple[list[store.Event], list[str]]:
    """Gate the model's events against the items it was shown. -> (events to write, reasons for the dropped)."""
    out, reasons = [], []
    for e in found.events:
        item = items[e.item] if 0 <= e.item < len(items) else None
        p, why = gate(e, item, tracked, now) if item else (None, f"no item {e.item}")
        if why:
            reasons.append(f"#{e.item} {e.subject}: {why}")
            continue
        src = store.Source(name=SOURCE_NAMES.get(item["source"], item["source"]), url=item["url"],
                           excerpt=" ".join(_trim(e.quote).split())[:300])
        out.append(to_event(e, p, src, tracked))
    return out, reasons


async def run(write: bool = True, now: datetime | None = None) -> int:
    """Read the unread dated news, gate what the model finds, write it. Returns events written (or found, dry)."""
    items = await _unread()
    todo = items[:BATCH * MAX_BATCHES]
    if not todo:
        return 0
    tracked = await tracked_assets()
    tier12 = [s["symbol"] for s in await universe()]
    pool = await db.pool()
    now = now or datetime.now(UTC)
    kept: list[store.Event] = []
    read: list[tuple[int, list[int]]] = []          # (run id, news ids it read), marked once events are stored
    ok = failed = 0
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        try:
            found, run_id = await openrouter.structured(
                AGENT, NewsEvents, SYSTEM, _payload(chunk, now.date(), tier12), model=openrouter.LUNA,
                daily_budget_usd=DAILY_BUDGET_USD)
        except openrouter.BudgetExceeded as e:
            log.log(logging.DEBUG if isinstance(e, openrouter.NotConfigured) else logging.WARNING, "%s", e)
            break
        except RuntimeError as e:                       # model/parse failure, already logged in agent_runs
            log.warning("%s", e)
            failed += 1
            continue
        ok += 1
        events, reasons = extract(found, chunk, tracked, now)
        kept += events
        if reasons:
            await pool.execute("UPDATE agent_runs SET error = $2 WHERE id = $1", run_id,
                               f"dropped {len(reasons)}/{len(found.events)}: " + "; ".join(reasons)[:900])
        read.append((run_id, [r["id"] for r in chunk]))
        if not write:
            _print(chunk, found, events, reasons)
    if failed and not ok:
        raise RuntimeError(f"all {failed} {AGENT} batches failed; see agent_runs")
    if write:
        new = await store.upsert(kept, collector=AGENT)
        await pool.executemany("UPDATE news_items SET events_run_id = $1 WHERE id = ANY($2::bigint[])", read)
        log.info("%s: %d items read, %d events (%d new)", AGENT, sum(len(ids) for _, ids in read), len(kept), new)
    return len(kept)


def _print(items: list[dict], found: NewsEvents, events: list[store.Event], reasons: list[str]) -> None:
    print(f"\n{len(items)} items, {len(found.events)} events proposed, {len(events)} kept")
    for e in events:
        print(f"  KEEP {store.make_key(e.kind, e.subject, e.starts_at, e.venue):<48} {e.precision:<7} "
              f"imp {e.importance} {e.status:<9} {e.title}\n"
              f"       \"{e.sources[0].excerpt}\"\n       {e.sources[0].url}")
    for r in reasons:
        print(f"  DROP {r}")
