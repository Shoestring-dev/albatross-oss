"""events_polymarket (hourly): what Polymarket prices about token launches (TGEs) and airdrops.

A "Will X launch a token by ___?" event has one YES/NO market per date; each open market's YES price is the chance the
token is live by that date. One `tge` row per project (`airdrop` for "X airdrop by ___?"): at the earliest date priced
at 50% or more, else the latest priced date. Only markets whose book is tighter than 10 points count as priced (an
empty book shows 50%). Status rumored, probability = that date's price, the summary quoting the odds by date (plus the
"FDV above ___ one day after launch" ladder when there is one). Only events with at least $50k traded in total: below
that the prices are a handful of bets. Projects with a date already resolved YES have launched and are left to the
listing feeds.

Events come from the Gamma tags token-launch and pre-market, which hold every launch/FDV market public search finds
(checked 2026-09-29) without paging through keyword results. When the chosen date moves, the old row is retired (see
retire()). Read-only public data; we never trade.
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import orjson

from albatross import db
from albatross.events import feeds
from albatross.events.store import Event, Source, day_start, make_key
from albatross.venues import polymarket

log = logging.getLogger(__name__)

COLLECTOR = "events_polymarket"
TAGS = ("token-launch", "pre-market")
MIN_VOLUME = 50_000
MAX_SPREAD = 0.10       # a wider book's midpoint is no price (an empty book shows 50%); Polymarket's own display cutoff
KINDS = ["tge", "airdrop"]
ET = ZoneInfo("America/New_York")

_LAUNCH = re.compile(r"^Will (.+?) launch an? (?:token|coin)\b", re.I)
_AIRDROP = re.compile(r"^(?:Will )?(.+?) airdrop\b", re.I)
_FDV = re.compile(r"^(.+?) FDV above .*after launch", re.I)
_DAY = re.compile(r"([A-Z][a-z]+) (\d{1,2})(?:, (\d{4}))?")
_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def market_date(m: dict) -> date | None:
    """The 'by' date of one dated market: its label ('December 31, 2026', or 'September 30' with the year of the
    market's end), else the end itself (markets close at 11:59 pm ET on the day)."""
    end = (datetime.fromisoformat(m["endDate"].replace("Z", "+00:00")).astimezone(ET) - timedelta(minutes=1)).date() \
        if m.get("endDate") else None
    lab = _DAY.search(m.get("groupItemTitle") or "")
    if lab and lab[1][:3].lower() in _MONTHS:
        mo, day = _MONTHS.index(lab[1][:3].lower()) + 1, int(lab[2])
        if lab[3]:
            return date(int(lab[3]), mo, day)
        if end:
            d = date(end.year, mo, day)
            return d if d <= end + timedelta(days=1) else date(end.year - 1, mo, day)
    return end


def yes_price(m: dict) -> float | None:
    try:
        outcomes, prices = orjson.loads(m["outcomes"]), orjson.loads(m["outcomePrices"])
        return float(prices[outcomes.index("Yes")])
    except (KeyError, ValueError, TypeError, IndexError):
        return None


def priced(m: dict) -> bool:
    """An open market with a two-sided book tight enough for its midpoint to mean something."""
    try:
        return not m.get("closed") and float(m["bestAsk"]) - float(m["bestBid"]) <= MAX_SPREAD
    except (KeyError, TypeError, ValueError):
        return False


def _money(x: float, fmt: str = ".1f") -> str:
    return f"${x / 1e9:{fmt}}B" if x >= 1e9 else f"${x / 1e6:{fmt}}M" if x >= 1e6 else f"${x / 1e3:.0f}k"


def _pct(p: float) -> str:
    return f"{p:.1%}" if p < 0.1 else f"{p:.0%}"


def fdv_ladder(ev: dict) -> str | None:
    """'FDV a day after launch: >$500M 17%, >$1B 13%' from an FDV event's open markets."""
    rungs = {}
    for m in ev.get("markets") or []:
        p, lab = yes_price(m), m.get("groupItemTitle") or ""
        v = re.match(r"\$?([\d.]+)\s*([kMB])", lab.replace(",", "").strip())
        if priced(m) and p is not None and v:
            rungs.setdefault(float(v[1]) * {"k": 1e3, "M": 1e6, "B": 1e9}[v[2]], p)     # Polymarket repeats rungs
    ladder = ", ".join(f">{_money(x, 'g')} {_pct(p)}" for x, p in sorted(rungs.items())[:5])
    return f"FDV a day after launch: {ladder}" if rungs else None


def events(raw: list[dict], today: date) -> list[Event]:
    """Gamma events (any mix of the two tags, duplicates fine) -> one row per project and kind."""
    by_id = {e["id"]: e for e in raw}
    fdv = {}
    for e in by_id.values():
        m = _FDV.match(e.get("title") or "")
        if m and not e.get("closed"):
            fdv[_norm(m[1])] = fdv_ladder(e)
    out = []
    for e in sorted(by_id.values(), key=lambda e: -(e.get("volume") or 0)):
        title = e.get("title") or ""
        m, kind = _LAUNCH.match(title), "tge"
        if not m and not _FDV.match(title):
            m, kind = _AIRDROP.match(title), "airdrop"
        if not m or e.get("closed") or (e.get("volume") or 0) < MIN_VOLUME:
            continue
        name = m[1].strip()
        markets = e.get("markets") or []
        if any(mk.get("closed") and yes_price(mk) == 1 for mk in markets):
            continue                                        # resolved YES: it launched
        dated = sorted((d, p, mk.get("question") or title) for mk in markets if priced(mk)
                       and (d := market_date(mk)) and d >= today and (p := yes_price(mk)) is not None)
        if not dated:
            continue
        d, p, question = next((x for x in dated if x[1] >= 0.5), dated[-1])
        what = "token launch" if kind == "tge" else "airdrop"
        odds = "; ".join(f"{dd:%b %-d, %Y} {_pct(pp)}" for dd, pp, _ in dated)
        ladder = fdv.get(_norm(name)) if kind == "tge" else None
        vol = e.get("volume") or 0
        out.append(Event(
            kind=kind, subject=name, starts_at=day_start(d), precision="day", status="rumored", probability=p,
            title=f"{name} {what} by {d:%b %-d, %Y}?", importance=4 if vol >= 1e6 else 3 if vol >= 250e3 else 2,
            summary=f"Polymarket odds for the {name} {what}, by date: {odds}." + (f" {ladder}." if ladder else "")
                    + f" {_money(vol)} traded.",
            sources=[Source(name="Polymarket", url=f"https://polymarket.com/event/{e['slug']}",
                            excerpt=f"{question} Yes {_pct(p)}")]))
    return out


async def retire(keep: list[str]) -> int:
    """Rows this feed filed at a date its markets no longer point to (the odds moved, a date expired, the market
    closed): deleted when only Polymarket ever reported them; otherwise their stale odds are cleared. Skipped when this
    run found under half the rows we hold, which smells like a short read rather than a market change."""
    pool = await db.pool()
    ours = """kind = ANY($1::text[]) AND sources @> '[{"collector": "events_polymarket"}]'"""
    held = await pool.fetchval(f"SELECT count(*) FROM events WHERE {ours} AND probability IS NOT NULL", KINDS)
    if len(keep) < held / 2:
        log.warning("polymarket: %d events this run vs %d held; not retiring", len(keep), held)
        return 0
    stale = f"{ours} AND NOT key = ANY($2::text[])"
    gone = await pool.execute(
        f"""DELETE FROM events WHERE {stale} AND status = 'rumored' AND NOT EXISTS (
              SELECT 1 FROM jsonb_array_elements(sources) s WHERE s->>'collector' <> 'events_polymarket')""",
        KINDS, keep)
    # The summary went with the odds (store.merge), so it goes with them too.
    await pool.execute(f"UPDATE events SET probability = NULL, summary = NULL, updated_at = now() "
                       f"WHERE {stale} AND probability IS NOT NULL", KINDS, keep)
    return int(gone.split()[-1])


async def run(dry_run: bool = False) -> int:
    raw = [e for tag in TAGS for e in await polymarket.open_events(tag)]
    found = events(raw, datetime.now(UTC).date())
    n = await feeds.write(found, COLLECTOR, dry_run)
    if not dry_run:
        retired = await retire([make_key(e.kind, e.subject, e.starts_at, e.venue) for e in found])
        if retired:
            log.info("polymarket: retired %d rows whose date moved", retired)
    return n
