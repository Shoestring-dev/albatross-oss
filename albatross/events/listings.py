"""events_listings (every 10 min): exchange listings and delistings from four public feeds.

  OKX          new-listing and delisting notices; the time comes from the article ("spot trading will open at ...").
               From a US address the API serves OKX US's (en-us) notices.
  Upbit        trade notices (Korean): new listings and market additions, delistings, investment warnings; the ticker
               sits in parentheses, the time in the notice body (KST). KRW-market listings are the big ones.
  Coinbase     products diff: a base currency with its first online USD product = listed.
  Hyperliquid  perp universe diff: a new perp = listed, a perp newly marked delisted = delisted.
The two diffs keep what they have seen in event_feed_seen; a feed's first run only records that baseline. Binance and
Bybit are out: geo-restricted in some regions, and we don't work around geo-blocks.

Importance: a listing on Coinbase or Upbit's KRW market 5 for a tracked tier-1/2 coin, 4 for tier 3 (and 3 for an
untracked coin on Upbit KRW, which moves those too); Upbit delisting of a tracked coin 4; everything else 3 for a
tracked coin and 2 for an untracked one; OKX adding or dropping single pairs of a token that stays listed 1.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import asyncpg

from albatross import db
from albatross.events import feeds
from albatross.events.store import Event, Source, day_start
from albatross.venues import coinbase, hyperliquid, okx, upbit

log = logging.getLogger(__name__)

COLLECTOR = "events_listings"
LOOKBACK = timedelta(days=30)           # notices older than this are history, not calendar
KST = ZoneInfo("Asia/Seoul")

# Pages already read, so each notice costs one fetch per process: {url or id: (edit stamp, text)}.
_pages: dict = {}


def _status(t: datetime, now: datetime) -> str:
    return "scheduled" if t > now else "happened"


def _imp(sym: str, tiers: dict[str, int], top: int, tracked: int, untracked: int = 2) -> int:
    tier = tiers.get(sym)
    return untracked if tier is None else top if tier <= 2 else tracked


def _assets(sym: str, tiers: dict[str, int]) -> list[str]:
    return [sym] if sym in tiers else []


# ─── OKX ────────────────────────────────────────────────────────────────────
_MON = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
# "Sep 15, 2026 14:00 UTC", "October 3, 2026, 08:00 - 10:00 UTC" / "14:00 UTC on June 18, 2026", "8:00 am UTC on ..."
_T_DATE_FIRST = re.compile(_MON + r" (\d{1,2}),? (\d{4}),? (\d{1,2}):(\d{2})\s*(am|pm)?(?:\s*-\s*\d{1,2}:\d{2})?\s*UTC",
                           re.I)
_T_TIME_FIRST = re.compile(r"(\d{1,2}):(\d{2})\s*(am|pm)?\s*UTC on " + _MON + r" (\d{1,2}),? (\d{4})", re.I)
_OPENS = re.compile(r"(?:trading|futures)[^.]{0,80}?will (?:open|start|begin|go live|be available)"
                    r"|in our spot trading at", re.I)
_PAIR = re.compile(r"\b([A-Z0-9]{2,15}) ?/(?:USDT|USDC|USD|EUR|BTC|ETH)\b")
_QUOTES_AND_WORDS = {"OKX", "USD", "USDT", "USDC", "EUR", "BTC", "ETH", "US", "UTC", "API"}


def _utc(mon: str, day: str, year: str, hh: str, mm: str, ampm: str | None) -> datetime:
    h = int(hh) % 12 + (12 if ampm and ampm.lower() == "pm" else 0) if ampm else int(hh)
    return datetime(int(year), _month(mon), int(day), h, int(mm), tzinfo=UTC)


def _month(mon: str) -> int:
    return "jan feb mar apr may jun jul aug sep oct nov dec".split().index(mon[:3].lower()) + 1


def okx_times(text: str) -> list[tuple[int, datetime]]:
    """Every 'date time UTC' in an OKX article, as (position, time), in reading order."""
    out = [(m.start(), _utc(m[1], m[2], m[3], m[4], m[5], m[6])) for m in _T_DATE_FIRST.finditer(text)]
    out += [(m.start(), _utc(m[4], m[5], m[6], m[1], m[2], m[3])) for m in _T_TIME_FIRST.finditer(text)]
    return sorted(out)


def _sentence(text: str, pos: int) -> str:
    start = text.rfind(". ", 0, pos) + 2 if text.rfind(". ", 0, pos) >= 0 else 0
    end = text.find(". ", pos)
    return re.sub(r" ([/.])", r"\1", text[start:end + 1 if end >= 0 else None]).strip()[:300]


def okx_tickers(title: str) -> list[str]:
    """Tickers a notice title names: 'VVV/USDT (Venice)', 'RE (Re)', 'DORA, ICX, STORJ, ZEUS and ELF'."""
    found = _PAIR.findall(title) + [w for w in re.findall(r"\b[A-Z][A-Z0-9]{1,14}\b", title)
                                    if w not in _QUOTES_AND_WORDS]
    return list(dict.fromkeys(found))


def okx_events(ann: dict, text: str, tiers: dict[str, int], now: datetime) -> list[Event]:
    """One OKX notice (with its article as plain text) -> a listing or delisting event per ticker."""
    title = ann["title"]
    low = title.lower()
    kind = "delisting" if "delist" in low else "listing" if re.search(r"\b(list|launch)", low) else None
    if kind is None:                                        # migrations and other housekeeping
        return []
    posted = datetime.fromtimestamp(int(ann["pTime"]) / 1000, UTC)
    tickers = okx_tickers(title)
    # "OKX to delist selected USD spot trading pairs" names no token: only the pairs in the article change, the
    # tokens stay listed on their other pairs. Minor, so importance 1.
    pairs_only = not tickers
    if pairs_only:
        tickers = list(dict.fromkeys(_PAIR.findall(text)))
    times = okx_times(text)
    # A listing opens at the first time after "... trading will open at" (deposits and call auctions come earlier).
    opens = _OPENS.search(text) if kind == "listing" else None
    opening = next((t for p, t in times if opens and 0 <= p - opens.start() <= 200), None)
    src = [Source(name="OKX announcements", url=ann["url"], excerpt=title[:300])]
    product = "spot trading" if "spot" in low else "perpetual futures" if "perpetual" in low else "trading"
    out = []
    for sym in tickers:
        when, precision, said = day_start(posted.date()), "day", None
        if kind == "listing":
            if opening:
                when, precision, said = opening, "exact", _sentence(text, opens.start())
        else:
            # Per-pair schedule rows ("ICX/USDT October 3, 2026, 08:00 - 10:00 UTC"): the token leaves OKX with its
            # last pair. Otherwise the notice's one execution time.
            mine = [t for p, t in times if re.search(rf"\b{re.escape(sym)} ?/\w+\s*$", text[max(0, p - 30):p])]
            if mine or times:
                when, precision = max(mine) if mine else times[0][1], "exact"
                said = f"{sym} trading on OKX ends {when:%b %-d, %Y %H:%M} UTC."
        name = re.search(rf"\b{re.escape(sym)}(?:/\w+)? \(([^)]+)\)", title)
        if pairs_only:
            pairs = sorted({f"{sym}/{q}" for q in re.findall(rf"\b{re.escape(sym)} ?/([A-Z]+)\b", text)})
            head = f"OKX {'adds' if kind == 'listing' else 'drops'} the {', '.join(pairs)} pair{'s' * (len(pairs) > 1)}"
        elif kind == "listing":
            head = f"OKX lists {sym}" + (f" ({name[1]})" if name else "") + f" for {product}"
        else:
            head = f"OKX delists {sym} ({product})"
        out.append(Event(
            kind=kind, subject=sym, venue="okx", starts_at=when, precision=precision, title=head[:120],
            assets=_assets(sym, tiers), importance=1 if pairs_only else _imp(sym, tiers, 3, 3),
            status=_status(when, now),
            summary=said or f"{title} (posted {posted:%b %-d, %Y}; the notice gives no time).", sources=src))
    return out


async def _okx(tiers: dict[str, int], now: datetime) -> list[Event]:
    out = []
    for ann_type in ("announcements-new-listings", "announcements-delistings"):
        for ann in await okx.announcements(ann_type):
            if now - datetime.fromtimestamp(int(ann["pTime"]) / 1000, UTC) > LOOKBACK:
                continue
            if _pages.get(ann["url"], (None,))[0] != ann["pTime"]:
                try:
                    _pages[ann["url"]] = (ann["pTime"], feeds.plain_text(await okx.page(ann["url"])))
                except Exception as e:  # noqa: BLE001 - try again next run rather than file it under the wrong day
                    log.warning("okx notice %s unreadable: %s", ann["url"], e)
                    continue
            out += okx_events(ann, _pages[ann["url"]][1], tiers, now)
    return out


# ─── Upbit ──────────────────────────────────────────────────────────────────
LISTS = ("신규 거래지원", "디지털 자산 추가")         # new trading support; asset added to a market
DELISTS = "거래지원 종료"                          # trading support ends
WARNS = "거래 유의 종목 지정 안내"                    # designated a caution coin (not "지정 해제": lifted)
CANCELS = "거래지원 취소"                          # a listing called off
_KDATE = r"(\d{4})-(\d{2})-(\d{2})(?:\([^)]\))? (\d{2}):(\d{2})"
_CHANGED_OPEN = re.compile(r"변경된 거래지원 개시 시점\s*:\s*" + _KDATE)
_TABLE_OPEN = re.compile(r"(\d{1,2})월 (\d{1,2})일 (\d{1,2})시(?: (\d{1,2})분)? 예정")
_CLOSES = re.compile(r"(?:거래지원 종료 예정일\s*:\s*" + _KDATE + r")|(?:" + _KDATE + r" KST ?에 거래지원을 종료)")
_TITLE_CLOSE = re.compile(r"\((\d{1,2})/(\d{1,2}) (\d{1,2}):(\d{2})")


def _kst(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=KST).astimezone(UTC)


def _year(mo: int, posted: datetime) -> int:
    """A month-day without a year, read as the next such date around the notice (December notice, January date)."""
    return posted.year + (1 if mo < posted.month - 6 else 0)


def upbit_open_time(sym: str, body: str, posted: datetime) -> datetime | None:
    """When trading opens for one ticker. Edits are prepended to the body above '---', newest first, and a
    postponement names the ticker it moves; otherwise the original table row after the ticker."""
    *edits, original = body.split("---")
    for part in edits:
        m = _CHANGED_OPEN.search(part)
        if m and re.search(rf"\b{re.escape(sym)}\b", part):
            return _kst(*map(int, m.groups()))
    at = original.find(f"({sym})")
    m = _TABLE_OPEN.search(original, at) if at >= 0 else None
    if m:
        mo, d, h, mi = int(m[1]), int(m[2]), int(m[3]), int(m[4] or 0)
        return _kst(_year(mo, posted), mo, d, h, mi)
    return None


def upbit_close_time(title: str, body: str, posted: datetime) -> datetime | None:
    m = _CLOSES.search(body)
    if m:
        g = [int(x) for x in m.groups() if x is not None]
        return _kst(*g)
    m = _TITLE_CLOSE.search(title)
    if m:
        mo, d, h, mi = map(int, m.groups())
        return _kst(_year(mo, posted), mo, d, h, mi)
    return None


def upbit_kind(title: str) -> str | None:
    if any(k in title for k in LISTS):
        return "listing"
    if DELISTS in title:
        return "delisting"
    if WARNS in title:
        return "warning"
    return None


def upbit_events(n: dict, body: str, tiers: dict[str, int], now: datetime) -> list[Event]:
    """One trade notice (body as plain text; '' for warnings, which need none) -> events per ticker."""
    title, kind = n["title"], upbit_kind(n["title"])
    if kind is None:
        return []
    posted = datetime.fromisoformat(n["first_listed_at"]).astimezone(UTC)
    tickers = list(dict.fromkeys(re.findall(r"\(([A-Z0-9]{1,15})\)", title)))
    markets = re.findall(r"([A-Z]{3,4}(?:, ?[A-Z]{3,4})*) 마켓", title)
    markets = [m.strip() for m in markets[0].split(",")] if markets else []
    krw = "KRW" in markets
    src = [Source(name="Upbit notices", url=f"https://upbit.com/service_center/notice?id={n['uuid']}",
                  excerpt=title[:300])]
    out = []
    for sym in tickers:
        if kind == "warning":
            out.append(Event(
                kind="other", subject=sym, venue="upbit", starts_at=posted, precision="exact",
                title=f"Upbit flags {sym} with an investment warning", assets=_assets(sym, tiers),
                importance=_imp(sym, tiers, 3, 3), status="happened",
                summary=f"Upbit designated {sym} a caution coin (거래 유의 종목), often a step before delisting.",
                sources=src))
            continue
        when = (upbit_open_time(sym, body, posted) if kind == "listing" else upbit_close_time(title, body, posted))
        exact = when is not None
        when = when or day_start(posted.date())
        local = f"{when.astimezone(KST):%Y-%m-%d %H:%M} KST ({when:%H:%M} UTC)" if exact else "no time given"
        if kind == "listing":
            where = ", ".join(markets) or "new"
            status = "cancelled" if f"({sym}) {CANCELS}" in title else _status(when, now)
            out.append(Event(
                kind="listing", subject=sym, venue="upbit", starts_at=when, precision="exact" if exact else "day",
                title=f"Upbit lists {sym} ({where} market{'s' if len(markets) > 1 else ''})",
                assets=_assets(sym, tiers), status=status,
                importance=_imp(sym, tiers, 5, 4, 3) if krw else _imp(sym, tiers, 3, 3),
                summary=(f"Upbit called off the {sym} listing." if status == "cancelled"
                         else f"Upbit {where} trading in {sym} opens {local}."), sources=src))
        else:
            out.append(Event(
                kind="delisting", subject=sym, venue="upbit", starts_at=when, precision="exact" if exact else "day",
                title=f"Upbit delists {sym}", assets=_assets(sym, tiers), importance=_imp(sym, tiers, 4, 4),
                status=_status(when, now), summary=f"Upbit ends trading support for {sym}: {local}.", sources=src))
    return out


async def _upbit(tiers: dict[str, int], now: datetime) -> list[Event]:
    out = []
    for n in await upbit.notices():
        kind = upbit_kind(n["title"])
        if kind is None or now - datetime.fromisoformat(n["first_listed_at"]) > LOOKBACK:
            continue
        body = ""
        if kind != "warning":
            key = ("upbit", n["id"])
            if _pages.get(key, (None,))[0] != n["listed_at"]:
                try:
                    _pages[key] = (n["listed_at"], feeds.plain_text((await upbit.notice(n["id"]))["body"]))
                except Exception as e:  # noqa: BLE001 - next run
                    log.warning("upbit notice %s unreadable: %s", n["id"], e)
                    continue
            body = _pages[key][1]
        out += upbit_events(n, body, tiers, now)
    return out


# ─── Coinbase and Hyperliquid: diffs against what we have seen ─────────────
async def _seen(feed: str, dry_run: bool) -> set[str]:
    pool = await db.pool()
    try:
        return {r["item"] for r in await pool.fetch("SELECT item FROM event_feed_seen WHERE feed = $1", feed)}
    except asyncpg.UndefinedTableError:
        if dry_run:                                         # a read-only look at a database without the table yet
            return set()
        raise


BASELINE = ""                           # marks a feed's baseline as taken, even when the baseline is empty


def announce(seen: set[str], live: set[str]) -> set[str]:
    """Items to announce: live and never seen. Nothing on a feed's first run, which only records the baseline."""
    return live - seen if BASELINE in seen else set()


def remember(seen: set[str], live: set[str]) -> set[str]:
    return live - seen | ({BASELINE} if BASELINE not in seen else set())


def seen_event(venue: str, kind: str, sym: str, what: str, url: str, importance: int, tiers: dict[str, int],
               now: datetime) -> Event:
    name = {"coinbase": "Coinbase", "hyperliquid": "Hyperliquid"}[venue]
    at = now.replace(second=0, microsecond=0)
    verb = "lists" if kind == "listing" else "delists"
    return Event(
        kind=kind, subject=sym, venue=venue, starts_at=at, precision="exact", title=f"{name} {verb} {sym} {what}",
        assets=_assets(sym, tiers), importance=importance, status="happened",
        summary=f"{name} {verb} {sym} {what}: first seen {'in' if kind == 'listing' else 'marked delisted in'} its "
                f"market list at {at:%H:%M} UTC (checked every 10 min).",
        sources=[Source(name=f"{name} markets", url=url, excerpt=f"first seen {at:%Y-%m-%d %H:%M} UTC")])


async def _diffs(tiers: dict[str, int], now: datetime, dry_run: bool) -> tuple[list[Event], list[tuple]]:
    """-> (events, (feed, item) rows to store once the events are written)."""
    async def hl():
        return {p.venue_symbol: p for p, _ in await hyperliquid.perps_with_contexts()}

    async def cb():
        return [p["symbol"] for p in await coinbase.usd_products()]

    perps, cb_symbols = await _each("hyperliquid", hl) or {}, await _each("coinbase", cb)
    live = {}                       # a feed that failed this run is left out: no baseline change, no events
    if cb_symbols:
        live["coinbase_usd"] = set(cb_symbols)
    if perps:
        live["hyperliquid_perps"] = {n for n, p in perps.items() if not p.delisted}
        live["hyperliquid_delisted"] = {n for n, p in perps.items() if p.delisted}
    events, rows = [], []
    for feed, items in live.items():
        seen = await _seen(feed, dry_run)
        if BASELINE not in seen:
            log.info("%s: first run, recording a baseline of %d", feed, len(items))
        rows += [(feed, i) for i in remember(seen, items)]
        for i in sorted(announce(seen, items)):
            if feed == "coinbase_usd":
                events.append(seen_event("coinbase", "listing", i, "for spot trading (USD)",
                                         f"https://exchange.coinbase.com/trade/{i}-USD",
                                         _imp(i, tiers, 5, 4), tiers, now))
            else:
                sym = perps[i].symbol
                events.append(seen_event("hyperliquid", "listing" if feed == "hyperliquid_perps" else "delisting",
                                         sym, "perpetual" + (f" ({i})" if i != sym else ""),
                                         f"https://app.hyperliquid.xyz/trade/{i}", _imp(sym, tiers, 3, 3), tiers, now))
    return events, rows


async def _each(name: str, fn, *args) -> list:
    """One source at a time: a source that fails (Upbit answers 403 from some regions, and we don't work around
    geo-blocks) is logged and skipped, never allowed to stop the others."""
    try:
        return await fn(*args)
    except Exception as e:  # noqa: BLE001 - per-source isolation
        status = getattr(getattr(e, "response", None), "status_code", None)
        log.warning("events_listings: %s skipped (%s)", name, f"HTTP {status}" if status else type(e).__name__)
        return []


async def run(dry_run: bool = False) -> int:
    now = datetime.now(UTC)
    tiers = await feeds.tiers()
    events = await _each("okx", _okx, tiers, now) + await _each("upbit", _upbit, tiers, now)
    found, rows = await _diffs(tiers, now, dry_run)
    n = await feeds.write(events + found, COLLECTOR, dry_run)
    if dry_run:
        for feed in sorted({f for f, _ in rows}):
            print(f"# {feed}: would store {sum(f == feed for f, _ in rows)} seen items")
    elif rows:
        await db.upsert("event_feed_seen", ["feed", "item"], rows, ["feed", "item"], update=[])
    return n
