"""Reads the date words news uses ("Oct. 7", "next Tuesday", "later this month", "Q4 2026") into the periods they
name, counted from the day the item was published. This is the deterministic half of the LLM extractors' date
gate: the model says which words date an event and which day it takes them to mean, and the event is kept only
if this code reads a period containing that day from the same words.

Rules, so a reader can predict every verdict:
- Relative words ("tomorrow", "Tuesday", "next week", "in two weeks") are counted from the publication day in UTC
  and in New York. The two differ for items published 00:00-04:00 UTC, when a US writer's "tomorrow" is the next
  UTC day but one.
- A bare weekday ("Tuesday", "this Tuesday", "on Tuesday") can be the last six days, the day itself or the next
  seven, because news uses it for both ("began trading Tuesday", "votes close Tuesday"). "Next Tuesday" is the
  next one or the one after, and "last Tuesday" the previous one.
- A day or month without a year ("Oct. 7", "in March", "Q1") is the first one on or after a week before
  publication, so "Sept. 25" in a Sept. 29 story is last week, not next year, and "in March" is next March.
- Recognised: ISO dates, "Oct. 7" / "October 7th, 2026" / "7 October" (with a day range such as "Oct. 7-9"),
  quarters ("Q4", "fourth quarter 2026"), full month names ("October", "mid-October"; "May" only capitalised),
  today / tonight / this morning / tomorrow / yesterday, weekdays, this / next / last week, this / next month,
  and "in N days / weeks". Anything else is not understood, and the gate drops the event.
"""

import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
LOOKBACK = timedelta(days=7)            # a yearless date may be this far before publication

MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "oct": 10,
          "nov": 11, "dec": 12}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
NUMBERS = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
           "nine": 9, "ten": 10}

MON = (r"(?P<mon>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?"
       r"|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
FULL_MON = r"(?P<mon>january|february|march|april|may|june|july|august|september|sept|october|november|december)"
DAY = r"(?P<d>\d{1,2})(?:st|nd|rd|th)?"
YEAR = r"(?:,?\s+(?P<y>20\d{2}))?"
RANGE = r"(?:\s*(?:-|–|—|to|through|until)\s*(?P<d2>\d{1,2})(?:st|nd|rd|th)?)?"
I = re.IGNORECASE

# Most precise first: read() answers with the first pattern that matches.
ISO = re.compile(r"\b(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})\b")
MONTH_DAY = re.compile(rf"\b{MON}\.?\s+{DAY}{RANGE}{YEAR}\b", I)
DAY_MONTH = re.compile(rf"\b{DAY}\s+(?:of\s+)?{MON}\b\.?{YEAR}", I)
REL_DAY = re.compile(r"\b(?P<w>today|tonight|this (?:morning|afternoon|evening)|tomorrow|yesterday)\b", I)
WEEKDAY = re.compile(r"\b(?:(?P<rel>next|last|this|coming|this coming)\s+)?(?P<wd>" + "|".join(WEEKDAYS) + r")\b", I)
IN_N = re.compile(r"\bin (?P<n>\d{1,2}|a|one|two|three|four|five|six|seven|eight|nine|ten) (?P<u>day|week)s?\b", I)
REL_WEEK = re.compile(r"\b(?P<rel>next|last|this|the coming|coming)\s+week\b", I)
MONTH = re.compile(rf"\b(?:(?:early|mid|late|end of|the end of)[- ]?)?{FULL_MON}\b{YEAR}", I)
REL_MONTH = re.compile(r"\b(?P<rel>next|this|end of the|end of this)\s+month\b", I)
QUARTER = re.compile(r"\b(?:Q(?P<q>[1-4])|(?P<qw>first|second|third|fourth)[- ]quarter)"
                     r"(?:\s+(?:of\s+)?(?P<y>20\d{2}))?\b", I)
PATTERNS = [ISO, MONTH_DAY, DAY_MONTH, REL_DAY, WEEKDAY, IN_N, REL_WEEK, MONTH, REL_MONTH, QUARTER]


@dataclass(frozen=True)
class Period:
    start: date
    last: date              # inclusive; after start for a week, month, quarter or a day range ("Oct. 7-9")
    precision: str          # day | week | month | quarter (store.PRECISION_RANK)

    def __contains__(self, d: date) -> bool:
        return self.start <= d <= self.last


def _day(d: date) -> Period:
    return Period(d, d, "day")


def _week(d: date) -> Period:
    monday = d - timedelta(days=d.weekday())
    return Period(monday, monday + timedelta(days=6), "week")


def _month(y: int, m: int) -> Period:
    return Period(date(y, m, 1), date(y, m, monthrange(y, m)[1]), "month")


def _quarter(y: int, q: int) -> Period:
    end = _month(y, 3 * q).last
    return Period(date(y, 3 * q - 2, 1), end, "quarter")


def _next_on_or_after(make, anchor: date) -> Period | None:
    """The yearless period `make(year)` that ends on or after a week before `anchor`."""
    for y in (anchor.year - 1, anchor.year, anchor.year + 1):
        try:
            p = make(y)
        except ValueError:              # Feb 30 and friends
            continue
        if p.last >= anchor - LOOKBACK:
            return p
    return None


def anchors(published: datetime) -> list[date]:
    """The publication day in UTC and in New York (the same day for most of the day)."""
    published = published if published.tzinfo else published.replace(tzinfo=UTC)
    return sorted({published.astimezone(UTC).date(), published.astimezone(NEW_YORK).date()})


def mentions_date(text: str) -> bool:
    """Whether the text has any date words read() understands (the cheap pre-filter before the LLM)."""
    return any(_matches(p, text) for p in PATTERNS)


def _matches(p: re.Pattern, text: str):
    for m in p.finditer(text):
        if "mon" in p.groupindex and m["mon"].lower() == "may" and "May" not in m[0]:
            continue                    # "may" the verb, not the month
        return m
    return None


def read(words: str, published: datetime) -> list[Period]:
    """Every period `words` can mean, read at the first recognised date expression in them. [] = not understood."""
    for p in PATTERNS:
        if m := _matches(p, words):
            out: list[Period] = []
            for a in anchors(published):
                out += [x for x in _read(p, m, a) if x not in out]
            return out
    return []


def _year(m: re.Match) -> int | None:
    return int(m["y"]) if m["y"] else None


def _read(p: re.Pattern, m: re.Match, a: date) -> list[Period]:
    try:
        if p is ISO:
            return [_day(date(int(m["y"]), int(m["m"]), int(m["d"])))]
        if p is MONTH_DAY or p is DAY_MONTH:
            mon, d = MONTHS[m["mon"][:3].lower()], int(m["d"])
            d2 = int(m["d2"]) if "d2" in p.groupindex and m["d2"] else None

            def make(y, mon=mon, d=d, d2=d2):
                return Period(date(y, mon, d), date(y, mon, d2), "day") if d2 and d2 > d else _day(date(y, mon, d))

            p1 = make(y) if (y := _year(m)) else _next_on_or_after(make, a)
            return [p1] if p1 else []
    except ValueError:
        return []
    if p is REL_DAY:
        w = m["w"].lower()
        return [_day(a + timedelta(days={"tomorrow": 1, "yesterday": -1}.get(w, 0)))]
    if p is WEEKDAY:
        rel, wd = (m["rel"] or "").lower(), WEEKDAYS.index(m["wd"].lower())
        ahead = (wd - a.weekday()) % 7 or 7                     # days to the next one, 1..7
        behind = (a.weekday() - wd) % 7 or 7                    # days since the last one, 1..7
        if rel == "next":
            days = [ahead, ahead + 7]
        elif rel == "last":
            days = [-behind]
        else:
            days = ([-behind] if behind < 7 else []) + ([0] if a.weekday() == wd else []) + [ahead]
        return [_day(a + timedelta(days=n)) for n in days]
    if p is IN_N:
        n = NUMBERS.get(m["n"].lower()) or int(m["n"])
        return [_day(a + timedelta(days=n))] if m["u"].lower() == "day" else [_week(a + timedelta(weeks=n))]
    if p is REL_WEEK:
        shift = {"next": 1, "last": -1, "the coming": 1, "coming": 1}.get(m["rel"].lower(), 0)
        return [_week(a + timedelta(weeks=shift))]
    if p is MONTH:
        mon = MONTHS[m["mon"][:3].lower()]
        p1 = _month(y, mon) if (y := _year(m)) else _next_on_or_after(lambda y: _month(y, mon), a)
        return [p1] if p1 else []
    if p is REL_MONTH:
        if m["rel"].lower() == "next":
            return [_month(a.year + a.month // 12, a.month % 12 + 1)]
        return [_month(a.year, a.month)]
    if p is QUARTER:
        q = int(m["q"]) if m["q"] else ["first", "second", "third", "fourth"].index(m["qw"].lower()) + 1
        p1 = _quarter(y, q) if (y := _year(m)) else _next_on_or_after(lambda y: _quarter(y, q), a)
        return [p1] if p1 else []
    return []


def period_of(d: date, precision: str) -> Period:
    """The period a (date, precision) pair names, e.g. from a model that gives no readable date words."""
    if precision == "week":
        return _week(d)
    if precision == "month":
        return _month(d.year, d.month)
    if precision == "quarter":
        return _quarter(d.year, (d.month - 1) // 3 + 1)
    return _day(d)
