"""events_oracle: once a day, the Oracle's model (web search through OpenRouter, GitHub never searched; Claude Code
until 2026-10-01) researches the next 30 days' token generation events and launches, unlocks, mainnets and upgrades,
exchange listings and airdrops, weighted to the coins we track.

Code keeps an event only if (extract()):
- its URL is one the call's own search returned: the Oracle's own citation check (oracle._grounded) against
  llm.openrouter.research's evidence
- its date falls between two days ago and WINDOW ahead. When its date words are ones dates.read() understands
  (counted from today), they must agree with its date, and the stored precision is theirs; otherwise its own
  (date, precision) stand, since the words come from a page we don't hold.
- its status is one the store knows, and it isn't a macro release (events_macro owns those).
At most MAX_CALLS_PER_DAY runs a day and DAILY_USD (agent_runs rows for AGENT, as openrouter.research counts them).

Dry run in the ingest container (uses one of the day's calls, writes nothing):
`docker compose exec -T ingest python -m albatross job events_oracle_dry`
"""

import logging
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, Field

from albatross import db
from albatross.agents import oracle
from albatross.agents.analysts import universe
from albatross.events import dates, store
from albatross.events.news_extract import (
    KINDS_GUIDE,
    LEFT_TO_FEEDS,
    STATUSES,
    Dated,
    to_event,
    tracked_assets,
    window,
)
from albatross.llm import openrouter

log = logging.getLogger(__name__)

AGENT = "oracle_calendar"
COLLECTOR = "events_oracle"
MAX_CALLS_PER_DAY = 2
MODEL = openrouter.RESEARCH         # LLM_MODEL_OVERRIDES=oracle_calendar=<model> switches it
DAILY_USD = 1.0
SEARCHES = 10                       # a month of research takes more searching than an escalation
TIMEOUT_S = 600
WINDOW = timedelta(days=30)
SOURCE = "Oracle (web)"


class WebEvent(Dated):
    url: str = Field(description="the exact page your search returned that states the event and its date")


class Calendar(BaseModel):
    events: list[WebEvent] = Field(default_factory=list)


PROMPT = """You are the Oracle, building a crypto desk's events calendar for {start} to {end} (today is {today},
UTC). Search and read the web for dated events in that window:
- token generation events (TGE) and token launches, including big new projects that have no token yet
- major token unlocks, with the share of supply each releases
- mainnet launches and major network upgrades or hard forks
- exchange listings announced with a date (Coinbase, Binance, OKX, Upbit, Bybit, Kraken, Hyperliquid, ...)
- large airdrops (claim, snapshot or distribution dates)
- governance votes that decide something material (the day voting ends)
Leave out macro releases and central-bank meetings: another feed covers them. Put the tracked coins below first,
but a big new project's TGE matters even if we don't track it. Prefer primary sources (project blogs and docs,
exchange announcements, governance forums, unlock trackers), and cite only pages your search returned.

Each event needs:
- url: the exact page your search returned that states the event and its date. Events citing a URL your search
  did not return are discarded.
- quote: a short excerpt from that page that states the event and its date
- date_text: the date as that page writes it, with the year when it gives one ("October 7, 2026", "Q4 2026");
  no relative words like "next week"
- date: the day it means, YYYY-MM-DD (for a week, month or quarter, its first day), and precision: day, week,
  month or quarter
""" + KINDS_GUIDE + """

Skip anything outside the window, anything without a stated date, forecasts and speculation, and events you
cannot source. Give the 15-40 that matter most; don't pad.

TRACKED COINS (tier 1-2, largest first):
{tracked}"""


def check_date(e: WebEvent, now: datetime) -> tuple[dates.Period | None, str | None]:
    try:
        said = date.fromisoformat(e.date.strip()[:10])
    except ValueError:
        return None, f"date {e.date!r} is not YYYY-MM-DD"
    if periods := dates.read(e.date_text, now):
        p = next((p for p in periods if said in p), None)
        if p is None:
            return None, f"{said} is not what {e.date_text!r} means ({', '.join(str(x.start) for x in periods)})"
    else:
        p = dates.period_of(said, e.precision if e.precision in store.PRECISION_RANK else "day")
    return window(p, now, WINDOW)


def extract(cal: Calendar, evidence: set[str], names: dict[str, str | None],
            now: datetime) -> tuple[list[store.Event], list[str]]:
    """-> (events to write, reasons for the dropped)."""
    grounded, _ = oracle._grounded(cal.events, evidence)          # the Oracle's citation check, as is
    kept_ids = {id(e) for e in grounded}
    reasons = [f"{e.subject}: url not in the search results ({e.url})" for e in cal.events if id(e) not in kept_ids]
    out = []
    for e in grounded:
        if e.kind.strip().lower() in LEFT_TO_FEEDS:
            p, why = None, f"{e.kind} events come from their own feed"
        elif e.status not in STATUSES:
            p, why = None, f"status {e.status!r}"
        else:
            p, why = check_date(e, now)
        if why:
            reasons.append(f"{e.subject}: {why}")
            continue
        src = store.Source(name=SOURCE, url=e.url.strip(), excerpt=" ".join(e.quote.split())[:300] or None)
        out.append(to_event(e, p, src, names))
    return out, reasons


def prompt(now: datetime, tier12: list[str], names: dict[str, str | None]) -> str:
    today = now.date()
    return PROMPT.format(start=today - timedelta(days=2), end=today + WINDOW, today=f"{today} ({today:%A})",
                         tracked=", ".join(f"{s} ({names[s]})" if names.get(s) else s for s in tier12))


async def run(write: bool = True, now: datetime | None = None) -> int:
    """One research run. Returns events written (or found, dry)."""
    now = now or datetime.now(UTC)
    names = await tracked_assets()
    tier12 = [s["symbol"] for s in await universe()]
    cal, run_id, evidence = await openrouter.research(AGENT, Calendar, prompt(now, tier12, names), model=MODEL,
                                                      daily_budget_usd=DAILY_USD, max_calls_per_day=MAX_CALLS_PER_DAY,
                                                      searches=SEARCHES, max_tokens=24000, deadline_s=TIMEOUT_S)
    events, reasons = extract(cal, evidence, names, now)
    pool = await db.pool()
    if reasons:
        await pool.execute("UPDATE agent_runs SET error = $2 WHERE id = $1", run_id,
                           f"dropped {len(reasons)}/{len(cal.events)}: " + "; ".join(reasons)[:900])
    if not write:
        print(f"{len(cal.events)} events proposed, {len(events)} kept, {len(evidence)} URLs the search returned")
        for e in sorted(events, key=lambda e: e.starts_at):
            print(f"  KEEP {store.make_key(e.kind, e.subject, e.starts_at, e.venue):<48} {e.precision:<7} "
                  f"imp {e.importance} {e.status:<9} {e.title}\n       {e.sources[0].url}")
        for r in reasons:
            print(f"  DROP {r}")
        return len(events)
    new = await store.upsert(events, collector=COLLECTOR)
    log.info("%s: %d proposed, %d kept (%d new), %d dropped", AGENT, len(cal.events), len(events), new, len(reasons))
    return len(events)
