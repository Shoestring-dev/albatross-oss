"""events_macro (daily): FOMC decisions from the Fed's calendar page and US data releases from FRED's release calendar.

FOMC: every meeting of the current and next year; the decision is on the meeting's last day. The page gives no times,
so precision is 'day' (the statement is usually 2:00 pm ET). FRED: release dates of CPI, the jobs report, PCE and GDP,
from two days back to 60 days ahead, also 'day' precision (FRED has dates only; these usually land at 8:30 am ET).
"""

import logging
import re
from datetime import UTC, date, datetime, timedelta

from albatross.config import settings
from albatross.events import feeds
from albatross.events.store import Event, Source, day_start
from albatross.http import client, retrying
from albatross.venues import fred

log = logging.getLogger(__name__)

COLLECTOR = "events_macro"
FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
BACK, AHEAD = timedelta(days=2), timedelta(days=60)

# FRED release id -> (subject, title, venue, importance, the words FRED's release name must contain). The name check
# makes a renumbered release fail the job loudly instead of filing some other release under CPI.
RELEASES = {
    10: ("CPI", "US CPI inflation report", "bls", 5, "consumer price index"),
    50: ("Jobs report", "US jobs report (nonfarm payrolls)", "bls", 4, "employment situation"),
    54: ("PCE", "US PCE inflation (personal income and outlays)", "bea", 4, "personal income and outlays"),
    53: ("GDP", "US GDP report", "bea", 3, "gross domestic product"),
}

_YEAR = re.compile(r'<h4><a id="\d+">(\d{4}) FOMC Meetings</a></h4>')
_MEETING = re.compile(r'fomc-meeting__month[^>]*><strong>([^<]+)</strong></div>\s*'
                      r'<div class="fomc-meeting__date[^>]*>([^<]+)</div>')
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}


def fomc(page: str, today: date) -> list[Event]:
    """Meetings of this year and next from the calendar page. Year sections come in page order (this year, past
    years, then next year), so each meeting takes the year of the heading above it. Notation votes are skipped."""
    heads = [(m.start(), int(m.group(1))) for m in _YEAR.finditer(page)]
    out = []
    for m in _MEETING.finditer(page):
        year = next((y for pos, y in reversed(heads) if pos < m.start()), None)
        month, days = m.group(1).strip(), m.group(2).strip()
        if year not in (today.year, today.year + 1) or "(" in days:
            continue
        # "27-28", "17-18*" (* = with projections), "Apr/May" + "30-1": the decision is on the last day.
        decision = date(year, _MONTHS[month.split("/")[-1].strip()[:3].lower()], int(re.findall(r"\d+", days)[-1]))
        if decision < today - BACK:
            continue
        sep = "*" in days
        out.append(Event(
            kind="macro", subject="FOMC", venue="fed", starts_at=day_start(decision), precision="day",
            title="FOMC rate decision" + (", with new projections (dot plot)" if sep else ""),
            importance=5, status="happened" if decision < today else "scheduled",
            summary=f"FOMC meeting {month} {days.rstrip('*')}, {year}; the statement usually comes at 2:00 pm ET, "
                    "then the Chair's press conference." + (" New economic projections too." if sep else ""),
            sources=[Source(name="Federal Reserve FOMC calendar", url=FOMC_URL, excerpt=f"{year}: {month} {days}")]))
    return out


def releases(rows: list[dict], today: date) -> list[Event]:
    """FRED release-date rows -> events for the releases in RELEASES."""
    out = []
    for r in rows:
        spec = RELEASES.get(r["release_id"])
        if spec is None:
            continue
        subject, title, venue, importance, name = spec
        if name not in r["release_name"].lower():
            raise RuntimeError(f"FRED release {r['release_id']} is now {r['release_name']!r}, expected {name!r}")
        d = date.fromisoformat(r["date"])
        if not today - BACK <= d <= today + AHEAD:
            continue
        out.append(Event(
            kind="macro", subject=subject, venue=venue, starts_at=day_start(d), precision="day", title=title,
            importance=importance, status="happened" if d < today else "scheduled",
            summary=f"{r['release_name']} ({venue.upper()}), usually released at 8:30 am ET.",
            sources=[Source(name="FRED release calendar",
                            url=f"https://fred.stlouisfed.org/releases/calendar?rid={r['release_id']}",
                            excerpt=f"{r['release_name']}: {r['date']}")]))
    return out


@retrying
async def _fomc_page() -> str:
    r = await client().get(FOMC_URL)
    r.raise_for_status()
    return r.text


async def run(dry_run: bool = False) -> int:
    today = datetime.now(UTC).date()
    events = fomc(await _fomc_page(), today)
    if not events:
        raise RuntimeError("no FOMC meetings parsed from the Fed calendar page (layout changed?)")
    if settings.fred_api_key:
        events += releases(await fred.release_dates(today - BACK, today + AHEAD), today)
    else:
        log.warning("no FRED key: skipping CPI / jobs / PCE / GDP release dates")
    return await feeds.write(events, COLLECTOR, dry_run)
