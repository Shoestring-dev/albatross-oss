# Events calendar (spec, 2026-09-30)

The aim: an intelligence pipeline for upcoming and new TGEs, launches and other important events, a calendar of
what matters. Catalysts matter more here because our own research (docs/RESEARCH-EDGE.md) found nothing
that predicts market direction from charts or macro series.

## Store (done): `db/migrations/015_events.sql`, `albatross/events/store.py`
One row per real-world event, keyed `kind:subject[:venue]:YYYY-MM-DD`. Every collector builds `Event`s and calls
`await store.upsert(events, collector="<name>")`; repeats (in one batch or across runs) merge sources by URL,
importance takes the max, status only moves forward, the more precise time wins (at equal precision only the same
source read again, e.g. an exchange postponing a listing within the day), the newest probability wins and brings its
summary (the summary quotes the odds).

Kinds: tge, listing, delisting, unlock, mainnet, upgrade, airdrop, governance, macro, conference, other.
Importance 1..5 (5 = market-moving: FOMC, CPI, a top-20 coin's unlock or a Coinbase/Upbit listing of a tracked coin).
`assets` holds tracked symbols (upper case) so coin pages and the Strategist can find an asset's events.

## Collectors
Structured feeds (no LLM), each a job in `albatross/__main__.py`, tracked in alerts CADENCE:
- `events_macro` (daily 01:10): FOMC meetings from federalreserve.gov/monetarypolicy/fomccalendars.htm (this year
  and next, on the decision day); FRED release dates (CPI, Employment Situation, PCE, GDP) via the FRED key, two days
  back to 60 ahead. Precision 'day' for all (neither source gives times). Importance FOMC 5, CPI 5, jobs 4, PCE 4, GDP 3.
- `events_listings` (every 10 min): OKX new-listing and delisting notices (time from the article), Upbit trade notices
  (Korean; the ticker sits in parentheses, the time in the body; listings, delistings, investment warnings as
  `other`), Coinbase products diff (a base's first online USD product = listed), Hyperliquid perps diff (new perp =
  listed, newly delisted = delisted). The diffs keep a baseline in `event_feed_seen` (migration 016); a feed's first
  run only records it. Binance and Bybit are out: geo-restricted in some regions, and we don't work around
  geo-blocks. Importance rules in the module docstring.
- `events_polymarket` (hourly): Gamma events under the tags token-launch and pre-market ("Will X launch a token by
  ___?", "X airdrop by ___?", "X FDV above ___ one day after launch?"), $50k+ traded. One `tge` (or `airdrop`) row per
  project at the earliest date priced at 50%+, else the latest date; only books tighter than 10 points count. Status
  rumored, probability = that date's YES price, summary = the odds by date and the FDV ladder. A row whose date the
  market no longer points to is deleted (only Polymarket reported it) or loses its odds (others did too).
LLM extractors, evidence-gated like every other LLM output here:
- `events_news` (every 30 min, OpenRouter): future dated events from news_items; the stated date must appear in the
  item's text and the source URL is the item's own.
- `events_oracle` (daily, Claude on the host worker, web search): upcoming TGEs, launches, unlocks, mainnets and
  listings for the next 30 days; every event must cite a URL that appears in the run's transcript (the Oracle gate).
As built (`albatross/events/news_extract.py`, `oracle_calendar.py`, `dates.py`): the model names the words that
date an event (`date_text`) and the day it takes them to mean; `dates.read()` reads the same words deterministically
(relative words from the item's publication day, UTC or New York) and the event survives only if its period
contains the model's day, and the stored date and precision are the code's. News events also need their quote
verbatim in the item, the date words inside the quote, and the subject named in the item. Items without date words
never reach the model; items it read carry `news_items.events_run_id` (migration 017). Subjects: the ticker for
listings, delistings and unlocks, else the project or event name, so feeds and extractors land on the same key.
Both extractors leave macro releases to `events_macro` (which keys them with a venue), so they are never duplicated.
Dry runs that print and write nothing: `python -m albatross job events_news_dry` (ingest container) and
`uv run --frozen python -m albatross job events_oracle_dry` (host; spends one of the Oracle calendar's 2 calls/day).
Unlock schedules proper need a paid source (DefiLlama emissions 402s; Tokenomist); until then unlocks come from
news and the Oracle.

## API (feeds builder owns it)
`GET /api/calendar?days_back=2&days_ahead=30&kind=&asset=&min_importance=1` ->
`{"events": [{id, key, kind, title, subject, venue, starts_at, precision, ends_at, assets, chains, importance,
status, probability, summary, sources: [{name, url, excerpt, collector, seen_at}], first_seen}], "counts": {kind: n}}`
sorted by starts_at. The window is midnight UTC `days_back` ago to the end of the day `days_ahead` on, and an event
counts while any of its period is inside (a month-precision TGE stays through its month). `kind` takes a comma list;
`counts` ignores it. Cached 60 s. `GET /api/calendar/{id}` returns one event.
Dry-run any structured feed against the live sources: `python -m albatross.events macro|listings|polymarket`.

## Later (lead): Calendar view under Research, "Coming up" on Today, events on coin pages, events in the
Strategist's inputs (an asset's events inside a call's horizon), and event-impact research once history exists.
